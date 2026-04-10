"""Discord listener using discord.py."""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import discord
import httpx

from brendbot.config import get_config

logger = logging.getLogger(__name__)

ATTACHMENTS_DIR = Path(__file__).parent.parent / "discord-attachments"
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
MAX_IMAGE_SIZE = 20 * 1024 * 1024

_SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".xlsb"}
_SPREADSHEET_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.ms-excel.sheet.macroenabled.12",
}
RECENCY_WINDOW_SECONDS = 300  # 5 minutes
CONTEXT_BUFFER_SIZE = 20  # messages per channel

type MessageCallback = Callable[..., Coroutine[Any, Any, None]]

# Channel state: tracks last time bot spoke per channel
_channel_last_spoke: dict[str, float] = {}

# Per-channel rolling buffer — seeded from Discord API on first message, then appended in-memory
_channel_context: dict[str, deque] = {}
_channel_seeded: set[str] = set()  # channels whose buffers have been seeded from API

# Module-level client reference for send_message and seeding
_discord_client: discord.Client | None = None

async def _haiku_gatecheck(text: str, context: list[dict]) -> bool:
    """
    Lightweight ambiguity classifier.
    Returns True if the message should escalate to full Claude.
    """
    recent = context[-5:] if context else []

    try:
        from brendbot.session import haiku_classify
        decision = await haiku_classify({
            "message": text,
            "recent_context": recent,
        })
        logger.info("Haiku gate: %s (message: %r)", decision.get("reason", "unknown"), text[:50])
        return bool(decision.get("engage", False))
    except Exception as e:
        logger.warning("Haiku gate failed: %s", e)
        return False


async def react_to_message(channel_id: str, message_id: str, emoji: str) -> None:
    """Add a reaction to a Discord message."""
    if _discord_client is None:
        return
    try:
        channel = _discord_client.get_channel(int(channel_id))
        if channel is None:
            channel = await _discord_client.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        await msg.add_reaction(emoji)
        logger.debug("Reacted to %s with %s", message_id, emoji)
    except Exception as e:
        logger.warning("Failed to react to %s: %s", message_id, e)


async def remove_reaction(channel_id: str, message_id: str, emoji: str) -> None:
    """Remove the bot's own reaction from a Discord message."""
    if _discord_client is None:
        return
    try:
        channel = _discord_client.get_channel(int(channel_id))
        if channel is None:
            channel = await _discord_client.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        await msg.remove_reaction(emoji, _discord_client.user)
        logger.debug("Removed reaction %s from %s", emoji, message_id)
    except Exception as e:
        logger.warning("Failed to remove reaction from %s: %s", message_id, e)


async def send_message(channel_id: str, text: str) -> None:
    """Send a message to a Discord channel by ID."""
    if _discord_client is None:
        logger.warning("send_message called before client is ready")
        return
    channel = _discord_client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await _discord_client.fetch_channel(int(channel_id))
        except Exception as e:
            logger.error("Could not fetch channel %s: %s", channel_id, e)
            return
    try:
        for chunk in [text[i:i+2000] for i in range(0, len(text), 2000)]:
            await channel.send(chunk)
        record_bot_spoke(channel_id)
    except Exception as e:
        logger.error("Failed to send message to %s: %s", channel_id, e)


def _load_domain_keywords() -> frozenset[str]:
    """Build a keyword set from knowledge module descriptions and IDs."""
    keywords: set[str] = set()

    # Explicit human-readable terms per domain
    static_terms = [
        # LOGIC
        "logic", "argument", "proof", "valid", "premise", "conclusion",
        "inference", "reasoning", "deduction", "induction", "theorem",
        "proposition", "syllogism", "formal language", "predicate",
        # STATS
        "probability", "statistics", "distribution", "bayesian", "random variable",
        "variance", "likelihood", "confidence interval", "regression", "correlation",
        "bayes", "bernoulli", "normal distribution", "standard deviation",
        # SYSTEMS
        "systems thinking", "feedback loop", "feedback", "tipping point",
        "oscillation", "overshoot", "stocks and flows", "emergence", "complexity",
        "pareto", "ashby", "goodhart", "diminishing returns", "delay",
        "leadership", "servant leadership", "situational leadership",
        "emotional intelligence", "forrester",
        # PERSONALITY
        "empathy", "interpersonal", "mediation", "therapy", "group dynamics",
        "respect", "boundary", "calibrate", "risk gate",
        # BUILDSCI
        "building science", "insulation", "hvac", "air barrier", "vapor barrier",
        "moisture", "thermal", "enclosure", "ventilation", "infiltration",
        "r-value", "blower door", "energy efficiency", "condensation",
        "building envelope", "duct", "mechanical ventilation", "combustion",
        "indoor air quality", "iaq", "heat loss", "heat gain", "air sealing",
        "hrv", "erv", "radiant", "conduction", "convection", "latent heat",
        "sensible heat", "dew point", "relative humidity",
        "fiberglass", "attic", "crawlspace", "pest", "shell tightening",
        "air leakage", "weatherization", "envelope", "rim joist", "slab",
    ]
    keywords.update(static_terms)

    # Supplement from manifest module descriptions
    try:
        manifest_path = KNOWLEDGE_DIR / "MANIFEST.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            for module in manifest.get("modules", []):
                desc = module.get("desc", "").lower()
                # Extract multi-word and single meaningful terms
                words = [w.strip(",.():") for w in desc.split() if len(w) > 4]
                keywords.update(words)
    except Exception as e:
        logger.warning("Failed to load manifest keywords: %s", e)

    return frozenset(keywords)


