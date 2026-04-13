"""Tests for the follow-up-after-tool-use engagement signal.

The follow-up signal catches iteration replies like "not that one, try again"
that would otherwise hard-drop because they lack a name-mention, @mention, or
domain keyword. When the bot completes a tool-using turn in response to user
X in channel C, any message from user X in channel C within
FOLLOW_UP_WINDOW_SECONDS gets a scoring boost equal to follow_up_after_tool_use.

These tests exercise the scoring-side behavior directly, bypassing the
session/inject path. The state dict _channel_last_tool_turn and the setter
record_tool_turn are module-level in brendbot.discord so tests can manipulate
them directly.
"""
from __future__ import annotations

import time

import pytest

from brendbot import discord as bd


@pytest.fixture(autouse=True)
def clear_followup_state():
    """Each test starts with clean follow-up state so prior tests don't
    leak into the current one. Also clears last-spoke state to prevent
    cross-test recency bleed."""
    bd._channel_last_tool_turn.clear()
    bd._channel_last_spoke.clear()
    yield
    bd._channel_last_tool_turn.clear()
    bd._channel_last_spoke.clear()


class TestRecordToolTurn:
    """Behavior of the record_tool_turn setter."""

    def test_writes_channel_entry(self) -> None:
        """After record_tool_turn, the channel entry exists with the
        expected user and a timestamp near now."""
        before = time.time()
        bd.record_tool_turn("chan_a", "user_1")
        after = time.time()

        entry = bd._channel_last_tool_turn.get("chan_a")
        assert entry is not None
        user_id, ts = entry
        assert user_id == "user_1"
        assert before <= ts <= after

    def test_overwrites_prior_entry_for_same_channel(self) -> None:
        """A second record_tool_turn for the same channel replaces the
        entry. Prior user's follow-up boost expires as soon as a new
        tool turn completes in the channel."""
        bd.record_tool_turn("chan_a", "user_1")
        bd.record_tool_turn("chan_a", "user_2")

        entry = bd._channel_last_tool_turn["chan_a"]
        assert entry[0] == "user_2"

    def test_distinct_channels_isolated(self) -> None:
        """Recording in chan_a does not affect chan_b state."""
        bd.record_tool_turn("chan_a", "user_1")
        bd.record_tool_turn("chan_b", "user_2")

        assert bd._channel_last_tool_turn["chan_a"][0] == "user_1"
        assert bd._channel_last_tool_turn["chan_b"][0] == "user_2"


