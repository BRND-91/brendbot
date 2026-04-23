"""Regression tests for SQLite WAL + busy_timeout hardening.

The bot opens three separate code paths against knowledge.db
(user_registry, episodes, and the grounded-facts reader in session.py).
Without WAL mode and a non-zero busy_timeout, concurrent writes from the
asyncio loop surface as `OperationalError: database is locked` under
burst traffic.

These tests exercise the write-heavy paths under contention and assert
no lock errors are raised. The fixtures monkeypatch the hard-coded
DB_PATH in each module so the test operates against a throwaway
database in tmp_path rather than the production knowledge.db.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest


def _init_user_registry_db(db_path: Path) -> None:
    """Create a knowledge.db with the user_registry schema (matches the
    CREATE TABLE in brendbot/user_registry.py)."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
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
            guild_ids     TEXT DEFAULT '',
            notes         TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


def _init_episodes_db(db_path: Path) -> None:
    """Create a knowledge.db with the episodes schema (mirrors the existing
    fixture in tests/test_episodes.py)."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
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
    conn.execute(
        "CREATE INDEX idx_episodes_channel_ts ON episodes (channel, ts_end DESC)"
    )
    conn.execute("CREATE INDEX idx_episodes_domains ON episodes (domains)")
    conn.commit()
    conn.close()


def test_user_registry_conn_sets_wal_and_busy_timeout(tmp_path, monkeypatch):
    """Every _conn() call must open in WAL mode with a non-zero busy_timeout.
    This is the primary invariant the hardening establishes."""
    db_path = tmp_path / "knowledge.db"
    _init_user_registry_db(db_path)

    from brendbot import user_registry
    monkeypatch.setattr(user_registry, "DB_PATH", db_path)

    conn = user_registry._conn()
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        conn.close()

    assert journal_mode.lower() == "wal", f"expected WAL mode, got {journal_mode!r}"
    assert busy_timeout >= 5000, f"expected busy_timeout>=5000ms, got {busy_timeout}"
    # synchronous=NORMAL is mode 1 in SQLite's numeric encoding.
    assert synchronous == 1, f"expected synchronous=NORMAL (1), got {synchronous}"


def test_episodes_open_sets_wal_and_busy_timeout(tmp_path):
    """episodes._open() is the shared helper for read and write paths; it
    must apply the same concurrency pragmas as user_registry._conn()."""
    db_path = tmp_path / "knowledge.db"
    _init_episodes_db(db_path)

    from brendbot import episodes
    conn = episodes._open(db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()

    assert journal_mode.lower() == "wal"
    assert busy_timeout >= 5000


def test_concurrent_user_registry_writes_do_not_lock(tmp_path, monkeypatch):
    """Fifty concurrent record_user calls against the same DB must not raise
    OperationalError. This is the production failure mode — message bursts
    spawn concurrent writes from the asyncio loop and (before WAL+timeout)
    the second writer onward gets `database is locked`."""
    db_path = tmp_path / "knowledge.db"
    _init_user_registry_db(db_path)

    from brendbot import user_registry
    monkeypatch.setattr(user_registry, "DB_PATH", db_path)

    errors: list[Exception] = []

    def writer(i: int) -> None:
        try:
            user_registry.record_user(
                user_id=f"user_{i}",
                display_name=f"User {i}",
                username=f"u{i}",
                tier="default",
                domains=["BUILDSCI"] if i % 2 == 0 else [],
                guild_id="9999",
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)

    assert not errors, f"writes raised {len(errors)} error(s): {errors[:3]}"

    # Confirm the writes actually landed.
    conn = sqlite3.connect(db_path)
    (count,) = conn.execute("SELECT COUNT(*) FROM user_registry").fetchone()
    conn.close()
    assert count == 50, f"expected 50 rows written, got {count}"


def test_concurrent_write_episode_does_not_lock(tmp_path):
    """Same contention test against episodes.write_episode. write_episode
    accepts a db_path override, so no monkeypatch is needed here."""
    db_path = tmp_path / "knowledge.db"
    _init_episodes_db(db_path)

    from brendbot import episodes

    errors: list[Exception] = []
    results: list[bool] = []

    def writer(i: int) -> None:
        try:
            ok = episodes.write_episode(
                channel=f"channel_{i % 5}",   # cluster into 5 channels to
                                               # exercise retention pruning
                                               # under contention
                ts_start="2026-04-16T00:00:00",
                turn_log=[
                    {"role": "user", "text": f"hello {i}"},
                    {"role": "assistant", "text": f"hi {i}"},
                ],
                domains=["BUILDSCI"],
                outcome="ok",
                db_path=db_path,
            )
            results.append(ok)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)

    assert not errors, f"episode writes raised {len(errors)} error(s): {errors[:3]}"
    assert all(results), "at least one write_episode returned False"


def test_concurrent_asyncio_record_user(tmp_path, monkeypatch):
    """Same invariant as test_concurrent_user_registry_writes_do_not_lock
    but exercised through asyncio.gather, which is how production traffic
    reaches record_user (from discord.py's on_message handler)."""
    db_path = tmp_path / "knowledge.db"
    _init_user_registry_db(db_path)

    from brendbot import user_registry
    monkeypatch.setattr(user_registry, "DB_PATH", db_path)

    async def write_one(i: int) -> None:
        # record_user is sync — schedule it on the default executor so
        # contention is real rather than cooperatively serialised.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            user_registry.record_user,
            f"async_user_{i}", f"AsyncUser{i}", f"au{i}", "default", [], "8888",
        )

    async def run() -> None:
        await asyncio.gather(*(write_one(i) for i in range(50)))

    asyncio.run(run())

    conn = sqlite3.connect(db_path)
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM user_registry WHERE user_id LIKE 'async_user_%'"
    ).fetchone()
    conn.close()
    assert count == 50


def test_flagged_path_not_in_engagement_yaml(tmp_path):
    """Post-2026-04-23 strip: ``content_gate.flagged_path`` must not
    reappear in engagement.yaml. The FLAG reroute to a soul-stripped
    sonnet-4-6 call produced confident out-of-character output and was
    deleted entirely. This guard catches accidental reintroduction
    (e.g. if someone copy-pastes the block back from the 2026-04-22
    version of the yaml during a merge conflict resolution).

    Previously ``test_flagged_path_model_is_unpinned`` checked that the
    model wasn't pinned to a dated snapshot. That check is obsolete —
    there's no flagged_path to pin."""
    import yaml

    cfg_path = Path(__file__).resolve().parent.parent / "engagement.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    assert "flagged_path" not in cfg.get("content_gate", {}), (
        "flagged_path block reappeared in engagement.yaml — the FLAG "
        "reroute was deleted in the 2026-04-23 strip and should not "
        "come back. See CLEANUP_LOG.md."
    )
