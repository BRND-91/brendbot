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


def test_session_init_sets_housekeeping_flag(tmp_path):
    """``_next_turn_is_housekeeping`` flag initializes False.

    Pre-2026-04-23 this flag was primarily set by the shallow-rest cycle
    (now deleted) to suppress response dispatch on the rest-injection
    turn. The flag survived the strip because context-summary refresh
    and related housekeeping paths still use the same suppress-dispatch
    semantic; only the shallow-rest path was removed.
    """
    s = _make_session(tmp_path)
    assert s._next_turn_is_housekeeping is False


def test_session_init_sets_phase3_episode_fields(tmp_path):
    """2A — episode write needs session-scope fields populated."""
    s = _make_session(tmp_path)
    # Started timestamp must be a non-empty ISO string.
    assert s._session_started_at
    assert "T" in s._session_started_at  # ISO format
    # Domains-seen accumulator starts empty.
    assert s._session_domains_seen == set()


def test_session_shallow_rest_method_removed(tmp_path):
    """``_trigger_shallow_rest`` was removed in the 2026-04-23 strip.

    The method fired a rest cycle when cumulative load crossed
    _LOAD_BUDGET_SHALLOW but stayed below _LOAD_BUDGET_PREEMPTIVE.
    Neither branch ever activated in any observed pilot — the pure
    token threshold at 400k always reached _CONTEXT_REFRESH_THRESHOLD
    first. The entire code path plus its load-budget constants were
    deleted. This test pins the removal so a future accidental
    reintroduction surfaces here.
    """
    s = _make_session(tmp_path)
    assert not hasattr(s, "_trigger_shallow_rest")
    assert not hasattr(s, "_shallow_rested")
    assert not hasattr(s, "_shallow_rest_count")


def test_session_init_sets_phase1_cache_fields(tmp_path):
    """Phase 1 — prompt-cache observability fields initialize to None.

    None is the correct sentinel because the ResultMessage handler may
    never fire for a session that gets restarted mid-boot, and in that
    case log_bot_response should omit the cache block entirely rather
    than write zeros that would skew hit-ratio aggregates.
    """
    s = _make_session(tmp_path)
    assert s._turn_input_tokens is None
    assert s._turn_cache_read_tokens is None
    assert s._turn_cache_creation_tokens is None


# ── Phase 2a stage-timing instrumentation ─────────────────────────────────

def test_session_init_sets_stage_timing_fields(tmp_path):
    """Phase 2a — all four _turn_t_* fields start as None. route_message
    populates recv_ts/engage_done_ts from the caller; content_gate_done
    and first_token are stamped later in the turn lifecycle."""
    s = _make_session(tmp_path)
    assert s._turn_t_received is None
    assert s._turn_t_engage_gate_done is None
    assert s._turn_t_content_gate_done is None
    assert s._turn_t_first_token is None


def test_compute_stage_timings_ms_returns_none_when_unstamped(tmp_path):
    """Phase 2a — no stamps → returns None so log_bot_response omits the
    field entirely. This is the DM / housekeeping-turn / legacy-caller
    path — no instrumentation, no noise in the log."""
    s = _make_session(tmp_path)
    assert s._compute_stage_timings_ms() is None


def test_compute_stage_timings_ms_computes_deltas(tmp_path):
    """Phase 2a — with all four stamps set, the helper returns four
    positive millisecond deltas."""
    s = _make_session(tmp_path)
    # Simulate a turn that ran through all four stages. time.monotonic()
    # returns seconds, so deltas * 1000 ms.
    s._turn_t_received = 100.0
    s._turn_t_engage_gate_done = 100.05          # 50 ms engage
    s._turn_t_content_gate_done = 100.80         # 750 ms content gate
    s._turn_t_first_token = 101.30               # 500 ms gate → first token
    timings = s._compute_stage_timings_ms()
    assert timings is not None
    assert timings["t_receive_to_engage_gate"] == pytest.approx(50.0, abs=0.1)
    assert timings["t_engage_gate_to_content_gate"] == pytest.approx(750.0, abs=0.1)
    assert timings["t_content_gate_to_first_token"] == pytest.approx(500.0, abs=0.1)
    # t_first_token_to_complete is computed against time.monotonic() at
    # call time, so only sanity-check it's a positive float.
    assert timings["t_first_token_to_complete"] > 0


def test_compute_stage_timings_ms_partial_stamps(tmp_path):
    """Phase 2a — partial stamps (DM path: no engage, no content-gate
    meaningful span) still produce whatever deltas ARE computable. Missing
    deltas get filtered out of the returned dict."""
    s = _make_session(tmp_path)
    s._turn_t_received = 100.0
    # Skip engage_gate_done — DM path: no engagement gate runs.
    s._turn_t_engage_gate_done = None
    s._turn_t_content_gate_done = 100.40
    s._turn_t_first_token = 101.00
    timings = s._compute_stage_timings_ms()
    assert timings is not None
    # receive→engage skipped (engage None); engage→gate skipped (engage None).
    assert "t_receive_to_engage_gate" not in timings
    assert "t_engage_gate_to_content_gate" not in timings
    # gate→first still computable.
    assert "t_content_gate_to_first_token" in timings
    assert "t_first_token_to_complete" in timings
