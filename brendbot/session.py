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

# FUSED-CORE knowledge loader removed — knowledge access now via SQLite (kb-query).
# The old loader baked all module content into CLAUDE.md; the new approach uses
# targeted queries that return ~200 bytes instead of 11KB per module.

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"

# ---------------------------------------------------------------------------
# Haiku ambiguity classifier (local heuristic — no API call)
# ---------------------------------------------------------------------------

# Signals that suggest engagement even when domain keywords are absent.
_ENGAGE_PATTERNS = (
    # Direct address or name
    "brend", "brendbot",
    # Conversational openers likely directed at the bot
    "what do you", "what do you think", "do you think", "do you know",
    "can you", "could you", "would you", "tell me", "explain",
    "what is", "what are", "how do", "how does", "why does", "why is",
    "is it true", "is that", "have you", "did you",
    # Soft requests
    "help me", "help with", "show me", "give me", "find me",
    "check ", "look at", "read this", "fix this",
)

# Patterns that are almost never worth engaging with.
_NOISE_PATTERNS = (
    "lol", "lmao", "haha", "hehe", "omg", "wtf", "brb", "gg", "oof",
    "💀", "😂", "🤣", "👍", "❤️",
)


async def haiku_classify(payload: dict) -> dict:
    """
    Second-pass engagement classifier via Claude CLI (OAuth, no API key charge).

    Returns {"engage": bool, "reason": str}.
    """
    text = payload.get("message", "")
    recent = payload.get("recent_context", [])
    context_lines = "\n".join(
        f"{m.get('display_name', 'unknown')}: {m.get('text', '')}"
        for m in recent[-5:]
    )
    prompt = (
        "Should brendbot respond to this Discord message? "
        "YES if: directly addresses brendbot/brend, asks a domain question "
        "(building science, logic, stats, systems), or continues an active conversation. "
        "NO if: casual chat between others, noise, messages to other bots. "
        f"Context:\n{context_lines}\nNew message: {text}\n"
        "Reply YES or NO only."
    )
    try:
        proc = await asyncio.subprocess.create_subprocess_exec(
            "claude", "-m", "claude-haiku-4-5", "-p", prompt,
            "--max-turns", "1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        answer = stdout.decode().strip().upper()
        engage = "YES" in answer[:10]
        return {"engage": engage, "reason": f"haiku:{answer[:20]}"}
    except Exception as e:
        logger.warning("haiku_classify error: %s", e)
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
# 400_000 = 40% of Claude Sonnet's 1M context window — fires before
# "lost in the middle" degradation degrades response quality.
_CONTEXT_REFRESH_THRESHOLD = 400_000

# Soft warning threshold — bot is told to stop reading modules and work
# from what's already in context.
_CONTEXT_SOFT_WARNING = 320_000

# Max turns to retain in the rolling turn log.
# At refresh, the last _MAX_TURN_LOG turns are written to CONTEXT_SUMMARY.md
# so they survive ice picks and session restarts.
_MAX_TURN_LOG = 30

# Hard cap on Bash calls within a single user-message turn.
# Prevents runaway tool loops from spiking context into restart territory.
_TOOL_CALL_BUDGET = 8

# Write a rolling checkpoint every N completed turns so manual ice picks
# don't wipe session context. At 5 turns × ~800 chars each = ~4KB max.
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
    ) -> None:
        self.key = key
        self.tier = tier
        self.cwd = cwd
        self._model = model
        self._on_text = on_text  # async callable(chat_id, text) or None
        self._client: Optional[ClaudeSDKClient] = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._error_count = 0
        self.running = False
        self.current_sender_tier: str | None = None
        self._last_input_tokens: int = 0
        # Context refresh state machine.
        # "normal"         → no refresh needed
        # "threshold_hit"  → threshold exceeded; raw log written, restart triggered
        # "restarting"     → restart triggered; terminal for this session instance
        self._context_state: str = "normal"
        self._soft_warning_sent: bool = False  # True after 80K warning injected
        self._on_needs_restart: Optional[Any] = None  # async callable() — set by SessionPool
        # Reaction tracking — set per turn, cleared on ResultMessage
        self._react_fn: Optional[Any] = None   # async callable(channel_id, msg_id, emoji)
        self._react_channel: str = ""
        self._react_msg_id: str = ""
        # Phase-based reaction (direct mentions only, fires once per turn):
        # 👁️  immediate on message receipt (seen)
        # 🧠  first user-facing tool call (working)
        # ✅  ResultMessage after any user-facing work (done)
        # Housekeeping-only turns: just 👁️, no further emoji
        self._react_phase: int = 0
        # Housekeeping path fragments — Read/Grep/Glob on these = no emoji
        self._HOUSEKEEPING_PATH_FRAGMENTS = frozenset({
            "session.py", "discord.py", "__init__.py",
            "CLAUDE.md", "MEMORY.md", "ai-image-shortfalls.md",
            "CONTEXT_SUMMARY.md",
        })
        # Knowledge module → reaction emoji mapping
        self._MODULE_EMOTES: dict[str, str] = {
            "LOGIC":       "🔣",
            "STATS":       "📊",
            "SYSTEMS":     "⚙️",
            "PERSONALITY": "🫀",
            "BUILDSCI":    "🏗️",
            "GOVERNANCE":  "⚖️",
            "IMAGEGEN":    "🎨",
        }
        self._NEUTRAL_EMOTE = "⬛"   # shown when >3 modules are loaded
        self._MAX_MODULE_EMOTES = 3
        # Per-turn reaction state
        self._unreact_fn: Optional[Any] = None  # async callable(channel_id, msg_id, emoji)
        self._active_reactions: set[str] = set()
        self._module_emote_count: int = 0
        # Rolling turn log: list of {"role": str, "text": str} dicts.
        # Capped at _MAX_TURN_LOG entries. Written to CONTEXT_SUMMARY.md on
        # MEMORY refresh so turns survive ice picks and session restarts.
        self._turn_log: list[dict] = []
        # Per-turn Bash call budget. Reset on each incoming user message.
        # Prevents runaway tool loops from ballooning context.
        self._tool_call_count: int = 0
        # Completed turn counter — drives rolling checkpoint writes.
        self._completed_turn_count: int = 0

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

    def log_turn(self, role: str, text: str, max_chars: int = 800) -> None:
        """Append a turn to the rolling log. Truncates long turns. Trims to _MAX_TURN_LOG."""
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
                # Let the SDK handle graceful shutdown → SIGTERM → SIGKILL.
                # Do NOT manually call process.terminate() first — that races
                # with the message reader and produces spurious exit-143 errors.
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
                    # Session is about to be replaced — drop all queued messages.
                    continue
                try:
                    assert self._client is not None
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
                # Capture state before _handle() to drive context refresh transitions below.
                prior_context_state = self._context_state
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
                        # Threshold exceeded — write raw log and restart immediately.
                        await self._trigger_clean_restart()
        except asyncio.CancelledError:
            pass
        except ProcessError as e:
            # SIGTERM (143) and SIGKILL (137) are expected exit codes from our
            # own shutdown path — not real crashes. Log at debug, not error.
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
                    # Externalize thinking to a persistent log file so reasoning
                    # is visible outside the subprocess context.
                    try:
                        import datetime
                        thoughts_path = Path(self.cwd) / "thoughts.log"
                        ts = datetime.datetime.now().isoformat(timespec="seconds")
                        with thoughts_path.open("a") as f:
                            f.write(f"\n--- [{ts}] turn {self._completed_turn_count} ---\n")
                            f.write(block.thinking)
                            f.write("\n")
                        logger.debug("[%s] thinking block written to thoughts.log", self.key)
                    except Exception as exc:
                        logger.warning("[%s] thoughts.log write failed: %s", self.key, exc)
                elif isinstance(block, TextBlock):
                    logger.info("[%s] %s", self.key, block.text[:200])
                    self.log_turn("assistant", block.text)
                elif isinstance(block, ToolUseBlock):
                    logger.info("[%s] tool: %s", self.key, block.name)
                    pass  # Reactions disabled by admin directive.
        elif isinstance(message, ResultMessage):
            cost = f" (${message.total_cost_usd:.4f})" if message.total_cost_usd else ""
            # Extract full context size from usage metadata.
            # input_tokens alone is only the non-cached portion — real context size
            # requires summing all three: input + cache_read + cache_creation.
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
                        logger.info(
                            "[%s] context at %d tokens — soft warning threshold",
                            self.key, self._last_input_tokens,
                        )
            logger.info(
                "[%s] turn complete%s (context_tokens=%d)",
                self.key, cost, self._last_input_tokens,
            )
            # Write token status file so the bot can read its own context usage.
            try:
                import pathlib
                status_path = pathlib.Path(self.cwd) / "context_status.txt"
                pct = round(self._last_input_tokens / 1_000_000 * 100, 1)
                status_path.write_text(f"{self._last_input_tokens} {pct}\n")
            except Exception:
                pass
            # Clear all reactions from this turn (clean slate on completion)
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
            # Clear turn state
            self._react_phase = 0
            self._react_msg_id = ""
            self._active_reactions = set()
            self._module_emote_count = 0
            # Context refresh state transition handled in _receive_loop after _handle() returns.

    def set_reaction_target(
        self,
        react_fn: Any,
        unreact_fn: Any,
        channel_id: str,
        message_id: str,
    ) -> None:
        """Register the Discord message to react to for the upcoming turn."""
        self._react_fn = react_fn
        self._unreact_fn = unreact_fn
        self._react_channel = channel_id
        self._react_msg_id = message_id
        self._react_phase = 0
        self._active_reactions = set()
        self._module_emote_count = 0

    async def _react(self, emoji: str) -> None:
        """Fire a single reaction if target is set, tracking it in _active_reactions."""
        if self._react_fn and self._react_channel and self._react_msg_id:
            try:
                await self._react_fn(self._react_channel, self._react_msg_id, emoji)
                self._active_reactions.add(emoji)
            except Exception as e:
                logger.debug("Reaction error: %s", e)

    async def _unreact(self, emoji: str) -> None:
        """Remove a reaction if unreact_fn is set."""
        if self._unreact_fn and self._react_channel and self._react_msg_id:
            try:
                await self._unreact_fn(self._react_channel, self._react_msg_id, emoji)
                self._active_reactions.discard(emoji)
            except Exception as e:
                logger.debug("Unreaction error: %s", e)

    def _is_housekeeping_tool_call(self, tool_name: str, tool_input: dict) -> bool:
        """Return True if this tool call is internal housekeeping (suppress progress emoji)."""
        # These tools are always user-facing
        if tool_name in ("Bash", "Write", "Edit", "WebSearch", "WebFetch", "NotebookEdit"):
            return False
        # Read/Grep/Glob: housekeeping if path targets own config/source files
        path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or tool_input.get("pattern", "")
            or ""
        )
        return any(frag in path for frag in self._HOUSEKEEPING_PATH_FRAGMENTS)

    async def _advance_reaction_phase(self, tool_name: str, tool_input: dict) -> None:
        """Update reactions based on tool type.

        Reaction lifecycle (direct mentions only):
          👁️  immediate at message receipt (fires in route_message)
          Module emotes (🔣📊⚙️🫀🏗️⚖️🎨) — up to _MAX_MODULE_EMOTES stacked
          ⬛  if >3 distinct modules loaded (replaces all module emotes)
          🧠  non-module user-facing tool work
          On turn complete: all reactions removed (clean slate)
          Housekeeping-only turns: just 👁️, removed on complete
        """
        if not self._react_msg_id:
            return
        if self._is_housekeeping_tool_call(tool_name, tool_input):
            return

        self._react_phase = 1  # mark user-facing work happened

        # Detect knowledge module read
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
                return  # already shown, idempotent
            if self._NEUTRAL_EMOTE in self._active_reactions:
                return  # already in overflow state
            if self._module_emote_count < self._MAX_MODULE_EMOTES:
                self._module_emote_count += 1
                await self._react(module_emote)
            else:
                # >3 modules: clear individual emotes, show neutral
                for emote in list(self._active_reactions):
                    if emote in self._MODULE_EMOTES.values():
                        await self._unreact(emote)
                await self._react(self._NEUTRAL_EMOTE)
        else:
            # Non-module user-facing work
            if "🧠" not in self._active_reactions:
                await self._react("🧠")

    def _write_checkpoint(self) -> None:
        """Write rolling checkpoint to CONTEXT_SUMMARY.md.

        Called every _CHECKPOINT_INTERVAL completed turns so manual ice picks
        always find a recent checkpoint on disk. Same path as the auto-restart
        write, same path as the init read — no ambiguity.
        """
        if not self._turn_log:
            return
        try:
            summary_path = Path(self.cwd) / "CONTEXT_SUMMARY.md"
            lines = [f"# CONTEXT SUMMARY — last {len(self._turn_log)} turns\n",
                     f"Written at context_tokens={self._last_input_tokens}\n\n"]
            for entry in self._turn_log:
                role = entry.get("role", "unknown")
                text = entry.get("text", "")
                lines.append(f"[{role}] {text}\n\n")
            summary_path.write_text("".join(lines))
            logger.info("[%s] checkpoint written (%d turns, turn=%d)",
                        self.key, len(self._turn_log), self._completed_turn_count)
        except Exception as exc:
            logger.warning("[%s] checkpoint write failed: %s", self.key, exc)

    async def _trigger_clean_restart(self) -> None:
        """Write raw turn log and restart immediately. No summarization turn.

        The old approach asked the bot to summarize while at 120K+ tokens —
        this pushed context to 150-170K and produced degraded output.
        Now: write the raw turn log (already capped at _MAX_TURN_LOG × 800 chars)
        and restart. The new session picks up CONTEXT_SUMMARY.md on init.
        """
        if self._turn_log:
            try:
                summary_path = Path(self.cwd) / "CONTEXT_SUMMARY.md"
                lines = [f"# CONTEXT SUMMARY — last {len(self._turn_log)} turns\n",
                         f"Written at context_tokens={self._last_input_tokens}\n\n"]
                for entry in self._turn_log:
                    role = entry.get("role", "unknown")
                    text = entry.get("text", "")
                    lines.append(f"[{role}] {text}\n\n")
                summary_path.write_text("".join(lines))
                logger.info("[%s] CONTEXT_SUMMARY.md written (%d turns) — triggering clean restart",
                            self.key, len(self._turn_log))
            except Exception as exc:
                logger.warning("[%s] CONTEXT_SUMMARY write failed: %s", self.key, exc)

        self._context_state = "restarting"
        if self._on_needs_restart:
            asyncio.create_task(self._on_needs_restart())

    def _build_options(self) -> ClaudeAgentOptions:
        import os

        is_root = os.getuid() == 0

        if self.tier == "admin":
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task", "NotebookEdit",
            ]
            # acceptEdits on all platforms — bypassPermissions skips can_use_tool
            # entirely, which means the per-turn tool budget never fires.
            # acceptEdits triggers the callback for non-edit tools (Bash, etc.)
            # so the budget is enforced. The callback returns Allow for admin
            # unless the budget is exceeded.
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
            max_thinking_tokens=8000,
            env=env,
        )

        opts.can_use_tool = self._permission_check
        opts.extra_args = {"session-id": str(uuid.uuid4())}
        return opts

    async def _permission_check(
        self, tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        # Block all tiers from editing CLAUDE.md
        if tool_name in ("Write", "Edit"):
            path = tool_input.get("file_path", "")
            if "CLAUDE.md" in path:
                return PermissionResultDeny(
                    message="CLAUDE.md is managed by admin only"
                )

        # Per-turn total tool call budget — applies to all tiers.
        # Covers Bash, Glob, Grep, Read, Edit, and all others.
        # Prevents runaway tool chains from spiking context into restart territory.
        self._tool_call_count += 1
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
        self._lock = asyncio.Lock()

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
    ) -> None:
        """Route a message to the right session."""
        key = f"{platform}:{chat_id}" if is_group else f"{platform}:{sender_id}"

        async with self._lock:
            session = self._sessions.get(key)
            if session is not None and not session.is_alive():
                logger.warning("Dead session for %s, recreating", key)
                await session.stop()
                del self._sessions[key]
                session = None
            is_new_session = session is None
            if session is None:
                session = await self._create(key, tier, platform, chat_id, is_group)
                self._sessions[key] = session

        # Build optional context block from recent channel messages.
        # Only injected on session start — subsequent turns already have
        # this content in conversation history. Injecting every turn was
        # causing O(n²) token growth (20-message block * n turns).
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

        # Wrap message with metadata
        msg_id_attr = f" message_id='{message_id}'" if message_id else ""
        reply_block = ""
        if reply_to_id:
            reply_block = (
                f"\n<reply_to message_id={quoteattr(reply_to_id)} "
                f"sender={quoteattr(reply_to_author)}>"
                f"{escape(reply_to_text)}</reply_to>"
            )
        type_attr = " type='group'" if is_group else ""
        wrapped = (
            f"{context_block}"
            f"<message platform='{platform}' sender='{sender_id}' "
            f"chat='{chat_id}' tier='{tier}'{type_attr}{msg_id_attr}"
            f" context_tokens='{session._last_input_tokens}'>"
            f"{reply_block}\n{text}\n</message>"
        )

        session.current_sender_tier = tier

        # Log user turn for cross-reset continuity
        session.log_turn("user", text)

        # Reset per-turn tool call budget for the incoming message.
        session._tool_call_count = 0

        # Reactions disabled by admin directive.
        # set_reaction_target / _react calls removed.

        await session.inject(wrapped)

    async def _create(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool
    ) -> Session:
        # Create transcript directory
        safe_id = chat_id.replace("+", "_").replace("/", "_").replace("=", "")
        prefix = "group_" if is_group else ""
        transcript_dir = TRANSCRIPTS_DIR / platform / f"{prefix}{safe_id}"
        transcript_dir.mkdir(parents=True, exist_ok=True)

        # Write CLAUDE.md if it doesn't exist
        claude_md = transcript_dir / "CLAUDE.md"
        # Always regenerate CLAUDE.md from template so behavioral changes
        # in GROUP_SOUL.md / SOUL.md propagate without manual cleanup.
        send_cmd = f'{SCRIPTS_DIR}/send-discord "{chat_id}" "<message>"'
        template_file = "GROUP_SOUL.md" if is_group else "SOUL.md"
        template = _load_template(template_file)
        prompt = _render(template, {
            "bot_name": self._bot_name,
            "send_command": send_cmd,
            "platform_name": "Discord",
        })
        claude_md.write_text(prompt)

        # For group sessions, use admin tier (bot owner controls the server)
        session_tier = "admin" if is_group else tier
        session = Session(
            key=key,
            tier=session_tier,
            cwd=str(transcript_dir),
            model=self._model,
            on_text=self._on_text,
        )
        await session.start()

        # Wire restart callback: when context overflows, session summarizes and
        # triggers pool to kill/recreate it with a clean context.
        _key, _tier, _platform, _chat_id, _is_group = key, tier, platform, chat_id, is_group

        async def _restart_cb() -> None:
            await self._restart_session(_key, _tier, _platform, _chat_id, _is_group)

        session._on_needs_restart = _restart_cb

        # Inject fragmented memory — essential files loaded, rest indexed for lazy-load.
        # Falls back to monolithic MEMORY.md if memory/ dir doesn't exist yet.
        memory_dir = transcript_dir / "memory"
        if memory_dir.is_dir():
            # Essential fragments: always loaded on start (~3-4KB total)
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

            # Index of remaining fragments for on-demand query via kb-query memory
            other_frags = [
                f for f in sorted(memory_dir.iterdir())
                if f.suffix == ".md" and f.name not in _ESSENTIAL_FRAGMENTS
            ]
            if other_frags:
                index_lines = [f"- {f.stem}: {f}" for f in other_frags]
                await session.inject(
                    "<memory-index>\n" + "\n".join(index_lines) + "\n</memory-index>\n"
                    "Additional memory fragments are available on disk. "
                    "Use `kb-query memory <source>` to load the relevant fragment when a question touches that domain "
                    "(e.g., `kb-query memory buildsci`, `kb-query memory tools`, `kb-query memory bots`). "
                    "Do not Read .md files directly — use kb-query memory for indexed fragments. "
                    "Do not load all fragments — only what the current question requires. "
                    "Do not respond to this injection."
                )
        else:
            # Fallback: monolithic MEMORY.md (pre-migration sessions)
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

        # Inject CONTEXT_SUMMARY.md if present — carries rolling turn log from prior session.
        context_summary = transcript_dir / "CONTEXT_SUMMARY.md"
        if context_summary.exists():
            try:
                summary_content = context_summary.read_text()
                await session.inject(
                    f"<context-summary>\n{summary_content}\n</context-summary>\n"
                    "The above is a summary of the most recent conversation turns from before this session started. "
                    "Use it to maintain continuity. Do not respond to this injection."
                )
                logger.info("CONTEXT_SUMMARY.md injected for %s", key)
                # File is NOT deleted — next checkpoint write will overwrite it.
                # Deleting here created a gap window: manual ice picks between
                # deletion and next checkpoint write started cold.
            except Exception as exc:
                logger.warning("CONTEXT_SUMMARY.md injection failed: %s", exc)

        # Inject FUSED-CORE knowledge index.
        # Primary access: kb-query via Bash (SQLite, ~200 bytes per query).
        # Fallback: Read the JSON file (only for IMAGEGEN which has non-standard structure).
        knowledge_dir = PROJECT_ROOT / "brendbot" / "knowledge"
        kb_query_path = SCRIPTS_DIR / "kb-query"
        manifest_path = knowledge_dir / "MANIFEST.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
                load_order = manifest.get("load_order", [])
                module_map = {m["id"]: m for m in manifest.get("modules", [])}
                index_lines = []
                for module_id in load_order:
                    desc = module_map.get(module_id, {}).get("desc", "")
                    index_lines.append(f"- {module_id}: {desc}")
                if index_lines:
                    index = "\n".join(index_lines)
                    await session.inject(
                        "<fused-core-index>\n" + index + "\n</fused-core-index>\n"
                        + "<knowledge-query-tool>\n"
                        + f"Use `{kb_query_path}` via Bash to query knowledge modules. Commands:\n"
                        + f"  {kb_query_path} search \"<terms>\"      — full-text search across all modules\n"
                        + f"  {kb_query_path} defs <MODULE>          — list definitions\n"
                        + f"  {kb_query_path} facts <MODULE> [topic] — list facts (optionally by topic)\n"
                        + f"  {kb_query_path} thms <MODULE>          — list theorems\n"
                        + f"  {kb_query_path} def <ID>               — look up a specific definition\n"
                        + f"  {kb_query_path} topics <MODULE>        — list available fact topics\n"
                        + f"  {kb_query_path} gates                  — list governance gates\n"
                        + "Each query returns ~200 bytes (vs 11KB for a full JSON read). "
                        + "Use kb-query instead of Read for all knowledge lookups. "
                        + "Exception: IMAGEGEN.json — read via Read tool (non-standard structure).\n"
                        + "</knowledge-query-tool>\n"
                        + "Do not answer from training weights when a module is available. Do not respond to this injection."
                    )
            except Exception as exc:
                logger.warning("FUSED-CORE index injection failed: %s", exc)

        return session

    async def _restart_session(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool
    ) -> None:
        """Kill the current session and recreate it with a clean context.

        Called after a successful summarization turn. The new session picks up
        CONTEXT_SUMMARY.md and MEMORY.md on init, giving it continuity with a
        fraction of the original token cost.
        """
        logger.info("Restarting session %s after context refresh", key)
        async with self._lock:
            old_session = self._sessions.pop(key, None)
            if old_session:
                try:
                    await old_session.stop()
                except Exception as exc:
                    logger.warning("Error stopping session %s during restart: %s", key, exc)
            try:
                new_session = await self._create(key, tier, platform, chat_id, is_group)
                self._sessions[key] = new_session
                logger.info("Session %s restarted successfully", key)
            except Exception as exc:
                logger.error("Failed to recreate session %s after restart: %s", key, exc)

    async def stop_all(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.stop()
            self._sessions.clear()
