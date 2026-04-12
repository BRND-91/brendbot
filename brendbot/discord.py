"""Discord listener using discord.py."""

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
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

# Name trigger pattern — word-boundary match for all three trigger forms, case-insensitive.
# Compiled once at module load. Covers: "brend", "brendan", "brendbot".
# Used in both the bot-message filter and the main engagement gate.
_NAME_PATTERN = re.compile(r"\b(brend|brendan|brendbot)\b", re.IGNORECASE)

# Context-filter name strings — used in _relevant() for context window filtering.
# Kept in sync with _NAME_PATTERN intentionally; separate because _relevant()
# operates on pre-lowercased strings via simple containment checks.
_BOT_NAMES = ("brendbot", "brendan", "brend")


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


def _load_domain_keywords() -> tuple[re.Pattern, dict[str, str]]:
    """Build a compiled keyword regex and keyword->module mapping from knowledge modules.

    Returns:
        (domain_pattern, keyword_to_module) where domain_pattern is a compiled
        regex alternation of all keywords, and keyword_to_module maps each keyword
        string to its source module ID (e.g. "insulation" -> "BUILDSCI").

    Using a single compiled regex instead of a frozenset loop gives correct
    word-boundary semantics (e.g. "stats" won't match "statistics") and
    reduces per-message work from O(n_keywords * len(text)) substring scans
    to a single regex pass.
    """
    # Explicit human-readable terms per domain
    _MODULE_TERMS: dict[str, list[str]] = {
        "LOGIC": [
            "logic", "argument", "proof", "valid", "premise", "conclusion",
            "inference", "reasoning", "deduction", "induction", "theorem",
            "proposition", "syllogism", "formal language", "predicate",
        ],
        "STATS": [
            "probability", "statistics", "distribution", "bayesian", "random variable",
            "variance", "likelihood", "confidence interval", "regression", "correlation",
            "bayes", "bernoulli", "normal distribution", "standard deviation",
        ],
        "SYSTEMS": [
            "systems thinking", "feedback loop", "feedback", "tipping point",
            "oscillation", "overshoot", "stocks and flows", "emergence", "complexity",
            "pareto", "ashby", "goodhart", "diminishing returns", "delay",
            "leadership", "servant leadership", "situational leadership",
            "emotional intelligence", "forrester",
        ],
        "PERSONALITY": [
            "empathy", "interpersonal", "mediation", "therapy", "group dynamics",
            "respect", "boundary", "calibrate", "risk gate",
        ],
        "BUILDSCI": [
            "building science", "insulation", "hvac", "air barrier", "vapor barrier",
            "moisture", "thermal", "enclosure", "ventilation", "infiltration",
            "r-value", "blower door", "energy efficiency", "condensation",
            "building envelope", "duct", "mechanical ventilation", "combustion",
            "indoor air quality", "iaq", "heat loss", "heat gain", "air sealing",
            "hrv", "erv", "radiant", "conduction", "convection", "latent heat",
            "sensible heat", "dew point", "relative humidity",
            "fiberglass", "attic", "crawlspace", "pest", "shell tightening",
            "air leakage", "weatherization", "envelope", "rim joist", "slab",
        ],
    }

    keyword_to_module: dict[str, str] = {}
    for module_id, terms in _MODULE_TERMS.items():
        for term in terms:
            keyword_to_module[term] = module_id

    # Supplement from manifest module descriptions
    try:
        manifest_path = KNOWLEDGE_DIR / "MANIFEST.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            for module in manifest.get("modules", []):
                mod_id = module.get("id", "").upper()
                desc = module.get("desc", "").lower()
                words = [w.strip(",.():") for w in desc.split() if len(w) > 4]
                for w in words:
                    if w not in keyword_to_module:
                        keyword_to_module[w] = mod_id
    except Exception as e:
        logger.warning("Failed to load manifest keywords: %s", e)

    # Build a single compiled regex alternation. Multi-word phrases are sorted
    # longest-first so they match before their component words (e.g. "feedback loop"
    # before "feedback"). Word boundaries applied around each term.
    sorted_kws = sorted(keyword_to_module.keys(), key=len, reverse=True)
    pattern_str = r"\b(?:" + "|".join(re.escape(k) for k in sorted_kws) + r")\b"
    domain_pattern = re.compile(pattern_str, re.IGNORECASE)

    return domain_pattern, keyword_to_module


