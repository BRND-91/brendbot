"""Tests for Phase 3 #2A/#2B — episodic memory store and retrieval."""
import sqlite3
from pathlib import Path

import pytest

from brendbot.episodes import (
    _RETENTION_PER_CHANNEL,
    _extract_entities,
    query_episodes,
    write_episode,
)


def _fresh_db(tmp_path: Path) -> Path:
    """Create a clean knowledge.db with the episodes schema."""
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE episodes (
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
    cur.execute(
        "CREATE INDEX idx_episodes_channel_ts ON episodes (channel, ts_end DESC)"
    )
    cur.execute("CREATE INDEX idx_episodes_domains ON episodes (domains)")
    conn.commit()
    conn.close()
    return db


# ── Entity extraction ────────────────────────────────────────────────────


def test_entity_extraction_pulls_capitalized_and_quoted():
    text = 'Brendan asked about "context restart" in the Phase 3 design'
    ents = _extract_entities(text)
    assert "Brendan" in ents
    assert "context restart" in ents
    assert "Phase" in ents


def test_entity_extraction_dedupes_case_insensitive():
    text = "Brendan and BRENDAN and brendan"
    ents = _extract_entities(text)
    lowered = [e.lower() for e in ents]
    assert lowered.count("brendan") == 1


def test_entity_extraction_caps_count():
    text = " ".join(f"Word{i}" for i in range(50))
    ents = _extract_entities(text, max_entities=5)
    assert len(ents) == 5


def test_entity_extraction_empty_input():
    assert _extract_entities("") == []
    assert _extract_entities(None) == []  # type: ignore


# ── write_episode ────────────────────────────────────────────────────────


def test_write_episode_round_trip(tmp_path):
    db = _fresh_db(tmp_path)
    turn_log = [
        {"role": "user", "text": "How does HVAC sizing work?"},
        {"role": "assistant", "text": "Manual J load calc first..."},
    ]
    ok = write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:00:00",
        turn_log=turn_log,
        domains=["BUILDSCI"],
        outcome="ok",
        db_path=db,
    )
    assert ok is True

    hits = query_episodes("ch1", domains=["BUILDSCI"], db_path=db)
    assert len(hits) == 1
    assert hits[0]["domains"] == "BUILDSCI"
    assert "HVAC" in hits[0]["summary"]
    assert "Manual J" in hits[0]["summary"]
    assert hits[0]["turn_count"] == 2


def test_write_episode_empty_turn_log_returns_false(tmp_path):
    db = _fresh_db(tmp_path)
    ok = write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:00:00",
        turn_log=[],
        domains=[],
        db_path=db,
    )
    assert ok is False


def test_write_episode_missing_db_returns_false(tmp_path):
    db = tmp_path / "nope.db"
    ok = write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:00:00",
        turn_log=[{"role": "user", "text": "hi"}],
        domains=[],
        db_path=db,
    )
    assert ok is False


def test_write_episode_outcome_persists(tmp_path):
    db = _fresh_db(tmp_path)
    write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:00:00",
        turn_log=[{"role": "user", "text": "hi"}],
        domains=[],
        outcome="rest_fired",
        db_path=db,
    )
    hits = query_episodes("ch1", domains=[], db_path=db)
    assert hits[0]["outcome"] == "rest_fired"


# ── Retention pruning ────────────────────────────────────────────────────


def test_retention_prunes_oldest_per_channel(tmp_path):
    """After N+5 writes to one channel, only the latest N survive."""
    db = _fresh_db(tmp_path)
    n_to_write = _RETENTION_PER_CHANNEL + 5
    for i in range(n_to_write):
        write_episode(
            channel="ch1",
            ts_start=f"2026-04-12T10:00:{i:02d}",
            turn_log=[
                {"role": "user", "text": f"msg {i}"},
                {"role": "assistant", "text": f"reply {i}"},
            ],
            domains=[],
            db_path=db,
        )
    # Confirm only N rows for ch1 survived.
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM episodes WHERE channel = ?", ("ch1",))
    count = cur.fetchone()[0]
    conn.close()
    assert count == _RETENTION_PER_CHANNEL


def test_retention_does_not_cross_channels(tmp_path):
    """Writing to ch1 must not prune ch2's episodes."""
    db = _fresh_db(tmp_path)
    # Seed ch2 with one episode.
    write_episode(
        channel="ch2",
        ts_start="2026-04-12T09:00:00",
        turn_log=[{"role": "user", "text": "ch2 msg"}],
        domains=[],
        db_path=db,
    )
    # Flood ch1 past retention.
    for i in range(_RETENTION_PER_CHANNEL + 10):
        write_episode(
            channel="ch1",
            ts_start=f"2026-04-12T10:00:{i:02d}",
            turn_log=[{"role": "user", "text": f"msg {i}"}],
            domains=[],
            db_path=db,
        )
    # ch2 episode must still be there.
    hits = query_episodes("ch2", domains=[], db_path=db)
    assert len(hits) == 1
    assert "ch2 msg" in hits[0]["summary"]


# ── query_episodes ───────────────────────────────────────────────────────


def test_query_filters_by_channel(tmp_path):
    db = _fresh_db(tmp_path)
    write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:00:00",
        turn_log=[{"role": "user", "text": "ch1 msg"}],
        domains=["BUILDSCI"],
        db_path=db,
    )
    write_episode(
        channel="ch2",
        ts_start="2026-04-12T10:00:00",
        turn_log=[{"role": "user", "text": "ch2 msg"}],
        domains=["BUILDSCI"],
        db_path=db,
    )
    hits = query_episodes("ch1", domains=["BUILDSCI"], db_path=db)
    assert len(hits) == 1
    assert "ch1 msg" in hits[0]["summary"]


def test_query_with_no_domains_returns_channel_history(tmp_path):
    db = _fresh_db(tmp_path)
    write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:00:00",
        turn_log=[{"role": "user", "text": "first"}],
        domains=["BUILDSCI"],
        db_path=db,
    )
    write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:01:00",
        turn_log=[{"role": "user", "text": "second"}],
        domains=["STATS"],
        db_path=db,
    )
    hits = query_episodes("ch1", domains=[], db_path=db)
    assert len(hits) == 2


def test_query_domain_overlap_match(tmp_path):
    db = _fresh_db(tmp_path)
    write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:00:00",
        turn_log=[{"role": "user", "text": "buildsci msg"}],
        domains=["BUILDSCI"],
        db_path=db,
    )
    write_episode(
        channel="ch1",
        ts_start="2026-04-12T10:01:00",
        turn_log=[{"role": "user", "text": "stats msg"}],
        domains=["STATS"],
        db_path=db,
    )
    # Query for BUILDSCI alone — only the first episode should match.
    hits = query_episodes("ch1", domains=["BUILDSCI"], db_path=db)
    assert len(hits) == 1
    assert "buildsci" in hits[0]["summary"]


def test_query_missing_db_returns_empty(tmp_path):
    db = tmp_path / "nope.db"
    hits = query_episodes("ch1", domains=["BUILDSCI"], db_path=db)
    assert hits == []


def test_query_respects_limit(tmp_path):
    db = _fresh_db(tmp_path)
    for i in range(10):
        write_episode(
            channel="ch1",
            ts_start=f"2026-04-12T10:{i:02d}:00",
            turn_log=[{"role": "user", "text": f"msg {i}"}],
            domains=["BUILDSCI"],
            db_path=db,
        )
    hits = query_episodes("ch1", domains=["BUILDSCI"], limit=3, db_path=db)
    assert len(hits) == 3