class TestFollowUpScoring:
    """Behavior of _score_message with the follow-up boost."""

    def test_same_user_in_window_gets_boost(self) -> None:
        """User X prompts a tool turn; user X sends an ambient message
        within the window. The boost is applied."""
        bd.record_tool_turn("chan_a", "user_1")

        # A message that otherwise scores 0 (no domain match, short, not reply)
        result = bd._score_message(
            text="no not like that",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id="user_1",
        )
        assert result.score == pytest.approx(bd._SCORE_FOLLOW_UP)

    def test_different_user_no_boost(self) -> None:
        """User X prompts a tool turn; user Y sends a message. No boost."""
        bd.record_tool_turn("chan_a", "user_1")

        result = bd._score_message(
            text="no not like that",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id="user_2",  # different user
        )
        assert result.score == 0.0

    def test_stale_entry_no_boost(self) -> None:
        """A tool turn from longer ago than FOLLOW_UP_WINDOW_SECONDS
        does not trigger a boost even for the same user."""
        stale_ts = time.time() - (bd.FOLLOW_UP_WINDOW_SECONDS + 10)
        bd._channel_last_tool_turn["chan_a"] = ("user_1", stale_ts)

        result = bd._score_message(
            text="no not like that",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id="user_1",
        )
        assert result.score == 0.0

    def test_no_entry_no_boost(self) -> None:
        """Channel has never had a recorded tool turn. No boost."""
        result = bd._score_message(
            text="no not like that",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id="user_1",
        )
        assert result.score == 0.0

    def test_cross_channel_no_boost(self) -> None:
        """Tool turn recorded in chan_a; message arrives in chan_b from
        the same user. No boost — the signal is channel-scoped."""
        bd.record_tool_turn("chan_a", "user_1")

        result = bd._score_message(
            text="no not like that",
            channel_id="chan_b",
            is_reply_to_bot=False,
            sender_id="user_1",
        )
        assert result.score == 0.0

    def test_sender_id_none_no_boost(self) -> None:
        """Backward-compat path: callers that omit sender_id (old tests,
        ad-hoc scoring calls) don't crash and don't get the boost."""
        bd.record_tool_turn("chan_a", "user_1")

        result = bd._score_message(
            text="no not like that",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id=None,
        )
        assert result.score == 0.0

    def test_boost_stacks_with_other_signals(self) -> None:
        """Follow-up boost is additive with other scoring signals.
        A reply-to-bot message from the same user after a tool turn
        gets both deltas applied."""
        bd.record_tool_turn("chan_a", "user_1")

        result = bd._score_message(
            text="nope, try again",
            channel_id="chan_a",
            is_reply_to_bot=True,
            sender_id="user_1",
        )
        expected = bd._SCORE_REPLY_TO_BOT + bd._SCORE_FOLLOW_UP
        assert result.score == pytest.approx(expected)

    def test_noise_only_short_message_still_rejected(self) -> None:
        """The early noise-rejection gate runs before the follow-up boost
        is applied, so a 1-word 'lol' does not become engage-worthy just
        because a tool turn happened. Follow-up is a boost on ambiguous
        messages, not a bypass of the noise floor."""
        bd.record_tool_turn("chan_a", "user_1")

        result = bd._score_message(
            text="lol",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id="user_1",
        )
        assert result.score == 0.0

    def test_exact_window_boundary(self) -> None:
        """At exactly FOLLOW_UP_WINDOW_SECONDS in the past, the entry
        is stale (strict <, not <=). This matches the recency logic
        in the rest of the scorer."""
        boundary_ts = time.time() - bd.FOLLOW_UP_WINDOW_SECONDS - 0.1
        bd._channel_last_tool_turn["chan_a"] = ("user_1", boundary_ts)

        result = bd._score_message(
            text="no not like that",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id="user_1",
        )
        assert result.score == 0.0

    def test_just_inside_window_boost_applied(self) -> None:
        """At FOLLOW_UP_WINDOW_SECONDS - 1 in the past, the boost applies."""
        recent_ts = time.time() - (bd.FOLLOW_UP_WINDOW_SECONDS - 1)
        bd._channel_last_tool_turn["chan_a"] = ("user_1", recent_ts)

        result = bd._score_message(
            text="no not like that",
            channel_id="chan_a",
            is_reply_to_bot=False,
            sender_id="user_1",
        )
        assert result.score == pytest.approx(bd._SCORE_FOLLOW_UP)


class TestFollowUpConfig:
    """Verify that the follow-up constants loaded correctly from yaml."""

    def test_score_delta_loaded_from_yaml(self) -> None:
        """_SCORE_FOLLOW_UP should match the yaml value. Default is 0.3."""
        assert bd._SCORE_FOLLOW_UP == pytest.approx(0.3)

    def test_window_seconds_loaded_from_yaml(self) -> None:
        """FOLLOW_UP_WINDOW_SECONDS should match the yaml value."""
        assert bd.FOLLOW_UP_WINDOW_SECONDS == 120

    def test_score_delta_is_positive(self) -> None:
        """Sanity: the delta must be positive for the boost to make sense."""
        assert bd._SCORE_FOLLOW_UP > 0

    def test_window_is_reasonable(self) -> None:
        """Sanity: window should be on the order of conversational
        iteration time — at least 30s (faster than typing) and at most
        10 minutes (slower than forgetting context)."""
        assert 30 <= bd.FOLLOW_UP_WINDOW_SECONDS <= 600
