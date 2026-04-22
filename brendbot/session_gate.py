"""Per-turn content-gate dispatch for ``Session``.

Previously colocated as ``Session.apply_content_gate`` in ``session.py``;
extracted as part of the Stage 5 repo cleanup. The public entry point
is :func:`apply_content_gate`, a module-level function taking a
``Session`` as its first argument. ``Session.apply_content_gate`` is
kept as a thin delegate so existing callers (including ~20 test call
sites) are unaffected.

Gate routing is unchanged from the pre-extraction implementation:

* **BYPASS** — admin italic ``*brend*`` token (tier=admin, bypass
  enabled): runs the classifier in shadow mode, refuses only on hard
  floors, otherwise marks ``_turn_bypass_pending`` and lets the normal
  injection proceed so the current session generates with a ``[bypass]``
  branch tag.
* **PASS** — classifier says clean: return ``'inject'`` and let the
  caller resume the normal query flow.
* **FLAG** — 2-of-3-band weighted result: bill against the per-session
  ``_flagged_count`` budget, spawn a background flagged-path generation
  on the configured sonnet model, dispatch via ``_fire_on_text``,
  return ``'handled'`` so the caller skips injection.
* **REFUSE / FLOOR_HIT** — dispatch a local refusal explanation. For
  FLOOR_HIT specifically, a haiku cross-check (see
  :func:`brendbot.classifier_pool.content_gate_cross_check_floor`) runs
  in-band: the refusal fires either way, but a DISPUTED verdict is
  logged for admin review so false-positive floor matches (technical
  vocabulary like ``trigger``, ``payload``, ``exploit``) can be tuned
  out of the primary classifier.

The classifier entry points (``content_gate_classify``,
``content_gate_cross_check_floor``, ``flagged_generate``) are resolved
via ``brendbot.session``'s module namespace at call time. That keeps
``tests/test_admin_bypass.py``'s
``monkeypatch.setattr(session_mod, "content_gate_classify", fake)``
contract working — the patched binding is what the gate reads when it
calls the classifier.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brendbot.session import Session

logger = logging.getLogger(__name__)


async def apply_content_gate(
    session: "Session",
    wrapped_text: str,
    raw_user_text: str,
    tier: str,
    sender_id: str,
    message_id: str,
) -> str:
    """Run the content gate on an incoming user message and return one
    of ``'inject'`` (caller should proceed with the normal inject+query
    cycle) or ``'handled'`` (response already dispatched via
    ``session._fire_on_text`` or the flagged path; caller must NOT
    inject).

    Gate failures (classifier spawn errors, etc.) fail conservative to
    REFUSE with an explanation noting the classifier error.
    """
    from brendbot.content_gate import (
        decide_outcome,
        detect_admin_bypass,
        format_refusal_explanation,
        Outcome,
    )

    # Resolved via session.py's namespace so
    # monkeypatch.setattr(session_mod, "content_gate_classify", …) still
    # affects the gate. Deferred to call time to also avoid a circular
    # import at module load (session.py imports us to delegate
    # Session.apply_content_gate).
    from brendbot import session as _session_mod

    gate_cfg = _load_gate_cfg()
    hard_floors, outcome_thresholds = _parse_gate_cfg_basics(gate_cfg)
    flagged_model, flagged_cap = _parse_flagged_cfg(gate_cfg)
    bypass_enabled, bypass_enforces_floors = _parse_bypass_cfg(gate_cfg)

    # Admin bypass detection runs before the classifier spawn so the
    # common non-bypass path skips the extra check cost.
    is_bypass = bypass_enabled and detect_admin_bypass(raw_user_text, tier)

    # Run the classifier. For bypass path, this is shadow-mode
    # (recording would-have-been decisions for audit). For normal
    # path, this is the primary gate decision.
    try:
        classifier_result = await _session_mod.content_gate_classify(raw_user_text)
    except Exception as exc:
        logger.warning(
            "[%s] content_gate_classify unexpected error: %s",
            session.key, exc,
        )
        from brendbot.content_gate import ClassifierResult
        classifier_result = ClassifierResult(
            criteria={"_parse_error": 2.0},
            reasoning=f"classifier error: {type(exc).__name__}",
            parse_error=True,
        )

    pass_thr, flag_thr, refuse_thr = outcome_thresholds
    shadow_outcome = decide_outcome(
        classifier_result, hard_floors, pass_thr, flag_thr, refuse_thr,
    )

    if is_bypass:
        return await _handle_bypass(
            session,
            classifier_result=classifier_result,
            shadow_outcome=shadow_outcome,
            raw_user_text=raw_user_text,
            tier=tier,
            sender_id=sender_id,
            message_id=message_id,
            hard_floors=hard_floors,
            bypass_enforces_floors=bypass_enforces_floors,
            format_refusal_explanation=format_refusal_explanation,
        )

    if shadow_outcome == Outcome.PASS:
        return "inject"

    if shadow_outcome == Outcome.FLAG:
        return await _handle_flag(
            session,
            wrapped_text=wrapped_text,
            raw_user_text=raw_user_text,
            tier=tier,
            sender_id=sender_id,
            message_id=message_id,
            classifier_result=classifier_result,
            flagged_model=flagged_model,
            flagged_cap=flagged_cap,
            gate_cfg=gate_cfg,
        )

    # REFUSE or FLOOR_HIT.
    return await _handle_refuse_or_floor(
        session,
        classifier_result=classifier_result,
        shadow_outcome=shadow_outcome,
        raw_user_text=raw_user_text,
        tier=tier,
        sender_id=sender_id,
        message_id=message_id,
        format_refusal_explanation=format_refusal_explanation,
        is_floor_hit=(shadow_outcome == Outcome.FLOOR_HIT),
    )


# ---------------------------------------------------------------------------
# Config helpers — read from engagement.yaml via discord._ENGAGEMENT_CFG
# ---------------------------------------------------------------------------


def _load_gate_cfg() -> dict:
    """Pull the ``content_gate`` sub-section out of ``engagement.yaml``.

    Returns an empty dict on any import/read failure so the gate still
    runs with hard-coded defaults rather than crashing the turn.
    """
    try:
        from brendbot.discord import _ENGAGEMENT_CFG
        return _ENGAGEMENT_CFG.get("content_gate", {}) or {}
    except Exception:
        return {}


def _parse_gate_cfg_basics(gate_cfg: dict) -> tuple[set[str], tuple[float, float, float]]:
    """Extract hard-floor set + (pass, flag, refuse) threshold tuple."""
    hard_floors_list = gate_cfg.get("hard_floors", []) or []
    hard_floors: set[str] = set(hard_floors_list)
    outcomes_cfg = gate_cfg.get("outcomes", {}) or {}
    pass_thr = float(outcomes_cfg.get("pass_threshold", 0.5))
    flag_thr = float(outcomes_cfg.get("flag_threshold", 1.5))
    refuse_thr = float(outcomes_cfg.get("refuse_threshold", 1.5))
    return hard_floors, (pass_thr, flag_thr, refuse_thr)


def _parse_flagged_cfg(gate_cfg: dict) -> tuple[str, int]:
    """Resolve flagged-path model (env > yaml > fallback) and per-session cap.

    The env-var precedence via ``Config.claude_flagged_model`` lets
    operators pin a fresh model without editing yaml and prevents
    silently-unreachable dated model strings from pinning FLAG-band
    traffic to a 410 Gone revision.
    """
    flagged_cfg = gate_cfg.get("flagged_path", {}) or {}
    from brendbot.config import get_config as _get_cfg
    _cfg = _get_cfg()
    flagged_model = (
        _cfg.claude_flagged_model
        or flagged_cfg.get("model")
        or "claude-sonnet-4-20250514"
    )
    flagged_cap = int(flagged_cfg.get("max_per_session", 2))
    return flagged_model, flagged_cap


def _parse_bypass_cfg(gate_cfg: dict) -> tuple[bool, bool]:
    """Return ``(enabled, hard_floors_still_enforced)`` flags for bypass."""
    bypass_cfg = gate_cfg.get("admin_bypass", {}) or {}
    bypass_enabled = bool(bypass_cfg.get("enabled", True))
    bypass_enforces_floors = bool(bypass_cfg.get("hard_floors_still_enforced", True))
    return bypass_enabled, bypass_enforces_floors


# ---------------------------------------------------------------------------
# Branch helpers — one per outcome
# ---------------------------------------------------------------------------


async def _handle_bypass(
    session: "Session",
    *,
    classifier_result,
    shadow_outcome,
    raw_user_text: str,
    tier: str,
    sender_id: str,
    message_id: str,
    hard_floors: set[str],
    bypass_enforces_floors: bool,
    format_refusal_explanation,
) -> str:
    """Admin bypass path. Hard floors are the only thing that can still
    refuse. Everything else generates on the session model with a
    ``[bypass]`` branch tag applied by ``_fire_on_text``."""
    from brendbot.feedback import log_bypass_event

    hard_floor_hit: str | None = None
    if bypass_enforces_floors and classifier_result.hard_floor in hard_floors:
        hard_floor_hit = classifier_result.hard_floor

    if hard_floor_hit:
        refusal = format_refusal_explanation(classifier_result)
        logger.info(
            "[%s] admin bypass + hard floor hit (%s) — refusing",
            session.key, hard_floor_hit,
        )
        asyncio.create_task(session._fire_on_text(refusal))
        log_bypass_event(
            channel_id=session._chat_id,
            user_message_id=message_id,
            user_text=raw_user_text,
            admin_sender_id=sender_id,
            tier=tier,
            would_have_tripped=classifier_result.to_dict()["criteria"],
            would_have_summed=classifier_result.weighted_sum,
            would_have_outcome=shadow_outcome.value,
            hard_floor_hit=hard_floor_hit,
            bot_message_id=None,
        )
        return "handled"

    # Bypass permitted — let the normal session inject proceed with a
    # sentinel flag so _fire_on_text tags the bot's response. The
    # session model stays the same (NOT rerouted). Audit entry is
    # written pre-dispatch so mid-pipeline failures still log.
    logger.info(
        "[%s] admin bypass invoked (shadow outcome=%s, sum=%.2f)",
        session.key, shadow_outcome.value, classifier_result.weighted_sum,
    )
    log_bypass_event(
        channel_id=session._chat_id,
        user_message_id=message_id,
        user_text=raw_user_text,
        admin_sender_id=sender_id,
        tier=tier,
        would_have_tripped=classifier_result.to_dict()["criteria"],
        would_have_summed=classifier_result.weighted_sum,
        would_have_outcome=shadow_outcome.value,
        hard_floor_hit=None,
        bot_message_id=None,
    )
    session._turn_bypass_pending = True
    return "inject"


async def _handle_flag(
    session: "Session",
    *,
    wrapped_text: str,
    raw_user_text: str,
    tier: str,
    sender_id: str,
    message_id: str,
    classifier_result,
    flagged_model: str,
    flagged_cap: int,
    gate_cfg: dict,
) -> str:
    """FLAG branch: channel-override bypass check, budget cap check,
    background flagged generation."""
    from brendbot.feedback import log_flag_event
    from brendbot import session as _session_mod

    # Channel-level gate2_bypass: treat FLAG as PASS in designated channels.
    channel_overrides = gate_cfg.get("channel_overrides", {}) or {}
    ch_cfg = channel_overrides.get(str(session._chat_id), {}) or {}
    if ch_cfg.get("gate2_bypass", False):
        logger.info(
            "[%s] FLAG outcome but gate2_bypass active for channel — treating as PASS",
            session.key,
        )
        return "inject"

    # Per-session budget cap.
    if session._flagged_count >= flagged_cap:
        refusal = (
            "can't do that one — flagged-path budget exhausted "
            f"for this session ({flagged_cap}/session). "
            "restart the session to reset."
        )
        logger.info(
            "[%s] FLAG outcome but budget exhausted (%d/%d)",
            session.key, session._flagged_count, flagged_cap,
        )
        asyncio.create_task(session._fire_on_text(refusal))
        return "handled"

    session._flagged_count += 1
    logger.info(
        "[%s] FLAG outcome (sum=%.2f, count=%d/%d) — rerouting to %s",
        session.key, classifier_result.weighted_sum,
        session._flagged_count, flagged_cap, flagged_model,
    )

    # Audit entry is written pre-dispatch so any failure in
    # flagged_generate still produces a log entry.
    log_flag_event(
        channel_id=session._chat_id,
        user_message_id=message_id,
        user_text=raw_user_text,
        admin_sender_id=sender_id,
        tier=tier,
        criteria_tripped=dict(classifier_result.criteria),
        weighted_sum=classifier_result.weighted_sum,
        flagged_model=flagged_model,
        bot_message_id=None,
        session_flag_count=session._flagged_count,
    )

    async def _flagged_task() -> None:
        try:
            response = await _session_mod.flagged_generate(
                wrapped_message=wrapped_text,
                model=flagged_model,
                cwd=None,
            )
            await session._fire_on_text(response)
        except Exception as exc:
            logger.warning(
                "[%s] flagged path failed: %s", session.key, exc,
            )
            await session._fire_on_text(
                "[flagged] (flagged path failed to produce output)"
            )

    asyncio.create_task(_flagged_task())
    return "handled"


async def _handle_refuse_or_floor(
    session: "Session",
    *,
    classifier_result,
    shadow_outcome,
    raw_user_text: str,
    tier: str,
    sender_id: str,
    message_id: str,
    format_refusal_explanation,
    is_floor_hit: bool,
) -> str:
    """REFUSE or FLOOR_HIT dispatch.

    FLOOR_HIT in particular is a single-model irreversible refusal based
    on a list-match classification — the class of error most worth
    catching with a second pass. The cross-check runs in-band: the
    refusal fires regardless, but a DISPUTED verdict is logged for
    admin review so false-positive floor matches (technical vocabulary
    like ``trigger``, ``payload``, ``exploit``) can be tuned out of
    the primary classifier.
    """
    from brendbot.feedback import log_disputed_floor_event
    from brendbot import session as _session_mod

    suspected_floor = classifier_result.hard_floor
    if is_floor_hit and suspected_floor:
        confirmed, cross_text = await _session_mod.content_gate_cross_check_floor(
            raw_user_text, suspected_floor,
        )
        if not confirmed:
            logger.info(
                "[%s] floor-hit cross-check DISPUTED (floor=%s)",
                session.key, suspected_floor,
            )
            log_disputed_floor_event(
                channel_id=session._chat_id,
                user_message_id=message_id,
                user_text=raw_user_text,
                sender_id=sender_id,
                tier=tier,
                suspected_floor=suspected_floor,
                cross_check_response=cross_text,
                bot_message_id=None,
            )

    refusal = format_refusal_explanation(classifier_result)
    logger.info(
        "[%s] content gate %s (sum=%.2f, floor=%s)",
        session.key, shadow_outcome.value,
        classifier_result.weighted_sum,
        classifier_result.hard_floor,
    )
    asyncio.create_task(session._fire_on_text(refusal))
    return "handled"
