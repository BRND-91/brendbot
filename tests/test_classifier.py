"""Tests for brendbot.classifier — extracted haiku gate (Phase 2b).

The classifier was pulled out of discord.py with zero behavioural change:
every input that produced a given decision before now produces the same
ClassifierResult. These tests pin the five behaviours that matter:

  - engage=True with non-error reason → ClassifierResult(engage=True, ...)
  - engage=False with non-error reason → ClassifierResult(engage=False, ...)
  - SDK raises an exception → _ERROR_RESULT
  - tone defaults to "neutral" when not supplied
  - recent_context is truncated to last 5 entries before being passed
    through to haiku_classify (so the classifier never gets more history
    than the gate has been tuned against)

SDK stubs are installed by tests/conftest.py; haiku_classify is
monkeypatched per-test.
"""
from __future__ import annotations

import asyncio

import pytest

from brendbot import classifier


def _run(coro):
    """Event-loop-per-test helper matching the pattern already used in
    tests/test_admin_bypass.py."""
    return asyncio.run(coro)


# ── ClassifierResult dataclass ───────────────────────────────────────────

class TestClassifierResult:
    def test_fields_attached(self) -> None:
        r = classifier.ClassifierResult(engage=True, reason="yes", tone="funny")
        assert r.engage is True
        assert r.reason == "yes"
        assert r.tone == "funny"

    def test_error_sentinel_shape(self) -> None:
        """The error sentinel must match the prior contract exactly —
        callers key off reason=='error' for fail-loud escalation."""
        assert classifier._ERROR_RESULT.engage is False
        assert classifier._ERROR_RESULT.reason == "error"
        assert classifier._ERROR_RESULT.tone == "neutral"


# ── classify() dispatch ──────────────────────────────────────────────────

class TestClassify:
    def test_yes_decision(self, monkeypatch) -> None:
        async def fake_haiku_classify(payload):
            return {"engage": True, "reason": "yes", "tone": "wholesome"}

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", fake_haiku_classify)

        result = _run(classifier.classify("hey brend", None))
        assert result.engage is True
        assert result.reason == "yes"
        assert result.tone == "wholesome"

    def test_no_decision(self, monkeypatch) -> None:
        async def fake_haiku_classify(payload):
            return {"engage": False, "reason": "no", "tone": "funny"}

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", fake_haiku_classify)

        result = _run(classifier.classify("not for you", None))
        assert result.engage is False
        assert result.reason == "no"
        assert result.tone == "funny"

    def test_exception_collapses_to_error(self, monkeypatch) -> None:
        async def raising(payload):
            raise RuntimeError("SDK down")

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", raising)

        result = _run(classifier.classify("anything", None))
        assert result.engage is False
        assert result.reason == "error"
        assert result.tone == "neutral"

    def test_missing_tone_defaults_to_neutral(self, monkeypatch) -> None:
        async def fake(payload):
            return {"engage": True, "reason": "yes"}  # no tone

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", fake)

        result = _run(classifier.classify("hi", None))
        assert result.tone == "neutral"

    def test_missing_reason_defaults_to_unknown(self, monkeypatch) -> None:
        async def fake(payload):
            return {"engage": True}  # no reason

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", fake)

        result = _run(classifier.classify("hi", None))
        assert result.reason == "unknown"

    def test_context_truncated_to_last_five(self, monkeypatch) -> None:
        """The classifier must only see the last 5 context entries. This
        is what the prior _haiku_gatecheck_with_reason did and what the
        haiku prompt was tuned on — widening it silently would change
        decisions."""
        seen_payload = {}

        async def capturing(payload):
            seen_payload.update(payload)
            return {"engage": False, "reason": "no", "tone": "neutral"}

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", capturing)

        ten_entries = [{"text": f"msg{i}"} for i in range(10)]
        _run(classifier.classify("current", ten_entries))

        assert len(seen_payload["recent_context"]) == 5
        # Should be the LAST 5, not the first 5.
        assert seen_payload["recent_context"][0]["text"] == "msg5"
        assert seen_payload["recent_context"][-1]["text"] == "msg9"

    def test_none_context_becomes_empty_list(self, monkeypatch) -> None:
        seen_payload = {}

        async def capturing(payload):
            seen_payload.update(payload)
            return {"engage": False, "reason": "no", "tone": "neutral"}

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", capturing)

        _run(classifier.classify("current", None))
        assert seen_payload["recent_context"] == []

    def test_engage_cast_to_bool(self, monkeypatch) -> None:
        """If the classifier returns a truthy non-bool (e.g. 1), the
        result's engage field must still be a proper bool — downstream
        code does `if engage:` but also type-checks against bool in some
        places."""
        async def fake(payload):
            return {"engage": 1, "reason": "yes", "tone": "neutral"}

        from brendbot import session
        monkeypatch.setattr(session, "haiku_classify", fake)

        result = _run(classifier.classify("hi", None))
        assert result.engage is True
        assert isinstance(result.engage, bool)


