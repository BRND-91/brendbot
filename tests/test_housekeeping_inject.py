"""Tests for Phase 3 phantom-first-turn fix.

The fix makes inject() accept a housekeeping=False kwarg, queue
(text, housekeeping) tuples, and have _run_loop set _next_turn_is_housekeeping
inside _turn_lock right before query(). This pairs the flag with its
specific turn and prevents races when multiple housekeeping injects are
queued in sequence (as happens at startup: memory fragments + ref block).

These tests verify the queueing and unpacking contract without spinning
up a real SDK subprocess.
"""
import asyncio
from pathlib import Path

import pytest

from brendbot import session as session_mod


def _make_session(tmp_path: Path):
    cwd = tmp_path / "transcript"
    cwd.mkdir()
    return session_mod.Session(
        key="test:ch1",
        tier="admin",
        cwd=str(cwd),
        chat_id="ch1",
    )


def test_inject_default_queues_non_housekeeping_tuple(tmp_path):
    """Default inject() marks the turn as normal (housekeeping=False)."""
    s = _make_session(tmp_path)

    async def _run():
        await s.inject("hello world")
        item = await s._queue.get()
        assert isinstance(item, tuple)
        text, housekeeping = item
        assert text == "hello world"
        assert housekeeping is False

    asyncio.run(_run())


def test_inject_housekeeping_flag_queues_housekeeping_tuple(tmp_path):
    """inject(housekeeping=True) marks the turn as housekeeping."""
    s = _make_session(tmp_path)

    async def _run():
        await s.inject("<system-ref>context</system-ref>", housekeeping=True)
        item = await s._queue.get()
        assert isinstance(item, tuple)
        text, housekeeping = item
        assert "<system-ref>" in text
        assert housekeeping is True

    asyncio.run(_run())


def test_sequential_housekeeping_injects_preserve_order(tmp_path):
    """Three sequential housekeeping injects land in the queue in order,
    each carrying its own housekeeping=True flag. The receive loop will
    consume them one at a time, and each turn will set the flag via the
    unpacked tuple — not via a racy shared field.
    """
    s = _make_session(tmp_path)

    async def _run():
        await s.inject("fragment_1", housekeeping=True)
        await s.inject("fragment_2", housekeeping=True)
        await s.inject("ref_block", housekeeping=True)

        item1 = await s._queue.get()
        item2 = await s._queue.get()
        item3 = await s._queue.get()

        assert item1 == ("fragment_1", True)
        assert item2 == ("fragment_2", True)
        assert item3 == ("ref_block", True)

    asyncio.run(_run())


def test_mixed_housekeeping_and_normal_injects(tmp_path):
    """A real user message queued after startup housekeeping injects
    retains housekeeping=False. This is the actual startup pattern:
    memory fragments (housekeeping) → ref block (housekeeping) →
    first user message (normal).
    """
    s = _make_session(tmp_path)

    async def _run():
        await s.inject("fragment_1", housekeeping=True)
        await s.inject("ref_block", housekeeping=True)
        await s.inject("hey brend")  # default: housekeeping=False

        item1 = await s._queue.get()
        item2 = await s._queue.get()
        item3 = await s._queue.get()

        assert item1 == ("fragment_1", True)
        assert item2 == ("ref_block", True)
        assert item3 == ("hey brend", False)

    asyncio.run(_run())


def test_housekeeping_flag_not_set_at_inject_time(tmp_path):
    """The housekeeping flag is NOT set on the Session at inject time —
    only when _run_loop consumes the tuple and dispatches to query().
    This is the actual race fix: the flag must be set inside _turn_lock,
    paired with the specific query call, not before queueing.
    """
    s = _make_session(tmp_path)
    assert s._next_turn_is_housekeeping is False

    async def _run():
        await s.inject("fragment_1", housekeeping=True)
        # Flag must still be False — it's set by _run_loop, not inject.
        assert s._next_turn_is_housekeeping is False
        await s.inject("fragment_2", housekeeping=True)
        assert s._next_turn_is_housekeeping is False

    asyncio.run(_run())
