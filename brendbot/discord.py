"""Discord listener using discord.py."""

import asyncio
import json
import logging
import random
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
# RECENCY_WINDOW_SECONDS defined below from engagement.yaml
CONTEXT_BUFFER_SIZE = 20  # messages per channel

type MessageCallback = Callable[..., Coroutine[Any, Any, None]]

# Channel state: tracks last time bot spoke per channel
_channel_last_spoke: dict[str, float] = {}

# Follow-up signal state: tracks the last user who triggered a tool-using turn
# per channel, and when that turn completed. A message from the same user
# within follow_up_window_seconds gets a score boost, catching iteration
# replies like "not what I meant, use this instead" that would otherwise
# hard-drop because they lack a name-mention or domain keyword.
# Updated by record_tool_turn() from session._handle() when a turn with
# _turn_tool_called=True completes. Read by _score_message() at ingest.
_channel_last_tool_turn: dict[str, tuple[str, float]] = {}

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


async def _haiku_gatecheck_with_reason(text: str, context: list[dict]) -> dict:
    """
    Ambiguity classifier returning the full {engage, reason} dict so callers
    can detect classifier errors and escalate rather than silently drop.

    Error semantics: any failure path — exception during the SDK call,
    auth error inside haiku_classify, or a malformed response — collapses
    to {"engage": False, "reason": "error"}. Callers should treat
    reason=="error" as "classifier unavailable, decide for yourself"
    rather than as a NO from the classifier.
    """
    recent = context[-5:] if context else []
    try:
        from brendbot.session import haiku_classify
        decision = await haiku_classify({
            "message": text,
            "recent_context": recent,
        })
        reason = decision.get("reason", "unknown")
        logger.info("Haiku gate: %s (message: %r)", reason, text[:50])
        return {
            "engage": bool(decision.get("engage", False)),
            "reason": reason,
            "tone": decision.get("tone", "neutral"),
        }
    except Exception as e:
        logger.warning("Haiku gate failed: %s", e)
        return {"engage": False, "reason": "error", "tone": "neutral"}


# ── Haiku failure log ─────────────────────────────────────────────────────
# Append-only record of every classifier outage. Read by an admin DM
# notifier (TODO: hook to a Discord channel via cfg.admin_alert_channel
# once that field exists in config.py — for now this file is the source
# of truth for "did the gate fail today?").
_HAIKU_FAILURE_LOG = Path(__file__).parent.parent / "logs" / "haiku_failures.log"

# Rate limit for Discord-side admin alerts on classifier failures.
# One post per hour per category; file log is always written.
_ADMIN_ALERT_MIN_INTERVAL_SECONDS = 3600
_admin_alert_last_posted: dict[str, float] = {}


def _admin_alert(category: str, message: str) -> None:
    """Post a rate-limited operator alert to ADMIN_ALERT_CHANNEL if set.
    Category is a short tag used for independent rate limiting
    (e.g. 'haiku_outage', 'session_evict). Silent no-op when the channel
    is unset or the last alert in this category is within the window."""
    cfg = get_config()
    if not cfg.admin_alert_channel:
        return
    now = time.time()
    last = _admin_alert_last_posted.get(category, 0.0)
    if now - last < _ADMIN_ALERT_MIN_INTERVAL_SECONDS:
        return
    _admin_alert_last_posted[category] = now
    try:
        asyncio.create_task(send_message(cfg.admin_alert_channel, message))
    except Exception as exc:
        logger.debug("admin alert dispatch failed: %s", exc)


def _log_haiku_failure(channel_id: str, text: str, score: float) -> None:
    """Append a single line to logs/haiku_failures.log when the haiku
    classifier returns reason='error'. Format: ISO timestamp, channel,
    score, first 80 chars of message. Also posts a rate-limited (1/hr)
    alert to ADMIN_ALERT_CHANNEL if configured."""
    try:
        _HAIKU_FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        import datetime
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        line = f"{ts}\t{channel_id}\t{score:.2f}\t{text[:80]!r}\n"
        with _HAIKU_FAILURE_LOG.open("a") as f:
            f.write(line)
    except Exception as exc:
        logger.warning("Failed to write haiku failure log: %s", exc)
    _admin_alert(
        "haiku_outage",
        f"⚠️ haiku classifier failure in <#{channel_id}> (score={score:.2f}). "
        f"Outages silently drop ambiguous messages below score=0.6. "
        f"Check logs/haiku_failures.log for details.",
    )


