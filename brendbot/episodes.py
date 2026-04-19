"""
Phase 3 #2A — episodic memory store.

Wraps the `episodes` table in brendbot/knowledge/knowledge.db. Two public
functions:

  - write_episode(...)  : called from Session._trigger_clean_restart at
                          context-restart time. Best-effort; failures are
                          logged but do not block the restart.
  - query_episodes(...) : called from SessionPool.route_message at
                          message-ingest time to fetch matching prior
                          episodes for retrieval cue scoring (Phase 3 #2B).

Schema mirrors the encoding-specificity principle: store with the cues
present at encoding time (channel, domains, entities), retrieve when
those cues match at recall time. No LLM inference at write or read.

Retention: keep last `_RETENTION_PER_CHANNEL` episodes per channel.
Older episodes are pruned at write time.
"""

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from brendbot import embedding_model

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "brendbot" / "knowledge" / "knowledge.db"

_RETENTION_PER_CHANNEL = 50

# Prefilter multiplier: fetch N*this from the lexical domain-LIKE tier
# then cosine-rerank to the caller's requested limit. 4x keeps the
# SQLite query cheap while giving the reranker enough candidates to
# promote semantically-close episodes over merely-recent-and-tagged
# ones.
_RERANK_PREFETCH = 4

# Cosine threshold for "semantically related" — below this we don't
# override lexical order. MiniLM on short summaries clusters closely
# for true matches (>0.6 common) so 0.35 is a generous floor that
# still excludes unrelated same-channel episodes.
_COSINE_MIN = 0.35

# Track migrated DB paths so we migrate each distinct database exactly
# once per process. A plain bool would stick on the first-seen DB and
# starve tests (or sidecar tools) that open fresh databases in the same
# process. Dropping down to resolve() handles symlinks / relative paths
# producing the same canonical key.
_migrated_paths: set[str] = set()


def _ensure_migrated(conn: sqlite3.Connection, db_key: str) -> None:
    """Add the `embedding` BLOB column if missing. Idempotent per
    `db_key`. Never raises — failed migration just means embedding
    columns won't be populated and the cosine re-rank will degrade to
    lexical order."""
    if db_key in _migrated_paths:
        return
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(episodes)")
        cols = {row[1] for row in cur.fetchall()}
        if "embedding" not in cols:
            cur.execute("ALTER TABLE episodes ADD COLUMN embedding BLOB")
            conn.commit()
            logger.info("episodes: added embedding BLOB column (%s)", db_key)
        _migrated_paths.add(db_key)
    except Exception as exc:
        logger.warning("episodes: migration check failed for %s: %s", db_key, exc)
        # Do not add to _migrated_paths — retry next call.

# Entity extraction: pull capitalized words and quoted strings from turn
# log text. Cheap, no LLM. Tuned for Discord chat — common nouns slip
# through but the retrieval scorer is forgiving.
_ENTITY_RE = re.compile(r'"([^"]{3,40})"|\b([A-Z][a-zA-Z0-9_]{2,30})\b')


def _extract_entities(text: str, max_entities: int = 10) -> list[str]:
    """Pull capitalized terms and quoted strings as candidate entities."""
    if not text:
        return []
    seen = set()
    out = []
    for m in _ENTITY_RE.finditer(text):
        ent = (m.group(1) or m.group(2) or "").strip()
        if ent and ent.lower() not in seen:
            seen.add(ent.lower())
            out.append(ent)
            if len(out) >= max_entities:
                break
    return out


