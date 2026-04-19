"""Ambiguity classifier — haiku-backed gate for messages in the middle band.

Messages whose heuristic score lands above ENGAGE_THRESHOLD but below
ENGAGE_HARD_PASS go through this classifier to decide engage / don't-engage.
The hard-pass path skips it entirely; the hard-drop path never reaches it.

This module was pulled out of discord.py in Phase 2b (zero-cost plumbing).
The interface is the stable hookpoint Phase 3 wrapped with a deterministic
pregate and Phase 4 extends with ``classify_combined`` — a single haiku
call that returns BOTH the engagement decision and the content-gate tags,
cutting one subprocess roundtrip off the middle-band-engaged path.

Current runtime behavior for ``classify()`` is preserved bit-for-bit —
it performs exactly the same work the old ``_haiku_gatecheck_with_reason``
did, logs the same lines, and returns the same decision shape (now wrapped
in a typed dataclass instead of a bare dict). ``classify_combined()`` is
new and only invoked from the middle-band defer branch.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from brendbot.content_gate import ClassifierResult as ContentClassifierResult

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


@dataclass
class CombinedResult:
    """Structured return shape for ``classify_combined()``.

    engagement: The parsed engagement half (same dataclass the standalone
      ``classify()`` returns). When SDK error or the engagement line is
      unparseable, this carries reason="error" — callers MUST treat it
      the same way they'd treat a standalone classifier error (escalate
      or drop based on score).
    content: Parsed content-gate result from the TRIGGERED/REASONING half,
      or ``None`` when the engagement half parsed but the content half
      did not. ``None`` is the signal the caller uses to decide whether
      to fall back to a standalone ``content_gate_classify`` call.
    content_parse_error: True iff the content half was present but
      unparseable. Distinct from ``content is None`` due to SDK error —
      the caller needs to know whether to fall back (parse error, retry
      standalone) or to skip content gating entirely (SDK error already
      failed the engagement half).
    """
    engagement: ClassifierResult
    content: "ContentClassifierResult | None" = None
    content_parse_error: bool = False


# Sentinel returned on total failure (SDK exception, empty response, or
# engagement line unparseable). Downstream treats engagement.reason=="error"
# exactly like a standalone classifier failure — no content fallback attempt
# because there's no reason to believe a standalone call would fare better
# when the subprocess itself failed.
_COMBINED_ERROR_RESULT = CombinedResult(engagement=_ERROR_RESULT)


# Valid tone palette — must stay in sync with haiku_classify in session.py.
_VALID_TONES = frozenset({
    "funny", "hype", "sad", "weird", "dumb", "wholesome", "neutral",
})


# Matches "ENGAGE: YES|NO [tone]" at the start of any line. Case-insensitive
# on the YES/NO token (the prompt asks for uppercase but models drift). The
# tone capture is optional — missing tone falls through to "neutral".
_ENGAGE_LINE_RE = re.compile(
    r"^\s*ENGAGE:\s*(YES|NO)\b\s*(\w+)?",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_engagement_line(raw: str) -> ClassifierResult | None:
    """Extract the engagement half of a combined classifier response.

    Returns a ClassifierResult on success, or None if the ENGAGE line is
    missing/unparseable — in which case the caller must fail the whole
    combined call (engagement decision is load-bearing; we can't recover).
    """
    m = _ENGAGE_LINE_RE.search(raw)
    if m is None:
        return None
    decision_tok = m.group(1).upper()
    engage = decision_tok == "YES"
    tone_tok = (m.group(2) or "").lower()
    tone = tone_tok if tone_tok in _VALID_TONES else "neutral"
    reason = f"sdk:combined:{decision_tok}"
    return ClassifierResult(engage=engage, reason=reason, tone=tone)


def _build_combined_prompt(
    text: str,
    recent_context: list[dict] | None,
) -> str:
    """Assemble the prompt handed to the combined classifier.

    Reads the ``combined_classifier_prompt`` block from engagement.yaml
    via the module-level cache in brendbot.discord. If the yaml doesn't
    ship the block (dev environment, out-of-sync config), falls back to
    a minimal inline prompt so the pipeline still functions — the
    standalone fallback path will pick up any content-gate gaps.
    """
    try:
        from brendbot.discord import _ENGAGEMENT_CFG
        classifier_rules = _ENGAGEMENT_CFG.get(
            "combined_classifier_prompt", ""
        ).strip()
    except Exception:
        classifier_rules = ""
    if not classifier_rules:
        classifier_rules = (
            "Perform TWO classifications on the message below. "
            "Output exactly three lines in this order:\n"
            "ENGAGE: YES|NO <tone>\n"
            "TRIGGERED: <criteria or none>\n"
            "REASONING: <one sentence>\n"
            "Tones: funny hype sad weird dumb wholesome neutral. "
            "Criteria: tragedy_old/mid/new/live, "
            "person_satire/neutral/targeted, "
            "frame_fictional/ambiguous/directed. "
            "Hard floors: minor_sexual, wmd_synth, malware, "
            "infra_attack, extremist_recruit, directed_incite."
        )

    recent = recent_context[-5:] if recent_context else []
    context_lines = "\n".join(
        f"{m.get('display_name', 'unknown')}: {m.get('text', '')}"
        for m in recent
    )
    # Truncate message to mirror content_gate_classify's 2000-char cap —
    # the content half of the classifier expects the same input budget.
    return (
        f"{classifier_rules}\n\n"
        f"Recent context:\n{context_lines}\n"
        f"New message: {text[:2000]}"
    )


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


async def classify_combined(
    text: str,
    recent_context: list[dict] | None,
) -> CombinedResult:
    """Combined engagement + content classifier — one haiku roundtrip.

    Returns CombinedResult with both halves parsed. Error semantics:

      * SDK exception or empty response → _COMBINED_ERROR_RESULT
        (engagement.reason=="error", content=None, parse_error=False).
        Caller treats this like a standalone classifier error.
      * Engagement line unparseable → same as above. Without a decision,
        the pipeline can't route.
      * Engagement parses, content half unparseable → CombinedResult with
        the engagement result and content=None, content_parse_error=True.
        Caller MUST fall back to a standalone content_gate_classify call
        if engagement said YES — we have an engage decision but no safety
        signal, and skipping the safety check would violate cost-neutrality
        on the "always-gate-before-generate" contract.
      * Both halves parse → CombinedResult with both populated.

    Caching: result is cached under the combined prompt string (separate
    singleton from get_engage_cache / get_content_cache so the two
    decision types never cross-pollinate).
    """
    # Late imports — same circular-dep reasoning as classify() above.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )
    from brendbot.classifier_cache import get_combined_cache
    from brendbot.content_gate import parse_classifier_response
    from brendbot.session import get_classifier_pool

    prompt = _build_combined_prompt(text, recent_context)

    cache = get_combined_cache()
    cached = cache.get(prompt)
    if cached is not None:
        logger.debug("classify_combined cache hit")
        return cached

    pool = get_classifier_pool()
    classifier_client: ClaudeSDKClient | None = None
    raw_text = ""
    try:
        classifier_client = await pool.acquire()
        await classifier_client.query(prompt)

        async for msg in classifier_client.receive_messages():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        raw_text += block.text
            if isinstance(msg, ResultMessage):
                break
    except Exception as e:
        logger.warning("classify_combined SDK error: %s", e)
        return _COMBINED_ERROR_RESULT
    finally:
        if classifier_client is not None:
            await pool.dispose(classifier_client)

    if not raw_text.strip():
        logger.warning("classify_combined empty response")
        return _COMBINED_ERROR_RESULT

    engagement = _parse_engagement_line(raw_text)
    if engagement is None:
        # No ENGAGE line → can't route. Treat as error.
        logger.warning(
            "classify_combined missing ENGAGE line: %r", raw_text[:200]
        )
        return _COMBINED_ERROR_RESULT

    logger.info(
        "Haiku combined: %s (message: %r)", engagement.reason, text[:50]
    )

    # Content half reuses content_gate.parse_classifier_response verbatim —
    # its MULTILINE regexes pick up TRIGGERED:/REASONING: lines regardless
    # of what comes before them, so the extra ENGAGE: line is ignored.
    content = parse_classifier_response(raw_text)
    if content.parse_error:
        result = CombinedResult(
            engagement=engagement,
            content=None,
            content_parse_error=True,
        )
    else:
        result = CombinedResult(
            engagement=engagement,
            content=content,
            content_parse_error=False,
        )
    cache.put(prompt, result)
    return result
