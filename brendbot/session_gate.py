"""Per-turn content-gate dispatch for ``Session``.

Previously colocated as ``Session.apply_content_gate`` in ``session.py``;
extracted as part of the Stage 5 repo cleanup. The public entry point
is :func:`apply_content_gate`, a module-level function taking a
``Session`` as its first argument. ``Session.apply_content_gate`` is
kept as a thin delegate so existing callers (including ~20 test call
sites) are unaffected.

Gate routing, post-2026-04-23 strip:

* **FRIEND-TIER BYPASS** — if the originating guild is in the friend-
  tier set (auto-classified at bot startup by
  :func:`brendbot.discord.classify_friend_guilds`), skip the entire
  gate and return ``'inject'``. No classifier spawn, no refusal.
* **BYPASS** — admin italic ``*brend*`` token (tier=admin, bypass
  enabled): runs the classifier in shadow mode, refuses only on hard
  floors, otherwise marks ``_turn_bypass_pending`` and lets the normal
  injection proceed with a ``[bypass]`` branch tag. Reachable only for
  non-friend-tier guilds.
* **PASS** — classifier says clean (or weighted-sum lands in the
  collapsed FLAG-band-now-PASS zone): return ``'inject'`` and let the
  caller resume the normal query flow.
* **REFUSE / FLOOR_HIT** — dispatch a local refusal explanation. For
  FLOOR_HIT specifically, a haiku cross-check (see
  :func:`brendbot.classifier_pool.content_gate_cross_check_floor`) runs
  in-band: the refusal fires either way, but a DISPUTED verdict is
  logged for admin review so false-positive floor matches (technical
  vocabulary like ``trigger``, ``payload``, ``exploit``) can be tuned
  out of the primary classifier.

The FLAG outcome (reroute to soul-stripped claude-sonnet-4-6) was
deleted in the 2026-04-23 strip. It produced confident out-of-
character output — "happy to oblige" LinkedIn voice, bold headers,
life-coach framing — and was the worst-behaving subsystem in the
2026-04-23 pilot. ``decide_outcome`` now collapses the former FLAG
band into PASS; there's no reroute, no ``_flagged_count`` budget,
no ``_handle_flag`` function.

The classifier entry points (``content_gate_classify``,
``content_gate_cross_check_floor``) are resolved via
``brendbot.session``'s module namespace at call time. That keeps
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
    from brendbot.config import is_friend_guild

    # Resolved via session.py's namespace so
    # monkeypatch.setattr(session_mod, "content_gate_classify", …) still
    # affects the gate. Deferred to call time to also avoid a circular
    # import at module load (session.py imports us to delegate
    # Session.apply_content_gate).
    from brendbot import session as _session_mod

    # ── Friend-tier bypass ──────────────────────────────────────────────
    # Skip the entire gate when the message originates in a friend-tier
    # guild (owner-owned, small, private — auto-classified at bot startup
    # by ``discord.classify_friend_guilds``). The classifier +
    # REFUSE / FLOOR_HIT machinery was designed to defend against hostile
    # users in public deployments; it has no business firing on a private
    # owner-run server where everyone present is known-trusted.
    #
    # This replaces the Stage-20 opt-in via OWNER_GUILD_ID env var. Auto-
    # detection is strictly better than opt-in because the opt-in silently
    # failed when the operator didn't know to flip the switch — which
    # happened in the 2026-04-23 pilot and caused the gate to FLAG/REFUSE
    # the owner in his own server despite the feature being "shipped."
    #
    # Tests stub ``is_friend_guild`` (or populate
    # ``config._FRIEND_GUILDS``) directly; the default at test time is
    # an empty friend-guild set so the gate still runs for every test
    # scenario that doesn't explicitly opt in.
    if is_friend_guild(getattr(session, "_guild_id", "")):
        logger.info(
            "[%s] friend-tier guild — content gate skipped",
            session.key,
        )
        _log_gate("FRIEND_TIER_SKIP", session, message_id, raw_user_text,
                  weighted_sum=None, criteria=None, refusal_text=None)
        return "inject"

    gate_cfg = _load_gate_cfg()
    hard_floors, outcome_thresholds = _parse_gate_cfg_basics(gate_cfg)
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
        # Record the classifier crash as an error event so the bot
        # can honestly answer "what happened" without guessing.
        try:
            from brendbot.obs import log_error
            log_error(
                session_key=session.key,
                error_class=f"ClassifierError:{type(exc).__name__}",
                error_msg=str(exc),
                recoverable=True,
                detail={"path": "content_gate_classify"},
            )
        except Exception:
            pass
        from brendbot.content_gate import ClassifierResult
        classifier_result = ClassifierResult(
            criteria={"_parse_error": 10.0},
            reasoning=f"classifier error: {type(exc).__name__}",
            parse_error=True,
        )

    pass_thr, flag_thr, refuse_thr = outcome_thresholds
    shadow_outcome = decide_outcome(
        classifier_result, hard_floors, pass_thr, flag_thr, refuse_thr,
    )

    if is_bypass:
        _log_gate("BYPASS", session, message_id, raw_user_text,
                  weighted_sum=classifier_result.weighted_sum,
                  criteria=dict(classifier_result.criteria),
                  refusal_text=None)
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
        _log_gate("PASS", session, message_id, raw_user_text,
                  weighted_sum=classifier_result.weighted_sum,
                  criteria=dict(classifier_result.criteria),
                  refusal_text=None)
        return "inject"

    # The FLAG outcome was removed in the 2026-04-23 strip; decide_outcome
    # no longer emits it, the soul-stripped reroute path is gone, and
    # ambiguous-band results fall through to PASS above.

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
        from brendbot.runtime_events import GATE_PREFIX
        asyncio.create_task(session._fire_on_text(f"{GATE_PREFIX} {refusal}"))
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
    # Log the refusal to gate_events.jsonl so that when the user later
    # asks "what was that refusal about" / "what are you responding to,"
    # the bot can grep for this message_id and quote the criteria and
    # refusal text verbatim rather than having to guess. Before this
    # log existed, the model in subsequent turns genuinely had no
    # visibility into what had fired the refusal — the gate dispatch
    # is a separate code path from the model.
    _log_gate(
        shadow_outcome.value.upper() if hasattr(shadow_outcome, "value") else str(shadow_outcome),
        session,
        message_id,
        raw_user_text,
        weighted_sum=classifier_result.weighted_sum,
        criteria=dict(classifier_result.criteria),
        refusal_text=refusal,
    )
    # Prefix the user-facing refusal with the infra marker so the user
    # can tell this was an external (gate) decision, not the model
    # speaking in character. The bare refusal_text goes into
    # gate_events.jsonl above without the prefix, so bot readback
    # queries return the clean content.
    from brendbot.runtime_events import GATE_PREFIX
    visible_refusal = f"{GATE_PREFIX} {refusal}"
    asyncio.create_task(session._fire_on_text(visible_refusal))
    return "handled"


def _log_gate(
    outcome: str,
    session: "Session",
    message_id: str,
    user_text: str,
    *,
    weighted_sum: float | None,
    criteria: dict[str, float] | None,
    refusal_text: str | None,
) -> None:
    """Record a gate outcome to logs/gate_events.jsonl.

    Called from every gate-decision point (friend-tier skip, BYPASS,
    PASS, REFUSE, FLOOR_HIT). Best-effort — a failed log write never
    blocks the gate's actual decision.
    """
    try:
        from brendbot.obs import log_gate_event
        log_gate_event(
            session_key=session.key,
            channel_id=getattr(session, "_chat_id", "") or "",
            message_id=message_id,
            outcome=outcome,
            weighted_sum=weighted_sum,
            criteria=criteria,
            refusal_text=refusal_text,
            user_text_preview=user_text,
        )
    except Exception as exc:
        logger.debug("[%s] _log_gate failed: %s", session.key, exc)
