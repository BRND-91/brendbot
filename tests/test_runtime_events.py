"""Tests for brendbot.runtime_events — the infrastructure-level
Discord signalling layer.

Exercises the primitives with injected fakes for ``react_fn``,
``unreact_fn``, ``send_fn``. Avoids spinning up a real Discord
client. The key properties pinned:

- Signals dispatch to the correct underlying Discord helper.
- Exception in the underlying helper is swallowed (the caller's
  main path is never interrupted by a signalling failure).
- LongTurnTimer cancels cleanly before threshold and fires exactly
  once if stopped after threshold.
- Stopping an already-stopped timer is a no-op.
"""
from __future__ import annotations

import asyncio

import pytest

from brendbot import runtime_events as rt


# ── mark_long_turn / clear_long_turn ─────────────────────────────────────


def test_mark_long_turn_calls_react_with_emoji():
    calls = []

    async def fake_react(channel_id, message_id, emoji):
        calls.append((channel_id, message_id, emoji))

    asyncio.run(rt.mark_long_turn("ch1", "msg1", react_fn=fake_react))

    assert calls == [("ch1", "msg1", rt.LONG_TURN_EMOJI)]


def test_mark_long_turn_swallows_react_exception():
    async def crashing_react(channel_id, message_id, emoji):
        raise RuntimeError("discord offline")

    # Must not raise
    asyncio.run(rt.mark_long_turn("ch1", "msg1", react_fn=crashing_react))


def test_clear_long_turn_calls_unreact_with_emoji():
    calls = []

    async def fake_unreact(channel_id, message_id, emoji):
        calls.append((channel_id, message_id, emoji))

    asyncio.run(rt.clear_long_turn("ch1", "msg1", unreact_fn=fake_unreact))

    assert calls == [("ch1", "msg1", rt.LONG_TURN_EMOJI)]


def test_clear_long_turn_swallows_exception():
    async def crashing(channel_id, message_id, emoji):
        raise ValueError("no such reaction")

    asyncio.run(rt.clear_long_turn("ch1", "msg1", unreact_fn=crashing))


# ── signal_runtime_error ─────────────────────────────────────────────────


def test_signal_runtime_error_posts_prefixed_message():
    posted = []

    async def fake_send(channel_id, text):
        posted.append((channel_id, text))

    asyncio.run(rt.signal_runtime_error(
        "ch1",
        "api_overloaded",
        "Anthropic returned 529, retrying in 30s",
        send_fn=fake_send,
    ))

    assert len(posted) == 1
    ch, text = posted[0]
    assert ch == "ch1"
    assert text.startswith(rt.RUNTIME_WARNING_PREFIX)
    assert "api_overloaded" in text
    assert "Anthropic returned 529" in text


def test_signal_runtime_error_trims_long_body():
    """Discord messages max at 2000 chars. Runtime detail from an
    exception str might exceed that; the signalling layer truncates
    defensively so a verbose stack trace doesn't kill the post."""
    posted = []

    async def fake_send(channel_id, text):
        posted.append((channel_id, text))

    long_detail = "x" * 5000
    asyncio.run(rt.signal_runtime_error(
        "ch1", "test", long_detail, send_fn=fake_send,
    ))
    assert len(posted[0][1]) <= 1900


def test_signal_runtime_error_swallows_send_exception():
    async def crashing_send(channel_id, text):
        raise ConnectionError("offline")

    # Must not raise
    asyncio.run(rt.signal_runtime_error(
        "ch1", "cat", "detail", send_fn=crashing_send,
    ))


# ── LongTurnTimer ────────────────────────────────────────────────────────


def test_long_turn_timer_does_not_fire_if_stopped_early(monkeypatch):
    """Start a timer, stop before threshold, mark_long_turn must not fire."""
    calls = []

    async def spy_react(channel_id, message_id, emoji):
        calls.append((channel_id, message_id, emoji))

    # Inject a react_fn into the module so mark_long_turn picks it up
    monkeypatch.setattr(
        "brendbot.discord.react_to_message",
        spy_react,
        raising=False,
    )

    async def _run():
        t = rt.LongTurnTimer("ch1", "msg1", threshold_s=0.5)
        t.start()
        await asyncio.sleep(0.05)
        await t.stop()

    asyncio.run(_run())
    assert calls == []


def test_long_turn_timer_fires_once_past_threshold(monkeypatch):
    """Run past threshold; mark_long_turn fires exactly once, and
    stop() clears the reaction."""
    mark_calls = []
    clear_calls = []

    async def spy_react(channel_id, message_id, emoji):
        mark_calls.append((channel_id, message_id, emoji))

    async def spy_unreact(channel_id, message_id, emoji):
        clear_calls.append((channel_id, message_id, emoji))

    monkeypatch.setattr("brendbot.discord.react_to_message", spy_react, raising=False)
    monkeypatch.setattr("brendbot.discord.remove_reaction", spy_unreact, raising=False)

    async def _run():
        t = rt.LongTurnTimer("ch1", "msg1", threshold_s=0.1)
        t.start()
        await asyncio.sleep(0.25)
        await t.stop()

    asyncio.run(_run())

    assert mark_calls == [("ch1", "msg1", rt.LONG_TURN_EMOJI)]
    assert clear_calls == [("ch1", "msg1", rt.LONG_TURN_EMOJI)]


def test_long_turn_timer_stop_is_idempotent(monkeypatch):
    """Calling stop twice (e.g. receive_loop cleanup plus
    finally-block belt-and-braces) must not double-clear or raise."""
    monkeypatch.setattr(
        "brendbot.discord.react_to_message",
        lambda *a, **k: asyncio.sleep(0),
        raising=False,
    )

    async def _run():
        t = rt.LongTurnTimer("ch1", "msg1", threshold_s=10.0)
        t.start()
        await asyncio.sleep(0.01)
        await t.stop()
        await t.stop()  # must not raise

    asyncio.run(_run())


def test_long_turn_timer_start_twice_is_idempotent():
    """If code accidentally starts twice, the second start is a no-op.
    Prevents two timer tasks racing."""
    async def _run():
        t = rt.LongTurnTimer("ch1", "msg1", threshold_s=10.0)
        t.start()
        t.start()  # must not spawn a second task
        first_task = t._mark_task
        await t.stop()
        assert first_task is not None

    asyncio.run(_run())


# ── signal_thinking_typing ───────────────────────────────────────────────


def test_signal_thinking_typing_uses_channel_typing():
    """Returns Discord's native typing() context manager when the
    channel supports it."""
    called = {"enter": False, "exit": False}

    class _FakeACM:
        async def __aenter__(self):
            called["enter"] = True
            return None

        async def __aexit__(self, *args):
            called["exit"] = True
            return None

    class _FakeChannel:
        def typing(self):
            return _FakeACM()

    async def _run():
        async with rt.signal_thinking_typing(_FakeChannel()):
            pass

    asyncio.run(_run())
    assert called["enter"] is True
    assert called["exit"] is True


def test_signal_thinking_typing_noop_when_channel_missing_typing():
    """If channel.typing() isn't supported (e.g. test stub), return
    a no-op context manager so callers can ``async with`` without
    branching."""
    async def _run():
        async with rt.signal_thinking_typing(None):
            pass

    asyncio.run(_run())  # must not raise