DOMAIN_KEYWORDS = _load_domain_keywords()


def _score_message(
    text: str,
    channel_id: str,
    is_reply_to_bot: bool,
    recent_context: list[dict] | None = None,
) -> float:
    """
    Score a message for engagement likelihood.

    Returns a float >= 0. Thresholds:
      >= 1.0 : high confidence, engage
      >= 0.4 : soft signal, engage
        < 0.4 : drop
    """
    score = 0.0
    text_lower = text.lower()
    word_count = len(text.split())

    # Direct reply to bot is a strong signal
    if is_reply_to_bot:
        score += 1.0

    # Recent thread participation lowers the bar — but only for messages with
    # enough content to be plausibly relevant. Single-word fragments ("huh?",
    # "lol", "ok") from non-addressed users are noise even in active threads.
    last_spoke = _channel_last_spoke.get(channel_id, 0.0)
    if time.time() - last_spoke < RECENCY_WINDOW_SECONDS and word_count >= 3:
        score += 0.3

    # Domain keyword match in current message
    for kw in DOMAIN_KEYWORDS:
        if kw in text_lower:
            score += 0.4
            break

    # Domain keyword match in recent context (conversation is on-topic even if this message isn't)
    if score < ENGAGE_THRESHOLD and recent_context:
        context_text = " ".join(m.get("text", "") for m in recent_context[-5:]).lower()
        for kw in DOMAIN_KEYWORDS:
            if kw in context_text:
                score += 0.3
                break

    # Conversational signal: question or direct address in an active thread
    # Only applied when recency is already contributing (bot recently spoke)
    last_spoke = _channel_last_spoke.get(channel_id, 0.0)
    if time.time() - last_spoke < RECENCY_WINDOW_SECONDS:
        _QUESTION_STARTERS = (
            "tell me", "what ", "why ", "how ", "who ", "when ", "where ",
            "do you", "can you", "would you", "could you", "are you",
            "is it", "have you", "did you", "does it", "will you",
        )
        _DIRECTIVE_STARTERS = (
            "operate", "respond", "explain", "describe", "show", "give",
            "list", "help", "stop", "start", "run", "check", "look",
            "find", "read", "write", "fix", "update", "act", "pretend",
        )
        is_conversational = (
            word_count >= 3 and (
                text_lower.endswith("?")
                or any(text_lower.startswith(s) for s in _QUESTION_STARTERS)
                or any(text_lower.startswith(s) for s in _DIRECTIVE_STARTERS)
            )
        )
        if is_conversational:
            score += 0.2

    return score


ENGAGE_THRESHOLD = 0.4


def record_bot_spoke(channel_id: str) -> None:
    """Call this after the bot sends a message to update recency state."""
    _channel_last_spoke[channel_id] = time.time()


