"""Tests for the three-case phantom-turn discriminator (phase3-phantom-fix-v2).

The prior silent-drop fallback collapsed three distinct cases into one
"fire fallback" branch, which caused bad-engagement reactions on legitimate
intentional silent drops observed in the 2026-04-12 runtime log.

Case A — Intentional silent drop:
  Model received a message, ran thinking tokens, correctly declined to
  respond (cross-talk, off-topic). AssistantMessage arrived with
  ThinkingBlock only, ResultMessage.stop_reason == 'end_turn'.
  → Fallback must be SUPPRESSED.

Case B — True broken turn:
  No AssistantMessage, or empty content list, or error stop_reason.
  User is waiting, nothing came. → Fallback must FIRE.

Case C — Housekeeping inject:
  Context-only injection where the model is told not to respond.
  Handled by _next_turn_is_housekeeping flag, existing behavior.
  → Fallback SUPPRESSED via the existing housekeeping branch.

These tests drive _handle() directly with mock messages and assert on
the state changes and whether _fire_on_text would be called. Because
_handle() uses asyncio.create_task(), we substitute a test double for
on_text that records invocations synchronously.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from brendbot import session as session_mod
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)


# ── Test doubles ──────────────────────────────────────────────────────────
#
# The SDK classes are stubbed as empty-body classes in conftest (no
# __init__, no __slots__, no dataclass machinery). We construct instances
# via a thin helper that sets attributes directly — _handle() only reads
# attribute names, so duck-typing is enough.


def _text_block(text: str) -> TextBlock:
    b = TextBlock()
    b.text = text
    return b


def _thinking_block(thinking: str = "...", signature: str = "sig") -> ThinkingBlock:
    b = ThinkingBlock()
    b.thinking = thinking
    b.signature = signature
    return b


def _tool_use_block(tool_id: str, name: str, tool_input: dict) -> ToolUseBlock:
    b = ToolUseBlock()
    b.id = tool_id
    b.name = name
    b.input = tool_input
    return b


def _assistant_message(content: list) -> AssistantMessage:
    m = AssistantMessage()
    m.content = content
    m.model = "claude-sonnet"
    return m


def _result_message(
    stop_reason: Any = "end_turn",
    total_cost_usd: float = 0.001,
    usage: dict | None = None,
) -> ResultMessage:
    m = ResultMessage()
    m.subtype = "success"
    m.duration_ms = 100
    m.duration_api_ms = 90
    m.is_error = False
    m.num_turns = 1
    m.session_id = "sess_test"
    m.total_cost_usd = total_cost_usd
    m.usage = usage or {}
    m.stop_reason = stop_reason
    return m


class FireOnTextSpy:
    """Records calls to _fire_on_text without actually dispatching to
    Discord. Substitute on a Session instance before driving _handle()."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, text: str) -> None:
        self.calls.append(text)

    def reset(self) -> None:
        self.calls.clear()


def _make_session(tmp_path: Path) -> session_mod.Session:
    """Construct a Session bound to a real event loop so asyncio.create_task
    in _handle() has somewhere to schedule. The spy replaces _fire_on_text
    so no real dispatch happens."""
    cwd = tmp_path / "transcript"
    cwd.mkdir()
    s = session_mod.Session(
        key="test:ch1",
        tier="admin",
        cwd=str(cwd),
        chat_id="ch1",
    )
    # _fire_on_text checks self._on_text and self._chat_id; both need to
    # be truthy for the dispatch branches to fire at all.
    s._on_text = lambda *a, **kw: None  # presence check only
    s._chat_id = "ch1"
    return s


def _run_handle(session: session_mod.Session, message: Any) -> FireOnTextSpy:
    """Drive _handle() inside an event loop so asyncio.create_task works.
    Returns the FireOnTextSpy capturing any fallback dispatches."""
    spy = FireOnTextSpy()
    session._fire_on_text = spy  # type: ignore

    async def _drive() -> None:
        session._handle(message)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    return spy


# ── Test cases ────────────────────────────────────────────────────────────


class TestInitFlags:
    """The new discriminator flags must initialize to False."""

    def test_phantom_flags_start_false(self, tmp_path) -> None:
        s = _make_session(tmp_path)
        assert s._turn_any_assistant_msg_seen is False
        assert s._turn_any_content_block_seen is False