# ── Tone-mapped reaction palette ──────────────────────────────────────────────
# Middle-band fallback reactions. Custom server emotes get 2x weight over
# unicode so the bot reads as a channel native rather than a generic bot.
# Palette spec from seb (1484069313802666064), signed off by Brendan, 2026-04-12.
#
# Tolerance: custom emote IDs can rot (server changes, emoji removed, wrong ID
# at paste time). _pick_reaction returns a fallback-ordered list rather than
# a single emote, and react_with_fallback walks it until one succeeds. The
# first item is the weighted pick; subsequent items are the palette's unicode
# entries in order. Unicode emotes never fail, so the fallback chain always
# terminates in a reaction the user actually sees.

_UNICODE = 1
_CUSTOM = 2

_REACTION_PALETTES: dict[str, list[tuple[str, int]]] = {
    "funny":     [("😂", _UNICODE), ("💀", _UNICODE), ("<:wholesquadslaughing:968321154488098816>", _CUSTOM)],
    "hype":      [("🔥", _UNICODE), ("<:this:840937418156933140>", _CUSTOM), ("<:hyperomegapoggers:680628570921500847>", _CUSTOM)],
    "sad":       [("😔", _UNICODE), ("<:sadbad:836301301402828860>", _CUSTOM)],
    "weird":     [("👀", _UNICODE), ("<:rusrs:1338759240751255592>", _CUSTOM)],
    "dumb":      [("🤦", _UNICODE), ("<:rusrs:1338759240751255592>", _CUSTOM)],
    "wholesome": [("🫡", _UNICODE), ("❤️", _UNICODE)],
    "neutral":   [("👀", _UNICODE), ("👍", _UNICODE), ("<:this:840937418156933140>", _CUSTOM)],
}


def _pick_reaction(tone: str) -> list[str]:
    """Weighted random pick from the palette for the given tone bucket,
    returned as a fallback-ordered list.

    Position 0 is the weighted pick (may be custom or unicode).
    Positions 1+ are the palette's unicode emotes in palette order, with
    the position-0 emote deduplicated if it was also unicode.

    The caller walks this list via react_with_fallback until one succeeds.
    Since every palette has at least one unicode entry and unicode emotes
    cannot fail on Discord, the list always contains a guaranteed-safe tail.

    Falls back to the 'neutral' palette for any unrecognised tone.
    """
    palette = _REACTION_PALETTES.get(tone, _REACTION_PALETTES["neutral"])
    emotes, weights = zip(*palette)
    picked = random.choices(emotes, weights=weights, k=1)[0]
    # Fallback tail: unicode entries from the same palette, in palette order,
    # minus the picked emote if it was already unicode.
    fallback_tail = [e for e, w in palette if w == _UNICODE and e != picked]
    return [picked] + fallback_tail


async def react_with_fallback(
    channel_id: str, message_id: str, emotes: list[str]
) -> bool:
    """Try each emote in order until one successfully reacts.

    Returns True if any reaction landed, False if every attempt failed
    (which should be impossible in practice — every palette's fallback
    tail contains at least one unicode emote, and unicode never fails
    on Discord).

    Logs each failure at DEBUG so dead custom emote IDs surface in the
    runtime log without spamming WARNING for expected retries.
    """
    if _discord_client is None:
        return False
    try:
        channel = _discord_client.get_channel(int(channel_id))
        if channel is None:
            channel = await _discord_client.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
    except Exception as exc:
        logger.warning("react_with_fallback: message lookup failed %s: %s", message_id, exc)
        return False
    for emote in emotes:
        try:
            await msg.add_reaction(emote)
            logger.debug("Reacted to %s with %s", message_id, emote)
            return True
        except Exception as exc:
            logger.debug(
                "react_with_fallback: %s failed on %s (%s), trying next",
                emote, message_id, exc,
            )
    logger.warning(
        "react_with_fallback: all %d emotes failed on %s",
        len(emotes), message_id,
    )
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