DOMAIN_PATTERN, KEYWORD_TO_MODULE = _load_domain_keywords()


@dataclass
class EngageResult:
    """Result from _score_message with score and matched domain modules."""
    score: float = 0.0
    domains: set[str] = field(default_factory=set)


# Noise tokens that never warrant engagement on their own.
_NOISE_TOKENS = frozenset({
    "lol", "lmao", "haha", "hehe", "omg", "wtf", "brb", "gg", "oof",
    "ok", "k", "yeah", "yep", "nah", "nope", "sure", "true", "same",
    "nice", "rip", "wow", "based", "fr", "bet", "cope", "ratio",
})

# Conversational starters — built once at module load, not per call.
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


def _score_message(
    text: str,
    channel_id: str,
    is_reply_to_bot: bool,
    recent_context: list[dict] | None = None,
) -> EngageResult:
    """
    Score a message for engagement likelihood.

    Returns an EngageResult with:
      score >= 1.0 : high confidence, engage
      score >= 0.4 : soft signal, engage
      score  < 0.4 : drop
      domains: set of module IDs matched by keywords (e.g. {"BUILDSCI", "STATS"})
    """
    result = EngageResult()
    text_lower = text.lower()
    words = text.split()
    word_count = len(words)

    # Early noise rejection: single-token messages that are never worth engaging.
    if word_count <= 2 and all(w.lower().strip("?!.,") in _NOISE_TOKENS for w in words):
        return result  # score=0.0, no domains

    # Direct reply to bot is a strong signal
    if is_reply_to_bot:
        result.score += 1.0

    # Recent thread participation lowers the bar — but only for messages with
    # enough content to be plausibly relevant.
    last_spoke = _channel_last_spoke.get(channel_id, 0.0)
    recency_active = time.time() - last_spoke < RECENCY_WINDOW_SECONDS
    if recency_active and word_count >= 3:
        result.score += 0.3

    # Domain keyword match via compiled regex — single pass, word-boundary aware.
    # Collects ALL matching domains in one scan.
    domain_scored = False
    for m in DOMAIN_PATTERN.finditer(text_lower):
        kw = m.group(0).lower()
        module = KEYWORD_TO_MODULE.get(kw)
        if module:
            result.domains.add(module)
            if not domain_scored:
                result.score += 0.4
                domain_scored = True

    # Domain keyword match in recent context — only if current message didn't match.
    if not domain_scored and recent_context:
        context_text = " ".join(
            m.get("text", "") for m in recent_context[-5:] if m.get("has_keyword")
        ).lower()
        for m in DOMAIN_PATTERN.finditer(context_text):
            kw = m.group(0).lower()
            module = KEYWORD_TO_MODULE.get(kw)
            if module:
                result.domains.add(module)
                if not domain_scored:
                    result.score += 0.3
                    domain_scored = True

    # Conversational signal: question or directive in an active thread.
    if recency_active and word_count >= 3:
        is_conversational = (
            text_lower.endswith("?")
            or any(text_lower.startswith(s) for s in _QUESTION_STARTERS)
            or any(text_lower.startswith(s) for s in _DIRECTIVE_STARTERS)
        )
        if is_conversational:
            result.score += 0.2

    return result


ENGAGE_THRESHOLD = 0.4      # bottom of middle band — below this, hard drop
ENGAGE_HARD_PASS = 0.7      # at or above this, skip haiku and engage directly


def record_bot_spoke(channel_id: str) -> None:
    """Call this after the bot sends a message to update recency state."""
    _channel_last_spoke[channel_id] = time.time()


_seeding_locks: dict[str, asyncio.Lock] = {}