class TestAssistantMessageSetsFlags:
    """_handle() AssistantMessage branch must set discriminator flags."""

    def test_assistant_msg_sets_assistant_flag(self, tmp_path) -> None:
        s = _make_session(tmp_path)
        msg = _assistant_message([_text_block("hello")])
        s._handle(msg)
        assert s._turn_any_assistant_msg_seen is True

    def test_content_block_sets_content_flag(self, tmp_path) -> None:
        s = _make_session(tmp_path)
        msg = _assistant_message([_text_block("hi")])
        s._handle(msg)
        assert s._turn_any_content_block_seen is True

    def test_thinking_only_sets_content_flag(self, tmp_path) -> None:
        """ThinkingBlock counts as a content block — this is the key
        distinguishing signal for Case A (intentional silent drop)."""
        s = _make_session(tmp_path)
        msg = _assistant_message([_thinking_block("should I respond?")])
        s._handle(msg)
        assert s._turn_any_assistant_msg_seen is True
        assert s._turn_any_content_block_seen is True

    def test_empty_content_leaves_content_flag_false(self, tmp_path) -> None:
        """An AssistantMessage with no content blocks — the any_assistant_msg
        flag flips but any_content_block stays False. Distinguishes
        structural-empty from thinking-only."""
        s = _make_session(tmp_path)
        msg = _assistant_message([])
        s._handle(msg)
        assert s._turn_any_assistant_msg_seen is True
        assert s._turn_any_content_block_seen is False


class TestCaseA_IntentionalSilentDrop:
    """Thinking-only + stop_reason='end_turn' → suppress fallback."""

    def test_thinking_only_end_turn_suppresses_fallback(self, tmp_path) -> None:
        s = _make_session(tmp_path)
        # Thinking-only turn: model decided not to respond
        s._handle(_assistant_message([_thinking_block("not addressed to me")]))
        # Then ResultMessage with stop_reason='end_turn' — clean completion
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert spy.calls == [], f"Fallback fired on intentional silent drop: {spy.calls}"

    def test_flags_reset_after_turn(self, tmp_path) -> None:
        """After a ResultMessage, the per-turn flags go back to False so
        the next turn starts clean."""
        s = _make_session(tmp_path)
        s._handle(_assistant_message([_thinking_block("...")]))
        _run_handle(s, _result_message(stop_reason="end_turn"))
        assert s._turn_any_assistant_msg_seen is False
        assert s._turn_any_content_block_seen is False


class TestCaseB_TrueBrokenTurn:
    """No AssistantMessage, or empty content, or error stop_reason → fire fallback."""

    def test_no_assistant_message_fires_fallback(self, tmp_path) -> None:
        """ResultMessage arrives without any preceding AssistantMessage
        — the SDK gave us nothing. This is the original phantom-turn
        failure mode before phase3-fixes."""
        s = _make_session(tmp_path)
        spy = _run_handle(s, _result_message(stop_reason=None))
        assert len(spy.calls) == 1
        assert "no response generated" in spy.calls[0]

    def test_empty_content_message_fires_fallback(self, tmp_path) -> None:
        """AssistantMessage with content=[] — structurally empty.
        any_assistant_msg flips True but any_content_block stays False."""
        s = _make_session(tmp_path)
        s._handle(_assistant_message([]))
        # Even with end_turn, empty content means no real output
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert len(spy.calls) == 1

    def test_thinking_only_with_null_stop_reason_fires_fallback(self, tmp_path) -> None:
        """Thinking block present but stop_reason=None — abnormal termination,
        not an intentional silent drop. Fire fallback."""
        s = _make_session(tmp_path)
        s._handle(_assistant_message([_thinking_block("...")]))
        spy = _run_handle(s, _result_message(stop_reason=None))
        assert len(spy.calls) == 1

    def test_thinking_only_with_refusal_fires_fallback(self, tmp_path) -> None:
        """stop_reason='refusal' — the API safety filter blocked the response.
        This is NOT an intentional silent drop; the user should know
        something happened."""
        s = _make_session(tmp_path)
        s._handle(_assistant_message([_thinking_block("...")]))
        spy = _run_handle(s, _result_message(stop_reason="refusal"))
        assert len(spy.calls) == 1


