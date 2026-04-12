"""Feedback infrastructure: three append-only JSONL streams.

Three independent log streams, joined at audit time by bot_message_id:

1. bot_responses.jsonl   — every response the bot posts
   Schema: {ts, channel_id, bot_message_id, user_message_id, user_text,
            score, domains, address_level, branch_tag}

2. branch_audit.jsonl    — every response that began with a [rejected]/
                           [searching]/[unverified] tag (subset of #1)
   Schema: {ts, channel_id, bot_message_id, branch, response_text}

3. feedback_events.jsonl — every admin reaction on a bot message
   Schema: {ts, channel_id, bot_message_id, emoji, signal, admin_id}

Files are append-only. No record is ever mutated. Audit pipelines join
by bot_message_id.

Reactions from non-admin users are dropped silently — not even logged
as ignored. The admin_discord_id is read from config.get_config() once
at module init.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent.parent / "logs"
BOT_RESPONSES_LOG = LOGS_DIR / "bot_responses.jsonl"
BRANCH_AUDIT_LOG = LOGS_DIR / "branch_audit.jsonl"
FEEDBACK_EVENTS_LOG = LOGS_DIR / "feedback_events.jsonl"

# Branch tag regex — matches a leading [rejected], [searching], or
# [unverified] token. The tag is stripped from the chat-bound text and
# written to branch_audit.jsonl. The names map 1:1 to the FUSED-CORE
# three-branch classifier (Branch 1: rejected, Branch 2: searching,
# Branch 3: unverified) but stay human-readable in log output.
_BRANCH_TAG_RE = re.compile(r'^\[(rejected|searching|unverified)\]\s*')

# Admin-only feedback reactions. Anything else from anyone is ignored.
# 👎 / 👍 — engagement quality (should/shouldn't have responded)
# 🚫 / 🎯 — answer quality (engaged correctly but answer was wrong/right)
FEEDBACK_REACTIONS: dict[str, str] = {
    "👎": "bad_engagement",
    "👍": "good_engagement",
    "🚫": "bad_answer",
    "🎯": "good_answer",
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _append_jsonl(path: Path, record: dict) -> None:
    """Atomic-ish append to a JSONL file. Best-effort: any exception is
    logged and swallowed because feedback failures must never break the
    chat path."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Failed to append to %s: %s", path.name, exc)


def extract_branch_tag(text: str) -> tuple[str | None, str]:
    """If `text` starts with [rejected]/[searching]/[unverified], return
    (tag, text_without_tag). Otherwise return (None, text)."""
    m = _BRANCH_TAG_RE.match(text)
    if not m:
        return None, text
    tag = m.group(1)
    stripped = _BRANCH_TAG_RE.sub('', text, count=1)
    return tag, stripped


def log_bot_response(
    channel_id: str,
    bot_message_id: str,
    user_message_id: str,
    user_text: str,
    score: float | None,
    domains: list[str] | None,
    address_level: str,
    branch_tag: str | None,
) -> None:
    """One line per posted response. Called from Session._fire_on_text
    after send_message returns the message ID."""
    _append_jsonl(BOT_RESPONSES_LOG, {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "bot_message_id": bot_message_id,
        "user_message_id": user_message_id,
        "user_text": user_text[:500],
        "score": score,
        "domains": domains or [],
        "address_level": address_level,
        "branch_tag": branch_tag,
    })


def log_branch_audit(
    channel_id: str,
    bot_message_id: str,
    branch: str,
    response_text: str,
) -> None:
    """One line per tagged response. Subset of bot_responses — only
    fires when extract_branch_tag found a tag."""
    _append_jsonl(BRANCH_AUDIT_LOG, {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "bot_message_id": bot_message_id,
        "branch": branch,
        "response_text": response_text[:500],
    })


def log_feedback_event(
    channel_id: str,
    bot_message_id: str,
    emoji: str,
    admin_id: str,
) -> None:
    """One line per admin reaction on a bot message. Caller has already
    confirmed admin_id == cfg.admin_discord_id and emoji is in
    FEEDBACK_REACTIONS — this function does not re-validate."""
    _append_jsonl(FEEDBACK_EVENTS_LOG, {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "bot_message_id": bot_message_id,
        "emoji": emoji,
        "signal": FEEDBACK_REACTIONS[emoji],
        "admin_id": admin_id,
    })
