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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
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

# ---------------------------------------------------------------------------
# Haiku ambiguity classifier — direct Anthropic API (no subprocess)
# ---------------------------------------------------------------------------


async def haiku_classify(payload: dict) -> dict:
    """
    Engagement classifier via direct Anthropic API call.

    Uses claude-haiku-4-5 with max_tokens=1 for a YES/NO decision.
    No subprocess spawned — far lower latency and overhead than the CLI path.

    Returns {"engage": bool, "reason": str}.
    """
    import anthropic

    text = payload.get("message", "")
    recent = payload.get("recent_context", [])
    context_lines = "\n".join(
        f"{m.get('display_name', 'unknown')}: {m.get('text', '')}"
        for m in recent[-5:]
    )

    # Classifier rules come from engagement.yaml's classifier_prompt block.
    # Single source of truth: discord.py uses the same yaml for heuristic
    # scoring and this function uses it for the LLM ambiguity gate.
    # Hard-coded fallback only for the case where the yaml load itself
    # failed at module init (which would have prevented startup anyway).
    try:
        from brendbot.discord import _ENGAGEMENT_CFG
        classifier_rules = _ENGAGEMENT_CFG.get("classifier_prompt", "").strip()
    except Exception:
        classifier_rules = ""
    if not classifier_rules:
        classifier_rules = (
            "Should brendbot respond? YES if directly addressed or domain question. "
            "NO if casual chat between others. Reply YES or NO only."
        )

    prompt = (
        f"{classifier_rules}\n\n"
        f"Recent context:\n{context_lines}\n"
        f"New message: {text}\n"
        "Reply YES or NO only."
    )

    try:
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip().upper() if response.content else "NO"
        engage = answer.startswith("Y")
        return {"engage": engage, "reason": f"api:{answer[:4]}"}
    except Exception as e:
        logger.warning("haiku_classify API error: %s", e)
        return {"engage": False, "reason": "error"}


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

# Hard restart when input tokens exceed this value.
_CONTEXT_REFRESH_THRESHOLD = 400_000

# Soft warning threshold.
_CONTEXT_SOFT_WARNING = 320_000

# Max turns to retain in the rolling turn log.
_MAX_TURN_LOG = 30

# Hard cap on Bash calls within a single user-message turn.
_TOOL_CALL_BUDGET = 8

# Write a rolling checkpoint every N completed turns.
_CHECKPOINT_INTERVAL = 5