# ── classify_combined — Phase 4 fold ─────────────────────────────────────

# The combined classifier issues one raw haiku roundtrip and parses the
# three-line response itself. These tests stub out get_classifier_pool so
# every test can script exactly what the SDK "returned" without touching
# a real subprocess.

def _install_fake_pool(monkeypatch, raw_text: str | None, raise_exc: Exception | None = None):
    """Monkeypatch brendbot.session.get_classifier_pool to return a pool
    whose acquire() yields a one-shot client that streams ``raw_text`` as
    an AssistantMessage then a ResultMessage. If ``raise_exc`` is given,
    the client's ``query`` coroutine raises it instead.

    Returns a list tracking dispose() calls so tests can assert the client
    is always torn down on both happy and error paths.
    """
    from brendbot import session as _session
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    dispose_log: list[str] = []

    def _make_block(text: str):
        # TextBlock is a stub class in conftest; instantiating it and
        # setting .text mirrors how real SDK TextBlocks look to isinstance.
        b = TextBlock()
        b.text = text
        return b

    class _FakeClient:
        async def query(self, prompt: str) -> None:
            if raise_exc is not None:
                raise raise_exc

        async def receive_messages(self):
            # Ship one AssistantMessage carrying the canned raw text,
            # then a ResultMessage so the loop exits cleanly.
            am = AssistantMessage()
            am.content = [_make_block(raw_text or "")]
            yield am
            yield ResultMessage()

    class _FakePool:
        async def acquire(self):
            return _FakeClient()

        async def dispose(self, client) -> None:
            dispose_log.append("disposed")

    # isinstance(msg, AssistantMessage) needs raw types that match, and
    # the stubs in conftest.py install them as bare `type(...)` subclasses
    # so the fake instances pass isinstance without extra wiring.

    monkeypatch.setattr(_session, "get_classifier_pool", lambda: _FakePool())
    # Caches are process-level singletons — reset between tests so a hit
    # from a previous test doesn't mask a miss in this one.
    from brendbot import classifier_cache
    classifier_cache._combined_cache = None
    return dispose_log


