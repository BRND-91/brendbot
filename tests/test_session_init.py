"""Smoke test for Session.__init__ — guards against regressions in field
initialization, especially the Phase 3 fields added by 1A/1B/2A.

Does not exercise the full session lifecycle (no subprocess, no SDK calls).
Just confirms that constructing a Session sets every field the rest of the
codebase reads from before any inject() is called.
"""
from pathlib import Path

import pytest

from brendbot import session as session_mod


def _make_session(tmp_path: Path):
    """Construct a Session without spinning up the SDK subprocess."""
    cwd = tmp_path / "transcript"
    cwd.mkdir()
    return session_mod.Session(
        key="test:ch1",
        tier="admin",
        cwd=str(cwd),
        chat_id="ch1",
    )


def test_session_init_sets_phase3_load_fields(tmp_path):
    """1A — cumulative load tracking fields exist with zero values."""
    s = _make_session(tmp_path)
    assert s._cumulative_load == 0.0
    assert s._cumulative_bash_calls == 0
    assert s._cumulative_haiku_invocations == 0
    assert s._cumulative_other_tools == 0
    assert s._turn_bash_calls == 0
    assert s._turn_other_tool_calls == 0


def test_session_init_sets_phase3_shallow_rest_fields(tmp_path):
    """1B — shallow rest tracking initializes correctly."""
    s = _make_session(tmp_path)
    assert s._shallow_rested is False
    assert s._shallow_rest_count == 0
    assert s._next_turn_is_housekeeping is False


def test_session_init_sets_phase3_episode_fields(tmp_path):
    """2A — episode write needs session-scope fields populated."""
    s = _make_session(tmp_path)
    # Started timestamp must be a non-empty ISO string.
    assert s._session_started_at
    assert "T" in s._session_started_at  # ISO format
    # Domains-seen accumulator starts empty.
    assert s._session_domains_seen == set()


def test_session_has_trigger_shallow_rest(tmp_path):
    """1B — _trigger_shallow_rest must be defined and async."""
    import inspect
    s = _make_session(tmp_path)
    method = getattr(s, "_trigger_shallow_rest", None)
    assert method is not None
    assert inspect.iscoroutinefunction(method)
