"""Feedback infrastructure: five append-only JSONL streams.

Five independent log streams, joined at audit time by bot_message_id:

1. bot_responses.jsonl   — every response the bot posts
   Schema: {ts, channel_id, bot_message_id, user_message_id, user_text,
            score, domains, address_level, branch_tag}

2. branch_audit.jsonl    — every response that began with a
                           [rejected]/[searching]/[unverified]/[flagged]/
                           [bypass] tag (subset of #1)
   Schema: {ts, channel_id, bot_message_id, branch, response_text}

3. feedback_events.jsonl — every admin reaction on a bot message
   Schema: {ts, channel_id, bot_message_id, emoji, signal, admin_id}

4. flag_audit.jsonl      — every content-gate FLAG outcome (2-of-3 band,
                           routed to looser-safety model via reroute)
   Schema: {ts, channel_id, user_message_id, user_text, admin_sender_id,
            tier, criteria_tripped, weighted_sum, flagged_model,
            bot_message_id, session_flag_count}

5. bypass_audit.jsonl    — every admin *brend* italic-bypass invocation
                           (admin-only backdoor, uncapped, hard-floors
                           still enforced). Shadow-runs the classifier
                           so would_have_* fields record the normal
                           gate's decision for audit review.
   Schema: {ts, channel_id, user_message_id, user_text, admin_sender_id,
            tier, would_have_tripped, would_have_summed,
            would_have_outcome, hard_floor_hit, bot_message_id}

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
FLAG_AUDIT_LOG = LOGS_DIR / "flag_audit.jsonl"
BYPASS_AUDIT_LOG = LOGS_DIR / "bypass_audit.jsonl"
# Negative-example stream — one row per engagement-gate drop. Pairs with
# bot_responses.jsonl (positive examples) to form a balanced training
# corpus for replacing the haiku ambiguity classifier with a local model.
# Written from discord.py's on_message drop paths via log_skip_decision.
SKIP_DECISIONS_LOG = LOGS_DIR / "skip_decisions.jsonl"

# Branch tag regex — matches a leading tag token. The tag is stripped from
# the chat-bound text and written to branch_audit.jsonl.
# rejected/searching/unverified: FUSED-CORE three-branch classifier.
# flagged: content-gate middle-band reroute to looser-safety model.
# bypass: admin-only *brend* italic backdoor.
# uncertain: metacognitive confidence self-assessment (low confidence).
_BRANCH_TAG_RE = re.compile(r'^\[(rejected|searching|unverified|flagged|bypass|uncertain)\]\s*')

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
    """If `text` starts with a branch tag ([rejected]/[searching]/
    [unverified]/[flagged]/[bypass]/[uncertain]), return
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


def log_flag_event(
    channel_id: str,
    user_message_id: str,
    user_text: str,
    admin_sender_id: str,
    tier: str,
    criteria_tripped: dict[str, float],
    weighted_sum: float,
    flagged_model: str,
    bot_message_id: str | None,
    session_flag_count: int,
) -> None:
    """One line per content-gate FLAG outcome. The gate classifier tagged
    the request as 2-of-3 weight-band (above pass_threshold, at or below
    flag_threshold) and the request was routed through the flagged path
    on the looser-safety model. bot_message_id is None if dispatch failed
    or the request was refused after hard-floor re-check at flag time."""
    _append_jsonl(FLAG_AUDIT_LOG, {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "user_message_id": user_message_id,
        "user_text": user_text[:500],
        "admin_sender_id": admin_sender_id,
        "tier": tier,
        "criteria_tripped": criteria_tripped,
        "weighted_sum": weighted_sum,
        "flagged_model": flagged_model,
        "bot_message_id": bot_message_id,
        "session_flag_count": session_flag_count,
    })


def log_bypass_event(
    channel_id: str,
    user_message_id: str,
    user_text: str,
    admin_sender_id: str,
    tier: str,
    would_have_tripped: dict[str, float] | None,
    would_have_summed: float | None,
    would_have_outcome: str | None,
    hard_floor_hit: str | None,
    bot_message_id: str | None,
) -> None:
    """One line per admin-bypass invocation. The *brend* italic token was
    detected in an admin-tier message and the weighted content gate was
    skipped. The classifier is still run (in shadow mode) so would_have_*
    fields record what the normal gate would have decided — this lets
    audit reviewers see which bypasses actually exercised the gate vs
    which were admin testing benign prompts.

    hard_floor_hit is non-None if a hard-floor criterion matched despite
    the bypass; in that case the request was still refused and
    bot_message_id will be None."""
    _append_jsonl(BYPASS_AUDIT_LOG, {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "user_message_id": user_message_id,
        "user_text": user_text[:500],
        "admin_sender_id": admin_sender_id,
        "tier": tier,
        "would_have_tripped": would_have_tripped or {},
        "would_have_summed": would_have_summed,
        "would_have_outcome": would_have_outcome,
        "hard_floor_hit": hard_floor_hit,
        "bot_message_id": bot_message_id,
    })


def log_skip_decision(
    channel_id: str,
    sender_id: str,
    user_message_id: str,
    user_text: str,
    score: float | None,
    reason: str,
    domains: list[str] | None = None,
) -> None:
    """One line per engagement-gate drop. Written from discord.py's
    on_message handler at any return path that skipped generation:
    hard drop (score below threshold), haiku NO, or haiku error with
    insufficient score for fail-loud escalation.

    reason is a short tag identifying the drop path: 'hard_drop',
    'haiku_no', 'haiku_error_low_score', 'bot_author_not_mentioned',
    'other'. Downstream training-data export joins this stream against
    bot_responses.jsonl to build balanced (engage, skip) pairs."""
    _append_jsonl(SKIP_DECISIONS_LOG, {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "sender_id": sender_id,
        "user_message_id": user_message_id,
        "user_text": user_text[:500],
        "score": score,
        "reason": reason,
        "domains": domains or [],
    })