async def send_message(channel_id: str, text: str) -> str | None:
    """Send a message to a Discord channel by ID.
    Returns the first chunk's message ID (str) so callers can log it for
    feedback correlation, or None on failure / pre-ready dispatch."""
    if _discord_client is None:
        logger.warning("send_message called before client is ready")
        return None
    channel = _discord_client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await _discord_client.fetch_channel(int(channel_id))
        except Exception as e:
            logger.error("Could not fetch channel %s: %s", channel_id, e)
            return None
    try:
        first_msg_id: str | None = None
        for chunk in [text[i:i+2000] for i in range(0, len(text), 2000)]:
            sent = await channel.send(chunk)
            if first_msg_id is None:
                first_msg_id = str(sent.id)
        record_bot_spoke(channel_id)
        return first_msg_id
    except Exception as e:
        logger.error("Failed to send message to %s: %s", channel_id, e)
        return None


async def edit_message(channel_id: str, message_id: str, text: str) -> bool:
    """Edit an existing Discord message in-place. Returns True on success.
    Used by the streaming response path to update a message as tokens
    arrive, giving users visual feedback before generation completes."""
    if _discord_client is None:
        return False
    try:
        channel = _discord_client.get_channel(int(channel_id))
        if channel is None:
            channel = await _discord_client.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(content=text[:2000])
        return True
    except Exception as e:
        logger.debug("edit_message failed for %s: %s", message_id, e)
        return False


# ── Engagement config: single source of truth ────────────────────────────
# All gating constants, scoring deltas, noise tokens, conversational starters,
# and domain keywords come from engagement.yaml. The same file's
# `classifier_prompt` block feeds the haiku ambiguity classifier in
# session.py. Edit engagement.yaml — never patch these in code.

_ENGAGEMENT_YAML = Path(__file__).parent.parent / "engagement.yaml"


def _load_engagement_config() -> dict:
    """Load engagement.yaml. Hard-fails on missing/invalid file — there is no
    sane default for engagement gating, and silently scoring everything to 0
    would be worse than refusing to start."""
    import yaml
    if not _ENGAGEMENT_YAML.exists():
        raise FileNotFoundError(
            f"engagement.yaml not found at {_ENGAGEMENT_YAML}. "
            "This file is required — it is the single source of truth for "
            "engagement gating. See repo root for the canonical version."
        )
    with _ENGAGEMENT_YAML.open() as f:
        cfg = yaml.safe_load(f)
    required = {"thresholds", "scoring", "noise_tokens", "domains",
                "question_starters", "directive_starters", "address_levels"}
    missing = required - set(cfg.keys())
    if missing:
        raise ValueError(f"engagement.yaml missing required keys: {missing}")
    return cfg


_ENGAGEMENT_CFG = _load_engagement_config()

# ── Derived engagement constants ─────────────────────────────────────────
# All derived from _ENGAGEMENT_CFG. Populated at module load and refreshed
# in-place by refresh_engagement_config(), which is wired to SIGHUP via
# SessionPool.refresh_cache(). No process restart needed to pick up
# engagement.yaml edits.

ENGAGE_HARD_PASS: float = 0.0
ENGAGE_THRESHOLD: float = 0.0
RECENCY_WINDOW_SECONDS: int = 0
FOLLOW_UP_WINDOW_SECONDS: int = 0
_ADDRESS_HIGH: float = 0.0
_ADDRESS_MODERATE: float = 0.0
_SCORE_REPLY_TO_BOT: float = 0.0
_SCORE_RECENCY: float = 0.0
_SCORE_DOMAIN: float = 0.0
_SCORE_DOMAIN_CTX: float = 0.0
_SCORE_CONVERSATIONAL: float = 0.0
_SCORE_FOLLOW_UP: float = 0.0
SCORE_NAME_MENTIONED: float = 0.0
_NOISE_TOKENS: frozenset = frozenset()
_QUESTION_STARTERS: tuple = ()
_DIRECTIVE_STARTERS: tuple = ()
DOMAIN_PATTERN: re.Pattern = re.compile(r"(?!)")  # placeholder, never matches
KEYWORD_TO_MODULE: dict[str, str] = {}