async def _ensure_seeded(channel: discord.abc.Messageable, channel_id: str) -> deque:
    """Return the context buffer for a channel, seeding from Discord API if first use."""
    if channel_id not in _channel_seeded:
        # Per-channel lock prevents double-seeding from concurrent messages
        if channel_id not in _seeding_locks:
            _seeding_locks[channel_id] = asyncio.Lock()
        async with _seeding_locks[channel_id]:
            if channel_id in _channel_seeded:
                return _channel_context[channel_id]
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
                        "has_keyword": bool(DOMAIN_PATTERN.search(seed_text.lower())),
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
                "has_keyword": bool(DOMAIN_PATTERN.search(buf_text.lower())),
            })

            # Don't process responses to other bots unless they directly @mention
            # or name-mention this bot. Name check uses word-boundary regex to
            # avoid false positives from bot output containing "brend" as a fragment.
            if message.author.bot:
                directly_mentioned = client.user and client.user.id in [m.id for m in message.mentions]
                name_mentioned_by_bot = bool(_NAME_PATTERN.search(text))
                if not directly_mentioned and not name_mentioned_by_bot:
                    return

            # Determine whether bot is @mentioned or name-mentioned.
            mentioned = client.user and client.user.id in [m.id for m in message.mentions]
            name_mentioned = bool(_NAME_PATTERN.search(text))

            matched_domains: set[str] = set()

            # ── Fetch reply reference once ────────────────────────────────────
            # If this message is a reply, fetch the referenced message a single
            # time here and reuse it for both reply-chain detection (engagement
            # gate) and reply context extraction (passed to Claude). This avoids
            # two separate API round-trips to Discord for the same message ID.
            reply_ref: discord.Message | None = None
            if message.reference and message.reference.message_id:
                reply_ref = message.reference.cached_message
                if reply_ref is None:
                    try:
                        reply_ref = await message.channel.fetch_message(
                            message.reference.message_id
                        )
                    except Exception:
                        reply_ref = None

            if message.guild:
                # ── Name-triggered path ───────────────────────────────────────
                # If the bot's name was used (any of: brend, brendan, brendbot),
                # bypass all scoring and haiku gating and route directly to Claude.
                # The knowledge-registry reasoning in FUSED-CORE handles engagement
                # from there — the name is sufficient signal that the message is
                # addressed to the bot.
                if mentioned or name_mentioned:
                    heuristic_pass = True
                    use_haiku = False
                else:
                    # ── Ambient path — reply-chain + heuristic scoring ────────
                    reply_to_bot = (
                        reply_ref is not None
                        and str(reply_ref.author.id) == self.bot_id
                    )

                    engage_result = _score_message(
                        text,
                        channel_id,
                        reply_to_bot,
                        recent_context=context_snapshot,
                    )
                    matched_domains = engage_result.domains

                    if engage_result.score >= ENGAGE_HARD_PASS:
                        heuristic_pass = True
                        use_haiku = False
                    elif engage_result.score >= ENGAGE_THRESHOLD:
                        heuristic_pass = False
                        use_haiku = True  # ambiguous middle — haiku classifier decides
                    else:
                        heuristic_pass = False
                        use_haiku = False  # hard drop

                # Admin bypass: admin messages skip all gates regardless of path.
                cfg = get_config()
                if sender_id == cfg.admin_discord_id:
                    heuristic_pass = True
                    use_haiku = False

                # Haiku ambiguity classifier — middle band of ambient path only.
                if not heuristic_pass:
                    if use_haiku:
                        engage = await _haiku_gatecheck(text, context_snapshot)
                        if not engage:
                            return
                    else:
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

            # Extract reply context from the already-fetched reply_ref.
            # No second API call needed.
            reply_to_id = ""
            reply_to_text = ""
            reply_to_author = ""
            if reply_ref is not None and message.reference and message.reference.message_id:
                reply_to_id = str(message.reference.message_id)
                reply_to_text = (reply_ref.content or "")[:500]
                reply_to_author = str(reply_ref.author.id)

            # Record that bot is engaging (recency tracking)
            record_bot_spoke(chat_id)

            is_direct_mention = bool(mentioned or name_mentioned)

            # Build thread-aware context: walk reply chain or fall back to recent relevant messages.
            _ADMIN_ID = get_config().admin_discord_id
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
                def _relevant(m: dict) -> bool:
                    if _now_ts - m.get("timestamp", 0) > _THREAD_MAX_AGE:
                        return False
                    if m.get("sender_id") == _ADMIN_ID:
                        return True
                    t = m.get("text", "")
                    if _bot_mention and _bot_mention in t:
                        return True
                    if any(n in t.lower() for n in _BOT_NAMES):
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
                domain_hint=",".join(sorted(matched_domains)) if matched_domains else "",
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