async def _ensure_seeded(channel: discord.abc.Messageable, channel_id: str) -> deque:
    """Return the context buffer for a channel, seeding from Discord API if first use."""
    if channel_id not in _channel_seeded:
        buf: deque = deque(maxlen=CONTEXT_BUFFER_SIZE)
        try:
            history = []
            async for m in channel.history(limit=CONTEXT_BUFFER_SIZE):
                history.append(m)
            for m in reversed(history):
                seed_text = m.content or ""
                if m.attachments:
                    att_urls = " ".join(f"[attachment: {a.url}]" for a in m.attachments)
                    seed_text = (seed_text + " " + att_urls).strip()
                buf.append({
                    "sender_id": str(m.author.id),
                    "display_name": m.author.display_name,
                    "text": seed_text,
                    "message_id": str(m.id),
                    "timestamp": m.created_at.timestamp(),
                    "reply_to_id": str(m.reference.message_id) if m.reference else None,
                })
            logger.debug("Seeded context buffer for channel %s (%d msgs)", channel_id, len(buf))
        except Exception as e:
            logger.warning("Failed to seed context for channel %s: %s", channel_id, e)
            buf = deque(maxlen=CONTEXT_BUFFER_SIZE)
        _channel_context[channel_id] = buf
        _channel_seeded.add(channel_id)
    return _channel_context[channel_id]


