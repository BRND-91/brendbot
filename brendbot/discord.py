"""Discord listener using discord.py."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import discord
import httpx

logger = logging.getLogger(__name__)

ATTACHMENTS_DIR = Path(__file__).parent.parent / "discord-attachments"
MAX_IMAGE_SIZE = 20 * 1024 * 1024

type MessageCallback = Callable[..., Coroutine[Any, Any, None]]


class DiscordListener:
    """Connects to Discord and forwards messages to the session backend."""

    def __init__(self, token: str, on_message: MessageCallback) -> None:
        self._token = token
        self._on_message = on_message
        self._client: discord.Client | None = None
        self._ready = asyncio.Event()

    async def run(self) -> None:
        """Start the Discord bot. Blocks until disconnected."""
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready() -> None:
            logger.info(
                "Bot connected as %s (id=%s)",
                client.user,
                client.user.id if client.user else "?",
            )
            guilds = [g.name for g in client.guilds]
            logger.info("In %d server(s): %s", len(guilds), ", ".join(guilds))
            self._ready.set()

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == client.user:
                return

            # In servers, ignore other bots unless they @mention us
            if message.guild and message.author.bot:
                mentions_us = client.user and client.user.id in [
                    m.id for m in message.mentions
                ]
                if not mentions_us:
                    return

            text = message.content or ""

            # Download and format attachments
            att_text = await _format_attachments(message.attachments)
            if text and att_text:
                text = text + att_text
            elif att_text:
                text = "(attachment)" + att_text

            if not text:
                return

            sender_id = str(message.author.id)
            chat_id = str(message.channel.id)
            is_dm = not message.guild
            platform = "discord_dm" if is_dm else "discord"

            logger.info(
                "%s from %s in #%s: %r",
                "DM" if is_dm else "Message",
                message.author.display_name,
                getattr(message.channel, "name", "DM"),
                text[:80],
            )

            # Extract reply context
            reply_to_id = ""
            reply_to_text = ""
            reply_to_author = ""
            if message.reference and message.reference.message_id:
                reply_to_id = str(message.reference.message_id)
                ref = message.reference.cached_message
                if ref is None:
                    try:
                        ref = await message.channel.fetch_message(
                            message.reference.message_id
                        )
                    except Exception:
                        ref = None
                if ref:
                    reply_to_text = (ref.content or "")[:500]
                    reply_to_author = str(ref.author.id)

            await self._on_message(
                platform,
                sender_id,
                chat_id,
                text,
                str(message.id),
                reply_to_id=reply_to_id,
                reply_to_text=reply_to_text,
                reply_to_author=reply_to_author,
            )

        @client.event
        async def on_disconnect() -> None:
            logger.warning("Disconnected from Discord")
            self._ready.clear()

        @client.event
        async def on_resumed() -> None:
            logger.info("Reconnected to Discord")
            self._ready.set()

        logger.info("Starting Discord bot...")
        try:
            await client.start(self._token)
        except discord.LoginFailure:
            logger.error("Invalid bot token! Check your .env file.")
            raise
        except asyncio.CancelledError:
            await client.close()
            await asyncio.sleep(0.5)
            raise


async def _download_attachment(att: discord.Attachment) -> str | None:
    """Download a Discord attachment to local disk."""
    if att.size > MAX_IMAGE_SIZE:
        return None

    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(att.filename).suffix if att.filename else ""
    local_path = ATTACHMENTS_DIR / f"{att.id}{suffix}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(att.url)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
        return str(local_path)
    except Exception as e:
        logger.warning("Failed to download %s: %s", att.filename, e)
        return None


async def _format_attachments(attachments: list[discord.Attachment]) -> str:
    """Format Discord attachments into text for Claude."""
    if not attachments:
        return ""

    lines = ["", "ATTACHMENTS:"]
    for att in attachments:
        size = att.size
        if size >= 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f}MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.0f}KB"
        else:
            size_str = f"{size}B"

        lines.append(f"  - {att.filename} ({att.content_type or 'unknown'}, {size_str})")

        is_image = att.content_type and att.content_type.startswith("image/")
        if is_image and att.size <= MAX_IMAGE_SIZE:
            local_path = await _download_attachment(att)
            if local_path:
                lines.append(f"    Path: {local_path}")
            else:
                lines.append(f"    URL: {att.url}")
        else:
            lines.append(f"    URL: {att.url}")

    if any(att.content_type and att.content_type.startswith("image/") for att in attachments):
        lines.append("  You can view images using the Read tool on the paths above.")

    return "\n".join(lines)