def _build_domain_pattern(domains: dict[str, list[str]]) -> tuple[re.Pattern, dict[str, str]]:
    """Compile domain keywords into a single word-boundary regex.
    Multi-word phrases sorted longest-first so they match before component words."""
    keyword_to_module: dict[str, str] = {}
    for module_id, terms in domains.items():
        for term in terms:
            keyword_to_module[term.lower()] = module_id.upper()
    sorted_kws = sorted(keyword_to_module.keys(), key=len, reverse=True)
    pattern_str = r"\b(?:" + "|".join(re.escape(k) for k in sorted_kws) + r")\b"
    return re.compile(pattern_str, re.IGNORECASE), keyword_to_module


def _apply_engagement_constants(cfg: dict) -> None:
    """Populate module-level engagement constants from a loaded config dict.
    Called at module init and on SIGHUP refresh."""
    global ENGAGE_HARD_PASS, ENGAGE_THRESHOLD, RECENCY_WINDOW_SECONDS
    global FOLLOW_UP_WINDOW_SECONDS, _ADDRESS_HIGH, _ADDRESS_MODERATE
    global _SCORE_REPLY_TO_BOT, _SCORE_RECENCY, _SCORE_DOMAIN
    global _SCORE_DOMAIN_CTX, _SCORE_CONVERSATIONAL, _SCORE_FOLLOW_UP
    global SCORE_NAME_MENTIONED, _NOISE_TOKENS
    global _QUESTION_STARTERS, _DIRECTIVE_STARTERS
    global DOMAIN_PATTERN, KEYWORD_TO_MODULE

    ENGAGE_HARD_PASS = float(cfg["thresholds"]["hard_pass"])
    ENGAGE_THRESHOLD = float(cfg["thresholds"]["haiku_floor"])
    RECENCY_WINDOW_SECONDS = int(cfg.get("recency_seconds", 300))
    FOLLOW_UP_WINDOW_SECONDS = int(cfg.get("follow_up_window_seconds", 120))
    _ADDRESS_HIGH = float(cfg["address_levels"]["high"])
    _ADDRESS_MODERATE = float(cfg["address_levels"]["moderate"])
    _SCORE_REPLY_TO_BOT = float(cfg["scoring"]["reply_to_bot"])
    _SCORE_RECENCY = float(cfg["scoring"]["recency_active"])
    _SCORE_DOMAIN = float(cfg["scoring"]["domain_match"])
    _SCORE_DOMAIN_CTX = float(cfg["scoring"]["domain_match_in_context"])
    _SCORE_CONVERSATIONAL = float(cfg["scoring"]["conversational_in_thread"])
    _SCORE_FOLLOW_UP = float(cfg["scoring"].get("follow_up_after_tool_use", 0.3))
    SCORE_NAME_MENTIONED = float(cfg["scoring"]["name_mentioned"])
    _NOISE_TOKENS = frozenset(cfg["noise_tokens"])
    _QUESTION_STARTERS = tuple(cfg["question_starters"])
    _DIRECTIVE_STARTERS = tuple(cfg["directive_starters"])
    DOMAIN_PATTERN, KEYWORD_TO_MODULE = _build_domain_pattern(cfg["domains"])


def refresh_engagement_config() -> None:
    """Re-read engagement.yaml and update all derived constants in-place.
    Wire to SIGHUP via SessionPool.refresh_cache(). On load failure, the
    previous constants remain — partial state is worse than stale state."""
    global _ENGAGEMENT_CFG
    try:
        new_cfg = _load_engagement_config()
        _ENGAGEMENT_CFG = new_cfg
        _apply_engagement_constants(new_cfg)
        logger.info("engagement.yaml reloaded — all derived constants updated")
    except Exception as exc:
        logger.error("engagement.yaml reload failed — keeping previous config: %s", exc)


# Initial population at module load.
_apply_engagement_constants(_ENGAGEMENT_CFG)


