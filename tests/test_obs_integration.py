"""Integration tests for the obs instrumentation wired into
session_handler, session_gate, classifier_pool, and session.

These confirm that the actual call sites reach ``obs.append_jsonl``
with the right schema — not just that the obs module itself works
(that's test_obs.py). A failure here means the instrumentation lost
a hook point during refactoring.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from brendbot import obs
from brendbot import session as session_mod
from brendbot.content_gate import ClassifierResult
from brendbot.session import Session


@pytest.fixture
def tmp_logs(tmp_path, monkeypatch):
    """Redirect obs._LOGS_DIR to a tmp path for the duration of each test."""
    monkeypatch.setattr(obs, "_LOGS_DIR", tmp_path)
    return tmp_path


def _make_fake_session_for_gate(
    key: str = "test:integration",
    chat_id: str = "100",
    guild_id: str = "",
) -> Session:
    """Fake session for exercising session_gate paths. Mirrors the
    pattern from tests/test_admin_bypass.py::_make_fake_session."""
    s = Session.__new__(Session)
    s.key = key
    s._chat_id = chat_id
    s._guild_id = guild_id
    s._turn_bypass_pending = False
    s._fire_on_text_log = []  # type: ignore

    async def _fake_fire_on_text(text: str) -> None:
        s._fire_on_text_log.append(text)  # type: ignore
    s._fire_on_text = _fake_fire_on_text  # type: ignore
    return s


# ── Gate event logging ──────────────────────────────────────────────────


def test_gate_pass_writes_pass_event(tmp_logs, monkeypatch):
    """A benign message routes through PASS and produces a
    gate_events.jsonl entry with outcome=PASS, weighted_sum, and the
    user_text_preview."""
    async def _fake_classify(text: str) -> ClassifierResult:
        return ClassifierResult(criteria={"tragedy_old": 0.2})
    monkeypatch.setattr(session_mod, "content_gate_classify", _fake_classify)

    s = _make_fake_session_for_gate()
    result = asyncio.run(s.apply_content_gate(
        wrapped_text="<w>benign</w>",
        raw_user_text="a historical reference about 1929",
        tier="admin",
        sender_id="admin1",
        message_id="pass-msg-1",
    ))
    assert result == "inject"

    lines = (tmp_logs / "gate_events.jsonl").read_text().splitlines()
    entries = [json.loads(line) for line in lines]
    pass_entry = next(e for e in entries if e["message_id"] == "pass-msg-1")
    assert pass_entry["outcome"] == "PASS"
    assert pass_entry["weighted_sum"] == pytest.approx(0.2)
    assert pass_entry["refusal_text"] is None
    assert "historical reference" in pass_entry["user_text_preview"]


def test_gate_refuse_writes_refuse_event_with_refusal_text(tmp_logs, monkeypatch):
    """A high-sum message is REFUSEd and the gate_events entry carries
    the full refusal_text and criteria map — the fields the bot reads
    when answering 'why did you refuse'."""
    async def _fake_classify(text: str) -> ClassifierResult:
        return ClassifierResult(criteria={
            "tragedy_new": 0.9,
            "person_targeted": 1.5,
            "frame_directed": 2.0,
        })
    monkeypatch.setattr(session_mod, "content_gate_classify", _fake_classify)

    s = _make_fake_session_for_gate()
    asyncio.run(s.apply_content_gate(
        wrapped_text="<w>x</w>",
        raw_user_text="bad request",
        tier="admin",
        sender_id="admin1",
        message_id="refuse-msg-1",
    ))

    lines = (tmp_logs / "gate_events.jsonl").read_text().splitlines()
    entries = [json.loads(line) for line in lines]
    ref = next(e for e in entries if e["message_id"] == "refuse-msg-1")
    assert ref["outcome"] == "REFUSE"
    assert ref["weighted_sum"] == pytest.approx(4.4)
    assert ref["criteria"]["frame_directed"] == 2.0
    assert ref["refusal_text"] is not None
    assert "can't do that one" in ref["refusal_text"].lower()


def test_gate_floor_hit_writes_floor_event(tmp_logs, monkeypatch):
    async def _fake_classify(text: str) -> ClassifierResult:
        return ClassifierResult(hard_floor="malware")
    monkeypatch.setattr(session_mod, "content_gate_classify", _fake_classify)

    # Mock the floor cross-check so it doesn't spawn a real classifier
    async def _fake_crosscheck(user_text, floor):
        return True, "confirmed"
    monkeypatch.setattr(session_mod, "content_gate_cross_check_floor", _fake_crosscheck)

    s = _make_fake_session_for_gate()
    asyncio.run(s.apply_content_gate(
        wrapped_text="<w>x</w>",
        raw_user_text="malware please",
        tier="admin",
        sender_id="admin1",
        message_id="floor-msg-1",
    ))

    entries = [
        json.loads(line)
        for line in (tmp_logs / "gate_events.jsonl").read_text().splitlines()
    ]
    floor = next(e for e in entries if e["message_id"] == "floor-msg-1")
    assert floor["outcome"] == "FLOOR_HIT"
    assert "malware" in floor["refusal_text"].lower()


def test_gate_friend_tier_skip_writes_skip_event(tmp_logs, monkeypatch):
    """Friend-tier bypass also writes a gate event so there's a
    complete audit trail. Outcome is FRIEND_TIER_SKIP, refusal_text
    is None, classifier criteria are absent because the classifier
    didn't run."""
    from brendbot import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_FRIEND_GUILDS", frozenset({"99988"}))

    # Shouldn't be called, but stub to detect if it is
    called = []
    async def _spy_classify(text: str) -> ClassifierResult:
        called.append(text)
        return ClassifierResult(criteria={})
    monkeypatch.setattr(session_mod, "content_gate_classify", _spy_classify)

    s = _make_fake_session_for_gate(guild_id="99988")
    result = asyncio.run(s.apply_content_gate(
        wrapped_text="<w>x</w>",
        raw_user_text="anything at all",
        tier="admin",
        sender_id="admin1",
        message_id="friend-msg-1",
    ))
    assert result == "inject"
    assert called == []  # classifier was skipped

    entries = [
        json.loads(line)
        for line in (tmp_logs / "gate_events.jsonl").read_text().splitlines()
    ]
    skip = next(e for e in entries if e["message_id"] == "friend-msg-1")
    assert skip["outcome"] == "FRIEND_TIER_SKIP"
    assert skip["weighted_sum"] is None
    assert skip["criteria"] is None


