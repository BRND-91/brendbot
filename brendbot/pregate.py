"""Deterministic pregate — short-circuit in front of the haiku classifier.

The middle band (score in [ENGAGE_THRESHOLD, ENGAGE_HARD_PASS)) is the only
path that pays haiku subprocess latency. Most middle-band messages have an
obvious outcome; the pregate catches the cheapest of those and answers
locally before the classifier is invoked.

Phase 3 ships exactly one heuristic — `short_pleasantry` — because it's the
only pattern that both (a) fires often enough to matter and (b) is
near-zero risk to short-circuit. Reply-to-bot and name-mention cases are
already handled upstream by the scorer (reply clears hard_pass, name
mention adds SCORE_NAME_MENTIONED) so they never land in this function's
middle-band caller.

Design notes:
  * Peer to classifier.py, not nested inside it. discord.py coordinates
    both — pregate first, then (if pregate defers) the haiku classifier.
  * Owns its own slice of engagement.yaml (the `pregate:` block). Keeps
    the module importable without pulling discord.py's engagement state.
  * Pure function — `pregate_classify` takes everything it needs as args
    and returns a dataclass. No side effects, no I/O, no logging inside
    the hot path. Trivial to unit-test.
  * SIGHUP refresh is wired through `SessionPool.refresh_cache` alongside
    the existing `refresh_engagement_config` call — same yaml file, same
    reload trigger.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Config loading ───────────────────────────────────────────────────────

_ENGAGEMENT_YAML = Path(__file__).parent.parent / "engagement.yaml"

# Trailing characters stripped from the last token before membership check.
# Kept narrow — we want "thanks!" → "thanks" but not "ok..." → "ok" only to
# then reject a legitimate "???" token later. Covers the common punctuation
# that ends a casual chat message without swallowing meaningful symbols.
_TRAILING_PUNCT = ".,!?;:\"')]}"


@dataclass
class PregateResult:
    """Structured return shape for `pregate_classify`.

    decision:
      * False → deterministic NO. Caller skips haiku and logs the outcome
                under `pregate_no`.
      * True  → deterministic YES. (No heuristic currently returns True —
                reserved for future accept-side short-circuits.)
      * None  → no deterministic answer. Caller falls through to haiku.
    reason:
      Short tag identifying which heuristic fired. Empty string when
      decision is None (no heuristic matched). Current tags:
        "short_pleasantry" — decision=False path
    """
    decision: bool | None
    reason: str


# Sentinel returned when no heuristic fires. Reused to avoid per-call
# allocation on the hot path (the common middle-band message falls through).
_DEFER = PregateResult(decision=None, reason="")


# ── Module-level config (populated from engagement.yaml) ─────────────────

_PLEASANTRIES: frozenset[str] = frozenset()
_MAX_LENGTH: int = 0


def _load_pregate_config() -> dict:
    """Read only the `pregate:` slice of engagement.yaml.

    Soft-fails on missing `pregate` block — the pregate is an optimization,
    not a correctness requirement. If the section disappears we fall back
    to a never-fires config (_MAX_LENGTH=0 means every message fails the
    length check) and log a warning. This keeps the bot alive if a yaml
    edit omits the block; SIGHUP can recover once the block is added back.
    """
    import yaml
    if not _ENGAGEMENT_YAML.exists():
        raise FileNotFoundError(
            f"engagement.yaml not found at {_ENGAGEMENT_YAML}. "
            "pregate cannot initialize without the canonical config file."
        )
    with _ENGAGEMENT_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    pregate_cfg = cfg.get("pregate")
    if not pregate_cfg:
        logger.warning(
            "engagement.yaml has no `pregate` block — pregate will never fire"
        )
        return {"max_pleasantry_length": 0, "pleasantries": []}
    return pregate_cfg


def _apply_pregate_constants(cfg: dict) -> None:
    """Populate module-level pregate constants from a loaded config dict."""
    global _PLEASANTRIES, _MAX_LENGTH
    _MAX_LENGTH = int(cfg.get("max_pleasantry_length", 0))
    _PLEASANTRIES = frozenset(
        str(t).lower() for t in cfg.get("pleasantries", [])
    )


def refresh_pregate_config() -> None:
    """Re-read engagement.yaml and update pregate constants in-place.

    Wired to SIGHUP via `SessionPool.refresh_cache` alongside the main
    engagement-config refresh. On load failure the previous constants
    remain — partial state is worse than stale state (matches the
    `refresh_engagement_config` contract).
    """
    try:
        new_cfg = _load_pregate_config()
        _apply_pregate_constants(new_cfg)
        logger.info("pregate config reloaded — %d pleasantries, max_len=%d",
                    len(_PLEASANTRIES), _MAX_LENGTH)
    except Exception as exc:
        logger.error("pregate config reload failed — keeping previous: %s", exc)


# Auto-populate at import time so `pregate_classify` is callable immediately.
_apply_pregate_constants(_load_pregate_config())


# ── Hot path ─────────────────────────────────────────────────────────────

def pregate_classify(
    text: str,
    has_domain: bool,
    name_mentioned: bool,
) -> PregateResult:
    """Deterministic short-circuit for middle-band messages.

    Returns a PregateResult. `decision is None` means the caller should
    fall through to the haiku classifier; `decision is False` means a
    heuristic matched and the caller should skip haiku entirely.

    Currently implements exactly one heuristic:

      short_pleasantry:
        len(text) ≤ _MAX_LENGTH
        AND not has_domain       (nothing topical to respond to)
        AND not name_mentioned   (user didn't address the bot)
        AND '?' not in text      (not a question)
        AND last whitespace-split token, stripped of trailing punctuation
            and lowercased, is in _PLEASANTRIES

    All four gates must clear before the token check runs; any single
    miss defers to haiku. The length gate is cheapest so it runs first.
    """
    if _MAX_LENGTH <= 0 or not _PLEASANTRIES:
        return _DEFER
    if len(text) > _MAX_LENGTH:
        return _DEFER
    if has_domain:
        return _DEFER
    if name_mentioned:
        return _DEFER
    if "?" in text:
        return _DEFER

    tokens = text.split()
    if not tokens:
        return _DEFER
    last = tokens[-1].rstrip(_TRAILING_PUNCT).lower()
    if not last:
        return _DEFER
    if last in _PLEASANTRIES:
        return PregateResult(decision=False, reason="short_pleasantry")
    return _DEFER
