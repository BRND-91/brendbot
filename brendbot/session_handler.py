"""SDK message-handling dispatch for ``Session``.

Previously colocated as ``Session._handle`` (plus ``_fire_on_text`` and
``_fire_on_text_streamed``) in ``session.py``; extracted as part of
the Stage 6 repo cleanup. The public entry points are module-level
functions that take a ``Session`` as their first argument:

* :func:`handle_message` — dispatch for ``AssistantMessage`` and
  ``ResultMessage`` from the SDK receive loop.
* :func:`fire_on_text` — send a complete (non-streamed) response and
  record feedback.
* :func:`fire_on_text_streamed` — finalize a streamed response (skip
  initial send, do final edit) and record feedback.

``Session._handle`` / ``Session._fire_on_text`` /
``Session._fire_on_text_streamed`` remain as thin delegates so the
~dozen test call-sites that invoke them directly (phantom
discriminator, load score, cache metrics) continue working unchanged.

Constants referenced here (``_CONTEXT_REFRESH_THRESHOLD``, the
``_LOAD_WEIGHT_*`` family, etc.) live in
``brendbot.session_constants`` as of Stage 7 and are imported
directly at module top.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from brendbot.session_constants import (
    _CONTEXT_REFRESH_THRESHOLD,
    _CONTEXT_SOFT_WARNING,
    _LOAD_BUDGET_PREEMPTIVE,
    _LOAD_BUDGET_SHALLOW,
    _LOAD_WEIGHT_BASH_CALL,
    _LOAD_WEIGHT_HAIKU_INVOCATION,
    _LOAD_WEIGHT_TOKENS_PER_K,
    _LOAD_WEIGHT_TOOL_OTHER,
)

if TYPE_CHECKING:
    from brendbot.session import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AssistantMessage / ResultMessage dispatch
# ---------------------------------------------------------------------------


def handle_message(session: "Session", message: Any) -> None:
    """Route one SDK message to the correct per-type handler.

    Synchronous — called directly from ``Session._receive_loop``.
    Sub-dispatch (fire_on_text tasks, shallow-rest trigger, reaction
    cleanup) is scheduled via ``asyncio.create_task`` as needed.
    """
    if isinstance(message, AssistantMessage):
        _handle_assistant_message(session, message)
    elif isinstance(message, ResultMessage):
        _handle_result_message(session, message)


def _handle_assistant_message(session: "Session", message: AssistantMessage) -> None:
    # Discriminator flag: an AssistantMessage arrived this turn
    # at all. Combined with any_content_block_seen below, this
    # distinguishes true broken turns (no message) from thinking-only
    # turns where the model correctly declined to respond.
    session._turn_any_assistant_msg_seen = True
    for block in message.content:
        # Discriminator flag: any content block at all (Text,
        # Thinking, ToolUse, ToolResult). An empty content list
        # combined with an AssistantMessage still means the SDK
        # gave us nothing actionable. A thinking-only content list
        # is the canonical shape of an intentional silent drop.
        session._turn_any_content_block_seen = True
        if isinstance(block, ThinkingBlock):
            _handle_thinking_block(session, block)
        elif isinstance(block, TextBlock):
            _handle_text_block(session, block)
        elif isinstance(block, ToolUseBlock):
            _handle_tool_use_block(session, block)


def _handle_thinking_block(session: "Session", block: ThinkingBlock) -> None:
    try:
        import datetime
        thoughts_path = Path(session.cwd) / "thoughts.log"
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with thoughts_path.open("a") as f:
            f.write(f"\n--- [{ts}] turn {session._completed_turn_count} ---\n")
            f.write(block.thinking)
            f.write("\n")
    except Exception as exc:
        logger.warning("[%s] thoughts.log write failed: %s", session.key, exc)


def _handle_text_block(session: "Session", block: TextBlock) -> None:
    logger.info("[%s] %s", session.key, block.text[:200])
    session.log_turn("assistant", block.text)
    if not block.text.strip():
        return
    # Phase 2a — stamp first-token time on the first
    # non-empty TextBlock this turn. Subsequent blocks
    # don't re-stamp. Non-empty guard prevents empty
    # SDK-drift blocks from registering as first token.
    if session._turn_t_first_token is None:
        session._turn_t_first_token = time.monotonic()
    session._turn_text_buffer.append(block.text)
    # Stream text to Discord as it arrives (text-only
    # turns only). Once a tool fires, streaming stops
    # and the final-segment-only dispatch in
    # ResultMessage takes over.
    if (not session._turn_tool_called
            and not session._turn_used_send_discord
            and session._on_text_edit):
        if not session._stream_initiated:
            # First streaming TextBlock this turn — set
            # the synchronous flag BEFORE creating the
            # async task so ResultMessage handler can't
            # race past it.
            session._stream_initiated = True
            session._stream_first_chunk_done = asyncio.Event()
        asyncio.create_task(session._stream_chunk(block.text))


def _handle_tool_use_block(session: "Session", block: ToolUseBlock) -> None:
    logger.info("[%s] tool: %s", session.key, block.name)
    session._turn_tool_called = True  # mark that tool use occurred this turn
    if block.name == "Bash":
        session._turn_bash_calls += 1
        tool_cmd = (block.input or {}).get("command", "")
        if "send-discord" in tool_cmd:
            session._turn_used_send_discord = True
        if "kb-query" in tool_cmd:
            session._turn_kb_query_used = True
            import re as _re
            _mod_match = _re.search(
                r'kb-query\s+(?:defs|facts|thms|topics|xlinks)\s+(\w+)',
                tool_cmd,
            )
            if _mod_match:
                session._turn_modules_queried.add(_mod_match.group(1).upper())
    elif block.name == "CronCreate":
        # Persist cron definition so it survives session restarts.
        session._persist_cron(block.input or {})
    elif block.name == "CronDelete":
        # Remove persisted cron entry.
        session._remove_cron(block.input or {})
    else:
        # Non-Bash tool use (Read, Write, Edit, Grep, Glob,
        # WebSearch, WebFetch, Task, NotebookEdit). Counted
        # at lower weight than Bash for cognitive load —
        # they're typically faster, narrower, less stateful.
        session._turn_other_tool_calls += 1


def _handle_result_message(session: "Session", message: ResultMessage) -> None:
    # ── Phase 1: cache-metric stash ──────────────────────────
    # Read usage up-front so the _turn_*_tokens fields are set
    # before _fire_on_text / _fire_on_text_streamed get scheduled
    # below. asyncio.create_task doesn't run the task immediately
    # (the receive loop must yield first), so this is belt-and-
    # braces — the values would be set in time anyway, but pulling
    # them here makes the ordering explicit rather than relying
    # on scheduler internals. The same `usage` dict is reused for
    # context / load tracking further down.
    _stash_cache_metrics(session, message)

    # Phase 3 #1B — housekeeping turns (e.g. shallow rest injection)
    # suppress both the normal text dispatch and the silent-drop
    # fallback. The model is told not to respond and we don't want
    # to leak a Discord message either way. Token usage is still
    # tracked normally below — the inject did consume tokens.
    # Flag is one-shot.
    is_housekeeping = session._next_turn_is_housekeeping
    if is_housekeeping:
        session._next_turn_is_housekeeping = False
        # Clear the text buffer so neither dispatch branch fires.
        session._turn_text_buffer.clear()
        logger.debug("[%s] housekeeping turn — suppressing dispatch", session.key)

    # Read stop_reason defensively — the Python SDK docs claim
    # this field is TypeScript-only but the dataclass actually
    # defines it as `stop_reason: str | None = None`. Using getattr
    # tolerates SDK versions where the field is missing entirely.
    # Values we care about: 'end_turn' (normal completion, model
    # chose to stop), 'refusal' (model declined), 'max_tokens'
    # (output cap hit), 'tool_use' (pause for tool call, should
    # not reach this branch). None means the SDK either didn't
    # populate it or the turn ended abnormally.
    stop_reason = getattr(message, "stop_reason", None)

    _dispatch_turn_output(session, is_housekeeping, stop_reason)

    # Follow-up signal: if this turn used tools and we know which
    # user triggered it, record (channel_id, user_id, now) so that
    # a follow-up message from the same user within the window
    # gets a score boost at ingest time. Skipped for housekeeping,
    # restart, and any turn where sender_id is unknown.
    if (
        session._turn_tool_called
        and session._turn_sender_id is not None
        and session._chat_id is not None
    ):
        try:
            from brendbot import discord as _bd
            _bd.record_tool_turn(session._chat_id, session._turn_sender_id)
        except Exception as exc:
            logger.debug(
                "[%s] record_tool_turn failed: %s", session.key, exc
            )

    _reset_per_turn_state(session)

    cost = f" (${message.total_cost_usd:.4f})" if message.total_cost_usd else ""
    _update_context_tracking(session, message)
    current_load = _update_load_score(session)

    logger.info(
        "[%s] turn complete%s (context_tokens=%d, load=%.1f)",
        session.key, cost, session._last_input_tokens, current_load,
    )
    _write_context_status(session)
    _clear_reactions(session)

    if session._turn_log:
        last = session._turn_log[-1]
        if last.get("role") == "assistant":
            if session._turn_modules_queried:
                last["kb_modules"] = sorted(session._turn_modules_queried)
            last["kb_grounded"] = session._turn_kb_query_used


def _stash_cache_metrics(session: "Session", message: ResultMessage) -> None:
    _usage_for_cache = message.usage or {}
    if isinstance(_usage_for_cache, dict) and _usage_for_cache:
        session._turn_input_tokens = int(
            _usage_for_cache.get("input_tokens", 0) or 0
        )
        session._turn_cache_read_tokens = int(
            _usage_for_cache.get("cache_read_input_tokens", 0) or 0
        )
        session._turn_cache_creation_tokens = int(
            _usage_for_cache.get("cache_creation_input_tokens", 0) or 0
        )
    else:
        # No usage dict this turn — leave fields as None so
        # log_bot_response omits the cache block entirely.
        session._turn_input_tokens = None
        session._turn_cache_read_tokens = None
        session._turn_cache_creation_tokens = None


def _dispatch_turn_output(
    session: "Session",
    is_housekeeping: bool,
    stop_reason: str | None,
) -> None:
    if (session._turn_text_buffer
            and not session._turn_used_send_discord
            and session._on_text
            and session._chat_id):
        if session._turn_tool_called:
            # Tools were called this turn — everything before the last
            # TextBlock was mid-turn narration. Only the final segment
            # is the intended response. Streaming was disabled when
            # tools fired, so send fresh.  If streaming had already
            # started before the first tool, the orphaned message
            # gets cleaned up in _stream_reset_with_cleanup.
            asyncio.create_task(session._stream_reset_with_cleanup())
            text_to_send = session._turn_text_buffer[-1]
            asyncio.create_task(session._fire_on_text(text_to_send))
        elif session._stream_initiated:
            # Streaming was initiated this turn. The message may or
            # may not be on Discord yet (first-chunk send is async).
            # _fire_on_text_streamed awaits _stream_first_chunk_done
            # to close the race window, then does a final edit.
            text_to_send = "\n".join(session._turn_text_buffer)
            asyncio.create_task(
                session._fire_on_text_streamed(text_to_send)
            )
        else:
            # Text-only turn but streaming didn't activate (no
            # on_text_edit callback, or first chunk failed). Fall
            # back to the original single-send path.
            text_to_send = "\n".join(session._turn_text_buffer)
            asyncio.create_task(session._fire_on_text(text_to_send))
    elif (not session._turn_used_send_discord
          and not session._turn_tool_called
          and not is_housekeeping
          and session._on_text
          and session._chat_id):
        # ── Phantom-turn discriminator (Phase 3 phantom-fix-v2) ──
        # Three cases to distinguish, previously all collapsed into
        # "fire fallback":
        #
        # Case A: Intentional silent drop. The model received a
        #   message, ran thinking tokens, and correctly decided
        #   not to respond per the soul's silent-drop rule (e.g.
        #   cross-talk addressed to another user). Shape:
        #     - AssistantMessage arrived (any_assistant_msg_seen=True)
        #     - ContentBlock(s) inside it (any_content_block_seen=True)
        #       — typically ThinkingBlock only, no TextBlock
        #     - stop_reason == 'end_turn' (clean completion)
        #   Correct handling: SUPPRESS fallback. The silence is
        #   the response.
        #
        # Case B: True broken turn. Something structurally failed —
        #   no AssistantMessage at all, or an empty content list
        #   with no blocks, or an error stop_reason. The user is
        #   waiting and nothing's coming. Shape:
        #     - any_assistant_msg_seen=False OR
        #       any_content_block_seen=False
        #     - stop_reason None or indicates error
        #   Correct handling: FIRE fallback so user knows it broke.
        #
        # Case C: Housekeeping turn (context injection, shallow rest).
        #   Handled above by is_housekeeping check — already suppressed.
        #
        # Decision rule: if the model got as far as emitting an
        # AssistantMessage with at least one content block AND the
        # stop_reason is 'end_turn', treat it as Case A and suppress.
        # Anything else falls through to the fallback path.
        is_intentional_silent_drop = (
            session._turn_any_assistant_msg_seen
            and session._turn_any_content_block_seen
            and stop_reason == "end_turn"
        )
        if is_intentional_silent_drop:
            logger.info(
                "[%s] intentional silent drop — model declined to respond "
                "(thinking-only, stop_reason=end_turn) — suppressing fallback",
                session.key,
            )
        else:
            logger.warning(
                "[%s] phantom turn — no output blocks "
                "(assistant_msg=%s, content_block=%s, stop_reason=%s) — "
                "sending fallback",
                session.key,
                session._turn_any_assistant_msg_seen,
                session._turn_any_content_block_seen,
                stop_reason,
            )
            fallback_text = (
                "(no response generated — try rephrasing or asking again)"
            )
            asyncio.create_task(session._fire_on_text(fallback_text))


def _reset_per_turn_state(session: "Session") -> None:
    session._turn_text_buffer.clear()
    session._turn_used_send_discord = False
    session._turn_tool_called = False
    session._turn_sender_id = None
    session._turn_bypass_pending = False
    # Reset phantom-turn discriminator flags for the next turn.
    session._turn_any_assistant_msg_seen = False
    session._turn_any_content_block_seen = False


def _update_context_tracking(session: "Session", message: ResultMessage) -> None:
    usage = message.usage or {}
    if not (isinstance(usage, dict) and usage):
        return
    input_tokens = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
    total_context = input_tokens + cache_read + cache_creation
    if not total_context:
        return
    session._last_input_tokens = int(total_context)
    if session._last_input_tokens >= _CONTEXT_REFRESH_THRESHOLD:
        if session._context_state == "normal":
            session._context_state = "threshold_hit"
        logger.warning(
            "[%s] context at %d tokens (>=%d threshold) — clean restart queued",
            session.key,
            session._last_input_tokens,
            _CONTEXT_REFRESH_THRESHOLD,
        )
    elif (session._last_input_tokens >= _CONTEXT_SOFT_WARNING
          and not session._soft_warning_sent):
        session._soft_warning_sent = True
        # Promote soft warning to threshold_hit so the receiver
        # loop fires _trigger_clean_restart() at end of this
        # turn — preempts the 400k mid-turn ambush by
        # restarting at ~320k while we still have headroom.
        if session._context_state == "normal":
            session._context_state = "threshold_hit"
        logger.info(
            "[%s] context at %d tokens — soft warning, preemptive restart queued",
            session.key, session._last_input_tokens,
        )


def _update_load_score(session: "Session") -> float:
    """Roll per-turn counters into cumulative load, trip budgets.

    Returns the current load score for logging.
    """
    # ── Cognitive load update (Phase 3 #1A) ───────────────────
    # Roll per-turn counters into cumulative load. Compute current
    # load score as weighted sum and trip preemptive restart if it
    # exceeds budget — independent of token count. Catches the
    # heavy-tool-use turns that don't spike tokens but do degrade
    # the worker's effective capacity.
    session._cumulative_bash_calls += session._turn_bash_calls
    session._cumulative_other_tools += session._turn_other_tool_calls
    current_load = (
        (session._last_input_tokens / 1000.0) * _LOAD_WEIGHT_TOKENS_PER_K
        + session._cumulative_bash_calls * _LOAD_WEIGHT_BASH_CALL
        + session._cumulative_haiku_invocations * _LOAD_WEIGHT_HAIKU_INVOCATION
        + session._cumulative_other_tools * _LOAD_WEIGHT_TOOL_OTHER
    )
    session._cumulative_load = current_load
    if (current_load >= _LOAD_BUDGET_PREEMPTIVE
            and session._context_state == "normal"):
        session._context_state = "threshold_hit"
        logger.info(
            "[%s] load score %.1f >= budget %.1f — preemptive restart queued "
            "(bash=%d, haiku=%d, other=%d, tokens=%d)",
            session.key, current_load, _LOAD_BUDGET_PREEMPTIVE,
            session._cumulative_bash_calls, session._cumulative_haiku_invocations,
            session._cumulative_other_tools, session._last_input_tokens,
        )
    elif (current_load >= _LOAD_BUDGET_SHALLOW
            and not session._shallow_rested
            and session._context_state == "normal"):
        # Phase 3 #1B — fire shallow rest. Scheduled as a task so
        # it runs after the current ResultMessage finishes processing
        # and doesn't block the receive loop.
        logger.info(
            "[%s] load score %.1f >= shallow budget %.1f — rest cycle queued",
            session.key, current_load, _LOAD_BUDGET_SHALLOW,
        )
        asyncio.create_task(session._trigger_shallow_rest())
    elif (session._shallow_rested
            and current_load < _LOAD_BUDGET_SHALLOW * 0.7):
        # Recovery: load dropped well below the shallow trigger.
        # Clear the rested flag so a future spike can re-fire.
        session._shallow_rested = False
    return current_load


def _write_context_status(session: "Session") -> None:
    try:
        import pathlib
        status_path = pathlib.Path(session.cwd) / "context_status.txt"
        pct = round(session._last_input_tokens / 1_000_000 * 100, 1)
        status_path.write_text(f"{session._last_input_tokens} {pct}\n")
    except Exception:
        pass


def _clear_reactions(session: "Session") -> None:
    if session._react_msg_id and session._active_reactions:
        reactions_to_clear = set(session._active_reactions)
        unreact_fn = session._unreact_fn
        channel = session._react_channel
        msg_id = session._react_msg_id

        async def _do_clear(
            reactions=reactions_to_clear,
            fn=unreact_fn,
            ch=channel,
            mid=msg_id,
        ):
            for emoji in reactions:
                if fn and ch and mid:
                    try:
                        await fn(ch, mid, emoji)
                    except Exception as e:
                        logger.debug("Unreaction cleanup error %s: %s", emoji, e)

        asyncio.create_task(_do_clear())
    session._react_phase = 0
    session._react_msg_id = ""
    session._active_reactions = set()
    session._module_emote_count = 0


# ---------------------------------------------------------------------------
# Send / edit + feedback-log dispatch
# ---------------------------------------------------------------------------


async def fire_on_text(session: "Session", text: str) -> None:
    """Send a turn's response to Discord with feedback-log side effects.

    Order: extract leading branch tag → strip from chat-bound text →
    call on_text(chat_id, stripped_text) which returns the posted
    message ID → log to bot_responses.jsonl → if there was a tag,
    also log to branch_audit.jsonl.

    Holds session._turn_lock for the entire send + log sequence so the
    next query() in _run_loop cannot dispatch turn N+1 until turn N's
    feedback writes have completed. This is the actual serialization
    the duplicate `turn complete` race fix requires — wrapping
    _client.query() alone in _run_loop is insufficient because
    _fire_on_text is fire-and-forget from _receive_loop.

    All log writes are best-effort; failures never break the chat path.
    """
    from brendbot.feedback import extract_branch_tag

    text = _prepare_send_text(session, text)
    async with session._turn_lock:
        branch_tag, stripped = extract_branch_tag(text)
        try:
            bot_message_id = await session._on_text(session._chat_id, stripped)
        except Exception as exc:
            logger.error("[%s] on_text callback failed: %s", session.key, exc)
            return
        if not bot_message_id:
            # Send failed or pre-ready dispatch — nothing to correlate against.
            return
        _post_send_bookkeeping(session, bot_message_id, branch_tag, stripped)


async def fire_on_text_streamed(session: "Session", text: str) -> None:
    """Finalize a streamed response: do the last edit, then audit-log.

    Like fire_on_text but skips the initial send because the message
    was already posted during streaming. Uses the stored _stream_msg_id
    for audit correlation.

    Awaits _stream_first_chunk_done so that if ResultMessage arrives
    before the first-chunk Discord send completes, we don't race past
    it and accidentally fall through to fire_on_text (the pre-fix
    double-message bug).
    """
    from brendbot.feedback import extract_branch_tag

    # Wait for the first-chunk send to complete so _stream_msg_id is
    # populated. Without this, a fast ResultMessage can arrive before
    # the Discord API returns, and _stream_msg_id would be None.
    if session._stream_first_chunk_done:
        try:
            await asyncio.wait_for(session._stream_first_chunk_done.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] stream finalize: first-chunk send timed out", session.key)

    # Cancel any pending edit timer and do one final flush
    if session._stream_timer and not session._stream_timer.done():
        session._stream_timer.cancel()

    text = _prepare_send_text(session, text)

    async with session._turn_lock:
        branch_tag, stripped = extract_branch_tag(text)
        # Final edit with the complete, tag-stripped text
        if session._on_text_edit and session._stream_msg_id and session._chat_id:
            try:
                await session._on_text_edit(
                    session._chat_id,
                    session._stream_msg_id,
                    stripped[:2000],
                )
            except Exception:
                pass
        bot_message_id = session._stream_msg_id
        session._stream_reset()
        if not bot_message_id:
            # First-chunk send failed — stream never reached Discord.
            # Fall back to a fresh send so the response isn't lost.
            logger.debug(
                "[%s] stream finalize: no msg_id, falling back to fresh send",
                session.key,
            )
            try:
                bot_message_id = await session._on_text(session._chat_id, stripped[:2000])
            except Exception as exc:
                logger.error("[%s] stream fallback send failed: %s", session.key, exc)
                return
            if not bot_message_id:
                return
        _post_send_bookkeeping(session, bot_message_id, branch_tag, stripped)


def _prepare_send_text(session: "Session", text: str) -> str:
    """Apply pre-send tag injections shared by both fire paths.

    Runs the admin bypass-tag prefix (if apply_content_gate flagged
    this turn) followed by the fabrication-risk ``[uncertain]`` prefix
    from ``_maybe_prepend_uncertain``. Order is load-bearing: bypass
    wins when both conditions trip so the downstream ``extract_branch_tag``
    call sees only one leading ``[tag]`` and strips/audits it cleanly.
    """
    # Admin bypass tag injection: if apply_content_gate detected the
    # *brend* token and set _turn_bypass_pending, prepend [bypass] to
    # any response that doesn't already carry a branch tag. Consumed
    # once per turn — reset by the ResultMessage handler alongside
    # other per-turn flags.
    if session._turn_bypass_pending and not text.lstrip().startswith("["):
        text = f"[bypass] {text}"
    # Pre-send fabrication-risk injection (see _maybe_prepend_uncertain
    # docstring). Runs after the bypass tag so [bypass] wins when both
    # conditions are true.
    return session._maybe_prepend_uncertain(text)


def _post_send_bookkeeping(
    session: "Session",
    bot_message_id: str,
    branch_tag: str | None,
    stripped: str,
) -> None:
    """Engagement, bot-spoke, response-log, and branch-audit writes.

    Shared between :func:`fire_on_text` and :func:`fire_on_text_streamed`.
    All writes are best-effort; failures never break the chat path.
    """
    from brendbot.feedback import log_bot_response, log_branch_audit

    # Increment per-user engaged_count in the registry so the model's
    # compact user table reflects actual interaction history over time.
    if session._turn_sender_id:
        try:
            from brendbot.user_registry import record_engagement
            record_engagement(session._turn_sender_id)
        except Exception:
            pass
    from brendbot.discord import record_bot_spoke
    record_bot_spoke(session._chat_id)
    log_bot_response(
        channel_id=session._chat_id,
        bot_message_id=bot_message_id,
        user_message_id=session._turn_user_message_id,
        user_text=session._turn_user_text,
        score=session._turn_score,
        domains=session._turn_domains,
        address_level=session.current_address_level,
        branch_tag=branch_tag,
        modules_queried=sorted(session._turn_modules_queried),
        haiku_invoked=session._turn_haiku_invoked,
        input_tokens=session._turn_input_tokens,
        cache_read_input_tokens=session._turn_cache_read_tokens,
        cache_creation_input_tokens=session._turn_cache_creation_tokens,
        stage_timings_ms=session._compute_stage_timings_ms(),
    )
    if branch_tag:
        log_branch_audit(
            channel_id=session._chat_id,
            bot_message_id=bot_message_id,
            branch=branch_tag,
            response_text=stripped,
        )