class TestClassifyCombined:
    def test_both_halves_parse_clean(self, monkeypatch) -> None:
        """Happy path — ENGAGE: YES tone, TRIGGERED: criterion=weight,
        REASONING: text. CombinedResult carries both halves, content
        is a ClassifierResult from content_gate.py, content_parse_error
        is False."""
        raw = (
            "ENGAGE: YES funny\n"
            "TRIGGERED: tragedy_old=0.3, frame_ambiguous=0.2\n"
            "REASONING: older historical reference, ambiguous framing.\n"
        )
        _install_fake_pool(monkeypatch, raw)

        result = _run(classifier.classify_combined("what happened in 1812?", None))

        assert result.engagement.engage is True
        assert result.engagement.tone == "funny"
        assert result.engagement.reason.startswith("sdk:combined:")
        assert result.content is not None
        assert result.content.parse_error is False
        assert result.content.criteria.get("tragedy_old") == 0.3
        assert result.content.criteria.get("frame_ambiguous") == 0.2
        assert result.content_parse_error is False

    def test_engage_no_benign_content(self, monkeypatch) -> None:
        """ENGAGE: NO + benign content (TRIGGERED: none) — the most
        common middle-band outcome when the classifier rejects."""
        raw = (
            "ENGAGE: NO neutral\n"
            "TRIGGERED: none\n"
            "REASONING: background chatter.\n"
        )
        _install_fake_pool(monkeypatch, raw)

        result = _run(classifier.classify_combined("lol same", None))

        assert result.engagement.engage is False
        assert result.engagement.tone == "neutral"
        assert result.content is not None
        assert result.content.is_benign
        assert result.content_parse_error is False

    def test_engagement_parses_content_unparseable(self, monkeypatch) -> None:
        """Engagement line clean, but content half missing TRIGGERED line —
        CombinedResult keeps the engagement decision, drops the content
        half, flags content_parse_error=True so the caller can fall back
        to a standalone content_gate_classify call."""
        raw = "ENGAGE: YES hype\nWAT: gibberish line\n"
        _install_fake_pool(monkeypatch, raw)

        result = _run(classifier.classify_combined("test", None))

        assert result.engagement.engage is True
        assert result.engagement.tone == "hype"
        assert result.content is None
        assert result.content_parse_error is True

    def test_sdk_exception_returns_error_sentinel(self, monkeypatch) -> None:
        """If the SDK call itself raises, return _COMBINED_ERROR_RESULT —
        no fallback data, caller treats like any other classifier outage."""
        _install_fake_pool(monkeypatch, raw_text=None, raise_exc=RuntimeError("boom"))

        result = _run(classifier.classify_combined("test", None))

        assert result is classifier._COMBINED_ERROR_RESULT
        assert result.engagement.reason == "error"
        assert result.content is None
        assert result.content_parse_error is False

    def test_empty_response_returns_error_sentinel(self, monkeypatch) -> None:
        """Pool delivers a client, query succeeds, but the message stream
        produces no usable text — same error path as SDK failure."""
        _install_fake_pool(monkeypatch, raw_text="")

        result = _run(classifier.classify_combined("test", None))

        assert result is classifier._COMBINED_ERROR_RESULT

    def test_missing_engage_line_returns_error_sentinel(self, monkeypatch) -> None:
        """Response is present but has no ENGAGE line — we can't route
        without an engagement decision, so fall back to the error sentinel
        rather than guess."""
        raw = "TRIGGERED: none\nREASONING: missing the ENGAGE prefix entirely.\n"
        _install_fake_pool(monkeypatch, raw)

        result = _run(classifier.classify_combined("test", None))

        assert result is classifier._COMBINED_ERROR_RESULT

    def test_cache_hit_skips_spawn(self, monkeypatch) -> None:
        """Second call with identical text + context must read from the
        combined cache and skip the pool entirely. If it doesn't, the
        second acquire() will fail because we've replaced the pool
        factory with one that raises."""
        raw = (
            "ENGAGE: YES wholesome\n"
            "TRIGGERED: none\n"
            "REASONING: direct address.\n"
        )
        _install_fake_pool(monkeypatch, raw)
        first = _run(classifier.classify_combined("hey brend", None))
        assert first.engagement.engage is True

        # Replace the pool with one that explodes on acquire — any call
        # proves the cache was bypassed.
        from brendbot import session as _session

        class _ExplodingPool:
            async def acquire(self):
                raise AssertionError("cache miss — pool acquired unexpectedly")

            async def dispose(self, client):
                pass

        monkeypatch.setattr(_session, "get_classifier_pool", lambda: _ExplodingPool())

        second = _run(classifier.classify_combined("hey brend", None))
        assert second.engagement.engage is True
        assert second.content is first.content  # same cached object

    def test_case_insensitive_engage_token(self, monkeypatch) -> None:
        """Models drift on case — ENGAGE: yes vs ENGAGE: YES must both work."""
        raw = "ENGAGE: yes neutral\nTRIGGERED: none\nREASONING: low case ok.\n"
        _install_fake_pool(monkeypatch, raw)
        result = _run(classifier.classify_combined("msg", None))
        assert result.engagement.engage is True

    def test_unknown_tone_defaults_to_neutral(self, monkeypatch) -> None:
        """If the classifier emits a tone word not in the valid set, we
        normalize to 'neutral' rather than propagate the drift."""
        raw = (
            "ENGAGE: YES chaotic\n"  # not in _VALID_TONES
            "TRIGGERED: none\n"
            "REASONING: test.\n"
        )
        _install_fake_pool(monkeypatch, raw)
        result = _run(classifier.classify_combined("msg", None))
        assert result.engagement.tone == "neutral"


# ── CombinedResult dataclass + constants ─────────────────────────────────

class TestCombinedResultShape:
    def test_error_sentinel_has_error_engagement(self) -> None:
        """Downstream code keys off engagement.reason == 'error' to trigger
        the fail-loud escalation rule. The combined error sentinel must
        propagate that exactly."""
        sentinel = classifier._COMBINED_ERROR_RESULT
        assert sentinel.engagement.reason == "error"
        assert sentinel.engagement.engage is False
        assert sentinel.content is None
        assert sentinel.content_parse_error is False

    def test_valid_tones_matches_haiku_palette(self) -> None:
        """The combined classifier's tone parser must accept the same
        seven tones haiku_classify uses — a drift here would silently
        turn every combined-path tone into 'neutral'."""
        assert classifier._VALID_TONES == {
            "funny", "hype", "sad", "weird", "dumb", "wholesome", "neutral",
        }
