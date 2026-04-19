"""Ambiguity classifier — haiku-backed gate for messages in the middle band.

Messages whose heuristic score lands above ENGAGE_THRESHOLD but below
ENGAGE_HARD_PASS go through this classifier to decide engage / don't-engage.
The hard-pass path skips it entirely; the hard-drop path never reaches it.

This module was pulled out of discord.py in Phase 2b (zero-cost plumbing).
The interface is the stable hookpoint Phase 3 will wrap with a
deterministic pre-gate (short-circuit obvious accepts/rejects without
paying the haiku subprocess latency) and Phase 4 may evolve to fold
the engagement gate and content gate into a single haiku call.

Current runtime behavior is preserved bit-for-bit — `classify()` performs
exactly the same work the old `_haiku_gatecheck_with_reason` did, logs
the same lines, and returns the same decision shape (now wrapped in a
typed dataclass instead of a bare dict).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ClassifierResult:
    """Structured return shape for `classify()`.

    Fields mirror the prior dict keys so downstream callers read the
    same information, just via attribute access instead of `.get()`.

    engage: True if the model said "respond", False otherwise.
    reason: short tag — "yes" / "no" on clean decisions, "error" when
      the SDK call failed for any reason, "unknown" if the classifier
      returned a response with no reason field populated.
    tone: palette key for react_with_fallback when a no-engage decision
      still warrants a reaction (e.g. tone="funny" → 😂). Defaults to
      "neutral" when the classifier doesn't supply one.
    """
    engage: bool
    reason: str
    tone: str


# Sentinel returned on any failure path. Callers should treat reason=="error"
# as "classifier unavailable, decide for yourself" rather than as a NO.
_ERROR_RESULT = ClassifierResult(engage=False, reason="error", tone="neutral")


async def classify(
    text: str,
    recent_context: list[dict] | None,
) -> ClassifierResult:
    """Ambiguity classifier — returns a typed result instead of the raw
    dict the SDK call produces.

    Error semantics: any failure path — SDK exception, auth error inside
    haiku_classify, or a malformed response — collapses to
    ClassifierResult(engage=False, reason="error", tone="neutral"). This
    matches the prior _haiku_gatecheck_with_reason contract exactly;
    call sites that want fail-loud behavior must inspect `reason` and
    apply their own escalation rule (see discord.py's on_message for
    the score>=0.6 escalation pattern).

    recent_context is truncated to the last 5 entries before being
    handed to the haiku classifier — matches the prior behavior.
    """
    # Late import breaks a circular dep: session.py imports feedback, and
    # we want classifier.py to be importable from discord.py which also
    # imports session. Keeping the import local to this function defers
    # resolution until after both modules are fully loaded.
    from brendbot.session import haiku_classify

    recent = recent_context[-5:] if recent_context else []
    try:
        decision = await haiku_classify({
            "message": text,
            "recent_context": recent,
        })
        reason = decision.get("reason", "unknown")
        logger.info("Haiku gate: %s (message: %r)", reason, text[:50])
        return ClassifierResult(
            engage=bool(decision.get("engage", False)),
            reason=reason,
            tone=decision.get("tone", "neutral"),
        )
    except Exception as e:
        logger.warning("Haiku gate failed: %s", e)
        return _ERROR_RESULT