def write_episode(
    channel: str,
    ts_start: str,
    turn_log: list[dict],
    domains: list[str],
    outcome: str = "ok",
    db_path: Optional[Path] = None,
) -> bool:
    """Write one episode row from a session's turn log.

    Returns True on success, False on failure (logged, never raised).

    `turn_log` is the Session._turn_log structure: list of dicts with
    keys "role" (user|assistant) and "text". We use the first user
    message and last assistant message as bookends; entities are
    extracted from both.
    """
    db = db_path or DB_PATH
    if not db.exists():
        logger.warning("episodes: knowledge.db not found at %s, skipping write", db)
        return False
    if not turn_log:
        return False

    first_user = next((t["text"] for t in turn_log if t.get("role") == "user"), "")
    last_assistant = next(
        (t["text"] for t in reversed(turn_log) if t.get("role") == "assistant"),
        "",
    )

    # Cap summary length — defensive against runaway turns.
    first_user = (first_user or "")[:500]
    last_assistant = (last_assistant or "")[:500]
    summary = f"{first_user}\n→\n{last_assistant}".strip()

    entities = _extract_entities(f"{first_user} {last_assistant}")
    entities_str = ",".join(entities)
    domains_str = ",".join(sorted(set(domains)))
    ts_end = datetime.now().isoformat(timespec="seconds")
    turn_count = len(turn_log)

    try:
        conn = sqlite3.connect(db)
        _ensure_migrated(conn, str(db))
        cur = conn.cursor()
        # Best-effort embedding — None when sentence-transformers is
        # unavailable. Retrieval-side re-rank handles missing blobs.
        embedding_blob = embedding_model.embed(summary) if summary else None
        cur.execute(
            """
            INSERT INTO episodes
            (channel, ts_start, ts_end, turn_count, domains, entities, summary, outcome, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel, ts_start, ts_end, turn_count, domains_str, entities_str, summary, outcome, embedding_blob),
        )

        # Retention: keep only the most recent N per channel.
        cur.execute(
            """
            DELETE FROM episodes
            WHERE channel = ?
              AND id NOT IN (
                  SELECT id FROM episodes
                  WHERE channel = ?
                  ORDER BY ts_end DESC
                  LIMIT ?
              )
            """,
            (channel, channel, _RETENTION_PER_CHANNEL),
        )

        conn.commit()
        conn.close()
        logger.info(
            "episode written: channel=%s turns=%d domains=%s entities=%d",
            channel, turn_count, domains_str or "none", len(entities),
        )
        return True
    except Exception as exc:
        logger.warning("episode write failed for channel %s: %s", channel, exc)
        return False


def query_episodes(
    channel: str,
    domains: list[str],
    limit: int = 3,
    db_path: Optional[Path] = None,
    query_text: str = "",
) -> list[dict]:
    """Phase 3 #2B — fetch matching prior episodes for retrieval cue scoring.

    Returns up to `limit` episodes for `channel` whose stored domains
    overlap with the provided `domains` list. If `domains` is empty,
    the candidate pool is simply the most recent N episodes for the
    channel (channel context alone).

    Ranking:
      1. Lexical prefilter (domain LIKE / channel) fetches `limit *
         _RERANK_PREFETCH` most-recent candidates.
      2. If `query_text` is provided AND the embedding model is
         available, candidates are re-ranked by cosine similarity of
         their stored embedding to the embedded query, filtered to
         score >= _COSINE_MIN, and truncated to `limit`.
      3. Otherwise the prefilter order (recency) is used.

    Callers that pre-date the embedding tier can omit `query_text`; the
    function then behaves exactly like the original lexical-only
    implementation so the upgrade is backward compatible.

    Returns empty list on any failure — never raises.
    """
    db = db_path or DB_PATH
    if not db.exists():
        return []

    prefetch_limit = limit * _RERANK_PREFETCH

    try:
        conn = sqlite3.connect(db)
        _ensure_migrated(conn, str(db))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        if domains:
            # Match if any provided domain appears in stored domains string.
            # SQLite has no array intersect; use LIKE chain. Cheap at our scale.
            like_clauses = " OR ".join(["domains LIKE ?" for _ in domains])
            params: list = [channel]
            params.extend([f"%{d}%" for d in domains])
            params.append(prefetch_limit)
            query = f"""
                SELECT id, ts_end, turn_count, domains, entities, summary, outcome, embedding
                FROM episodes
                WHERE channel = ? AND ({like_clauses})
                ORDER BY ts_end DESC
                LIMIT ?
            """
        else:
            params = [channel, prefetch_limit]
            query = """
                SELECT id, ts_end, turn_count, domains, entities, summary, outcome, embedding
                FROM episodes
                WHERE channel = ?
                ORDER BY ts_end DESC
                LIMIT ?
            """

        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as exc:
        logger.warning("episode query failed for channel %s: %s", channel, exc)
        return []

    # Semantic re-rank. Skipped when there's nothing to embed, when the
    # model isn't available, or when no rows carry an embedding blob.
    if query_text and rows and embedding_model.is_available():
        query_vec = embedding_model.embed_vector(query_text)
        if query_vec is not None:
            scored: list[tuple[float, dict]] = []
            for r in rows:
                score = embedding_model.cosine_similarity(
                    r.get("embedding"), query_vec,
                )
                if score >= _COSINE_MIN:
                    scored.append((score, r))
            if scored:
                scored.sort(key=lambda t: t[0], reverse=True)
                ranked = [r for _, r in scored[:limit]]
                # Strip embedding blob from the returned dicts — callers
                # want display data, not ~1.5KB of float32 per episode.
                for r in ranked:
                    r.pop("embedding", None)
                return ranked

    # Fallback: lexical/recency order, truncated to requested limit.
    for r in rows:
        r.pop("embedding", None)
    return rows[:limit]
