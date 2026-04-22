"""Tests for Phase 1 — prompt-cache observability.

Drives _handle() with mock ResultMessage objects carrying a usage dict,
then asserts that the per-turn cache fields on the Session get stashed
correctly so _fire_on_text / _fire_on_text_streamed can pass them to
log_bot_response.

The actual wiring into log_bot_response is tested at the feedback unit
level in tests/test_feedback.py::TestCacheMetrics. This file covers the
Session-side half: usage dict → _turn_*_tokens fields.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from brendbot import session as session_mod
from claude_agent_sdk import ResultMessage


# ── Test helpers (same pattern as test_phantom_discriminator) ────────────

def _result_message(
    usage: dict | None = None,
    stop_reason: Any = "end_turn",
) -> ResultMessage:
    m = ResultMessage()
    m.subtype = "success"
    m.duration_ms = 100
    m.duration_api_ms = 90
    m.is_error = False
    m.num_turns = 1
    m.session_id = "sess_test"
    m.total_cost_usd = 0.001
    m.usage = usage if usage is not None else {}
    m.stop_reason = stop_reason
    return m


def _make_session(tmp_path: Path) -> session_mod.Session:
    cwd = tmp_path / "transcript"
    cwd.mkdir()
    s = session_mod.Session(
        key="test:ch1",
        tier="admin",
        cwd=str(cwd),
        chat_id="ch1",
    )
    # Suppress real dispatch; only care about cache-field state after _handle.
    s._on_text = lambda *a, **kw: None  # type: ignore
    s._chat_id = "ch1"
    s._fire_on_text = _noop_async  # type: ignore
    return s


async def _noop_async(*args, **kwargs) -> None:  # pragma: no cover - stub
    return None


def _drive(session: session_mod.Session, message: Any) -> None:
    async def _run() -> None:
        session._handle(message)
        await asyncio.sleep(0)
    asyncio.run(_run())


# ── Tests ────────────────────────────────────────────────────────────────

class TestCacheFieldStash:
    """ResultMessage.usage → Session._turn_*_tokens."""

    def test_full_usage_dict_stashed(self, tmp_path) -> None:
        """Steady-state cache hit: all three fields pulled out of usage."""
        s = _make_session(tmp_path)
        _drive(s, _result_message(usage={
            "input_tokens": 50,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 120,
        }))
        assert s._turn_input_tokens == 50
        assert s._turn_cache_read_tokens == 4000
        assert s._turn_cache_creation_tokens == 0

    def test_first_turn_cache_creation(self, tmp_path) -> None:
        """Turn 1 of a new session: cache_creation_input_tokens populated,
        cache_read_input_tokens zero."""
        s = _make_session(tmp_path)
        _drive(s, _result_message(usage={
            "input_tokens": 200,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 3500,
        }))
        assert s._turn_input_tokens == 200
        assert s._turn_cache_read_tokens == 0
        assert s._turn_cache_creation_tokens == 3500

    def test_empty_usage_leaves_fields_none(self, tmp_path) -> None:
        """No usage dict at all → fields stay None. log_bot_response will
        omit the cache block entirely in this case."""
        s = _make_session(tmp_path)
        _drive(s, _result_message(usage={}))
        assert s._turn_input_tokens is None
        assert s._turn_cache_read_tokens is None
        assert s._turn_cache_creation_tokens is None

    def test_missing_cache_keys_default_to_zero(self, tmp_path) -> None:
        """Partial usage dict (input_tokens only, no cache fields) → the
        cache fields default to 0. This is the API returning a usage
        block but with older key set or cache disabled. The feedback
        layer will still write a cache block with zeros."""
        s = _make_session(tmp_path)
        _drive(s, _result_message(usage={"input_tokens": 42}))
        assert s._turn_input_tokens == 42
        assert s._turn_cache_read_tokens == 0
        assert s._turn_cache_creation_tokens == 0

    def test_stash_overwrites_previous_turn(self, tmp_path) -> None:
        """Per-turn stash, not cumulative. Turn N+1 overwrites turn N's
        values so each bot_responses row reflects its own cache state."""
        s = _make_session(tmp_path)
        _drive(s, _result_message(usage={
            "input_tokens": 10,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 500,
        }))
        _drive(s, _result_message(usage={
            "input_tokens": 20,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0,
        }))
        assert s._turn_input_tokens == 20
        assert s._turn_cache_read_tokens == 800
        assert s._turn_cache_creation_tokens == 0

    def test_none_values_in_usage_coerced_to_zero(self, tmp_path) -> None:
        """API occasionally returns None for unused cache fields rather
        than omitting them. The `or 0` fallback must handle this."""
        s = _make_session(tmp_path)
        _drive(s, _result_message(usage={
            "input_tokens": 99,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
        }))
        assert s._turn_input_tokens == 99
        assert s._turn_cache_read_tokens == 0
        assert s._turn_cache_creation_tokens == 0

    def test_empty_usage_after_populated_turn_resets_fields(self, tmp_path) -> None:
        """Turn N had usage; Turn N+1 has none (e.g. SDK error). Fields
        must reset to None so the N+1 log row correctly omits the cache
        block instead of leaking N's values."""
        s = _make_session(tmp_path)
        _drive(s, _result_message(usage={
            "input_tokens": 10,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 0,
        }))
        assert s._turn_input_tokens == 10
        _drive(s, _result_message(usage={}))
        assert s._turn_input_tokens is None
        assert s._turn_cache_read_tokens is None
        assert s._turn_cache_creation_tokens is None
