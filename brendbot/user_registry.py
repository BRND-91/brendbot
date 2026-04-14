"""Server user registry — persistent, per-user engagement profile.

Every Discord user seen in any on_message event (sender, @mention target,
or reply author) is upserted here. The registry accumulates across bot
restarts and serves two purposes:

1. @mention disambiguation: the bot can resolve snowflake IDs in message
   text to display names at ingest time, enabling the engagement gate to
   correctly identify whether a mention targets it or another user without
   running a tool call inside the session.

2. Per-user engagement priors: accumulated interaction counts, domain
   history, and admin-feedback signals feed back into the haiku classifier
   prompt as a compact user context block, letting the model recognise
   regulars and calibrate engagement style over time.

Schema (SQLite — shares knowledge.db):

  CREATE TABLE IF NOT EXISTS user_registry (
      user_id       TEXT PRIMARY KEY,
      display_name  TEXT NOT NULL,
      username      TEXT,
      tier          TEXT DEFAULT 'default',
      first_seen    TEXT,              -- ISO timestamp
      last_seen     TEXT,             -- ISO timestamp
      msg_count     INTEGER DEFAULT 0, -- total messages seen (not just engaged)
      engaged_count INTEGER DEFAULT 0, -- turns the bot responded to
      domains_seen  TEXT DEFAULT '',   -- comma-joined module IDs ever matched
      notes         TEXT DEFAULT ''    -- admin-written freeform (future)
  );

The registry is populated by `record_user` (called from discord.py on_message)
and `record_engagement` (called from session.py _fire_on_text).
`get_display_name` resolves a snowflake to a display name for @mention
disambiguation. `compact_table` renders a short text block injected into
the session system prompt so the model knows who's who without tool calls.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "brendbot" / "knowledge" / "knowledge.db"

# Max users included in compact_table injected into session prompt.
# At ~40 chars per entry this is ~800 chars for 20 users — well within
# the budget. Ordered by last_seen DESC so regulars appear first.
_COMPACT_TABLE_MAX = 20

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS user_registry (
    user_id       TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    username      TEXT,
    tier          TEXT DEFAULT 'default',
    first_seen    TEXT,
    last_seen     TEXT,
    msg_count     INTEGER DEFAULT 0,
    engaged_count INTEGER DEFAULT 0,
    domains_seen  TEXT DEFAULT '',
    notes         TEXT DEFAULT ''
)
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_SQL)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_user(
    user_id: str,
    display_name: str,
    username: str = "",
    tier: str = "default",
    domains: list[str] | None = None,
) -> None:
    """Upsert a user record on every message seen. Increments msg_count,
    updates last_seen and display_name (users can change their nicknames),
    and merges any new domain IDs into domains_seen. Best-effort — never
    raises; failures are logged and swallowed so the hot message path is
    never blocked."""
    if not user_id:
        return
    try:
        conn = _conn()
        now = _now()
        existing = conn.execute(
            "SELECT domains_seen, msg_count FROM user_registry WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if existing is None:
            merged_domains = ",".join(sorted(set(domains or [])))
            conn.execute(
                """INSERT INTO user_registry
                   (user_id, display_name, username, tier, first_seen, last_seen,
                    msg_count, engaged_count, domains_seen)
                   VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)""",
                (user_id, display_name, username, tier, now, now, merged_domains),
            )
        else:
            existing_domains = set(
                d for d in (existing["domains_seen"] or "").split(",") if d
            )
            new_domains = existing_domains | set(domains or [])
            merged_domains = ",".join(sorted(new_domains))
            conn.execute(
                """UPDATE user_registry
                   SET display_name = ?, username = ?, tier = ?, last_seen = ?,
                       msg_count = msg_count + 1, domains_seen = ?
                   WHERE user_id = ?""",
                (display_name, username, tier, now, merged_domains, user_id),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("user_registry.record_user failed for %s: %s", user_id, exc)


def record_engagement(user_id: str) -> None:
    """Increment engaged_count for user_id. Called from session._fire_on_text
    after the bot successfully posts a response to a message from this user."""
    if not user_id:
        return
    try:
        conn = _conn()
        conn.execute(
            "UPDATE user_registry SET engaged_count = engaged_count + 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("user_registry.record_engagement failed for %s: %s", user_id, exc)


def get_display_name(user_id: str) -> Optional[str]:
    """Return the stored display name for a snowflake, or None if unknown.
    Used by discord.py to resolve @mention snowflakes in message text before
    the engagement gate runs, so the bot can tell whether a mention targets
    it or another server member."""
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT display_name FROM user_registry WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
        return row["display_name"] if row else None
    except Exception as exc:
        logger.debug("user_registry.get_display_name failed for %s: %s", user_id, exc)
        return None


def resolve_mentions(text: str, bot_id: str) -> dict[str, str]:
    """Extract all <@snowflake> patterns from text and return a dict mapping
    each snowflake to its display name (or the raw snowflake if unknown).
    Also indicates whether any mention targets the bot itself.

    Used by the engagement gate and message wrapper to tell the model
    exactly who is being mentioned without a tool call.

    Returns: {snowflake: display_name_or_raw}
    """
    import re
    pattern = re.compile(r"<@!?(\d+)>")
    out: dict[str, str] = {}
    for m in pattern.finditer(text):
        uid = m.group(1)
        if uid == bot_id:
            out[uid] = "brendbot"
        else:
            name = get_display_name(uid)
            out[uid] = name if name else uid
    return out


def compact_table(bot_id: str = "") -> str:
    """Return a short, newline-separated text block mapping snowflake IDs to
    display names for the _COMPACT_TABLE_MAX most-recently-active users.
    Injected into session system prompt so the model can resolve @mentions
    at reasoning time without a Bash/Read tool call.

    Format: one line per user:
      <user id="..." name="..." tier="..." msgs="..." engaged="..." domains="..."/>

    Omits the bot's own entry if bot_id is provided. Returns empty string
    if the registry is empty or the DB isn't initialised yet."""
    try:
        conn = _conn()
        rows = conn.execute(
            """SELECT user_id, display_name, tier, msg_count, engaged_count, domains_seen
               FROM user_registry
               WHERE user_id != ?
               ORDER BY last_seen DESC
               LIMIT ?""",
            (bot_id or "", _COMPACT_TABLE_MAX),
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["<server_users>"]
        for r in rows:
            domains = r["domains_seen"] or ""
            lines.append(
                f'  <user id="{r["user_id"]}" name="{r["display_name"]}" '
                f'tier="{r["tier"]}" msgs="{r["msg_count"]}" '
                f'engaged="{r["engaged_count"]}"'
                + (f' domains="{domains}"' if domains else "")
                + "/>"
            )
        lines.append("</server_users>")
        return "\n".join(lines)
    except Exception as exc:
        logger.debug("user_registry.compact_table failed: %s", exc)
        return ""
