"""Infrastructure-level signalling to Discord.

Emits status, progress, and error information to Discord independent
of the model. The model is not the only source of state about the
bot; this module is the other source.

Why this module exists (2026-04-24 structural-honesty pass)
-----------------------------------------------------------
The 2026-04-23 pilot logs showed the user has no visibility into
several failure modes that produce user-visible silence:

- Turns that run 20+ minutes with no tool calls and no partial text
  (context stall, API retry, or long thinking chain with no
  externalized output)
- API 529 overloaded errors that cause the subprocess to silently
  retry or fail without any Discord-visible indication
- Classifier subprocess crashes that produce a gate REFUSE which
  then flows through as an unexplained bot message
- Content gate refusals that look identical to model output,
  leaving the user unable to tell "the bot declined" from "the
  infrastructure declined"

All four are cases where the bot's next turn tries to narrate what
happened and gets it wrong — because the model isn't looking at the
right source, or there isn't a source to look at. The fix is not to
train the model harder. The fix is to have the runtime itself put
status into Discord, visibly and promptly, so the user doesn't
depend on the model's post-hoc account.

Primitives
----------
- :func:`mark_long_turn` / :func:`clear_long_turn` — add/remove a
  ``🔄`` reaction on the triggering message while a turn is in
  flight past a threshold.
- :func:`signal_runtime_error` — post a ``⚠️ [runtime]`` message
  when something breaks at the infra layer (API overload,
  subprocess crash, classifier error).
- :func:`signal_thinking_typing` — wrap Discord's native
  ``channel.typing()`` context manager so "bot is typing…" appears
  while work is genuinely in progress.

All primitives are async and swallow their own exceptions. Signalling
must never block the operation it's signalling about.

These helpers are not the vehicle for normal model output. Text the
model generates still goes through the usual
``session._fire_on_text`` path so feedback logging and engagement
tracking continue to work. This module is for *infrastructure
speaking to the user on its own*, prefixed visibly so the user can
tell the source apart.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable

logger = logging.getLogger(__name__)


# Emoji / prefix conventions. Kept in one place so they can be changed
# coherently — the same ``⚠️ [runtime]`` prefix is what the soul's
# SELF-REPORT RULES section points at when telling the bot how to
# answer "what broke?" (read errors.jsonl AND look for ⚠️ [runtime]
# messages in recent channel context).

LONG_TURN_EMOJI = "🔄"
RUNTIME_WARNING_PREFIX = "⚠️ [runtime]"
GATE_PREFIX = "🚧 [gate]"

# Default long-turn threshold. Turns exceeding this without emitting
# text earn a LONG_TURN_EMOJI reaction on the triggering message.
# 30s was chosen because typical turns complete in under 10s and
# 30s+ matches the user experience of "this is taking longer than I
# expected, am I being heard?"
LONG_TURN_THRESHOLD_S = 30.0


async def mark_long_turn(
    channel_id: str,
    message_id: str,
    *,
    react_fn: Any = None,
) -> None:
    """Attach the long-turn marker reaction to a user message.

    Call at turn start after :data:`LONG_TURN_THRESHOLD_S` seconds of
    no text output — typically from a timer task the session starts
    on each turn and cancels on turn-complete. The reaction tells
    the user "yes, I heard you, I am still working" without making
    the model generate a progress narration (which would be a lie
    about the runtime if the model is actually idle).

    ``react_fn`` is injected for testability; production callers
    pass :func:`brendbot.discord.react_to_message`.
    """
    if react_fn is None:
        try:
            from brendbot.discord import react_to_message
            react_fn = react_to_message
        except Exception as exc:
            logger.debug("mark_long_turn: react_fn import failed: %s", exc)
            return
    try:
        await react_fn(channel_id, message_id, LONG_TURN_EMOJI)
    except Exception as exc:
        logger.debug("mark_long_turn: react failed: %s", exc)


async def clear_long_turn(
    channel_id: str,
    message_id: str,
    *,
    unreact_fn: Any = None,
) -> None:
    """Remove the long-turn marker. Called on turn-complete.

    Mirror of :func:`mark_long_turn`. Both sides are best-effort:
    the reaction staying on the message past turn completion is a
    visual inconvenience, not a correctness problem.
    """
    if unreact_fn is None:
        try:
            from brendbot.discord import remove_reaction
            unreact_fn = remove_reaction
        except Exception as exc:
            logger.debug("clear_long_turn: unreact_fn import failed: %s", exc)
            return
    try:
        await unreact_fn(channel_id, message_id, LONG_TURN_EMOJI)
    except Exception as exc:
        logger.debug("clear_long_turn: unreact failed: %s", exc)


async def signal_runtime_error(
    channel_id: str,
    category: str,
    detail: str,
    *,
    send_fn: Any = None,
) -> None:
    """Post an infrastructure-error message to the channel.

    Prefix is :data:`RUNTIME_WARNING_PREFIX` so the user can
    distinguish this from model output and from gate refusals.
    Category is a short machine-readable tag (e.g. ``api_overloaded``,
    ``subprocess_crash``, ``classifier_error``); detail is a short
    human-readable explanation (e.g. ``"Anthropic API returned 529,
    retrying in 30s"``).

    Use this over letting the model's next turn narrate the error
    because the model cannot reliably narrate errors it never saw —
    subprocess crashes, classifier failures, and 529 retries happen
    at layers below the model's context, so the model either lies
    about them (confabulates a cause) or omits them entirely.

    ``send_fn`` is injected for testability; production callers get
    :func:`brendbot.discord.send_message`.
    """
    if send_fn is None:
        try:
            from brendbot.discord import send_message
            send_fn = send_message
        except Exception as exc:
            logger.debug("signal_runtime_error: send_fn import failed: %s", exc)
            return
    try:
        body = f"{RUNTIME_WARNING_PREFIX} {category}: {detail}"
        # Discord caps messages at 2000 chars; infra messages should be
        # well under, but trim defensively.
        await send_fn(channel_id, body[:1900])
    except Exception as exc:
        logger.debug("signal_runtime_error: send failed: %s", exc)


class LongTurnTimer:
    """Async timer that fires :func:`mark_long_turn` after a delay.

    Usage:

    .. code-block:: python

        timer = LongTurnTimer(channel_id, message_id)
        timer.start()
        try:
            await do_the_turn()
        finally:
            await timer.stop()

    Calling ``stop`` cancels the pending mark task if it hasn't fired
    yet, and if it has, schedules a :func:`clear_long_turn` to remove
    the reaction. Idempotent: multiple stops are safe.
    """

    def __init__(
        self,
        channel_id: str,
        message_id: str,
        *,
        threshold_s: float = LONG_TURN_THRESHOLD_S,
    ) -> None:
        self.channel_id = channel_id
        self.message_id = message_id
        self.threshold_s = threshold_s
        self._mark_task: asyncio.Task | None = None
        self._marked = False

    def start(self) -> None:
        if self._mark_task is not None:
            return
        self._mark_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self.threshold_s)
            await mark_long_turn(self.channel_id, self.message_id)
            self._marked = True
        except asyncio.CancelledError:
            # Turn completed before threshold — normal case, no mark
            # was placed.
            pass
        except Exception as exc:
            logger.debug("LongTurnTimer: run failed: %s", exc)

    async def stop(self) -> None:
        """Cancel the pending mark or, if it already fired, clear the
        reaction from the triggering message.
        """
        if self._mark_task is not None and not self._mark_task.done():
            self._mark_task.cancel()
            try:
                await self._mark_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._marked:
            await clear_long_turn(self.channel_id, self.message_id)
            self._marked = False


def signal_thinking_typing(channel: Any) -> Any:
    """Return Discord's native ``channel.typing()`` async context
    manager. Thin wrapper so session code doesn't depend on discord.py
    types directly, and so a test can swap it with a no-op.

    Use as::

        async with signal_thinking_typing(channel):
            await do_the_turn()

    Discord shows "bot is typing…" for as long as the context is
    entered. Re-enters a heartbeat every 10s automatically so the
    indicator stays live on long turns.
    """
    try:
        return channel.typing()
    except Exception:
        # Fallback: a no-op async context manager, so callers can
        # unconditionally ``async with`` without branching.
        return _NoopACM()


class _NoopACM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None
