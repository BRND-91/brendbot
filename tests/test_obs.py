"""Tests for brendbot.obs — the append-only JSONL observability layer.

These pin:
- basic append semantics (one line, parse as JSON, correct field set)
- ts auto-fill behavior (added if absent, preserved if present)
- append-not-overwrite across multiple calls
- parent-dir auto-creation on first write
- failure swallowing (I/O error never raises to caller)
- rotation at size threshold
- rotation chain correctness (.1 → .2 → ..., oldest dropped)

The typed convenience wrappers (log_tool_call, log_turn_event, etc.)
are tested for field shape so a future schema change surfaces here
rather than at production debug time.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brendbot import obs


@pytest.fixture
def tmp_logs(tmp_path, monkeypatch):
    """Redirect obs._LOGS_DIR to a tmp path for each test."""
    monkeypatch.setattr(obs, "_LOGS_DIR", tmp_path)
    return tmp_path


# ── append_jsonl core ────────────────────────────────────────────────────


def test_append_writes_one_line(tmp_logs):
    obs.append_jsonl("test_log", {"foo": "bar", "n": 1})
    lines = (tmp_logs / "test_log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["foo"] == "bar"
    assert entry["n"] == 1


def test_ts_auto_filled_when_absent(tmp_logs):
    before = time.time()
    obs.append_jsonl("test_log", {"foo": "bar"})
    after = time.time()
    entry = json.loads((tmp_logs / "test_log.jsonl").read_text().splitlines()[0])
    assert "ts" in entry
    assert before <= entry["ts"] <= after


def test_ts_preserved_when_caller_provides(tmp_logs):
    """Callers can override ts for deterministic tests or for logging
    historical events. The auto-fill must not clobber an explicit value."""
    obs.append_jsonl("test_log", {"ts": 1234567890.5, "foo": "bar"})
    entry = json.loads((tmp_logs / "test_log.jsonl").read_text().splitlines()[0])
    assert entry["ts"] == 1234567890.5


def test_append_does_not_overwrite(tmp_logs):
    for i in range(5):
        obs.append_jsonl("test_log", {"n": i})
    lines = (tmp_logs / "test_log.jsonl").read_text().splitlines()
    assert len(lines) == 5
    values = [json.loads(line)["n"] for line in lines]
    assert values == [0, 1, 2, 3, 4]


def test_parent_dir_auto_created(tmp_path, monkeypatch):
    """obs._LOGS_DIR may not exist on first deploy. The first write
    must create it rather than fail."""
    deep = tmp_path / "not_yet" / "logs"
    monkeypatch.setattr(obs, "_LOGS_DIR", deep)
    assert not deep.exists()
    obs.append_jsonl("test_log", {"foo": "bar"})
    assert (deep / "test_log.jsonl").exists()


def test_append_with_name_suffix_idempotent(tmp_logs):
    """Accept both ``test_log`` and ``test_log.jsonl`` as names — the
    distinction shouldn't matter to callers."""
    obs.append_jsonl("test_log", {"n": 1})
    obs.append_jsonl("test_log.jsonl", {"n": 2})
    lines = (tmp_logs / "test_log.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_io_failure_swallowed(tmp_logs, monkeypatch):
    """Observability must never raise. A write that fails for any
    reason returns silently so the caller's main path is unaffected."""
    def _boom(*args, **kwargs):
        raise PermissionError("simulated")
    monkeypatch.setattr(Path, "open", _boom)
    # Should not raise
    obs.append_jsonl("test_log", {"foo": "bar"})


def test_unicode_preserved_not_escaped(tmp_logs):
    """ensure_ascii=False — Korean, Japanese, Swedish, emoji all stay
    as native characters in the log. The pilot logs had entries like
    'här är jag' and 안녕하세요 that would be unreadable if escaped."""
    obs.append_jsonl("test_log", {"text": "här är jag"})
    obs.append_jsonl("test_log", {"text": "안녕하세요"})
    obs.append_jsonl("test_log", {"text": "🫵"})
    raw = (tmp_logs / "test_log.jsonl").read_text()
    assert "här är jag" in raw
    assert "안녕하세요" in raw
    assert "🫵" in raw


# ── Rotation ─────────────────────────────────────────────────────────────


def test_rotation_fires_at_threshold(tmp_logs, monkeypatch):
    """When the base file crosses _ROTATE_BYTES, the next append
    rotates base → .1 and starts writing into a fresh base.

    Sizing note: rotation happens at the start of any write that
    observes the base file over threshold, not only at specific
    boundaries. The test sets threshold such that exactly one
    rotation fires across the whole burst — so the final state is
    exactly ``base + .1`` with no higher rotations."""
    # Threshold sized so 20 * ~100-byte entries crosses once, not twice.
    monkeypatch.setattr(obs, "_ROTATE_BYTES", 1500)

    for i in range(20):
        obs.append_jsonl("test_log", {"n": i, "padding": "x" * 50})

    assert (tmp_logs / "test_log.1.jsonl").exists()
    # No secondary rotation given the threshold
    assert not (tmp_logs / "test_log.2.jsonl").exists()

    base_lines = (tmp_logs / "test_log.jsonl").read_text().splitlines()
    rotated_lines = (tmp_logs / "test_log.1.jsonl").read_text().splitlines()

    all_ns = sorted(
        json.loads(line)["n"] for line in base_lines + rotated_lines
    )
    assert all_ns == list(range(20))

    # .1 is the prefix (earlier writes), base is the suffix (later writes)
    base_ns = [json.loads(line)["n"] for line in base_lines]
    rotated_ns = [json.loads(line)["n"] for line in rotated_lines]
    assert rotated_ns == sorted(rotated_ns)
    assert base_ns == sorted(base_ns)
    assert max(rotated_ns) + 1 == min(base_ns)


def test_rotation_chain_shifts(tmp_logs, monkeypatch):
    """After multiple rotations, files shift: .1 → .2, .2 → .3, etc.
    Oldest (.N) is dropped when the chain exceeds _MAX_ROTATIONS."""
    monkeypatch.setattr(obs, "_ROTATE_BYTES", 200)
    monkeypatch.setattr(obs, "_MAX_ROTATIONS", 3)

    # Trigger 4 rotations
    for rotation in range(4):
        for i in range(10):
            obs.append_jsonl("test_log", {"r": rotation, "n": i, "pad": "x" * 20})
        obs.append_jsonl("test_log", {"rotate_marker": rotation})

    # .1, .2, .3 should exist; .4 should have been dropped
    assert (tmp_logs / "test_log.1.jsonl").exists()
    assert (tmp_logs / "test_log.2.jsonl").exists()
    assert (tmp_logs / "test_log.3.jsonl").exists()
    assert not (tmp_logs / "test_log.4.jsonl").exists()


def test_rotation_below_threshold_no_op(tmp_logs, monkeypatch):
    """Small logs don't rotate. Important because most production
    deployments will have many small log files, not a few huge ones."""
    monkeypatch.setattr(obs, "_ROTATE_BYTES", 10_000)
    for i in range(5):
        obs.append_jsonl("test_log", {"n": i})
    assert not (tmp_logs / "test_log.1.jsonl").exists()
    lines = (tmp_logs / "test_log.jsonl").read_text().splitlines()
    assert len(lines) == 5


# ── Typed wrappers ───────────────────────────────────────────────────────


def test_log_tool_call_schema(tmp_logs):
    obs.log_tool_call(
        session_key="discord:123",
        turn_id="turn-42",
        tool="Bash",
        input_summary="ls /home/user",
        output_shape={"bytes": 500, "lines": 20, "error": False},
    )
    entry = json.loads((tmp_logs / "tool_calls.jsonl").read_text().splitlines()[0])
    assert entry["session_key"] == "discord:123"
    assert entry["turn_id"] == "turn-42"
    assert entry["tool"] == "Bash"
    assert entry["input_summary"] == "ls /home/user"
    assert entry["output_shape"]["lines"] == 20


def test_log_tool_call_truncates_input(tmp_logs):
    """input_summary is capped at 200 chars. A 5000-char Bash command
    is common (heredocs, large JSON payloads) and the log should not
    carry the whole thing."""
    obs.log_tool_call(
        session_key="discord:123",
        turn_id="t1",
        tool="Bash",
        input_summary="x" * 5000,
    )
    entry = json.loads((tmp_logs / "tool_calls.jsonl").read_text().splitlines()[0])
    assert len(entry["input_summary"]) == 200


def test_log_turn_event_schema(tmp_logs):
    obs.log_turn_event(
        session_key="discord:123",
        channel_id="456",
        turn_id="turn-42",
        model="sonnet",
        context_tokens=15317,
        cost_usd=0.0283,
        duration_ms=2200,
        text_emitted=True,
        tool_call_count=3,
        stop_reason="end_turn",
    )
    entry = json.loads((tmp_logs / "turn_events.jsonl").read_text().splitlines()[0])
    assert entry["model"] == "sonnet"
    assert entry["context_tokens"] == 15317
    assert entry["text_emitted"] is True
    assert entry["stop_reason"] == "end_turn"


def test_log_gate_event_refusal_fields(tmp_logs):
    obs.log_gate_event(
        session_key="discord:123",
        channel_id="456",
        message_id="msg-789",
        outcome="REFUSE",
        weighted_sum=3.5,
        criteria={"tragedy_new": 0.9, "person_targeted": 1.5, "frame_directed": 2.0},
        refusal_text="can't do that one — targeted real person stacks",
        user_text_preview="was objectively fabricated framing",
    )
    entry = json.loads((tmp_logs / "gate_events.jsonl").read_text().splitlines()[0])
    assert entry["outcome"] == "REFUSE"
    assert entry["weighted_sum"] == 3.5
    assert entry["criteria"]["frame_directed"] == 2.0
    assert "targeted real person" in entry["refusal_text"]


def test_log_gate_event_preview_truncated(tmp_logs):
    obs.log_gate_event(
        session_key="discord:123",
        channel_id="456",
        message_id="msg-789",
        outcome="PASS",
        weighted_sum=0.0,
        criteria={},
        refusal_text=None,
        user_text_preview="x" * 500,
    )
    entry = json.loads((tmp_logs / "gate_events.jsonl").read_text().splitlines()[0])
    assert len(entry["user_text_preview"]) == 200


def test_log_error_schema(tmp_logs):
    obs.log_error(
        session_key="discord:123",
        error_class="APIError529",
        error_msg="Overloaded",
        context_tokens=110855,
        recoverable=True,
        detail={"request_id": "req_011CaMqQ7rLHwdkwuW7BWikE"},
    )
    entry = json.loads((tmp_logs / "errors.jsonl").read_text().splitlines()[0])
    assert entry["error_class"] == "APIError529"
    assert entry["recoverable"] is True
    assert entry["detail"]["request_id"].startswith("req_")


def test_log_error_truncates_long_message(tmp_logs):
    obs.log_error(
        session_key="discord:123",
        error_class="Test",
        error_msg="x" * 2000,
    )
    entry = json.loads((tmp_logs / "errors.jsonl").read_text().splitlines()[0])
    assert len(entry["error_msg"]) == 500


# ── Readback compatibility ───────────────────────────────────────────────


def test_readback_grep_by_channel_id(tmp_logs):
    """The soul's SELF-REPORT RULES section tells the bot to answer
    'why did the gate refuse' via:

        tac logs/gate_events.jsonl | grep '"message_id":"<id>"'

    Confirm the on-disk format supports that: message_id appears as a
    literal JSON string value on each line, lines are ordered by
    append time, and grep can pick a single entry."""
    obs.log_gate_event(
        session_key="s1", channel_id="ch_a", message_id="m_older",
        outcome="PASS", weighted_sum=0.0, criteria={},
        refusal_text=None, user_text_preview="fine",
    )
    obs.log_gate_event(
        session_key="s1", channel_id="ch_a", message_id="m_target",
        outcome="REFUSE", weighted_sum=2.5, criteria={"tragedy_new": 0.9},
        refusal_text="no", user_text_preview="bad",
    )
    obs.log_gate_event(
        session_key="s1", channel_id="ch_a", message_id="m_newer",
        outcome="PASS", weighted_sum=0.0, criteria={},
        refusal_text=None, user_text_preview="fine again",
    )

    lines = (tmp_logs / "gate_events.jsonl").read_text().splitlines()
    # Simulate `grep '"message_id": "m_target"'`
    matching = [line for line in lines if '"message_id": "m_target"' in line]
    assert len(matching) == 1
    entry = json.loads(matching[0])
    assert entry["outcome"] == "REFUSE"
