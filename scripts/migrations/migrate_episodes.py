"""
Phase 3 #2A — episodic memory schema migration.

Adds the `episodes` table to brendbot/knowledge/knowledge.db. Idempotent —
safe to re-run; uses CREATE TABLE IF NOT EXISTS.

Usage (from project root):
    python scripts/migrations/migrate_episodes.py

Schema rationale: each row is one closed session segment between context
restarts. The session writes one row at restart time with the raw cues
that future sessions can use for retrieval (channel, domains, entities)
plus rule-based bookends (first user message, last bot message). No LLM
inference happens at write time — keeps the restart path cheap.

Retrieval is matched on (channel, domains overlap) at message-ingest time
by the receive loop, mirroring the encoding-specificity principle: store
with rich contextual cues, retrieve when the cue at recall time matches
the context at encoding time.
"""

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "brendbot" / "knowledge" / "knowledge.db"


def migrate() -> None:
    if not DB_PATH.exists():
        raise SystemExit(
            f"knowledge.db not found at {DB_PATH}. "
            f"Run scripts/migrations/migrate_to_sqlite.py first."
        )

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            ts_start TEXT NOT NULL,
            ts_end TEXT NOT NULL,
            turn_count INTEGER NOT NULL DEFAULT 0,
            domains TEXT NOT NULL DEFAULT '',
            entities TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT 'ok'
        )
    """)

    # Index on (channel, ts_end DESC) for fast "most recent N for this channel"
    # lookups during retrieval cue scoring.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_episodes_channel_ts
        ON episodes (channel, ts_end DESC)
    """)

    # Index on domains for the WHERE-clause filter at retrieval time.
    # SQLite doesn't have GIN; this is a plain text index on the comma-joined
    # domains string. Cardinality is low (max 7 domains × N channels) so a
    # full LIKE scan is acceptable for the expected scale.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_episodes_domains
        ON episodes (domains)
    """)

    conn.commit()

    # Report row count for sanity.
    cur.execute("SELECT COUNT(*) FROM episodes")
    count = cur.fetchone()[0]
    print(f"episodes table OK ({count} rows)")

    conn.close()


if __name__ == "__main__":
    migrate()