def test_gate_classifier_crash_writes_error_event(tmp_logs, monkeypatch):
    """When the classifier raises, an errors.jsonl entry is written
    alongside the conservative REFUSE. The bot should be able to
    answer 'the classifier crashed' by reading errors.jsonl."""
    async def _crashing_classify(text: str) -> ClassifierResult:
        raise RuntimeError("simulated SDK crash")
    monkeypatch.setattr(session_mod, "content_gate_classify", _crashing_classify)

    s = _make_fake_session_for_gate()
    asyncio.run(s.apply_content_gate(
        wrapped_text="<w>x</w>",
        raw_user_text="anything",
        tier="admin",
        sender_id="admin1",
        message_id="crash-msg-1",
    ))

    err_path = tmp_logs / "errors.jsonl"
    assert err_path.exists()
    errors = [json.loads(line) for line in err_path.read_text().splitlines()]
    assert any("Classifier" in e["error_class"] for e in errors)
    assert any("simulated SDK crash" in e["error_msg"] for e in errors)


# ── Readback patterns (how the bot reads these logs) ────────────────────


def test_readback_finds_most_recent_refusal_by_message_id(tmp_logs, monkeypatch):
    """The soul's SELF-REPORT RULES direct the bot to answer
    'why did the gate refuse' via:

        tac logs/gate_events.jsonl | grep '"message_id":"<id>"'

    This test confirms the on-disk layout supports that grep: each
    entry is a standalone JSON object on its own line, message_id is
    a quoted string field, and the file is append-ordered."""
    async def _fake_classify(text: str) -> ClassifierResult:
        # Third call has bad content; first two are benign
        if "bad" in text:
            return ClassifierResult(criteria={
                "tragedy_new": 0.9, "person_targeted": 1.5, "frame_directed": 2.0,
            })
        return ClassifierResult(criteria={})
    monkeypatch.setattr(session_mod, "content_gate_classify", _fake_classify)

    s = _make_fake_session_for_gate()
    for mid, text in [
        ("earlier-1", "hello"),
        ("target-id", "bad"),
        ("later-1", "thanks"),
    ]:
        asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text=text,
            tier="admin",
            sender_id="admin1",
            message_id=mid,
        ))

    lines = (tmp_logs / "gate_events.jsonl").read_text().splitlines()
    # Simulate tac | grep '"message_id": "target-id"'
    for line in reversed(lines):
        if '"message_id": "target-id"' in line:
            entry = json.loads(line)
            assert entry["outcome"] == "REFUSE"
            assert entry["refusal_text"] is not None
            break
    else:
        pytest.fail("target message_id not found in gate_events.jsonl")
