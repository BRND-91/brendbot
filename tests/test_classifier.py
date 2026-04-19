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