@dataclass
class EngageResult:
    """Result from _score_message with score, matched domains, and address level."""
    score: float = 0.0
    domains: set[str] = field(default_factory=set)
    # Domains matched only via recent-channel-context fallback (not in the
    # current message text). Tracked separately so logging and downstream
    # consumers can tell whether the domain hint reflects the current
    # message or stale context. Always a subset of `domains`.
    context_domains: set[str] = field(default_factory=set)
    address_level: str = "low"  # low | moderate | high — see FUSED-CORE Budget Throttle


def _classify_address(
    score: float,
    is_at_mention: bool,
    is_name_mention: bool = False,
) -> str:
    """Map score → address level.

    @mention or name-mention is always high regardless of score.
    Name-mentions (brend/brendan/brendbot matched by _NAME_PATTERN)
    carry the same address-level weight as a direct @mention because
    a user typing the bot's name is explicitly directing conversation
    at it and deserves full tool-budget engagement.
    """
    if is_at_mention or is_name_mention or score >= _ADDRESS_HIGH:
        return "high"
    if score >= _ADDRESS_MODERATE:
        return "moderate"
    return "low"


def _score_message(
    text: str,
    channel_id: str,
    is_reply_to_bot: bool,
    recent_context: list[dict] | None = None,
    sender_id: str | None = None,
) -> EngageResult:
    """
    Score a message for engagement likelihood.

    Returns an EngageResult with:
      score >= ENGAGE_HARD_PASS : high confidence, engage without haiku
      score >= ENGAGE_THRESHOLD : soft signal, escalate to haiku
      score  < ENGAGE_THRESHOLD : drop
      domains: set of module IDs matched by keywords
      address_level: caller fills this via _classify_address after adding name boost

    sender_id is optional for backward compatibility with existing callers
    and tests. When provided, enables the follow-up-after-tool-use boost.
    """
    result = EngageResult()
    text_lower = text.lower()
    words = text.split()
    word_count = len(words)

    # Early noise rejection: short messages composed entirely of noise tokens.
    if word_count <= 2 and all(w.lower().strip("?!.,") in _NOISE_TOKENS for w in words):
        return result

    if is_reply_to_bot:
        result.score += _SCORE_REPLY_TO_BOT

    last_spoke = _channel_last_spoke.get(channel_id, 0.0)
    recency_active = time.time() - last_spoke < RECENCY_WINDOW_SECONDS
    if recency_active and word_count >= 3:
        result.score += _SCORE_RECENCY

    # Follow-up after bot tool use: same user, within window, gets a boost.
    # Catches iteration replies like "not quite, try again" that lack name
    # mentions or domain keywords but are clearly directed at the bot because
    # the user just prompted a tool-using turn.
    if sender_id is not None:
        tool_entry = _channel_last_tool_turn.get(channel_id)
        if tool_entry is not None:
            last_user_id, last_ts = tool_entry
            if (
                sender_id == last_user_id
                and time.time() - last_ts < FOLLOW_UP_WINDOW_SECONDS
            ):
                result.score += _SCORE_FOLLOW_UP

    # Domain keyword match via compiled regex — single pass, word-boundary aware.
    domain_scored = False
    for m in DOMAIN_PATTERN.finditer(text_lower):
        kw = m.group(0).lower()
        module = KEYWORD_TO_MODULE.get(kw)
        if module:
            result.domains.add(module)
            if not domain_scored:
                result.score += _SCORE_DOMAIN
                domain_scored = True

    # Domain match in recent context — fallback only if current message didn't match.
    if not domain_scored and recent_context:
        context_text = " ".join(
            m.get("text", "") for m in recent_context[-5:] if m.get("has_keyword")
        ).lower()
        for m in DOMAIN_PATTERN.finditer(context_text):
            kw = m.group(0).lower()
            module = KEYWORD_TO_MODULE.get(kw)
            if module:
                result.domains.add(module)
                result.context_domains.add(module)  # mark as context-source
                if not domain_scored:
                    result.score += _SCORE_DOMAIN_CTX
                    domain_scored = True

    if recency_active and word_count >= 3:
        is_conversational = (
            text_lower.endswith("?")
            or any(text_lower.startswith(s) for s in _QUESTION_STARTERS)
            or any(text_lower.startswith(s) for s in _DIRECTIVE_STARTERS)
        )
        if is_conversational:
            result.score += _SCORE_CONVERSATIONAL

    return result