class Session:
    """One Claude Agent SDK session for a contact or channel."""

    def __init__(
        self,
        key: str,
        tier: str,
        cwd: str,
        model: str = "sonnet",
        on_text: Optional[Any] = None,
        chat_id: str = "",
    ) -> None:
        self.key = key
        self.tier = tier
        self.cwd = cwd
        self._model = model
        self._on_text = on_text
        self._chat_id = chat_id
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
        self._MODULE_EMOTES: dict[str, str] = {
            "LOGIC":       "🔣",
            "STATS":       "📊",
            "SYSTEMS":     "⚙️",
            "PERSONALITY": "🫀",
            "BUILDSCI":    "🏗️",
            "GOVERNANCE":  "⚖️",
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

    async def inject(self, text: str) -> None:
        await self._queue.put(text)

    async def _fire_on_text(self, text: str) -> None:
        """Send a turn's response to Discord with feedback-log side effects.

        Order: extract leading branch tag → strip from chat-bound text →
        call on_text(chat_id, stripped_text) which returns the posted
        message ID → log to bot_responses.jsonl → if there was a tag,
        also log to branch_audit.jsonl.

        Holds self._turn_lock for the entire send + log sequence so the
        next query() in _run_loop cannot dispatch turn N+1 until turn N's
        feedback writes have completed. This is the actual serialization
        the duplicate `turn complete` race fix requires — wrapping
        _client.query() alone in _run_loop is insufficient because
        _fire_on_text is fire-and-forget from _receive_loop.

        All log writes are best-effort; failures never break the chat path.
        """
        from brendbot.feedback import (
            extract_branch_tag,
            log_bot_response,
            log_branch_audit,
        )
        async with self._turn_lock:
            branch_tag, stripped = extract_branch_tag(text)
            try:
                bot_message_id = await self._on_text(self._chat_id, stripped)
            except Exception as exc:
                logger.error("[%s] on_text callback failed: %s", self.key, exc)
                return
            if not bot_message_id:
                # Send failed or pre-ready dispatch — nothing to correlate against.
                return
            log_bot_response(
                channel_id=self._chat_id,
                bot_message_id=bot_message_id,
                user_message_id=self._turn_user_message_id,
                user_text=self._turn_user_text,
                score=self._turn_score,
                domains=self._turn_domains,
                address_level=self.current_address_level,
                branch_tag=branch_tag,
            )
            if branch_tag:
                log_branch_audit(
                    channel_id=self._chat_id,
                    bot_message_id=bot_message_id,
                    branch=branch_tag,
                    response_text=stripped,
                )

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
                    msg = await asyncio.wait_for(self._queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                if self._context_state == "restarting":
                    continue
                try:
                    assert self._client is not None
                    async with self._turn_lock:
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
            self.running = False
        except Exception as e:
            logger.error("Session %s receiver error: %s", self.key, e)
            self.running = False

    def _handle(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingBlock):
                    try:
                        import datetime
                        thoughts_path = Path(self.cwd) / "thoughts.log"
                        ts = datetime.datetime.now().isoformat(timespec="seconds")
                        with thoughts_path.open("a") as f:
                            f.write(f"\n--- [{ts}] turn {self._completed_turn_count} ---\n")
                            f.write(block.thinking)
                            f.write("\n")
                    except Exception as exc:
                        logger.warning("[%s] thoughts.log write failed: %s", self.key, exc)
                elif isinstance(block, TextBlock):
                    logger.info("[%s] %s", self.key, block.text[:200])
                    self.log_turn("assistant", block.text)
                    if block.text.strip():
                        self._turn_text_buffer.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    logger.info("[%s] tool: %s", self.key, block.name)
                    self._turn_tool_called = True  # mark that tool use occurred this turn
                    if block.name == "Bash":
                        tool_cmd = (block.input or {}).get("command", "")
                        if "send-discord" in tool_cmd:
                            self._turn_used_send_discord = True
                        if "kb-query" in tool_cmd:
                            self._turn_kb_query_used = True
                            import re as _re
                            _mod_match = _re.search(
                                r'kb-query\s+(?:defs|facts|thms|topics|xlinks)\s+(\w+)',
                                tool_cmd,
                            )
                            if _mod_match:
                                self._turn_modules_queried.add(_mod_match.group(1).upper())
        elif isinstance(message, ResultMessage):
            if (self._turn_text_buffer
                    and not self._turn_used_send_discord
                    and self._on_text
                    and self._chat_id):
                if self._turn_tool_called:
                    # Tools were called this turn — everything before the last
                    # TextBlock was mid-turn narration. Only the final segment
                    # is the intended response.
                    text_to_send = self._turn_text_buffer[-1]
                else:
                    # Text-only turn — all segments are intentional, send in full.
                    text_to_send = "\n".join(self._turn_text_buffer)
                asyncio.create_task(self._fire_on_text(text_to_send))
            self._turn_text_buffer.clear()
            self._turn_used_send_discord = False
            self._turn_tool_called = False

            cost = f" (${message.total_cost_usd:.4f})" if message.total_cost_usd else ""
            usage = message.usage or {}
            if isinstance(usage, dict) and usage:
                input_tokens = usage.get("input_tokens", 0) or 0
                cache_read = usage.get("cache_read_input_tokens", 0) or 0
                cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                total_context = input_tokens + cache_read + cache_creation
                if total_context:
                    self._last_input_tokens = int(total_context)
                    if self._last_input_tokens >= _CONTEXT_REFRESH_THRESHOLD:
                        if self._context_state == "normal":
                            self._context_state = "threshold_hit"
                        logger.warning(
                            "[%s] context at %d tokens (>=%d threshold) — clean restart queued",
                            self.key, self._last_input_tokens, _CONTEXT_REFRESH_THRESHOLD,
                        )
                    elif (self._last_input_tokens >= _CONTEXT_SOFT_WARNING
                          and not self._soft_warning_sent):
                        self._soft_warning_sent = True
                        # Promote soft warning to threshold_hit so the receiver
                        # loop fires _trigger_clean_restart() at end of this
                        # turn — preempts the 400k mid-turn ambush by
                        # restarting at ~320k while we still have headroom.
                        if self._context_state == "normal":
                            self._context_state = "threshold_hit"
                        logger.info(
                            "[%s] context at %d tokens — soft warning, preemptive restart queued",
                            self.key, self._last_input_tokens,
                        )
            logger.info(
                "[%s] turn complete%s (context_tokens=%d)",
                self.key, cost, self._last_input_tokens,
            )
            try:
                import pathlib
                status_path = pathlib.Path(self.cwd) / "context_status.txt"
                pct = round(self._last_input_tokens / 1_000_000 * 100, 1)
                status_path.write_text(f"{self._last_input_tokens} {pct}\n")
            except Exception:
                pass
            if self._react_msg_id and self._active_reactions:
                reactions_to_clear = set(self._active_reactions)
                unreact_fn = self._unreact_fn
                channel = self._react_channel
                msg_id = self._react_msg_id
                async def _do_clear(reactions=reactions_to_clear, fn=unreact_fn, ch=channel, mid=msg_id):
                    for emoji in reactions:
                        if fn and ch and mid:
                            try:
                                await fn(ch, mid, emoji)
                            except Exception as e:
                                logger.debug("Unreaction cleanup error %s: %s", emoji, e)
                asyncio.create_task(_do_clear())
            self._react_phase = 0
            self._react_msg_id = ""
            self._active_reactions = set()
            self._module_emote_count = 0

            if self._turn_log:
                last = self._turn_log[-1]
                if last.get("role") == "assistant":
                    if self._turn_modules_queried:
                        last["kb_modules"] = sorted(self._turn_modules_queried)
                    last["kb_grounded"] = self._turn_kb_query_used

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

        self._context_state = "restarting"
        if self._on_needs_restart:
            asyncio.create_task(self._on_needs_restart())

    def _build_options(self) -> ClaudeAgentOptions:
        import os

        if self.tier == "admin":
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task", "NotebookEdit",
            ]
            perm_mode = "acceptEdits"
            turn_limit = 200
        elif self.tier == "trusted":
            tools = ["Read", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
            perm_mode = "acceptEdits"
            turn_limit = 50
        else:
            tools = ["Read", "Bash", "Glob", "Grep"]
            perm_mode = "acceptEdits"
            turn_limit = 30

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

        self._tool_call_count += 1

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


# ---------------------------------------------------------------------------
# Session pool
# ---------------------------------------------------------------------------

class SessionPool:
    """Manages one Session per contact/channel."""

    def __init__(self, model: str, bot_name: str, on_text: Optional[Any] = None) -> None:
        self._model = model
        self._bot_name = bot_name
        self._on_text = on_text
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()          # guards _sessions dict reads/writes
        self._creation_locks: dict[str, asyncio.Lock] = {}  # per-key; serialises _create()

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
                "Core modules (query first): BUILDSCI, HVAC, ENERGY, SYSTEMS, ENVELOPE.\n"
                "Extended theorems (geometry, spatial) are available via --extended flag\n"
                "but cost more context. Only query extended when the question explicitly\n"
                "involves spatial reasoning, geometry proofs, or dimensional analysis."
            )
        except Exception as exc:
            logger.warning("FUSED-CORE system prompt index build failed: %s", exc)
            return ""

    def refresh_cache(self) -> None:
        """Re-read all cached prompt fragments. Wire to SIGHUP in main.py
        so live edits to SOUL.md / FUSED-CORE.md / MANIFEST.json take
        effect on the next _create() without a full process restart."""
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
        logger.info("SessionPool cache refreshed")

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
                session = None
            if key not in self._creation_locks:
                self._creation_locks[key] = asyncio.Lock()
            creation_lock = self._creation_locks[key]

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
                    session = await self._create(key, tier, platform, chat_id, is_group)
                    async with self._lock:
                        self._sessions[key] = session
                    is_new_session = True

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
        wrapped = (
            f"{context_block}"
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
        session.log_turn("user", text)
        session._tool_call_count = 0
        session._turn_modules_queried = set()
        session._turn_kb_query_used = False

        await session.inject(wrapped)

    async def _create(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool
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

        # Append cached knowledge index block (was a per-spawn JSON parse).
        if self._cached_kb_index_block:
            prompt += self._cached_kb_index_block

        claude_md.write_text(prompt)

        session_tier = "admin" if is_group else tier
        session = Session(
            key=key,
            tier=session_tier,
            cwd=str(transcript_dir),
            model=self._model,
            on_text=self._on_text,
            chat_id=chat_id,
        )
        await session.start()

        _key, _tier, _platform, _chat_id, _is_group = key, tier, platform, chat_id, is_group

        async def _restart_cb() -> None:
            await self._restart_session(_key, _tier, _platform, _chat_id, _is_group)

        session._on_needs_restart = _restart_cb

        # ── Startup injection: reasoning chain + reference block ──────────
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
                            "Do not respond to this injection."
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
                        "The above is your persistent memory. Treat all ## PERSISTENT entries as active context. Do not respond to this injection."
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
                "Do not respond to this injection."
            )
            logger.info("Reference block injected for %s (%d bytes, %d sections)",
                        key, len(combined_ref), len(ref_sections))

        return session

    async def _restart_session(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool
    ) -> None:
        logger.info("Restarting session %s after context refresh", key)
        async with self._lock:
            old_session = self._sessions.pop(key, None)
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
                new_session = await self._create(key, tier, platform, chat_id, is_group)
                async with self._lock:
                    self._sessions[key] = new_session
                logger.info("Session %s restarted successfully", key)
            except Exception as exc:
                logger.error("Failed to recreate session %s after restart: %s", key, exc)

    async def stop_all(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.stop()
            self._sessions.clear()

