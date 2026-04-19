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

# Phase 2b — gate outcome taxonomy.
# Canonical set of engagement-gate decisions recorded under the
# `gate_outcome` field in bot_responses.jsonl (when the gate ultimately
# engaged) and skip_decisions.jsonl (when it dropped). Every path in
# discord.py's on_message that returns OR dispatches to the session
# corresponds to exactly one of these strings.
#
# Values are intentionally free-form strings rather than an Enum so that
# downstream consumers (jq, pandas, BigQuery) can filter on literal
# values without a schema migration. Adding a new outcome to this set
# is a non-breaking log change — existing consumers that filter on the
# known set will simply see the new value as "other".
#
# Engaged paths (written to bot_responses.jsonl):
#   hard_pass_at_mention — message contained @bot mention
#   hard_pass_score      — heuristic score ≥ ENGAGE_HARD_PASS
#   pregate_yes          — deterministic pregate said engage (Phase 3,
#                          reserved — no heuristic returns True today)
#   haiku_yes            — middle-band message, classifier said engage
#   haiku_error_escalate — classifier failed, but score ≥ 0.6 so
#                          engaged anyway under fail-loud policy
#   dm_always_engage     — DM path, no engagement gate runs
#
# Skip paths (written to skip_decisions.jsonl):
#   hard_drop                 — heuristic score < ENGAGE_THRESHOLD
#   pregate_no                — deterministic pregate rejected the message
#                               before haiku ran (Phase 3; e.g. short
#                               pleasantry filler in an active thread)
#   haiku_no                  — classifier said don't-engage
#   haiku_error_low_score     — classifier failed AND score < 0.6
#   bot_author_not_mentioned  — another bot posted and didn't address us
#   wrong_mention_target      — guild @mention addressed to another user
GATE_OUTCOMES: frozenset[str] = frozenset({
    "hard_pass_at_mention",
    "hard_pass_score",
    "pregate_yes",
    "haiku_yes",
    "haiku_error_escalate",
    "dm_always_engage",
    "hard_drop",
    "pregate_no",
    "haiku_no",
    "haiku_error_low_score",
    "bot_author_not_mentioned",
    "wrong_mention_target",
})

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
    modules_queried: list[str] | None = None,
    haiku_invoked: bool = False,
    gate_outcome: str | None = None,
) -> None:
    """One line per posted response. Called from Session._fire_on_text
    and _fire_on_text_streamed after send_message returns the message ID.

    Observability fields added for flow-class / fabrication-risk diagnostics:

      modules_queried: KB modules actually hit via kb-query this turn.
        Non-empty means the answer was grounded in the knowledge base.
        Empty with non-empty `domains` means the model answered from
        training weights despite a domain keyword match — the
        "weight-carried" failure mode.

      flow_class (derived):
        - "no_domain":       domain_hint was empty; no grounding expected
        - "module_sourced":  domain matched AND KB was queried
        - "weight_carried":  domain matched AND KB was NOT queried
                             (the devourer-mimicking-source failure mode)

      fabrication_risk (derived): True when haiku_invoked AND domains
        non-empty AND modules_queried empty AND no branch_tag. That's
        the shape of a turn where the bot engaged ambiguously, matched
        a domain, skipped the KB, and produced untagged output — the
        highest-risk profile for fabrication."""
    domains = domains or []
    modules_queried = modules_queried or []
    if not domains:
        flow_class = "no_domain"
    elif modules_queried:
        flow_class = "module_sourced"
    else:
        flow_class = "weight_carried"
    fabrication_risk = bool(
        haiku_invoked and domains and not modules_queried and not branch_tag
    )
    record = {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "bot_message_id": bot_message_id,
        "user_message_id": user_message_id,
        "user_text": user_text[:500],
        "score": score,
        "domains": domains,
        "address_level": address_level,
        "branch_tag": branch_tag,
        "modules_queried": modules_queried,
        "haiku_invoked": haiku_invoked,
        "flow_class": flow_class,
        "fabrication_risk": fabrication_risk,
    }
    # Phase 2b — gate_outcome is omitted when None so pre-Phase-2b log
    # consumers see no shape change. Values are one of the canonical
    # strings defined in GATE_OUTCOMES below; free-form strings are
    # written through unchanged so new paths can add outcomes without
    # a coordinated feedback.py bump.
    if gate_outcome is not None:
        record["gate_outcome"] = gate_outcome
    _append_jsonl(BOT_RESPONSES_LOG, record)


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
    gate_outcome: str | None = None,
) -> None:
    """One line per engagement-gate drop. Written from discord.py's
    on_message handler at any return path that skipped generation:
    hard drop (score below threshold), haiku NO, or haiku error with
    insufficient score for fail-loud escalation.

    reason is a short tag identifying the drop path: 'hard_drop',
    'haiku_no', 'haiku_error_low_score', 'bot_author_not_mentioned',
    'wrong_mention_target', 'other'. Downstream training-data export
    joins this stream against bot_responses.jsonl to build balanced
    (engage, skip) pairs.

    Phase 2b — gate_outcome duplicates `reason` for skip decisions but
    lives under the unified taxonomy shared with bot_responses.jsonl
    (see GATE_OUTCOMES). Keeping both lets existing `reason`-joining
    queries work unchanged while new cross-stream analytics can pivot
    on gate_outcome as a single column."""
    record = {
        "ts": _now_iso(),
        "channel_id": channel_id,
        "sender_id": sender_id,
        "user_message_id": user_message_id,
        "user_text": user_text[:500],
        "score": score,
        "reason": reason,
        "domains": domains or [],
    }
    if gate_outcome is not None:
        record["gate_outcome"] = gate_outcome
    _append_jsonl(SKIP_DECISIONS_LOG, record)