# TTL for channel recency/tool-turn dicts — any entry older than this is
# evicted on the next prune sweep. Prevents unbounded dict growth over
# long uptimes in busy servers.
_CHANNEL_STATE_TTL_SECONDS = 86400  # 24 hours
_CHANNEL_PRUNE_INTERVAL_SECONDS = 3600  # sweep at most once per hour
_channel_last_pruned: float = 0.0


def _prune_channel_state() -> None:
    """Evict entries older than TTL from the recency and tool-turn dicts.
    Rate-limited to one sweep per hour; no-op otherwise. Called from
    record_bot_spoke (hot path, cheap check)."""
    global _channel_last_pruned
    now = time.time()
    if now - _channel_last_pruned < _CHANNEL_PRUNE_INTERVAL_SECONDS:
        return
    _channel_last_pruned = now
    cutoff = now - _CHANNEL_STATE_TTL_SECONDS
    for cid in list(_channel_last_spoke.keys()):
        if _channel_last_spoke[cid] < cutoff:
            del _channel_last_spoke[cid]
    for cid in list(_channel_last_tool_turn.keys()):
        _, ts = _channel_last_tool_turn[cid]
        if ts < cutoff:
            del _channel_last_tool_turn[cid]


def record_bot_spoke(channel_id: str) -> None:
    """Call this after the bot sends a message to update recency state."""
    _channel_last_spoke[channel_id] = time.time()
    _prune_channel_state()


