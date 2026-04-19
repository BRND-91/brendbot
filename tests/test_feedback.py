"""Tests for feedback infrastructure: branch tag extraction and JSONL writers.

SDK stubs are installed by tests/conftest.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brendbot import feedback as fb


# ── extract_branch_tag ───────────────────────────────────────────────────

class TestExtractBranchTag:
    def test_rejected_tag(self) -> None:
        tag, stripped = fb.extract_branch_tag("[rejected] That's not how R-value works.")
        assert tag == "rejected"
        assert stripped == "That's not how R-value works."

    def test_searching_tag(self) -> None:
        tag, stripped = fb.extract_branch_tag("[searching] Let me check the latest data.")
        assert tag == "searching"
        assert stripped == "Let me check the latest data."

    def test_unverified_tag(self) -> None:
        tag, stripped = fb.extract_branch_tag("[unverified] No source on hand for that.")
        assert tag == "unverified"
        assert stripped == "No source on hand for that."

    def test_no_tag(self) -> None:
        tag, stripped = fb.extract_branch_tag("Just a normal response.")
        assert tag is None
        assert stripped == "Just a normal response."

    def test_unknown_tag_not_extracted(self) -> None:
        # Only the three valid tags are recognized — anything else is left in.
        tag, stripped = fb.extract_branch_tag("[bogus] Random tag.")
        assert tag is None
        assert stripped == "[bogus] Random tag."

    def test_tag_must_be_at_start(self) -> None:
        # Mid-message tags are ignored.
        tag, stripped = fb.extract_branch_tag("Some text [rejected] more text.")
        assert tag is None
        assert stripped == "Some text [rejected] more text."

    def test_tag_with_extra_whitespace(self) -> None:
        tag, stripped = fb.extract_branch_tag("[searching]    Extra spaces.")
        assert tag == "searching"
        assert stripped == "Extra spaces."


# ── log writers (JSONL append) ───────────────────────────────────────────

class TestLogWriters:
    def test_log_bot_response_writes_jsonl(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        fb.log_bot_response(
            channel_id="ch1",
            bot_message_id="m1",
            user_message_id="u1",
            user_text="hi",
            score=0.5,
            domains=["BUILDSCI"],
            address_level="moderate",
            branch_tag=None,
        )
        assert log_path.exists()
        record = json.loads(log_path.read_text().strip())
        assert record["bot_message_id"] == "m1"
        assert record["score"] == 0.5
        assert record["domains"] == ["BUILDSCI"]
        assert record["address_level"] == "moderate"
        assert record["branch_tag"] is None
        assert "ts" in record

    def test_log_branch_audit_writes_jsonl(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "branch_audit.jsonl"
        monkeypatch.setattr(fb, "BRANCH_AUDIT_LOG", log_path)
        fb.log_branch_audit("ch1", "m1", "rejected", "stripped response")
        record = json.loads(log_path.read_text().strip())
        assert record["branch"] == "rejected"
        assert record["response_text"] == "stripped response"

    def test_log_feedback_event_writes_jsonl(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "feedback_events.jsonl"
        monkeypatch.setattr(fb, "FEEDBACK_EVENTS_LOG", log_path)
        fb.log_feedback_event("ch1", "m1", "👎", "admin_id_123")
        record = json.loads(log_path.read_text().strip())
        assert record["emoji"] == "👎"
        assert record["signal"] == "bad_engagement"
        assert record["admin_id"] == "admin_id_123"

    def test_appends_not_overwrites(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        for i in range(3):
            fb.log_bot_response(
                channel_id="ch1", bot_message_id=f"m{i}",
                user_message_id=f"u{i}", user_text="t",
                score=None, domains=[], address_level="high",
                branch_tag=None,
            )
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 3
        ids = [json.loads(l)["bot_message_id"] for l in lines]
        assert ids == ["m0", "m1", "m2"]

    def test_user_text_truncated_to_500(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        long_text = "x" * 1000
        fb.log_bot_response(
            channel_id="ch1", bot_message_id="m1",
            user_message_id="u1", user_text=long_text,
            score=None, domains=[], address_level="high",
            branch_tag=None,
        )
        record = json.loads(log_path.read_text().strip())
        assert len(record["user_text"]) == 500


# ── stage_timings_ms (Phase 2a instrumentation) ──────────────────────────

class TestStageTimings:
    def test_stage_timings_omitted_when_none(self, tmp_path, monkeypatch) -> None:
        # Absent param → key must not appear in record.
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        fb.log_bot_response(
            channel_id="ch1", bot_message_id="m1",
            user_message_id="u1", user_text="hi",
            score=None, domains=[], address_level="high",
            branch_tag=None,
        )
        record = json.loads(log_path.read_text().strip())
        assert "stage_timings_ms" not in record

    def test_stage_timings_omitted_when_empty(self, tmp_path, monkeypatch) -> None:
        # Empty dict → key must not appear (avoid emitting noise).
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        fb.log_bot_response(
            channel_id="ch1", bot_message_id="m1",
            user_message_id="u1", user_text="hi",
            score=None, domains=[], address_level="high",
            branch_tag=None,
            stage_timings_ms={},
        )
        record = json.loads(log_path.read_text().strip())
        assert "stage_timings_ms" not in record

    def test_stage_timings_emitted_and_rounded(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        fb.log_bot_response(
            channel_id="ch1", bot_message_id="m1",
            user_message_id="u1", user_text="hi",
            score=None, domains=[], address_level="high",
            branch_tag=None,
            stage_timings_ms={
                "t_receive_to_engage_gate": 12.34567,
                "t_engage_gate_to_content_gate": 500.1,
                "t_content_gate_to_first_token": 7200.123,
                "t_first_token_to_complete": 1800.5,
            },
        )
        record = json.loads(log_path.read_text().strip())
        timings = record["stage_timings_ms"]
        # Rounded to 2 decimal places — avoids spurious precision in JSONL.
        assert timings["t_receive_to_engage_gate"] == 12.35
        assert timings["t_engage_gate_to_content_gate"] == 500.1
        assert timings["t_content_gate_to_first_token"] == 7200.12
        assert timings["t_first_token_to_complete"] == 1800.5

    def test_stage_timings_drops_non_numeric(self, tmp_path, monkeypatch) -> None:
        # Callers that pass None for a missing stage should have that
        # stage silently dropped. Non-numeric (str, bool-ish) likewise.
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        fb.log_bot_response(
            channel_id="ch1", bot_message_id="m1",
            user_message_id="u1", user_text="hi",
            score=None, domains=[], address_level="high",
            branch_tag=None,
            stage_timings_ms={
                "t_receive_to_engage_gate": 5.0,
                "t_engage_gate_to_content_gate": None,
                "t_content_gate_to_first_token": "oops",
                "t_first_token_to_complete": 100.0,
            },
        )
        record = json.loads(log_path.read_text().strip())
        timings = record["stage_timings_ms"]
        assert set(timings.keys()) == {
            "t_receive_to_engage_gate", "t_first_token_to_complete",
        }

    def test_stage_timings_all_none_omits_field(self, tmp_path, monkeypatch) -> None:
        # Every stage None → filtered dict is empty → field omitted.
        log_path = tmp_path / "bot_responses.jsonl"
        monkeypatch.setattr(fb, "BOT_RESPONSES_LOG", log_path)
        fb.log_bot_response(
            channel_id="ch1", bot_message_id="m1",
            user_message_id="u1", user_text="hi",
            score=None, domains=[], address_level="high",
            branch_tag=None,
            stage_timings_ms={"t_receive_to_engage_gate": None},
        )
        record = json.loads(log_path.read_text().strip())
        assert "stage_timings_ms" not in record


# ── FEEDBACK_REACTIONS map ───────────────────────────────────────────────

class TestFeedbackReactions:
    def test_all_four_emotes_mapped(self) -> None:
        assert fb.FEEDBACK_REACTIONS == {
            "👎": "bad_engagement",
            "👍": "good_engagement",
            "🚫": "bad_answer",
            "🎯": "good_answer",
        }
