"""Claude Agent SDK session manager.

Each contact/channel gets one persistent Claude session. The SDK spawns a
Claude Code subprocess — no API key needed, uses OAuth via `claude login`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape, quoteattr

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

# Patch SDK parser to handle unknown message types gracefully
try:
    import claude_agent_sdk._internal.message_parser as _mp
    import claude_agent_sdk._internal.client as _client

    _original_parse = _mp.parse_message

    def _tolerant_parse(data):
        try:
            return _original_parse(data)
        except Exception:
            return SystemMessage(subtype=data.get("type", "unknown"), data=data)

    _client.parse_message = _tolerant_parse
except (ImportError, AttributeError):
    pass

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"

CRON_FILE = "crons.json"  # per-session cron persistence file (lives in session cwd)

# ---------------------------------------------------------------------------
# Warm classifier pool — extracted to brendbot.classifier_pool in Stage 4.
#
# These names are re-imported here so:
#   1. External callers (main.py, discord.py) can keep `from brendbot.session
#      import warm_classifier_pool / haiku_classify` — no downstream diff.
#   2. The content-gate helper can call `content_gate_classify(...)` as a
#      bare name, which means `tests/test_admin_bypass.py`'s
#      `monkeypatch.setattr(session_mod, "content_gate_classify", …)`
#      contract keeps working — the patched binding in session.py's module
#      dict is the one resolved at call time.
#
# ``flagged_generate`` was in this list pre-2026-04-23; removed with the
# FLAG reroute strip.
# ---------------------------------------------------------------------------

from brendbot.classifier_pool import (
    ClassifierPool,
    acquire_classifier_client,
    content_gate_classify,
    content_gate_cross_check_floor,
    get_classifier_pool,
    haiku_classify,
    warm_classifier_pool,
)


def _load_template(name: str) -> str:
    """Load a soul template file."""
    path = PROJECT_ROOT / name
    if path.exists():
        return path.read_text()
    return "You are a helpful Discord bot.\nTo reply, run: {{ send_command }}"


def _render(template: str, variables: dict[str, str]) -> str:
    """Simple {{ var }} substitution."""
    def replacer(m: re.Match) -> str:
        return variables.get(m.group(1).strip(), m.group(0))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, template)


# ---------------------------------------------------------------------------
# Single SDK session
# ---------------------------------------------------------------------------

# Runtime constants moved to brendbot.session_constants in Stage 7. Re-
# exported here because tests (notably tests/test_load_score.py) read
# them off this module's namespace as session_mod._LOAD_WEIGHT_* etc.
# session_handler.py imports directly from session_constants now.
from brendbot.session_constants import (  # noqa: E402 — after the class-doc comment banner is intentional
    _CONTEXT_REFRESH_THRESHOLD,
    _CONTEXT_SOFT_WARNING,
    _LOAD_WEIGHT_TOKENS_PER_K,
    _LOAD_WEIGHT_BASH_CALL,
    _LOAD_WEIGHT_HAIKU_INVOCATION,
    _LOAD_WEIGHT_TOOL_OTHER,
    _MAX_TURN_LOG,
    _TOOL_CALL_BUDGET,
    _BASH_CALL_BUDGET,
    _TURN_TIME_CAP_S,
    _CHECKPOINT_INTERVAL,
)


class Session:
    """One Claude Agent SDK session for a contact or channel."""

    def __init__(
        self,
        key: str,
        tier: str,
        cwd: str,
        model: str = "sonnet",
        on_text: Optional[Any] = None,
        on_text_edit: Optional[Any] = None,
        chat_id: str = "",
        guild_id: str = "",
    ) -> None:
        self.key = key
        self.tier = tier
        self.cwd = cwd
        self._model = model
        self._on_text = on_text
        self._on_text_edit = on_text_edit
        self._chat_id = chat_id
        # Guild snowflake for multi-server user_registry filtering.
        # Stashed here so _restart_session can preserve it across restarts
        # without re-plumbing it through every caller.
        self._guild_id: str = guild_id
        self._client: Optional[ClaudeSDKClient] = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._error_count = 0
        self.running = False
        self.current_sender_tier: str | None = None
        # Address level for the current turn (low/moderate/high). Set by
        # SessionPool.route_message before each inject. Read by
        # _permission_check to enforce FUSED-CORE Budget Throttle caps.
        self.current_address_level: str = "high"
        self._last_input_tokens: int = 0
        self._context_state: str = "normal"
        self._soft_warning_sent: bool = False
        self._on_needs_restart: Optional[Any] = None
        self._react_fn: Optional[Any] = None
        self._react_channel: str = ""
        self._react_msg_id: str = ""
        self._react_phase: int = 0
        self._HOUSEKEEPING_PATH_FRAGMENTS = frozenset({
            "session.py", "discord.py", "__init__.py",
            "CLAUDE.md", "MEMORY.md", "ai-image-shortfalls.md",
            "CONTEXT_SUMMARY.md",
        })
        self._turn_text_buffer: list[str] = []
        self._turn_used_send_discord: bool = False
        self._turn_tool_called: bool = False  # True once any ToolUseBlock seen this turn
        # Phantom-turn discriminator flags (Phase 3 phantom-fix-v2).
        # The silent-drop fallback was firing on two distinct failure modes:
        # (a) intentional silent drops where the model correctly declined
        # to respond per the soul rules (case A), and (b) true broken turns
        # where no output arrived at all (case B). These two flags plus
        # ResultMessage.stop_reason are the three-way discriminator:
        #   Case A: any_assistant_msg_seen=True, content_block_seen=True,
        #           stop_reason='end_turn' → suppress fallback
        #   Case B: any_assistant_msg_seen=False OR content_block_seen=False,
        #           stop_reason=None or error → fire fallback
        #   Case C: is_housekeeping=True → suppress (existing behavior)
        self._turn_any_assistant_msg_seen: bool = False
        self._turn_any_content_block_seen: bool = False
        # Discord sender id of the user whose message triggered the current
        # turn. Set by _pool_inject just before session.inject(); consumed in
        # the ResultMessage handler if _turn_tool_called was True, to record
        # the follow-up signal state so iteration replies ("no, try again")
        # from the same user get a score boost. None means no follow-up
        # signal will be recorded for this turn (housekeeping, restart, etc).
        self._turn_sender_id: str | None = None
        # Per-turn: set by apply_content_gate when an admin bypass was
        # invoked, consumed by _fire_on_text to prepend [bypass] tag to
        # the response text before extract_branch_tag strips and logs.
        self._turn_bypass_pending: bool = False
        self._MODULE_EMOTES: dict[str, str] = {
            "BUILDSCI":    "🏗️",
            "IMAGEGEN":    "🎨",
        }
        self._NEUTRAL_EMOTE = "⬛"
        self._MAX_MODULE_EMOTES = 3
        self._unreact_fn: Optional[Any] = None
        self._active_reactions: set[str] = set()
        self._module_emote_count: int = 0
        self._turn_log: list[dict] = []
        self._tool_call_count: int = 0
        self._completed_turn_count: int = 0
        self._turn_modules_queried: set[str] = set()
        self._turn_kb_query_used: bool = False
        # ── Cognitive load tracking (Phase 3 #1A) ─────────────────────
        # Running cumulative load across turns. Reset only on full restart.
        # Updated in _handle() ResultMessage branch from per-turn counts.
        self._cumulative_load: float = 0.0
        self._cumulative_bash_calls: int = 0
        self._cumulative_haiku_invocations: int = 0
        self._cumulative_other_tools: int = 0
        # Per-turn counters reset before each inject in route_message.
        self._turn_bash_calls: int = 0
        self._turn_other_tool_calls: int = 0
        # Patch 1a — agent_core.budgets halting-problem defence. A typed
        # per-turn BudgetState enforced in _permission_check on every
        # tool call. step_cap duplicates _tool_call_count enforcement
        # (kept separately for back-compat) and time_cap_s is the new
        # wall-clock cap with no prior enforcement in the response path.
        # Reset at turn boundary in SessionPool.route_message.
        self._turn_budget: Optional[Any] = None
        # Flag for the next inject: if True, suppress both the response
        # dispatch AND the silent-drop fallback for the resulting turn.
        # Historically used by the shallow-rest cycle (deleted in the
        # 2026-04-23 strip) to inject a housekeeping turn the model was
        # told not to respond to. Retained because
        # SessionPool.route_message still has a code path that sets it
        # around context-summary refresh housekeeping.
        self._next_turn_is_housekeeping: bool = False
        # Long-turn visibility timer (PR #27). Started in _run_loop
        # immediately before client.query() for non-housekeeping
        # turns; stopped by the ResultMessage handler in
        # _receive_loop. None when no turn is in flight. The actual
        # timer object lives in brendbot.runtime_events.LongTurnTimer.
        self._long_turn_timer: Any = None
        # Phase 3 #2A — session-start timestamp, used as ts_start when an
        # episode row is written at restart time. Reset implicitly on every
        # restart because SessionPool._restart_session creates a fresh
        # Session instance via _create(), which re-runs __init__.
        from datetime import datetime as _dt
        self._session_started_at: str = _dt.now().isoformat(timespec="seconds")
        # Aggregate domains seen across this session segment, for the
        # episode write at restart time.
        self._session_domains_seen: set[str] = set()
        # Turn metadata for feedback logging — set by SessionPool.route_message
        # before each inject and read by _fire_on_text after send_message
        # returns the bot_message_id.
        self._turn_user_message_id: str = ""
        self._turn_user_text: str = ""
        self._turn_score: float | None = None
        self._turn_domains: list[str] = []
        # Serializes turn boundaries: ensures _fire_on_text + state reset
        # for turn N completes before query() for turn N+1 dispatches.
        # Fixes the duplicate `turn complete` race observed when two messages
        # land while a result is still being handled.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        # ── Streaming response state ──────────────────────────────────
        # On text-only turns (no tool calls), TextBlocks are streamed to
        # Discord via message edits as they arrive. The first chunk creates
        # a new message; subsequent chunks edit it on a 400ms timer to
        # stay under Discord's rate limit (~5 edits / 5s / channel).
        self._stream_msg_id: str | None = None   # Discord message being streamed
        self._stream_buffer: str = ""             # accumulated text so far
        self._stream_timer: Optional[asyncio.Task] = None  # pending edit task
        self._stream_dirty: bool = False          # buffer changed since last edit
        # Synchronous flag set the instant the first _stream_chunk task is
        # created — before the async Discord send completes and populates
        # _stream_msg_id. The ResultMessage handler checks this instead of
        # _stream_msg_id to decide whether streaming was initiated, closing
        # the race window where ResultMessage arrives before the first-chunk
        # send returns.
        # Per-turn haiku-invoked flag. Set by route_message before each
        # inject from the haiku_invoked parameter passed through from
        # discord.py. Read by _fire_on_text / _fire_on_text_streamed for
        # flow-class and fabrication-risk diagnostics in log_bot_response.
        self._turn_haiku_invoked: bool = False
        # Phase 1 — prompt-cache observability. The CLI surfaces these on
        # ResultMessage.usage; we stash them at result time so both
        # _fire_on_text and _fire_on_text_streamed can log per-turn cache
        # behaviour to bot_responses.jsonl. None means the SDK/API didn't
        # return a usage block for this turn (e.g. housekeeping, restart,
        # SDK error); log_bot_response omits the fields entirely in that
        # case so downstream consumers never see partial rows.
        self._turn_input_tokens: int | None = None
        self._turn_cache_read_tokens: int | None = None
        self._turn_cache_creation_tokens: int | None = None
        # Phase 2a — stage-timing instrumentation. time.monotonic() stamps
        # at four points in the turn lifecycle; _fire_on_text computes
        # deltas in ms for log_bot_response. None means the stage didn't
        # execute this turn (e.g. DMs skip the engagement gate, housekeeping
        # turns skip first-token). Reset in route_message at turn start.
        self._turn_t_received: float | None = None           # on_message entry in discord.py
        self._turn_t_engage_gate_done: float | None = None   # engagement decision complete
        self._turn_t_content_gate_done: float | None = None  # content gate classifier returned
        self._turn_t_first_token: float | None = None        # first AssistantMessage TextBlock
        # Patch A — deduplicated recall injection.
        # Tracks episode IDs (SQLite rowid from episodes table) that have
        # already been injected into this session. On subsequent turns,
        # query_episodes may return the same episode(s) — skipping them
        # prevents re-injecting identical recall fragments every message,
        # which was adding 200–600 chars of context to every turn with
        # zero marginal information gain. Reset on session restart via
        # fresh Session.__init__ in _restart_session → _create.
        self._recalled_episode_ids: set[int] = set()
        self._stream_initiated: bool = False
        # Future that resolves once the first-chunk send has completed
        # (successfully or not). _fire_on_text_streamed awaits this so it
        # doesn't finalize before _stream_msg_id is populated.
        self._stream_first_chunk_done: Optional[asyncio.Event] = None
        # Stage 4 — deferred KB index expansion. True after the full
        # FUSED-CORE MODULES + KNOWLEDGE QUERY TOOL block has been
        # injected into this session (happens on the first domain-matched
        # message). Sessions that never touch a knowledge module never
        # incur the full-index cost.
        self._kb_index_expanded: bool = False
        # Premise Check enforcement — tracks which knowledge modules have
        # had their structured content (defs/facts/thms) pre-fetched and
        # injected into this session. On every message whose domain_hint
        # contains a module NOT in this set, the module's content is
        # auto-injected as <grounded_facts> housekeeping before the user
        # message is processed. This converts the documented Premise Check
        # gate from honor-system ("the model will query kb-query") to
        # enforced-by-construction ("the module content is already in the
        # context window when the model generates the answer").
        self._grounded_modules: set[str] = set()

    async def start(self) -> None:
        opts = self._build_options()
        logger.info(
            "Creating session %s (tier=%s, model=%s, perm=%s, cwd=%s)",
            self.key, self.tier, self._model, opts.permission_mode, self.cwd,
        )
        try:
            self._client = ClaudeSDKClient(options=opts)
            await self._client.connect()
        except Exception as e:
            logger.error("Failed to start Claude session %s: %s", self.key, e)
            raise
        self.running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"session:{self.key}")
        logger.info("Session started: %s", self.key)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._kill()

    async def inject(self, text: str, housekeeping: bool = False) -> None:
        """Queue a message for the next turn.

        housekeeping=True marks this inject as a context-only injection
        that the model is told not to respond to. The result handler will
        suppress both the normal text dispatch and the silent-drop fallback
        for the resulting turn. Used for: startup memory fragments, ref
        block injection, shallow rest cycles. The flag is consumed atomically
        by _run_loop right before query(), so multiple housekeeping injects
        queued in a row don't race against each other.
        """
        await self._queue.put((text, housekeeping))

    async def apply_content_gate(
        self,
        wrapped_text: str,
        raw_user_text: str,
        tier: str,
        sender_id: str,
        message_id: str,
    ) -> str:
        """Delegate to :func:`brendbot.session_gate.apply_content_gate`.

        Kept as a Session method so existing direct callers — notably
        ``tests/test_admin_bypass.py``'s ~20 ``s.apply_content_gate(…)``
        invocations and ``SessionPool.route_message`` — do not need to
        change. See Stage 5 of CLEANUP_LOG.md for context.
        """
        from brendbot.session_gate import apply_content_gate as _apply_gate
        return await _apply_gate(
            self, wrapped_text, raw_user_text, tier, sender_id, message_id,
        )

    async def _stream_chunk(self, text: str) -> None:
        """Handle an incoming TextBlock for the streaming path.

        First chunk: send a new Discord message via _on_text to get a
        message ID. Subsequent chunks: schedule a 400ms debounced edit.
        Only active for text-only turns (no tool calls yet this turn).
        """
        self._stream_buffer += text
        self._stream_dirty = True

        if self._stream_msg_id is None and self._on_text and self._chat_id:
            # First chunk — send immediately to get message ID and show
            # the user that the bot is responding. Truncate to 2000 chars
            # (Discord limit); the final edit will handle overflow.
            try:
                msg_id = await self._on_text(self._chat_id, self._stream_buffer[:2000])
                if msg_id:
                    self._stream_msg_id = msg_id
                    self._stream_dirty = False
            except Exception as exc:
                logger.debug("[%s] stream first-chunk send failed: %s", self.key, exc)
            finally:
                # Signal that the first-chunk send has resolved (success or
                # failure). _fire_on_text_streamed awaits this event so it
                # doesn't finalize before _stream_msg_id is populated.
                if self._stream_first_chunk_done:
                    self._stream_first_chunk_done.set()
        elif self._stream_msg_id and self._on_text_edit:
            # Subsequent chunks — debounce edits at 400ms intervals
            if self._stream_timer is None or self._stream_timer.done():
                self._stream_timer = asyncio.create_task(
                    self._stream_flush_after_delay(0.4)
                )

    async def _stream_flush_after_delay(self, delay: float) -> None:
        """Wait `delay` seconds then edit the streamed message if dirty."""
        await asyncio.sleep(delay)
        await self._stream_flush()

    async def _stream_flush(self) -> None:
        """Edit the streamed Discord message with the current buffer."""
        if (self._stream_msg_id
                and self._stream_dirty
                and self._on_text_edit
                and self._chat_id):
            try:
                await self._on_text_edit(
                    self._chat_id,
                    self._stream_msg_id,
                    self._stream_buffer[:2000],
                )
                self._stream_dirty = False
            except Exception as exc:
                logger.debug("[%s] stream edit failed: %s", self.key, exc)

    def _stream_reset(self) -> None:
        """Reset streaming state between turns."""
        if self._stream_timer and not self._stream_timer.done():
            self._stream_timer.cancel()
        self._stream_msg_id = None
        self._stream_buffer = ""
        self._stream_timer = None
        self._stream_dirty = False
        self._stream_initiated = False
        self._stream_first_chunk_done = None

    async def _stream_reset_with_cleanup(self) -> None:
        """Reset streaming state, cleaning up any orphaned Discord message.

        When a tool fires after streaming has started, there's already a
        partial message on Discord. Wait for the first-chunk send to
        resolve, then edit the orphaned message to indicate a tool-augmented
        response is coming. This prevents stale partial text from lingering.
        """
        if self._stream_first_chunk_done:
            try:
                await asyncio.wait_for(self._stream_first_chunk_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.debug("[%s] stream cleanup: first-chunk send timed out", self.key)
        if self._stream_msg_id and self._on_text_edit and self._chat_id:
            try:
                await self._on_text_edit(
                    self._chat_id,
                    self._stream_msg_id,
                    "\u2026",  # ellipsis — will be replaced by the tool-turn response
                )
            except Exception:
                pass
        self._stream_reset()

    def _compute_stage_timings_ms(self) -> dict[str, float] | None:
        """Phase 2a — compute per-stage wall-time deltas in milliseconds
        from the four _turn_t_* stamps. Returns None if no stamps were
        collected (e.g. legacy caller that didn't pass recv_ts). Missing
        stamps produce None for the affected delta, which log_bot_response
        filters out before writing. t_first_token_to_complete is stamped
        here (at log time) rather than earlier so it captures the full
        stream-finalize + Discord-send latency."""
        now = time.monotonic()
        t_recv = self._turn_t_received
        t_engage = self._turn_t_engage_gate_done
        t_gate = self._turn_t_content_gate_done
        t_first = self._turn_t_first_token
        timings: dict[str, float | None] = {
            "t_receive_to_engage_gate": (
                (t_engage - t_recv) * 1000.0
                if (t_recv is not None and t_engage is not None) else None
            ),
            "t_engage_gate_to_content_gate": (
                (t_gate - t_engage) * 1000.0
                if (t_engage is not None and t_gate is not None) else None
            ),
            "t_content_gate_to_first_token": (
                (t_first - t_gate) * 1000.0
                if (t_gate is not None and t_first is not None) else None
            ),
            "t_first_token_to_complete": (
                (now - t_first) * 1000.0
                if t_first is not None else None
            ),
        }
        # Drop None entries; if every entry is None return None so callers
        # can skip the field entirely.
        result = {k: v for k, v in timings.items() if v is not None}
        return result or None

    async def _fire_on_text_streamed(self, text: str) -> None:
        """Finalize a streamed response. Extracted to session_handler in Stage 6."""
        from brendbot.session_handler import fire_on_text_streamed
        await fire_on_text_streamed(self, text)

    def _maybe_prepend_uncertain(self, text: str) -> str:
        """Pre-send fabrication-risk check. If the triadic pattern fires
        (haiku_invoked + domain match + no kb-query + no branch tag), the
        response sits on the highest-risk profile for fabrication documented
        in feedback.log_bot_response. Prepend [uncertain] so the response
        reaches Discord with the audit tag applied rather than relying on
        retrospective log review.

        The retrospective audit record in bot_responses.jsonl is still
        written downstream by log_bot_response — the derived
        fabrication_risk flag there preserves the training signal on the
        un-intervened pattern. The chat-bound text just gets the
        [uncertain] prefix that the model would have self-applied if it
        were reliably applying the three-branch classifier.

        Called from both _fire_on_text and _fire_on_text_streamed after
        the bypass-tag check and before extract_branch_tag, so if the
        injected tag makes it to extract_branch_tag it gets stripped
        from the visible text and routed to branch_audit.jsonl normally.
        """
        if text.lstrip().startswith("["):
            return text
        triadic = (
            self._turn_haiku_invoked
            and bool(self._turn_domains)
            and not self._turn_modules_queried
        )
        if triadic:
            return f"[uncertain] {text}"
        return text

    async def _fire_on_text(self, text: str) -> None:
        """Send a turn's response to Discord. Extracted to session_handler in Stage 6."""
        from brendbot.session_handler import fire_on_text
        await fire_on_text(self, text)

    def log_turn(self, role: str, text: str, max_chars: int = 800) -> None:
        self._turn_log.append({"role": role, "text": text[:max_chars]})
        if len(self._turn_log) > _MAX_TURN_LOG:
            self._turn_log = self._turn_log[-_MAX_TURN_LOG:]

    def is_alive(self) -> bool:
        return self.running and self._task is not None and not self._task.done()

    async def _kill(self) -> None:
        if not self._client:
            return
        try:
            transport = getattr(self._client, "_transport", None)
            if transport and hasattr(transport, "close"):
                close_result = transport.close()
                if asyncio.iscoroutine(close_result):
                    await close_result
        except Exception as e:
            logger.warning("Kill error for %s: %s", self.key, e)
        finally:
            self._client = None

    async def _run_loop(self) -> None:
        receiver = asyncio.create_task(self._receive_loop())
        try:
            while self.running:
                if receiver.done():
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                # Backwards compat: queue may contain raw strings (legacy
                # callers that pre-date the housekeeping tuple) or
                # (text, housekeeping) tuples. Unpack accordingly.
                if isinstance(item, tuple):
                    msg, housekeeping = item
                else:
                    msg, housekeeping = item, False
                if self._context_state == "restarting":
                    continue
                try:
                    assert self._client is not None
                    async with self._turn_lock:
                        # Set housekeeping flag inside the lock so it's
                        # paired with this specific query() call. Consumed
                        # by the result handler when the ResultMessage for
                        # this turn arrives.
                        if housekeeping:
                            self._next_turn_is_housekeeping = True

                        # Start the long-turn visibility timer for
                        # user-facing turns. After
                        # runtime_events.LONG_TURN_THRESHOLD_S (30s)
                        # without the turn completing, this attaches
                        # a 🔄 reaction to the triggering message so
                        # the user knows work is in progress and the
                        # bot hasn't simply gone silent. Stopped by
                        # the ResultMessage handler in _receive_loop.
                        # Skipped on housekeeping (no user-visible
                        # message to react to) and when chat_id /
                        # message_id aren't populated yet (early
                        # startup injection of memory blocks).
                        if (
                            not housekeeping
                            and self._chat_id
                            and self._turn_user_message_id
                        ):
                            try:
                                from brendbot.runtime_events import LongTurnTimer
                                self._long_turn_timer = LongTurnTimer(
                                    self._chat_id,
                                    self._turn_user_message_id,
                                )
                                self._long_turn_timer.start()
                            except Exception as exc:
                                logger.debug(
                                    "[%s] long-turn timer start failed: %s",
                                    self.key, exc,
                                )

                        await self._client.query(msg)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._error_count += 1
                    logger.error("Session %s query error: %s", self.key, e)
                    if self._error_count >= 3:
                        self.running = False
                        break
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        finally:
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass
            await self._kill()

    async def _receive_loop(self) -> None:
        try:
            assert self._client is not None
            async for message in self._client.receive_messages():
                try:
                    self._handle(message)
                except Exception as e:
                    logger.exception("Error handling message: %s", e)
                if isinstance(message, ResultMessage):
                    # Stop the long-turn visibility timer. If the
                    # 🔄 reaction had already been placed (turn
                    # exceeded the threshold), this also clears it.
                    # Idempotent — safe even if the timer was never
                    # started (housekeeping turns, missing chat_id).
                    if getattr(self, "_long_turn_timer", None) is not None:
                        try:
                            asyncio.create_task(self._long_turn_timer.stop())
                        except Exception as exc:
                            logger.debug(
                                "[%s] long-turn timer stop failed: %s",
                                self.key, exc,
                            )
                        self._long_turn_timer = None
                    self._error_count = 0
                    self._completed_turn_count += 1
                    if self._completed_turn_count == 1 or self._completed_turn_count % _CHECKPOINT_INTERVAL == 0:
                        self._write_checkpoint()
                    if self._context_state == "threshold_hit":
                        await self._trigger_clean_restart()
        except asyncio.CancelledError:
            pass
        except ProcessError as e:
            if getattr(e, "exit_code", None) in (137, 143):
                logger.debug("Session %s subprocess exited via signal (exit %d) — clean shutdown", self.key, e.exit_code)
            else:
                logger.error("Session %s subprocess error (exit %s): %s", self.key, getattr(e, "exit_code", "?"), e)
                # Record non-signal subprocess exits as runtime errors
                # so the bot can honestly answer "what happened" after
                # a crash. Signal exits (137/143) are clean shutdowns
                # and intentionally skipped.
                try:
                    from brendbot.obs import log_error
                    log_error(
                        session_key=self.key,
                        error_class=f"SubprocessError:exit_{getattr(e, 'exit_code', '?')}",
                        error_msg=str(e),
                        context_tokens=self._last_input_tokens,
                        recoverable=False,
                        detail={"path": "receive_loop"},
                    )
                except Exception:
                    pass
                # Surface the crash to Discord so the user sees
                # "⚠️ [runtime] subprocess_crash: ..." instead of
                # unexplained silence. Best-effort — if the channel
                # isn't reachable (DM closed, bot kicked, etc.) we
                # just log and move on.
                if self._chat_id:
                    try:
                        from brendbot.runtime_events import signal_runtime_error
                        asyncio.create_task(signal_runtime_error(
                            self._chat_id,
                            "subprocess_crash",
                            f"exit {getattr(e, 'exit_code', '?')} — session restarting",
                        ))
                    except Exception:
                        pass
            self.running = False
        except Exception as e:
            logger.error("Session %s receiver error: %s", self.key, e)
            try:
                from brendbot.obs import log_error
                log_error(
                    session_key=self.key,
                    error_class=f"ReceiverError:{type(e).__name__}",
                    error_msg=str(e),
                    context_tokens=self._last_input_tokens,
                    recoverable=False,
                    detail={"path": "receive_loop"},
                )
            except Exception:
                pass
            if self._chat_id:
                try:
                    from brendbot.runtime_events import signal_runtime_error
                    asyncio.create_task(signal_runtime_error(
                        self._chat_id,
                        "receiver_error",
                        f"{type(e).__name__}: {str(e)[:200]}",
                    ))
                except Exception:
                    pass
            self.running = False

    def _handle(self, message: Any) -> None:
        """Dispatch one SDK message. Extracted to session_handler in Stage 6."""
        from brendbot.session_handler import handle_message
        handle_message(self, message)

    def set_reaction_target(
        self,
        react_fn: Any,
        unreact_fn: Any,
        channel_id: str,
        message_id: str,
    ) -> None:
        self._react_fn = react_fn
        self._unreact_fn = unreact_fn
        self._react_channel = channel_id
        self._react_msg_id = message_id
        self._react_phase = 0
        self._active_reactions = set()
        self._module_emote_count = 0

    async def _react(self, emoji: str) -> None:
        if self._react_fn and self._react_channel and self._react_msg_id:
            try:
                await self._react_fn(self._react_channel, self._react_msg_id, emoji)
                self._active_reactions.add(emoji)
            except Exception as e:
                logger.debug("Reaction error: %s", e)

    async def _unreact(self, emoji: str) -> None:
        if self._unreact_fn and self._react_channel and self._react_msg_id:
            try:
                await self._unreact_fn(self._react_channel, self._react_msg_id, emoji)
                self._active_reactions.discard(emoji)
            except Exception as e:
                logger.debug("Unreaction error: %s", e)

    def _is_housekeeping_tool_call(self, tool_name: str, tool_input: dict) -> bool:
        if tool_name in ("Bash", "Write", "Edit", "WebSearch", "WebFetch", "NotebookEdit"):
            return False
        path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or tool_input.get("pattern", "")
            or ""
        )
        return any(frag in path for frag in self._HOUSEKEEPING_PATH_FRAGMENTS)

    async def _advance_reaction_phase(self, tool_name: str, tool_input: dict) -> None:
        if not self._react_msg_id:
            return
        if self._is_housekeeping_tool_call(tool_name, tool_input):
            return

        self._react_phase = 1

        path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or ""
        )
        module_emote = None
        for module_name, emote in self._MODULE_EMOTES.items():
            if f"knowledge/{module_name}.json" in path:
                module_emote = emote
                break

        if module_emote:
            if module_emote in self._active_reactions:
                return
            if self._NEUTRAL_EMOTE in self._active_reactions:
                return
            if self._module_emote_count < self._MAX_MODULE_EMOTES:
                self._module_emote_count += 1
                await self._react(module_emote)
            else:
                for emote in list(self._active_reactions):
                    if emote in self._MODULE_EMOTES.values():
                        await self._unreact(emote)
                await self._react(self._NEUTRAL_EMOTE)
        else:
            if "🧠" not in self._active_reactions:
                await self._react("🧠")

    def _write_checkpoint(self) -> None:
        if not self._turn_log:
            return
        try:
            summary_path = Path(self.cwd) / "CONTEXT_SUMMARY.md"
            lines = [f"# CTX {len(self._turn_log)}t @{self._last_input_tokens}tok\n"]
            for entry in self._turn_log:
                role = entry.get("role", "unknown")
                text = entry.get("text", "")
                cap = 400 if role == "user" else 200
                truncated = text[:cap] + ("…" if len(text) > cap else "")
                gate_tag = ""
                if role == "assistant" and entry.get("kb_grounded"):
                    mods = entry.get("kb_modules", [])
                    gate_tag = f"|kb:{','.join(mods)}" if mods else "|kb:?"
                lines.append(f"{role[0]}{gate_tag}:{truncated}\n")
            summary_path.write_text("".join(lines))
            logger.info("[%s] checkpoint written (%d turns, turn=%d)",
                        self.key, len(self._turn_log), self._completed_turn_count)
        except Exception as exc:
            logger.warning("[%s] checkpoint write failed: %s", self.key, exc)

    async def _trigger_clean_restart(self) -> None:
        if self._turn_log:
            self._write_checkpoint()
            logger.info("[%s] CONTEXT_SUMMARY.md written (%d turns) — triggering clean restart",
                        self.key, len(self._turn_log))

        # Phase 3 #2A — write an episode row before respawning. Best-effort:
        # any failure is logged inside write_episode and does not block the
        # restart. The episode captures the cues (channel, domains, entities,
        # bookends) that future sessions can use for retrieval matching.
        try:
            from brendbot.episodes import write_episode
            write_episode(
                channel=self._chat_id or self.key,
                ts_start=self._session_started_at,
                turn_log=list(self._turn_log),
                domains=sorted(self._session_domains_seen),
                outcome="ok",
            )
        except Exception as exc:
            logger.warning("[%s] episode write skipped: %s", self.key, exc)

        self._context_state = "restarting"
        if self._on_needs_restart:
            asyncio.create_task(self._on_needs_restart())

    # _trigger_shallow_rest was removed in the 2026-04-23 strip. The
    # shallow-rest cycle (fire when load crosses SHALLOW budget, flush
    # non-token load components, inject a no-response housekeeping
    # turn) never actually fired in any pilot — the pure token
    # threshold always reached _CONTEXT_REFRESH_THRESHOLD first. The
    # code path and its load-budget constants (_LOAD_BUDGET_SHALLOW,
    # _LOAD_BUDGET_PREEMPTIVE) were deleted.

    def _build_options(self) -> ClaudeAgentOptions:
        import os

        if self.tier == "admin":
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task", "NotebookEdit",
                "CronCreate", "CronDelete", "CronList",
            ]
            perm_mode = "acceptEdits"
            turn_limit = 200
            # Admin sessions get full reasoning depth — deep tool use,
            # complex code generation, and multi-step planning are common.
            effort = "high"
        elif self.tier == "trusted":
            tools = ["Read", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
            perm_mode = "acceptEdits"
            turn_limit = 50
            # Trusted users get balanced effort — enough depth for
            # meaningful tool use, but not burning tokens on overanalysis.
            effort = "medium"
        else:
            tools = ["Read", "Bash", "Glob", "Grep"]
            perm_mode = "acceptEdits"
            turn_limit = 30
            # Default tier: optimize for speed and cost. Limited tool
            # budget anyway, so deeper reasoning yields diminishing returns.
            effort = "low"

        env: dict[str, str] = {}

        opts = ClaudeAgentOptions(
            cwd=self.cwd,
            allowed_tools=tools,
            permission_mode=perm_mode,
            setting_sources=["project"],
            model=self._model,
            fallback_model="haiku" if self._model != "haiku" else "sonnet",
            max_turns=turn_limit,
            max_buffer_size=10 * 1024 * 1024,
            thinking={"type": "adaptive"},
            effort=effort,
            env=env,
        )

        opts.can_use_tool = self._permission_check
        opts.extra_args = {"session-id": str(uuid.uuid4())}
        return opts

    async def _permission_check(
        self, tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name in ("Write", "Edit"):
            path = tool_input.get("file_path", "")
            if "CLAUDE.md" in path:
                return PermissionResultDeny(
                    message="CLAUDE.md is managed by admin only"
                )

        # Bash-specific sub-budget — enforced before the overall tool count so
        # a cascade of git/gcloud/cat calls is cut off independently of how
        # many Read/Edit/Grep calls are in flight. _turn_bash_calls is already
        # incremented in the ToolUseBlock handler; read it here pre-increment
        # because the ToolUseBlock fires before _permission_check in the SDK
        # event order — so by the time _permission_check sees this Bash call,
        # _turn_bash_calls already reflects the current call.
        if tool_name == "Bash":
            if self._turn_bash_calls >= _BASH_CALL_BUDGET:
                logger.warning(
                    "[%s] Bash budget exceeded (%d/%d) — blocking",
                    self.key, self._turn_bash_calls, _BASH_CALL_BUDGET,
                )
                return PermissionResultDeny(
                    message=(
                        f"Bash budget exceeded ({self._turn_bash_calls}/{_BASH_CALL_BUDGET} "
                        "per turn). Consolidate remaining work into a single command "
                        "or respond with what is known so far."
                    )
                )

        self._tool_call_count += 1

        # Patch 1a — agent_core.BudgetState enforcement. step_cap is
        # parallel to the legacy _tool_call_count check below (kept for
        # back-compat with operators reading existing log lines), but
        # time_cap_s is a new halting-problem defence: a turn that stays
        # under the step budget can still run wall-clock unbounded on
        # slow I/O (flaky network retries, gcloud timeouts). The cap
        # here bounds the entire turn.
        if self._turn_budget is not None:
            from brendbot.agent_core.budgets import BudgetExceeded
            # Sync step count from the in-house counter so both views agree.
            self._turn_budget.steps = self._tool_call_count
            tcap = self._turn_budget.budget.time_cap_s
            if tcap is not None and self._turn_budget.elapsed_s > tcap:
                exc = BudgetExceeded("time_s", self._turn_budget.elapsed_s, tcap)
                logger.warning("[%s] turn budget %s", self.key, exc)
                return PermissionResultDeny(
                    message=(
                        f"Turn budget exceeded on {exc.dimension}: "
                        f"used={exc.used:.1f}s, cap={exc.cap:.0f}s. "
                        "Stop tool use and respond with what is known."
                    )
                )

        # Address-level budget cap (FUSED-CORE Budget Throttle enforcement).
        # Maps to the levels defined in engagement.yaml address_levels:
        #   high     → full budget (_TOOL_CALL_BUDGET)
        #   moderate → 3 tool calls per turn
        #   low      → 0 tool calls (text-only response)
        # The model is also told this in the <message> XML, but the model
        # follows instructions inconsistently — this is the enforcement layer.
        _ADDRESS_BUDGETS = {"high": _TOOL_CALL_BUDGET, "moderate": 3, "low": 0}
        addr_cap = _ADDRESS_BUDGETS.get(self.current_address_level, _TOOL_CALL_BUDGET)
        if self._tool_call_count > addr_cap:
            logger.info(
                "[%s] Address-level budget exceeded (%d/%d, level=%s) — blocking",
                self.key, self._tool_call_count, addr_cap, self.current_address_level,
            )
            return PermissionResultDeny(
                message=(
                    f"Address level '{self.current_address_level}' caps tool calls at "
                    f"{addr_cap} per turn ({self._tool_call_count} attempted). "
                    "Stop tool use and respond with what is known."
                )
            )

        if self._tool_call_count > _TOOL_CALL_BUDGET:
            logger.warning(
                "[%s] Tool call budget exceeded (%d/%d) — blocking",
                self.key, self._tool_call_count, _TOOL_CALL_BUDGET,
            )
            return PermissionResultDeny(
                message=(
                    f"Tool call budget exceeded ({self._tool_call_count}/{_TOOL_CALL_BUDGET} "
                    "per turn). Stop tool use and respond with what is known."
                )
            )

        effective = self.current_sender_tier or self.tier

        if effective == "admin":
            return PermissionResultAllow()

        if effective in ("default", "unknown"):
            if tool_name in ("Write", "Edit", "NotebookEdit"):
                return PermissionResultDeny(
                    message=f"{tool_name} blocked for {effective} tier"
                )
            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                if "send-discord" not in cmd:
                    return PermissionResultDeny(
                        message="Bash blocked for default tier (only send-discord allowed)"
                    )
            if tool_name == "Read":
                path = tool_input.get("file_path", "")
                sensitive = [".ssh", ".env", "credentials", "secrets", "token"]
                if any(s in path for s in sensitive):
                    return PermissionResultDeny(
                        message="Sensitive file blocked"
                    )

        if effective == "trusted":
            if tool_name in ("Write", "Edit", "NotebookEdit"):
                return PermissionResultDeny(
                    message=f"{tool_name} blocked for trusted tier"
                )

        return PermissionResultAllow()

    # ------------------------------------------------------------------
    # Cron persistence helpers
    # ------------------------------------------------------------------

    def _cron_file_path(self) -> Path:
        """Return the path to this session's cron persistence file."""
        return Path(self.cwd) / CRON_FILE

    def _persist_cron(self, tool_input: dict) -> None:
        """Append or update a cron entry from a CronCreate tool call.

        Stores the full tool_input dict as-is. Earlier versions looked for
        hard-coded "schedule"/"command" keys, but the CronCreate tool
        actually uses taskId/cronExpression/fireAt/prompt/description —
        so every entry collapsed to the same empty (schedule, command)
        tuple and the dedupe filter wiped prior entries, leaving only
        one cron in the file regardless of how many were created.

        Dedupe now uses a canonical JSON serialisation of the full entry,
        so identical re-creations collapse but genuinely distinct crons
        (different taskId or fire time) each persist as their own row."""
        cron_path = self._cron_file_path()
        try:
            existing: list[dict] = []
            if cron_path.exists():
                existing = json.loads(cron_path.read_text())

            entry = dict(tool_input)
            entry_canonical = json.dumps(entry, sort_keys=True)
            existing = [
                e for e in existing
                if json.dumps(e, sort_keys=True) != entry_canonical
            ]
            existing.append(entry)
            cron_path.write_text(json.dumps(existing, indent=2))
            # Log a human-readable marker. Try the canonical CronCreate
            # fields first, then legacy names, then fall back to a tag.
            marker = (
                entry.get("taskId")
                or entry.get("fireAt")
                or entry.get("cronExpression")
                or entry.get("schedule")
                or entry.get("description")
                or "cron"
            )
            logger.info(
                "[%s] Persisted cron: %s (total=%d)",
                self.key, marker, len(existing),
            )
        except Exception as exc:
            logger.warning("[%s] Failed to persist cron: %s", self.key, exc)

    def _remove_cron(self, tool_input: dict) -> None:
        """Remove a cron entry matching a CronDelete tool call.

        Matches across all plausible identifier fields (taskId, id,
        cronExpression, fireAt, schedule, command, prompt) so removal
        works regardless of which naming convention the SDK passes."""
        cron_path = self._cron_file_path()
        if not cron_path.exists():
            return
        try:
            existing: list[dict] = json.loads(cron_path.read_text())

            # Identifier candidates from the CronDelete input. An empty
            # string never matches anything in _matches, so missing keys
            # simply don't contribute to the match predicate.
            delete_task_id = tool_input.get("taskId", "")
            delete_id = tool_input.get("id", "")
            delete_cron_expr = tool_input.get("cronExpression", "")
            delete_fire_at = tool_input.get("fireAt", "")
            delete_schedule = tool_input.get("schedule", "")
            delete_command = tool_input.get("command", "")
            delete_prompt = tool_input.get("prompt", "")

            def _matches(e: dict) -> bool:
                if delete_task_id and e.get("taskId") == delete_task_id:
                    return True
                if delete_id and e.get("id") == delete_id:
                    return True
                if delete_cron_expr and e.get("cronExpression") == delete_cron_expr:
                    return True
                if delete_fire_at and e.get("fireAt") == delete_fire_at:
                    return True
                if delete_schedule and e.get("schedule") == delete_schedule:
                    if not delete_command or e.get("command") == delete_command:
                        return True
                if delete_command and e.get("command") == delete_command:
                    return True
                if delete_prompt and e.get("prompt") == delete_prompt:
                    return True
                return False

            filtered = [e for e in existing if not _matches(e)]
            if len(filtered) < len(existing):
                cron_path.write_text(json.dumps(filtered, indent=2))
                logger.info("[%s] Removed %d cron(s)", self.key,
                            len(existing) - len(filtered))
        except Exception as exc:
            logger.warning("[%s] Failed to remove cron: %s", self.key, exc)

    @staticmethod
    def load_persisted_crons(cwd: str) -> list[dict]:
        """Load persisted crons from a session's working directory."""
        cron_path = Path(cwd) / CRON_FILE
        if not cron_path.exists():
            return []
        try:
            data = json.loads(cron_path.read_text())
            return data if isinstance(data, list) else []
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Session pool
# ---------------------------------------------------------------------------

class SessionPool:
    """Manages one Session per contact/channel."""

    def __init__(
        self,
        model: str,
        bot_name: str,
        on_text: Optional[Any] = None,
        on_text_edit: Optional[Any] = None,
        max_sessions: int = 0,
    ) -> None:
        self._model = model
        self._bot_name = bot_name
        self._on_text = on_text
        self._on_text_edit = on_text_edit
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()          # guards _sessions dict reads/writes
        self._creation_locks: dict[str, asyncio.Lock] = {}  # per-key; serialises _create()
        # LRU eviction (Stage 3). _lru_order is the recency list, most-recent
        # last. max_sessions=0 disables the cap (previous unbounded behaviour).
        # When a new session would exceed the cap, the oldest idle session
        # (not currently processing a turn) is stopped and evicted.
        self._max_sessions: int = max_sessions
        self._lru_order: list[str] = []

        # ── Static prompt-fragment cache ──────────────────────────────────
        # SOUL.md, GROUP_SOUL.md, FUSED-CORE.md, and the MANIFEST.json
        # knowledge-index block were read from disk on every _create() call.
        # That's 4 file reads per session spawn, and the manifest also
        # parsed JSON every time. Cached here at pool init; refreshed via
        # SIGHUP if the host process catches one (handler wired in main.py).
        self._cached_soul = _load_template("SOUL.md")
        self._cached_group_soul = _load_template("GROUP_SOUL.md")
        self._cached_fused_core: str = ""
        try:
            fused_core_path = PROJECT_ROOT / "FUSED-CORE.md"
            if fused_core_path.exists():
                self._cached_fused_core = fused_core_path.read_text()
        except Exception as exc:
            logger.warning("FUSED-CORE.md cache load failed: %s", exc)
        self._cached_kb_index_block: str = self._build_kb_index_block()
        self._cached_kb_index_stub: str = self._build_kb_index_stub()

    def _build_kb_index_stub(self) -> str:
        """Small pointer injected at session-creation time in place of the
        full FUSED-CORE MODULES + KNOWLEDGE QUERY TOOL block. The full
        block is deferred until the first domain-matched message on this
        channel, injected as housekeeping. Keeps initial system prompt
        lean (~2-3k tokens saved on first-response prefill for sessions
        that never touch a knowledge module, which is the majority in
        casual group chat)."""
        kb_query_path = SCRIPTS_DIR / "kb-query"
        return (
            "\n\n## KNOWLEDGE BASE\n"
            f"Domain knowledge modules are available via `{kb_query_path}` "
            f"(Bash). Full module index and query reference will be injected "
            f"when a domain keyword matches the incoming message. Until then, "
            f"answer from general knowledge — do not fabricate claims that "
            f"would require a module lookup.\n"
        )

    def _build_kb_index_block(self) -> str:
        """Build the FUSED-CORE MODULES + KNOWLEDGE QUERY TOOL block once.
        Returns empty string if MANIFEST.json missing or unreadable —
        _create() handles the empty case by skipping the append."""
        knowledge_dir = PROJECT_ROOT / "brendbot" / "knowledge"
        kb_query_path = SCRIPTS_DIR / "kb-query"
        manifest_path = knowledge_dir / "MANIFEST.json"
        if not manifest_path.exists():
            return ""
        try:
            manifest = json.loads(manifest_path.read_text())
            load_order = manifest.get("load_order", [])
            module_map = {m["id"]: m for m in manifest.get("modules", [])}
            idx_lines = []
            for module_id in load_order:
                desc = module_map.get(module_id, {}).get("desc", "")
                idx_lines.append(f"- {module_id}: {desc}")
            if not idx_lines:
                return ""
            idx = "\n".join(idx_lines)
            return (
                "\n\n## FUSED-CORE MODULES\n"
                f"{idx}\n\n"
                "## KNOWLEDGE QUERY TOOL\n"
                f"Use `{kb_query_path}` via Bash to query knowledge modules.\n"
                f"  {kb_query_path} search \"<terms>\"      — full-text search\n"
                f"  {kb_query_path} defs <MODULE>          — list definitions\n"
                f"  {kb_query_path} facts <MODULE> [topic] — list facts\n"
                f"  {kb_query_path} thms <MODULE>          — core theorems\n"
                f"  {kb_query_path} thms <MODULE> --extended — all theorems (incl. geometry etc.)\n"
                f"  {kb_query_path} def <ID>               — look up one definition\n"
                f"  {kb_query_path} topics <MODULE>        — list fact topics\n"
                f"  {kb_query_path} gates                  — governance gates\n"
                f"  {kb_query_path} imgstyle <id|label>    — image style descriptor set\n"
                f"  {kb_query_path} imgstyle list          — list all styles\n"
                f"  {kb_query_path} imgfail <class>        — remediation for render failure\n"
                f"  {kb_query_path} imgfail list           — list all failure classes\n"
                f"  {kb_query_path} imglog recent [N]      — recent render outcomes\n"
                "Use kb-query for all knowledge lookups (~200 bytes per result).\n"
                "Do not answer from training weights when a module is available.\n\n"
                "## MODULE PRIORITY\n"
                "BUILDSCI covers building-science facts (insulation, HVAC, moisture,\n"
                "enclosure, combustion). IMAGEGEN covers image-generation style and\n"
                "failure-mode data. Anything outside those two domains is answered\n"
                "from general knowledge — no module lookup needed, and no T1/T2\n"
                "provenance obligation applies."
            )
        except Exception as exc:
            logger.warning("FUSED-CORE system prompt index build failed: %s", exc)
            return ""

    # Modules with structured defs/facts/thms content in knowledge.db.
    # IMAGEGEN uses imagegen_config/prompt_styles/render_* schemas instead
    # and is not fetched via this path — it's queried on-demand via
    # kb-query imgstyle/imgfail/imglog when the model needs it.
    _GROUNDABLE_MODULES = frozenset({"BUILDSCI"})

    # Hard character cap on the injected grounded_facts block per module.
    # BUILDSCI currently runs ~6-9KB across defs+facts+thms; the cap keeps
    # unexpectedly large future modules from dominating the context window.
    _GROUNDED_FACTS_MAX_CHARS = 12000

    def _fetch_grounded_facts(self, module_id: str) -> str:
        """Read a module's defs/facts/thms from knowledge.db and render a
        compact grounded-facts block suitable for housekeeping injection.

        Returns empty string for modules not in _GROUNDABLE_MODULES, for
        any DB error, or when the module has no content. Hard-truncated at
        _GROUNDED_FACTS_MAX_CHARS so a future content explosion can't
        silently eat the context budget — the block is bounded and
        observable in the log line written by the caller."""
        if module_id not in self._GROUNDABLE_MODULES:
            return ""
        knowledge_db = PROJECT_ROOT / "brendbot" / "knowledge" / "knowledge.db"
        if not knowledge_db.exists():
            return ""
        import sqlite3
        try:
            # timeout waits for a contending writer rather than raising; the
            # pragmas match user_registry._conn / episodes._open so the DB
            # runs uniformly in WAL mode across all call sites.
            conn = sqlite3.connect(str(knowledge_db), timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            defs = conn.execute(
                "SELECT id, term, description, formal, source FROM definitions "
                "WHERE module_id = ? ORDER BY id",
                (module_id,),
            ).fetchall()
            facts = conn.execute(
                "SELECT topic, name, description, source FROM facts "
                "WHERE module_id = ? ORDER BY topic, name",
                (module_id,),
            ).fetchall()
            thms = conn.execute(
                "SELECT id, description, formal, source FROM theorems "
                "WHERE module_id = ? AND extended = 0 ORDER BY id",
                (module_id,),
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.debug("_fetch_grounded_facts(%s) DB error: %s", module_id, exc)
            return ""
        if not (defs or facts or thms):
            return ""
        parts: list[str] = [f"<grounded_facts module='{module_id}'>"]
        if defs:
            parts.append("  <definitions>")
            for did, term, desc, formal, src in defs:
                line = f"    {did}: {term}"
                if desc:
                    line += f" = {desc}"
                if formal:
                    line += f" | F: {formal}"
                if src:
                    line += f" | {src}"
                parts.append(line)
            parts.append("  </definitions>")
        if facts:
            parts.append("  <facts>")
            for topic, name, desc, src in facts:
                line = f"    {topic}.{name} = {desc}"
                if src:
                    line += f" | {src}"
                parts.append(line)
            parts.append("  </facts>")
        if thms:
            parts.append("  <theorems>")
            for tid, desc, formal, src in thms:
                line = f"    {tid}"
                if desc:
                    line += f" = {desc}"
                if formal:
                    line += f" | F: {formal}"
                if src:
                    line += f" | {src}"
                parts.append(line)
            parts.append("  </theorems>")
        parts.append("</grounded_facts>")
        block = "\n".join(parts)
        if len(block) > self._GROUNDED_FACTS_MAX_CHARS:
            block = (
                block[: self._GROUNDED_FACTS_MAX_CHARS]
                + "\n[truncated — full module via kb-query defs/facts/thms]"
            )
        return block

    def refresh_cache(self) -> None:
        """Re-read all cached prompt fragments and engagement config. Wire
        to SIGHUP in main.py so live edits to SOUL.md / FUSED-CORE.md /
        MANIFEST.json / engagement.yaml take effect without a full process
        restart."""
        self._cached_soul = _load_template("SOUL.md")
        self._cached_group_soul = _load_template("GROUP_SOUL.md")
        try:
            fused_core_path = PROJECT_ROOT / "FUSED-CORE.md"
            self._cached_fused_core = (
                fused_core_path.read_text() if fused_core_path.exists() else ""
            )
        except Exception as exc:
            logger.warning("FUSED-CORE.md refresh failed: %s", exc)
        self._cached_kb_index_block = self._build_kb_index_block()
        self._cached_kb_index_stub = self._build_kb_index_stub()
        # Hot-reload engagement.yaml — scoring deltas, thresholds, noise
        # tokens, domain keywords, classifier prompts, content gate config.
        try:
            from brendbot.discord import refresh_engagement_config
            refresh_engagement_config()
        except Exception as exc:
            logger.warning("engagement.yaml refresh failed: %s", exc)
        logger.info("SessionPool cache refreshed")

    def _bump_lru(self, key: str) -> None:
        """Move `key` to the most-recently-used position. Caller must hold
        self._lock (or be in a create path where the new key isn't yet
        visible to other coroutines)."""
        try:
            self._lru_order.remove(key)
        except ValueError:
            pass
        self._lru_order.append(key)

    async def _evict_if_over_cap(self, protect_key: str) -> None:
        """If _sessions count exceeds max_sessions, evict the oldest idle
        session. `protect_key` is the key currently being created — never
        evicted even if it sits at the head of the LRU list.

        An 'idle' session is one with an empty queue and no current turn
        lock held. If every session is busy, no eviction happens and the
        cap is soft — we log a warning and proceed. This keeps the hot
        path from blocking on arbitrary turn latency."""
        if self._max_sessions <= 0:
            return
        excess = len(self._sessions) - self._max_sessions
        if excess <= 0:
            return
        evicted = 0
        for candidate in list(self._lru_order):
            if evicted >= excess:
                break
            if candidate == protect_key:
                continue
            sess = self._sessions.get(candidate)
            if sess is None:
                try:
                    self._lru_order.remove(candidate)
                except ValueError:
                    pass
                continue
            # Idle check: queue empty and turn lock not held.
            if not sess._queue.empty():
                continue
            if sess._turn_lock.locked():
                continue
            logger.info(
                "SessionPool LRU eviction: stopping idle session %s "
                "(cap=%d, size=%d)",
                candidate, self._max_sessions, len(self._sessions),
            )
            try:
                await sess.stop()
            except Exception as exc:
                logger.warning("LRU evict: stop failed for %s: %s", candidate, exc)
            self._sessions.pop(candidate, None)
            try:
                self._lru_order.remove(candidate)
            except ValueError:
                pass
            try:
                from brendbot import discord as _bd
                _bd._admin_alert(
                    "session_evict",
                    f"ℹ️ session pool evicted idle session `{candidate}` "
                    f"(cap={self._max_sessions}).",
                )
            except Exception:
                pass
            evicted += 1
        if evicted < excess:
            logger.warning(
                "SessionPool at %d/%d sessions — all candidates busy, "
                "cap temporarily exceeded",
                len(self._sessions), self._max_sessions,
            )

    async def route_message(
        self,
        platform: str,
        sender_id: str,
        chat_id: str,
        text: str,
        tier: str,
        is_group: bool = False,
        message_id: str = "",
        reply_to_id: str = "",
        reply_to_text: str = "",
        reply_to_author: str = "",
        context_messages: list | None = None,
        is_direct_mention: bool = False,
        domain_hint: str = "",
        address_level: str = "high",
        score: float | None = None,
        haiku_invoked: bool = False,
        guild_id: str = "",
        recv_ts: float | None = None,
        engage_done_ts: float | None = None,
    ) -> None:
        key = f"{platform}:{chat_id}" if is_group else f"{platform}:{sender_id}"

        # Phase 1: check existing session under the global dict lock.
        # Dead sessions are reaped here. If none exists, acquire a per-key
        # creation lock before calling _create() so concurrent messages for
        # the same channel don't each spin up independent sessions.
        async with self._lock:
            session = self._sessions.get(key)
            if session is not None and not session.is_alive():
                logger.warning("Dead session for %s, recreating", key)
                await session.stop()
                del self._sessions[key]
                try:
                    self._lru_order.remove(key)
                except ValueError:
                    pass
                session = None
            if key not in self._creation_locks:
                self._creation_locks[key] = asyncio.Lock()
            creation_lock = self._creation_locks[key]
            if session is not None:
                self._bump_lru(key)

        is_new_session = False
        if session is None:
            # Phase 2: per-key lock serialises creation for this channel only.
            # Other channels are unaffected — no global lock held during _create().
            async with creation_lock:
                # Re-check under creation lock: another coroutine may have
                # completed _create() while we were waiting.
                async with self._lock:
                    session = self._sessions.get(key)
                if session is None or not session.is_alive():
                    session = await self._create(
                        key, tier, platform, chat_id, is_group, guild_id=guild_id,
                    )
                    async with self._lock:
                        self._sessions[key] = session
                        self._bump_lru(key)
                        await self._evict_if_over_cap(protect_key=key)
                    is_new_session = True
        # Keep the stashed guild_id fresh on pre-existing sessions too —
        # allows an existing session to pick up a guild association if it
        # was created before this plumbing existed.
        if session is not None and guild_id and not getattr(session, "_guild_id", ""):
            session._guild_id = guild_id

        context_block = ""
        if context_messages and is_new_session:
            lines = []
            for m in context_messages:
                s = escape(m.get("sender_id", ""))
                n = escape(m.get("display_name", ""))
                mid = escape(m.get("message_id", ""))
                t = escape(m.get("text", ""))
                lines.append(
                    f"  <prior_message sender='{s}' display_name='{n}' message_id='{mid}'>"
                    f"{t}</prior_message>"
                )
            context_block = "\n<channel_context>\n" + "\n".join(lines) + "\n</channel_context>\n"

        msg_id_attr = f" message_id='{message_id}'" if message_id else ""
        reply_block = ""
        if reply_to_id:
            reply_block = (
                f"\n<reply_to message_id={quoteattr(reply_to_id)} "
                f"sender={quoteattr(reply_to_author)}>"
                f"{escape(reply_to_text)}</reply_to>"
            )
        type_attr = " type='group'" if is_group else ""
        domain_attr = f" domain_hint='{escape(domain_hint)}'" if domain_hint else ""
        # address_level surfaces FUSED-CORE Budget Throttle to the model AND is
        # used by _permission_check to cap the per-turn Bash budget below.
        addr_attr = f" address_level='{escape(address_level)}'"

        # Phase 3 #2B — retrieval cue scoring at message ingest.
        # Query prior episodes for this channel matching the current
        # domain hints. If matches exist, prepend a brief <recall> block
        # to the message — bookended summaries from up to 3 prior session
        # segments. This implements the encoding-specificity principle:
        # the cue at retrieval time (current channel + matched domains)
        # surfaces memories whose encoding context matched.
        #
        # Hard-bounded to ~600 chars total to prevent recall bloat.
        # Logged for hit-rate audit.
        recall_block = ""
        try:
            from brendbot.episodes import query_episodes
            current_domains = (
                sorted(domain_hint.split(",")) if domain_hint else []
            )
            episodes = query_episodes(
                channel=chat_id,
                domains=current_domains,
                limit=3,
                # Patch 3: semantic re-rank. The raw inbound text is
                # the recall cue — passing it lets query_episodes
                # cosine-score stored episode summaries against the
                # current question instead of relying purely on the
                # lexical domain-LIKE prefilter + recency order.
                # Degrades gracefully when sentence-transformers is
                # unavailable.
                query_text=text,
            )
            if episodes:
                # Patch A: filter out episodes already injected this session.
                # Episodes are keyed by their SQLite rowid (ep["id"]). An
                # episode already in _recalled_episode_ids is already in the
                # model's context window — re-injecting it adds identical
                # text to every subsequent message with zero information gain.
                novel_episodes = [
                    ep for ep in episodes
                    if ep.get("id") not in session._recalled_episode_ids
                ]
                recall_lines = []
                used_chars = 0
                injected_ids: list[int] = []
                for ep in novel_episodes:
                    summary = (ep.get("summary") or "")[:200]
                    if not summary:
                        continue
                    line = (
                        f"  <prior_episode ts={quoteattr(ep.get('ts_end', ''))} "
                        f"domains={quoteattr(ep.get('domains', ''))} "
                        f"turns='{ep.get('turn_count', 0)}'>"
                        f"{escape(summary)}</prior_episode>"
                    )
                    if used_chars + len(line) > 600:
                        break
                    recall_lines.append(line)
                    used_chars += len(line)
                    if ep.get("id") is not None:
                        injected_ids.append(ep["id"])
                if recall_lines:
                    recall_block = (
                        "\n<recall>\n" + "\n".join(recall_lines) + "\n</recall>\n"
                    )
                    session._recalled_episode_ids.update(injected_ids)
                    logger.info(
                        "[%s] recall injected: %d novel episode(s), %d chars "
                        "(%d already known, skipped)",
                        key, len(recall_lines), used_chars,
                        len(episodes) - len(novel_episodes),
                    )
                elif episodes:
                    logger.debug(
                        "[%s] recall: all %d episode(s) already in context — skipped",
                        key, len(episodes),
                    )
        except Exception as exc:
            logger.warning("[%s] recall query skipped: %s", key, exc)

        wrapped = (
            f"{context_block}"
            f"{recall_block}"
            f"<message platform='{platform}' sender='{sender_id}' "
            f"chat='{chat_id}' tier='{tier}'{type_attr}{msg_id_attr}"
            f"{domain_attr}{addr_attr}"
            f" context_tokens='{session._last_input_tokens}'>"
            f"{reply_block}\n{text}\n</message>"
        )

        session.current_sender_tier = tier
        # Stash address level on the session so _permission_check can read it
        # when enforcing per-turn tool budgets. Reset on every new turn.
        session.current_address_level = address_level
        # Stash turn metadata for feedback logging in _fire_on_text.
        session._turn_user_message_id = message_id
        session._turn_user_text = text
        session._turn_score = score
        session._turn_domains = sorted(domain_hint.split(",")) if domain_hint else []
        # Phase 3 #2A — accumulate domains seen across the whole session segment
        # for the episode row written at restart time.
        if session._turn_domains:
            session._session_domains_seen.update(session._turn_domains)
        session.log_turn("user", text)
        session._tool_call_count = 0
        session._turn_modules_queried = set()
        session._turn_kb_query_used = False
        # Reset per-turn load counters; cumulative counters persist until restart.
        session._turn_bash_calls = 0
        session._turn_other_tool_calls = 0
        # Patch 1a — fresh BudgetState for this turn. BudgetState carries
        # typed caps from agent_core.budgets.Budget (step_cap, time_cap_s)
        # and a monotonic start timestamp; _permission_check reads from
        # it to enforce the wall-clock cap on every tool-call request.
        from brendbot.agent_core.budgets import Budget, BudgetState
        session._turn_budget = BudgetState(budget=Budget(
            step_cap=_TOOL_CALL_BUDGET,
            time_cap_s=_TURN_TIME_CAP_S,
            token_cap=None,
        ))
        session._turn_sender_id = sender_id
        # If the engagement gate fired the haiku classifier on this message,
        # account for it in the cumulative load score (Phase 3 #1A). Also
        # record per-turn for flow-class / fabrication-risk in log_bot_response.
        session._turn_haiku_invoked = bool(haiku_invoked)
        if haiku_invoked:
            session._cumulative_haiku_invocations += 1

        # Phase 2a — carry discord.py-side timing stamps onto the session.
        # These are time.monotonic() values; deltas are computed in
        # _fire_on_text / _fire_on_text_streamed. None is tolerated at every
        # stage (callers that don't instrument still work). Set before the
        # complexity preflight so an early refusal still logs timings.
        session._turn_t_received = recv_ts
        session._turn_t_engage_gate_done = engage_done_ts
        session._turn_t_content_gate_done = None
        session._turn_t_first_token = None

        # Patch 1b — agent_core.complexity pre-flight. Before the content
        # gate spends a haiku call and the session spends a full
        # generation pass, classify the inbound request by Chapter-5
        # complexity tier. Hard-reject requests that classify as RE
        # (halting-problem-adjacent) or NON_RE (beyond semi-decidable)
        # with a local refusal — the model would either loop or
        # fabricate a "proof" that isn't one, and neither is cheap.
        # PSPACE hints (long-horizon planning, game-tree search) are
        # not rejected but are logged; callers may still get a useful
        # approximation.
        try:
            from brendbot.agent_core.complexity import (
                classify as _complexity_classify,
                Route as _ComplexityRoute,
                Tier as _ComplexityTier,
            )
            classification = _complexity_classify(text)
            if classification.route == _ComplexityRoute.REJECT:
                logger.info(
                    "[%s] complexity preflight REJECT tier=%s rationale=%s",
                    session.key, classification.tier.name, classification.rationale,
                )
                refusal = (
                    f"can't take that one — the request is tier "
                    f"{classification.tier.name} "
                    f"({classification.rationale}). "
                    "beyond what can be decided in finite time; "
                    "reframe with a concrete bound and I can try."
                )
                asyncio.create_task(session._fire_on_text(refusal))
                return
            if classification.tier == _ComplexityTier.PSPACE:
                logger.info(
                    "[%s] complexity preflight PSPACE tier (bounded_search path) — %s",
                    session.key, classification.rationale,
                )
        except Exception as exc:
            # Preflight is advisory; fall through on any error to keep
            # the response path live.
            logger.debug(
                "[%s] complexity preflight error (advisory, ignoring): %s",
                session.key, exc,
            )

        # Content gate (phase 4) runs here, between turn state setup and
        # inject. Decides PASS / FLAG / REFUSE / FLOOR_HIT / BYPASS via
        # a one-shot haiku classifier spawn. Returns 'inject' if the caller
        # should proceed with normal injection, or 'handled' if the gate
        # already dispatched a response (refusal, flagged-path reroute,
        # or budget-exhausted message) via _fire_on_text.
        try:
            gate_action = await session.apply_content_gate(
                wrapped_text=wrapped,
                raw_user_text=text,
                tier=tier,
                sender_id=sender_id,
                message_id=message_id,
            )
        except Exception as exc:
            logger.error(
                "[%s] content gate raised unexpected error, failing open to inject: %s",
                session.key, exc,
            )
            gate_action = "inject"
        # Phase 2a — stamp content-gate-done after the classifier returns.
        # Stamped regardless of gate_action so FLAG/REFUSE/BYPASS turns still
        # emit a timing for the gate stage (useful when tuning gate latency
        # relative to generation latency).
        session._turn_t_content_gate_done = time.monotonic()

        if gate_action == "inject":
            # Stage 4 — expand the KB index on first domain match. The
            # stub injected at session creation points to this mechanism.
            # The full block is injected as housekeeping so the model
            # doesn't respond to it but learns the full query reference
            # before tackling the domain-matching message.
            if (
                domain_hint
                and not session._kb_index_expanded
                and self._cached_kb_index_block
            ):
                try:
                    session._next_turn_is_housekeeping = True
                    await session.inject(
                        "<kb_index_expansion>\n"
                        "The following index is now live. Use it to answer "
                        "the next message and any subsequent domain-matched "
                        "queries in this session.\n"
                        + self._cached_kb_index_block
                        + "\n</kb_index_expansion>",
                        housekeeping=True,
                    )
                    session._kb_index_expanded = True
                except Exception as exc:
                    logger.warning(
                        "[%s] KB index expansion inject failed: %s",
                        session.key, exc,
                    )
            # Premise Check enforcement — pre-fetch structured module
            # content for any matched module not already grounded in this
            # session. Converts the documented gate from "model chooses to
            # call kb-query" to "module data is already in context". The
            # [ctx] suffix (set by discord.py when the match came from
            # recent-channel-context fallback rather than the current
            # message) is stripped before lookup — the grounded_facts
            # block should fire on context matches too, since the model
            # still needs the data to reason correctly.
            if domain_hint:
                new_modules: list[str] = []
                for token in domain_hint.split(","):
                    mod = token.strip().replace("[ctx]", "")
                    if mod and mod not in session._grounded_modules:
                        new_modules.append(mod)
                for mod in new_modules:
                    block = self._fetch_grounded_facts(mod)
                    if not block:
                        # Mark as grounded anyway so we don't retry every
                        # message for modules that have no structured data
                        # (e.g. IMAGEGEN — queried on-demand via imgstyle).
                        session._grounded_modules.add(mod)
                        continue
                    try:
                        session._next_turn_is_housekeeping = True
                        await session.inject(
                            "[SYSTEM] Pre-fetched module content for Premise "
                            f"Check. Use this to answer the next message "
                            f"rather than answering from general knowledge.\n"
                            + block,
                            housekeeping=True,
                        )
                        session._grounded_modules.add(mod)
                        logger.info(
                            "[%s] grounded_facts injected: module=%s (%d chars)",
                            session.key, mod, len(block),
                        )
                    except Exception as exc:
                        logger.warning(
                            "[%s] grounded_facts inject failed for %s: %s",
                            session.key, mod, exc,
                        )
            await session.inject(wrapped)
        # else 'handled' — gate dispatched directly, no inject needed

    async def _create(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool,
        guild_id: str = "",
    ) -> Session:
        safe_id = chat_id.replace("+", "_").replace("/", "_").replace("=", "")
        prefix = "group_" if is_group else ""
        transcript_dir = TRANSCRIPTS_DIR / platform / f"{prefix}{safe_id}"
        transcript_dir.mkdir(parents=True, exist_ok=True)

        claude_md = transcript_dir / "CLAUDE.md"
        # Build send_command and companion script paths — all resolved at runtime
        # from SCRIPTS_DIR so hardcoded paths never appear in soul files.
        send_cmd = f'{SCRIPTS_DIR}/send-discord "{chat_id}" "<message>"'
        react_cmd = f'{SCRIPTS_DIR}/react-discord "{chat_id}"'
        generate_image_cmd = f'{SCRIPTS_DIR}/generate-image'
        kb_db_path = PROJECT_ROOT / "brendbot" / "knowledge" / "knowledge.db"

        template_file = "GROUP_SOUL.md" if is_group else "SOUL.md"
        # Read from SessionPool cache instead of hitting disk on every spawn.
        # Refreshable via SessionPool.refresh_cache() (SIGHUP-wired in main.py).
        template = self._cached_group_soul if is_group else self._cached_soul
        prompt = _render(template, {
            "bot_name": self._bot_name,
            "send_command": send_cmd,
            "react_command": react_cmd,
            "generate_image_command": generate_image_cmd,
            "kb_path": str(kb_db_path),
            "platform_name": "Discord",
        })

        # Append cached FUSED-CORE.md content (was a per-spawn disk read).
        if self._cached_fused_core:
            prompt += "\n\n" + self._cached_fused_core

        # Append the lightweight KB stub instead of the full index.
        # The full MODULES + KNOWLEDGE QUERY TOOL block is deferred to the
        # first domain-matched message and injected as housekeeping via
        # route_message. See Session._kb_index_expanded and the Stage 4
        # note in SessionPool._build_kb_index_stub for rationale.
        if self._cached_kb_index_stub:
            prompt += self._cached_kb_index_stub

        # Append compact user registry table so the model can resolve
        # @mention snowflakes at reasoning time without a tool call. Lists
        # the _COMPACT_TABLE_MAX most recently active server members with
        # their display names, tiers, message counts, and domain history.
        # The bot's own ID is excluded from the table.
        try:
            from brendbot.user_registry import compact_table
            from brendbot.discord import _discord_client as _dc
            _bot_id = str(_dc.user.id) if _dc and _dc.user else ""
            # Filter to this guild's users so a Wheat session doesn't see
            # Pizzacord members in its user table (multi-server isolation).
            # Empty guild_id (DM sessions or unplumbed callers) falls back
            # to the unfiltered global list.
            _user_table = compact_table(bot_id=_bot_id, guild_id=guild_id)
            if _user_table:
                prompt += (
                    "\n\n## SERVER MEMBERS\n"
                    "Known server members. Use this table to resolve @mention "
                    "snowflakes — if a mention target is not you, do not respond "
                    "unless your name also appears in the message.\n"
                    + _user_table
                )
        except Exception as exc:
            logger.debug("user_registry compact_table inject failed: %s", exc)

        claude_md.write_text(prompt)

        session_tier = "admin" if is_group else tier
        session = Session(
            key=key,
            tier=session_tier,
            cwd=str(transcript_dir),
            model=self._model,
            on_text=self._on_text,
            on_text_edit=self._on_text_edit,
            chat_id=chat_id,
            guild_id=guild_id,
        )
        await session.start()

        _key, _tier, _platform, _chat_id, _is_group = key, tier, platform, chat_id, is_group
        _guild_id = guild_id

        async def _restart_cb() -> None:
            await self._restart_session(
                _key, _tier, _platform, _chat_id, _is_group, guild_id=_guild_id,
            )

        session._on_needs_restart = _restart_cb

        # ── Startup injection: reasoning chain + reference block ──────────
        # Each of these injects context-only material that the model is told
        # not to respond to. Without marking them as housekeeping, the result
        # handler treats the empty ResultMessage as a "phantom turn" and
        # fires the silent-drop fallback (sending a "(no response generated)"
        # message that the user sees before the real reply arrives on the
        # next turn). The housekeeping flag makes the result handler clear
        # the buffer and skip both dispatch branches, same as shallow rest.
        memory_dir = transcript_dir / "memory"
        if memory_dir.is_dir():
            _ESSENTIAL_FRAGMENTS = ("behavior.md", "identity.md")
            for frag_name in _ESSENTIAL_FRAGMENTS:
                frag_path = memory_dir / frag_name
                if frag_path.exists():
                    try:
                        content = frag_path.read_text()
                        await session.inject(
                            f"<system-memory source='{frag_name}'>\n{content}\n</system-memory>\n"
                            "Do not respond to this injection.",
                            housekeeping=True,
                        )
                    except Exception as exc:
                        logger.warning("Memory fragment %s injection failed: %s", frag_name, exc)
        else:
            memory_md = transcript_dir / "MEMORY.md"
            if memory_md.exists():
                try:
                    memory_content = memory_md.read_text()
                    await session.inject(
                        f"<system-memory>\n{memory_content}\n</system-memory>\n"
                        "The above is your persistent memory. Treat all ## PERSISTENT entries as active context. Do not respond to this injection.",
                        housekeeping=True,
                    )
                except Exception as exc:
                    logger.warning("MEMORY.md injection failed: %s", exc)

        # ── Phase 2: Reference material (single consolidated turn) ────────
        ref_sections: list[str] = []

        if memory_dir.is_dir():
            other_frags = [
                f for f in sorted(memory_dir.iterdir())
                if f.suffix == ".md" and f.name not in ("behavior.md", "identity.md")
            ]
            if other_frags:
                index_lines = [f"- {f.stem}: {f}" for f in other_frags]
                ref_sections.append(
                    "<memory-index>\n" + "\n".join(index_lines) + "\n</memory-index>\n"
                    "Use `kb-query memory <source>` to load fragments on demand. "
                    "Do not Read .md files directly. Load only what the current question requires."
                )

        context_summary = transcript_dir / "CONTEXT_SUMMARY.md"
        if context_summary.exists():
            try:
                summary_content = context_summary.read_text()
                ref_sections.append(
                    f"<context-summary>\n{summary_content}\n</context-summary>\n"
                    "Recent conversation turns from prior session. Use for continuity."
                )
                logger.info("CONTEXT_SUMMARY.md included in ref block for %s", key)
            except Exception as exc:
                logger.warning("CONTEXT_SUMMARY.md read failed: %s", exc)

        if ref_sections:
            combined_ref = "\n\n".join(ref_sections)
            await session.inject(
                f"<system-ref>\n{combined_ref}\n</system-ref>\n"
                "Do not respond to this injection.",
                housekeeping=True,
            )
            logger.info("Reference block injected for %s (%d bytes, %d sections)",
                        key, len(combined_ref), len(ref_sections))

        # ── Phase 3: Cron job replay ─────────────────────────────────────
        # If previous sessions had active cron jobs, inject a housekeeping
        # message asking Claude to re-create them via CronCreate.
        persisted_crons = Session.load_persisted_crons(str(transcript_dir))
        if persisted_crons:
            cron_specs = json.dumps(persisted_crons, indent=2)
            await session.inject(
                "[SYSTEM] The following cron jobs were active before your last session reset. "
                "Please re-create them now using CronCreate for each entry:\n"
                f"```json\n{cron_specs}\n```",
                housekeeping=False,
            )
            logger.info("Cron replay injected for %s (%d cron(s))",
                        key, len(persisted_crons))

        return session

    async def _restart_session(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool,
        guild_id: str = "",
    ) -> None:
        logger.info("Restarting session %s after context refresh", key)
        async with self._lock:
            old_session = self._sessions.pop(key, None)
        # Preserve guild_id across restarts: fall back to the dying
        # session's stashed value if no explicit guild_id was passed.
        if not guild_id and old_session is not None:
            guild_id = getattr(old_session, "_guild_id", "") or ""
        if old_session:
            try:
                await old_session.stop()
            except Exception as exc:
                logger.warning("Error stopping session %s during restart: %s", key, exc)
        # Use per-key creation lock so restart doesn't block other channels.
        creation_lock = self._creation_locks.get(key)
        if creation_lock is None:
            creation_lock = asyncio.Lock()
            self._creation_locks[key] = creation_lock
        async with creation_lock:
            try:
                new_session = await self._create(
                    key, tier, platform, chat_id, is_group, guild_id=guild_id,
                )
                async with self._lock:
                    self._sessions[key] = new_session
                    self._bump_lru(key)
                logger.info("Session %s restarted successfully", key)
            except Exception as exc:
                logger.error("Failed to recreate session %s after restart: %s", key, exc)

    async def stop_all(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.stop()
            self._sessions.clear()
        # Drain the warm classifier pool so spawned subprocesses don't leak
        pool = get_classifier_pool()
        await pool.drain()