def record_tool_turn(channel_id: str, user_id: str) -> None:
    """Record that the bot just completed a tool-using turn in response to
    a specific user. Subsequent messages from the same user within
    FOLLOW_UP_WINDOW_SECONDS will get a follow-up scoring boost, catching
    iteration replies ('not that one, try again') that would otherwise
    hard-drop because they lack a name-mention or domain keyword.

    Called from session._handle() in the ResultMessage branch when
    _turn_tool_called was True for the just-completed turn.
    """
    _channel_last_tool_turn[channel_id] = (user_id, time.time())


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
        intents.reactions = True  # admin feedback emote handler in on_raw_reaction_add

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
            # Guild snowflake for multi-server isolation of user_registry.
            # Empty for DMs — user_registry filters by guild only when
            # non-empty, so DM sessions still work with the global list.
            guild_id = str(message.guild.id) if message.guild else ""

            # ── User registry update ─────────────────────────────────────
            # Record every sender seen, regardless of whether the bot
            # engages. This builds the persistent member table used for
            # @mention resolution and per-user engagement priors.
            try:
                from brendbot.user_registry import record_user
                _tier = get_config().tier_for(sender_id)
                record_user(
                    user_id=sender_id,
                    display_name=message.author.display_name,
                    username=str(message.author) if hasattr(message.author, "name") else "",
                    tier=_tier,
                    guild_id=guild_id,
                )
            except Exception:
                pass

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
                    try:
                        from brendbot.feedback import log_skip_decision
                        log_skip_decision(
                            channel_id=channel_id,
                            sender_id=sender_id,
                            user_message_id=msg_id,
                            user_text=text,
                            score=None,
                            reason="bot_author_not_mentioned",
                        )
                    except Exception:
                        pass
                    return

            # Determine whether bot is @mentioned or name-mentioned.
            mentioned = client.user and client.user.id in [m.id for m in message.mentions]
            name_mentioned = bool(_NAME_PATTERN.search(text))

            # ── @mention pre-filter using user registry ───────────────────
            # If the message contains @mentions but none of them is the bot,
            # and there is no name-mention and no reply-to-bot, the message
            # is very likely addressed to another user. Resolve mentions via
            # the user registry and drop early so the haiku classifier never
            # fires on misdirected @mentions (e.g. VoX @mentioning seb while
            # the bot sees it as ambiguous and responds erroneously).
            #
            # Condition: guild message + at least one @mention in the raw
            # text + none resolve to the bot's own ID + no name-mention.
            # DMs never enter this branch (no guild, no cross-user @mentions).
            if message.guild and not mentioned and not name_mentioned and message.mentions:
                try:
                    from brendbot.user_registry import resolve_mentions
                    _resolved = resolve_mentions(text, self.bot_id)
                    # If all mentions are non-bot users and no name trigger,
                    # hard-drop before scoring. Log as 'wrong_mention_target'.
                    if _resolved and all(uid != self.bot_id for uid in _resolved):
                        try:
                            from brendbot.feedback import log_skip_decision
                            log_skip_decision(
                                channel_id=channel_id,
                                sender_id=sender_id,
                                user_message_id=msg_id,
                                user_text=text,
                                score=None,
                                reason="wrong_mention_target",
                            )
                        except Exception:
                            pass
                        return
                except Exception:
                    pass  # registry unavailable — fall through to normal gate

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

            # Defaults for the DM path (no engagement gate runs in DMs).
            # DMs are always treated as direct address — full tool budget.
            address_level = "high"
            matched_domains: set[str] = set()
            matched_context_domains: set[str] = set()  # Phase 3 fix: context-only domains
            final_score: float | None = None  # set in guild path; None in DMs

            if message.guild:
                # ── Name-triggered path ───────────────────────────────────────
                # Direct @mention: hard pass — the bot was explicitly addressed.
                # Name mention (brend/brendbot in text): routes through scoring +
                # haiku gating same as ambient. Name is a hint, not a guarantee.
                if mentioned:
                    heuristic_pass = True
                    use_haiku = False
                    # @mention is unconditionally high address regardless of score.
                    address_level = "high"
                    matched_domains = set()
                    # Score not computed on @mention path; use sentinel so the
                    # feedback log can distinguish "mention bypass" from
                    # "scored 0" at audit time.
                    final_score = None
                else:
                    # ── Ambient path — reply-chain + heuristic scoring ────────
                    # Name-mentioned messages enter here and are scored normally.
                    reply_to_bot = (
                        reply_ref is not None
                        and str(reply_ref.author.id) == self.bot_id
                    )

                    engage_result = _score_message(
                        text,
                        channel_id,
                        reply_to_bot,
                        recent_context=context_snapshot,
                        sender_id=str(message.author.id),
                    )
                    # Name mention boosts score — sufficient signal that the message
                    # is directed at the bot, but not a bypass. Boost magnitude
                    # comes from engagement.yaml (SCORE_NAME_MENTIONED).
                    if name_mentioned:
                        engage_result.score += SCORE_NAME_MENTIONED

                    matched_domains = engage_result.domains
                    matched_context_domains = engage_result.context_domains
                    address_level = _classify_address(
                        engage_result.score,
                        is_at_mention=False,
                        is_name_mention=name_mentioned,
                    )
                    final_score = engage_result.score

                    if engage_result.score >= ENGAGE_HARD_PASS:
                        heuristic_pass = True
                        use_haiku = False
                    elif engage_result.score >= ENGAGE_THRESHOLD:
                        heuristic_pass = False
                        use_haiku = True  # ambiguous middle — haiku classifier decides
                    else:
                        heuristic_pass = False
                        use_haiku = False  # hard drop

                # Admin messages follow the same engagement gates as all others.
                # Admin tier governs trust and permissions only, not engagement bypass.

                # Haiku ambiguity classifier — middle band of ambient path only.
                # On haiku fail-LOUD: classifier API errors escalate any score
                # >= 0.6 to engage anyway, and log to logs/haiku_failures.log so
                # outages don't silently drop ambiguous messages (regression seen
                # 2026-04-12 when API auth was misconfigured for ~1 minute).
                haiku_invoked = False
                if not heuristic_pass:
                    if use_haiku:
                        haiku_invoked = True  # tracked for cognitive load (Phase 3 #1A)
                        haiku_result = await _haiku_gatecheck_with_reason(
                            text, context_snapshot
                        )
                        engage = haiku_result["engage"]
                        if haiku_result["reason"] == "error":
                            _log_haiku_failure(
                                channel_id, text, engage_result.score
                            )
                            # Fail-loud escalation: if score was already close to
                            # the hard-pass band, treat the classifier outage as
                            # "engage" rather than silently dropping.
                            if engage_result.score >= 0.6:
                                engage = True
                                logger.warning(
                                    "Haiku failed but score=%.2f — escalating to engage",
                                    engage_result.score,
                                )
                        if not engage:
                            tone = haiku_result.get("tone", "neutral")
                            await react_with_fallback(
                                channel_id, str(message.id), _pick_reaction(tone)
                            )
                            try:
                                from brendbot.feedback import log_skip_decision
                                _reason = (
                                    "haiku_error_low_score"
                                    if haiku_result["reason"] == "error"
                                    else "haiku_no"
                                )
                                log_skip_decision(
                                    channel_id=channel_id,
                                    sender_id=sender_id,
                                    user_message_id=msg_id,
                                    user_text=text,
                                    score=engage_result.score,
                                    reason=_reason,
                                    domains=sorted(engage_result.domains),
                                )
                            except Exception:
                                pass
                            return
                    else:
                        try:
                            from brendbot.feedback import log_skip_decision
                            log_skip_decision(
                                channel_id=channel_id,
                                sender_id=sender_id,
                                user_message_id=msg_id,
                                user_text=text,
                                score=engage_result.score,
                                reason="hard_drop",
                                domains=sorted(engage_result.domains),
                            )
                        except Exception:
                            pass
                        return
            else:
                # DM path — no engagement gate, no haiku.
                haiku_invoked = False

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

            # Build domain_hint with [ctx] suffix on domains that matched
            # only via recent-channel-context fallback (Phase 3 fix). Helps
            # distinguish "current message contains BUILDSCI keyword" from
            # "prior chatter in this channel mentioned BUILDSCI." Without
            # this distinction, the routing log says domains=IMAGEGEN on
            # messages like "hey brend; how's it going?" which is misleading.
            if matched_domains:
                domain_parts = []
                for d in sorted(matched_domains):
                    if d in matched_context_domains:
                        domain_parts.append(f"{d}[ctx]")
                    else:
                        domain_parts.append(d)
                _domain_hint_str = ",".join(domain_parts)
            else:
                _domain_hint_str = ""

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
                domain_hint=_domain_hint_str,
                address_level=address_level,
                score=final_score,
                haiku_invoked=haiku_invoked,
                guild_id=guild_id,
            )

        @client.event
        async def on_disconnect() -> None:
            logger.warning("Disconnected from Discord")
            self._ready.clear()

        @client.event
        async def on_resumed() -> None:
            logger.info("Reconnected to Discord")
            self._ready.set()

        @client.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
            """Admin-only feedback channel.

            Filters: (1) reactor must be the configured admin, (2) emoji
            must be in FEEDBACK_REACTIONS, (3) message must have been
            posted by this bot. All three conditions silent-drop on miss.
            Surviving events append a row to logs/feedback_events.jsonl.

            Uses raw reactions so feedback works on bot messages from
            sessions older than the discord.py message cache.
            """
            from brendbot.feedback import FEEDBACK_REACTIONS, log_feedback_event
            cfg = get_config()
            if str(payload.user_id) != cfg.admin_discord_id:
                return
            emoji_name = str(payload.emoji)
            if emoji_name not in FEEDBACK_REACTIONS:
                return
            # Verify the reacted-to message was posted by the bot. fetch_message
            # is required because raw events don't include message author.
            try:
                channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
                msg = await channel.fetch_message(payload.message_id)
            except Exception as e:
                logger.debug("reaction lookup failed for %s: %s", payload.message_id, e)
                return
            if not (client.user and msg.author.id == client.user.id):
                return
            log_feedback_event(
                channel_id=str(payload.channel_id),
                bot_message_id=str(payload.message_id),
                emoji=emoji_name,
                admin_id=str(payload.user_id),
            )
            logger.info(
                "Feedback recorded: %s on %s by admin",
                FEEDBACK_REACTIONS[emoji_name], payload.message_id,
            )

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
