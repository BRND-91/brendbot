"""Claude Agent SDK session manager.

Each contact/channel gets one persistent Claude session. The SDK spawns a
Claude Code subprocess — no API key needed, uses OAuth via `claude login`.
"""

from __future__ import annotations

import asyncio
import logging
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
    ResultMessage,
    SystemMessage,
    TextBlock,
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


def _load_template(name: str) -> str:
    """Load a soul template file."""
    path = PROJECT_ROOT / name
    if path.exists():
        return path.read_text()
    return f"You are a helpful Discord bot.\nTo reply, run: {{{{ send_command }}}}"


def _render(template: str, variables: dict[str, str]) -> str:
    """Simple {{ var }} substitution."""
    def replacer(m: re.Match) -> str:
        return variables.get(m.group(1).strip(), m.group(0))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, template)


# ---------------------------------------------------------------------------
# Single SDK session
# ---------------------------------------------------------------------------

class Session:
    """One Claude Agent SDK session for a contact or channel."""

    def __init__(
        self,
        key: str,
        tier: str,
        cwd: str,
        model: str = "sonnet",
    ) -> None:
        self.key = key
        self.tier = tier
        self.cwd = cwd
        self._model = model
        self._client: Optional[ClaudeSDKClient] = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._error_count = 0
        self.running = False
        self.current_sender_tier: str | None = None

    async def start(self) -> None:
        opts = self._build_options()
        self._client = ClaudeSDKClient(options=opts)
        await self._client.connect()
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

    def is_alive(self) -> bool:
        return self.running and self._task is not None and not self._task.done()

    async def _kill(self) -> None:
        if not self._client:
            return
        try:
            transport = getattr(self._client, "_transport", None)
            if transport:
                process = getattr(transport, "_process", None)
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        process.kill()
                if hasattr(transport, "close"):
                    close_result = transport.close()
                    if asyncio.iscoroutine(close_result):
                        await close_result
                    await asyncio.sleep(0.1)
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
                try:
                    self._handle(message)
                except Exception as e:
                    logger.exception("Error handling message: %s", e)
                if isinstance(message, ResultMessage):
                    self._error_count = 0
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Session %s receiver error: %s", self.key, e)
            self.running = False

    def _handle(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    logger.info("[%s] %s", self.key, block.text[:200])
                elif isinstance(block, ToolUseBlock):
                    logger.info("[%s] tool: %s", self.key, block.name)
        elif isinstance(message, ResultMessage):
            cost = f" (${message.total_cost_usd:.4f})" if message.total_cost_usd else ""
            logger.info("[%s] turn complete%s", self.key, cost)

    def _build_options(self) -> ClaudeAgentOptions:
        if self.tier == "admin":
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task", "NotebookEdit",
            ]
            perm_mode = "bypassPermissions"
            turn_limit = 200
        elif self.tier == "trusted":
            tools = ["Read", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
            perm_mode = "default"
            turn_limit = 50
        else:
            tools = ["Read", "Bash", "Glob", "Grep"]
            perm_mode = "default"
            turn_limit = 30

        env: dict[str, str] = {}
        if self.tier == "admin":
            env["IS_SANDBOX"] = "1"

        opts = ClaudeAgentOptions(
            cwd=self.cwd,
            allowed_tools=tools,
            permission_mode=perm_mode,
            setting_sources=["project"],
            model=self._model,
            fallback_model="sonnet",
            max_turns=turn_limit,
            max_buffer_size=10 * 1024 * 1024,
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

    def __init__(self, model: str, bot_name: str) -> None:
        self._model = model
        self._bot_name = bot_name
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
            if session is None:
                session = await self._create(key, tier, platform, chat_id, is_group)
                self._sessions[key] = session

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
            f"<message platform='{platform}' sender='{sender_id}' "
            f"chat='{chat_id}' tier='{tier}'{type_attr}{msg_id_attr}>"
            f"{reply_block}\n{text}\n</message>"
        )

        session.current_sender_tier = tier
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
        if not claude_md.exists():
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
        )
        await session.start()
        return session

    async def stop_all(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.stop()
            self._sessions.clear()