class TestCaseC_HousekeepingSuppression:
    """Existing housekeeping behavior must still work — this fix does not
    regress the startup-phantom-turn handling from commit 0d07f22."""

    def test_housekeeping_suppresses_even_with_text(self, tmp_path) -> None:
        """When _next_turn_is_housekeeping is True, neither dispatch nor
        fallback fires, regardless of content."""
        s = _make_session(tmp_path)
        s._next_turn_is_housekeeping = True
        s._handle(_assistant_message([_text_block("some output")]))
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert spy.calls == []
        # Flag is one-shot — consumed this turn
        assert s._next_turn_is_housekeeping is False

    def test_housekeeping_suppresses_empty_turn(self, tmp_path) -> None:
        """Housekeeping + phantom-shape turn: still suppressed."""
        s = _make_session(tmp_path)
        s._next_turn_is_housekeeping = True
        spy = _run_handle(s, _result_message(stop_reason=None))
        assert spy.calls == []


class TestNormalTextResponse:
    """The existing happy path must still work — text turns dispatch."""

    def test_text_response_dispatches(self, tmp_path) -> None:
        s = _make_session(tmp_path)
        s._handle(_assistant_message([_text_block("hello world")]))
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert spy.calls == ["hello world"]

    def test_tool_using_turn_with_final_text_dispatches_last_segment(self, tmp_path) -> None:
        """Tool-using turns: only the final text segment dispatches (existing behavior)."""
        s = _make_session(tmp_path)
        s._handle(_assistant_message([
            _text_block("let me check that"),
            _tool_use_block("t1", "Bash", {"command": "echo hi"}),
        ]))
        s._handle(_assistant_message([_text_block("done, result is hi")]))
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert spy.calls == ["done, result is hi"]


class TestCaseD_TaskRequestStall:
    """2026-04-24 refinement: thinking-only + end_turn is NOT an
    intentional silent drop when the user message was a task request.

    Pre-refinement, the discriminator would suppress the fallback on
    any thinking-only end_turn turn, regardless of what the user had
    asked for. That caused the 2026-04-23 pilot bug at 22:33 where
    'two changes' got silent-dropped and the user had to re-prompt.

    Post-refinement: if ``_turn_user_text`` looks like an imperative
    (``_looks_like_task_request`` returns True), the silent-drop
    branch is disabled and a fallback fires instead."""

    def test_task_request_thinking_only_fires_fallback(self, tmp_path) -> None:
        """User said 'make me a song', bot ran thinking only, ended
        with end_turn, no output. Pre-refinement this suppressed the
        fallback. Post-refinement it fires."""
        s = _make_session(tmp_path)
        s._turn_user_text = "make me a song"
        s._handle(_assistant_message([_thinking_block("composing...")]))
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert len(spy.calls) == 1
        assert "no response generated" in spy.calls[0]

    def test_non_task_thinking_only_still_suppresses_fallback(
        self, tmp_path,
    ) -> None:
        """User said 'lmao ok' — ambient chatter, no task. Bot
        thinking-drops. The fallback stays suppressed because this is
        a legitimate Case A drop."""
        s = _make_session(tmp_path)
        s._turn_user_text = "lmao ok"
        s._handle(_assistant_message([_thinking_block("not addressed to me")]))
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert spy.calls == []

    def test_task_request_with_mention_preamble_fires_fallback(
        self, tmp_path,
    ) -> None:
        """@mention preamble plus imperative — still a task request,
        still must fire on no-output."""
        s = _make_session(tmp_path)
        s._turn_user_text = "<@1490829355214049451> generate an image"
        s._handle(_assistant_message([_thinking_block("...")]))
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        assert len(spy.calls) == 1

    def test_empty_user_text_falls_back_to_pre_refinement_behavior(
        self, tmp_path,
    ) -> None:
        """If _turn_user_text is empty (edge case: session state not
        fully populated), the is_task heuristic returns False, so the
        discriminator behaves as it did pre-refinement."""
        s = _make_session(tmp_path)
        s._turn_user_text = ""
        s._handle(_assistant_message([_thinking_block("...")]))
        spy = _run_handle(s, _result_message(stop_reason="end_turn"))
        # Pre-refinement behavior: thinking-only + end_turn + no task
        # suppresses the fallback.
        assert spy.calls == []
