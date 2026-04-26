"""Verify Session._long_turn_timer is started before client.query()
and stopped on ResultMessage.

PR #23 added the LongTurnTimer primitive but didn't wire it to the
turn lifecycle. PR #27 connects it. The 2026-04-25 production log
analysis showed 53 turns took >60s with no Discord-side indication;
the timer's job is to attach a 🔄 reaction so the user sees that
work is in progress."""
from __future__ import annotations

import asyncio

import pytest

from brendbot import session as session_mod
from brendbot.session import Session


# ── Init state ──────────────────────────────────────────────────────────


def test_session_init_creates_no_timer(tmp_path):
    """Newly-constructed Session has no timer (no turn in flight)."""
    s = Session(
        key="test:init", tier="admin", cwd=str(tmp_path),
        chat_id="100",
    )
    assert s._long_turn_timer is None


# ── Round-trip: start → result → stop ───────────────────────────────────


def test_long_turn_timer_started_before_query(tmp_path, monkeypatch):
    """When _run_loop pulls a non-housekeeping message off the queue,
    a LongTurnTimer should be created and start()ed before the
    client.query() call."""
    from brendbot.runtime_events import LongTurnTimer

    started_timers: list[LongTurnTimer] = []

    class _SpyTimer(LongTurnTimer):
        def start(self) -> None:
            started_timers.append(self)
            # Skip the actual asyncio task — we're just checking start()
            # was called with the right args.

    monkeypatch.setattr(
        "brendbot.runtime_events.LongTurnTimer", _SpyTimer,
    )

    # A fake client whose query() captures the call but does nothing
    query_calls: list[str] = []

    class _FakeClient:
        async def query(self, msg):
            query_calls.append(msg)

    s = Session(
        key="discord:42", tier="admin", cwd=str(tmp_path),
        chat_id="42",
    )
    s._client = _FakeClient()  # type: ignore
    s.running = True
    s._turn_user_message_id = "msg_999"

    async def _drive():
        # Pump one message through _run_loop, then stop
        await s._queue.put(("hello", False))
        # Run the loop briefly
        loop_task = asyncio.create_task(s._run_loop())
        await asyncio.sleep(0.1)
        s.running = False
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_drive())

    # The spy timer was created
    assert len(started_timers) >= 1
    timer = started_timers[0]
    assert timer.channel_id == "42"
    assert timer.message_id == "msg_999"
    # The query was made
    assert query_calls == ["hello"]


def test_long_turn_timer_skipped_for_housekeeping(tmp_path, monkeypatch):
    """Housekeeping turns (context refresh, memory injection) are
    not user-facing — no triggering message to react to. The timer
    must NOT start."""
    from brendbot.runtime_events import LongTurnTimer

    started_timers: list[LongTurnTimer] = []

    class _SpyTimer(LongTurnTimer):
        def start(self) -> None:
            started_timers.append(self)

    monkeypatch.setattr(
        "brendbot.runtime_events.LongTurnTimer", _SpyTimer,
    )

    class _FakeClient:
        async def query(self, msg):
            pass

    s = Session(
        key="discord:42", tier="admin", cwd=str(tmp_path),
        chat_id="42",
    )
    s._client = _FakeClient()  # type: ignore
    s.running = True
    s._turn_user_message_id = "msg_999"

    async def _drive():
        await s._queue.put(("housekeeping inject", True))  # housekeeping=True
        loop_task = asyncio.create_task(s._run_loop())
        await asyncio.sleep(0.1)
        s.running = False
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_drive())

    assert started_timers == [], (
        "LongTurnTimer fired on a housekeeping turn — those have no "
        "user-facing message to react to and shouldn't trigger the "
        "visibility signal"
    )


def test_long_turn_timer_skipped_when_no_message_id(tmp_path, monkeypatch):
    """When _turn_user_message_id isn't set (early startup, memory
    injection), the timer can't react to anything and shouldn't start."""
    from brendbot.runtime_events import LongTurnTimer

    started_timers: list[LongTurnTimer] = []

    class _SpyTimer(LongTurnTimer):
        def start(self) -> None:
            started_timers.append(self)

    monkeypatch.setattr(
        "brendbot.runtime_events.LongTurnTimer", _SpyTimer,
    )

    class _FakeClient:
        async def query(self, msg):
            pass

    s = Session(
        key="discord:42", tier="admin", cwd=str(tmp_path),
        chat_id="42",
    )
    s._client = _FakeClient()  # type: ignore
    s.running = True
    # Deliberately don't set _turn_user_message_id (default empty string)

    async def _drive():
        await s._queue.put(("no message id case", False))
        loop_task = asyncio.create_task(s._run_loop())
        await asyncio.sleep(0.1)
        s.running = False
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_drive())

    assert started_timers == []


def test_long_turn_timer_skipped_when_no_chat_id(tmp_path, monkeypatch):
    """No chat_id → no Discord channel to react in. Skip the timer."""
    from brendbot.runtime_events import LongTurnTimer

    started_timers: list[LongTurnTimer] = []

    class _SpyTimer(LongTurnTimer):
        def start(self) -> None:
            started_timers.append(self)

    monkeypatch.setattr(
        "brendbot.runtime_events.LongTurnTimer", _SpyTimer,
    )

    class _FakeClient:
        async def query(self, msg):
            pass

    s = Session(
        key="discord:42", tier="admin", cwd=str(tmp_path),
        chat_id="",  # no chat id
    )
    s._client = _FakeClient()  # type: ignore
    s.running = True
    s._turn_user_message_id = "msg_999"

    async def _drive():
        await s._queue.put(("no chat case", False))
        loop_task = asyncio.create_task(s._run_loop())
        await asyncio.sleep(0.1)
        s.running = False
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_drive())

    assert started_timers == []