class DiscordListener:
    """Connects to Discord and forwards messages to the session backend."""

    def __init__(self, token: str, on_message: MessageCallback) -> None:
        self._token = token
        self._on_message = on_message
        self._client: discord.Client | None = None
        self._ready = asyncio.Event()
        self.bot_id: str = ""  # Set on_ready, used for mention detection

    async def run(self) -> None:
        """Start the Discord bot. Blocks until disconnected."""
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        client = discord.Client(intents=intents)
        self._client = client

        global _discord_client
        _discord_client = client

        @client.event
        async def on_ready() -> None:
            if client.user:
                self.bot_id = str(client.user.id)
            logger.info(
                "Bot connected as %s (id=%s)",
                client.user,
                self.bot_id or "?",
            )
            guilds = [g.name for g in client.guilds]
            logger.info("In %d server(s): %s", len(guilds), ", ".join(guilds))
            self._ready.set()

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == client.user:
                return

            text = message.content or ""
            channel_id = str(message.channel.id)
            sender_id = str(message.author.id)
            msg_id = str(message.id)

            # Ensure context buffer is seeded (API fetch on first message per channel/restart).
            buf = await _ensure_seeded(message.channel, channel_id)

            # Snapshot context before appending current message.
            context_snapshot = list(buf)

            # Append current message to buffer (including bot messages for context awareness).
            buf_text = text
            if message.attachments:
                att_urls = " ".join(f"[attachment: {a.url}]" for a in message.attachments)
                buf_text = (buf_text + " " + att_urls).strip()
            buf.append({
                "sender_id": sender_id,
                "display_name": message.author.display_name,
                "text": buf_text,
                "message_id": msg_id,
                "timestamp": message.created_at.timestamp(),
                "reply_to_id": str(message.reference.message_id) if message.reference else None,
            })

            # Don't process responses to other bots unless they directly @mention or name-mention this bot.
            if message.author.bot:
                directly_mentioned = client.user and client.user.id in [m.id for m in message.mentions]
                name_mentioned_by_bot = any(n in text.lower() for n in ["brendbot", "brend"])
                if not directly_mentioned and not name_mentioned_by_bot:
                    return

            # Hard pass: direct @mention or name mention
            mentioned = client.user and client.user.id in [m.id for m in message.mentions]
            name_mentioned = any(n in text.lower() for n in ["brendbot", "brend"])

            if message.guild:
                # Stage 0: hard-pass on direct mention
                if mentioned or name_mentioned:
                    heuristic_pass = True
                else:
                    # Stage 1: reply-chain detection
                    reply_to_bot = False
                    if message.reference and message.reference.message_id:
                        ref = message.reference.cached_message
                        if ref is None:
                            try:
                                ref = await message.channel.fetch_message(
                                    message.reference.message_id
                                )
                            except Exception:
                                ref = None
                        if ref and str(ref.author.id) == self.bot_id:
                            reply_to_bot = True

                    # Stage 1a: cheap heuristic scoring
                    score = _score_message(
                        text,
                        channel_id,
                        reply_to_bot,
                        recent_context=context_snapshot,
                    )

                    heuristic_pass = score >= ENGAGE_THRESHOLD

                # Admin bypass: admin messages skip the haiku gate
                if sender_id == "369485175329128448":
                    heuristic_pass = True

                # Stage 2: Haiku ambiguity classifier
                if not heuristic_pass:
                    engage = await _haiku_gatecheck(text, context_snapshot)
                    if not engage:
                        # Silent drop, but buffer already updated
                        return

            # Download and format attachments
            att_text = await _format_attachments(message.attachments)
            if text and att_text:
                text = text + att_text
            elif att_text:
                text = "(attachment)" + att_text

            if not text:
                return

            chat_id = channel_id
            is_dm = not message.guild
            platform = "discord_dm" if is_dm else "discord"

            logger.info(
                "%s from %s in #%s: %r",
                "DM" if is_dm else "Message",
                message.author.display_name,
                getattr(message.channel, "name", "DM"),
                text[:80],
            )

            # Extract reply context (if not already fetched above)
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

            # Record that bot is engaging (recency tracking)
            record_bot_spoke(chat_id)

            # 👁️ is fired via session._react() in route_message (tracked, gets cleaned up).
            # Do NOT fire it here — untracked calls bypass the clearing logic.
            is_direct_mention = bool(mentioned or name_mentioned)

            # Filter context to admin messages + messages that address this bot.
            # Drops bot-to-bot noise and unaddressed human chatter from the
            # per-turn context block, reducing input token cost.
            _ADMIN_ID = "369485175329128448"
            # Build thread-aware context: walk reply chain or fall back to recent relevant messages
            _THREAD_MAX = 10
            _THREAD_MAX_AGE = 900  # 15 minutes
            _now_ts = message.created_at.timestamp()
            _buf_index = {m["message_id"]: m for m in context_snapshot if "message_id" in m}

            if reply_to_id:
                _thread_context: list[dict] = []
                _cur_id: str | None = reply_to_id
                while _cur_id and len(_thread_context) < _THREAD_MAX:
                    _parent = _buf_index.get(_cur_id)
                    if not _parent:
                        break
                    if _now_ts - _parent.get("timestamp", 0) > _THREAD_MAX_AGE:
                        break
                    _thread_context.append(_parent)
                    _cur_id = _parent.get("reply_to_id")
                _thread_context.reverse()
                filtered_context = _thread_context
            else:
                _bot_mention = f"<@{self.bot_id}>" if self.bot_id else ""
                _bot_names = ("brendbot", "brend")
                def _relevant(m: dict) -> bool:
                    if _now_ts - m.get("timestamp", 0) > _THREAD_MAX_AGE:
                        return False
                    if m.get("sender_id") == _ADMIN_ID:
                        return True
                    t = m.get("text", "")
                    if _bot_mention and _bot_mention in t:
                        return True
                    if any(n in t.lower() for n in _bot_names):
                        return True
                    return False
                filtered_context = [m for m in context_snapshot if _relevant(m)][-5:]

            await self._on_message(
                platform,
                sender_id,
                chat_id,
                text,
                msg_id,
                reply_to_id=reply_to_id,
                reply_to_text=reply_to_text,
                reply_to_author=reply_to_author,
                context_messages=filtered_context,
                is_direct_mention=is_direct_mention,
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


def _summarize_xlsx(local_path: str) -> str:
    """Extract a compact manifest from an xlsx file. Replaces raw URL injection to prevent context explosion."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(local_path, data_only=True)
        sheet_lines = []
        for name in wb.sheetnames:
            ws = wb[name]
            dims = ws.dimensions or "empty"
            sheet_lines.append(f"    {name}: {dims}")
        nr_count = len(wb.defined_names)
        wb.close()
        summary_parts = [f"  Sheets ({len(sheet_lines)}):"] + sheet_lines
        if nr_count:
            summary_parts.append(f"  Named ranges: {nr_count}")
        summary_parts.append(f"  Local path: {local_path}")
        summary_parts.append(
            "  Do not read this file directly. Request a specific sheet name and cell range — only that slice will be extracted."
        )
        return "\n".join(summary_parts)
    except Exception as e:
        return f"  [xlsx manifest failed: {e}]"


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
        is_spreadsheet = (
            att.content_type in _SPREADSHEET_CONTENT_TYPES
            or Path(att.filename or "").suffix.lower() in _SPREADSHEET_EXTENSIONS
        )

        if is_image and att.size <= MAX_IMAGE_SIZE:
            local_path = await _download_attachment(att)
            if local_path:
                lines.append(f"    Path: {local_path}")
            else:
                lines.append(f"    URL: {att.url}")
        elif is_spreadsheet:
            local_path = await _download_attachment(att)
            if local_path:
                lines.append(_summarize_xlsx(local_path))
            else:
                lines.append(f"    URL: {att.url}")
        else:
            lines.append(f"    URL: {att.url}")

    if any(att.content_type and att.content_type.startswith("image/") for att in attachments):
        lines.append("  You can view images using the Read tool on the paths above.")

    return "\n".join(lines)
