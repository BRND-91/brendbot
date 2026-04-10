"""
Migrate FUSED-CORE JSON knowledge modules to SQLite.

Usage (from project root):
    python brendbot/knowledge/migrate_to_sqlite.py

Creates knowledge.db in the same directory. Each module's defs, facts, and
theorems become rows in normalized tables. A single kb-query SELECT returns
~200 bytes instead of reading an 11KB JSON file into context (55x improvement).
"""

import json
import sqlite3
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent
DB_PATH = KNOWLEDGE_DIR / "knowledge.db"

DATA_MODULES = ["LOGIC", "STATS", "SYSTEMS", "PERSONALITY", "BUILDSCI", "IMAGEGEN"]


def to_str(val):
    """Coerce any value to string for SQLite binding."""
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    return str(val)


def create_schema(conn):
    conn.executescript("""
        CREATE TABLE modules (id TEXT PRIMARY KEY, version TEXT, description TEXT, deps TEXT, source_map TEXT);
        CREATE TABLE definitions (id TEXT PRIMARY KEY, module_id TEXT, term TEXT, description TEXT, formal TEXT, source TEXT);
        CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, module_id TEXT, topic TEXT, name TEXT, description TEXT, source TEXT);
        CREATE TABLE theorems (id TEXT, module_id TEXT, name TEXT, description TEXT, formal TEXT, source TEXT, PRIMARY KEY (id, module_id));
        CREATE TABLE crosslinks (id INTEGER PRIMARY KEY AUTOINCREMENT, from_id TEXT, to_ids TEXT, link_type TEXT, note TEXT, module_id TEXT);
        CREATE TABLE governance_gates (id TEXT PRIMARY KEY, rule TEXT, fail_action TEXT, note TEXT);
        CREATE TABLE governance_provenance (tag TEXT PRIMARY KEY, meaning TEXT);
        CREATE VIRTUAL TABLE fts_knowledge USING fts5(module_id, entry_type, term, description, source);
    """)


def migrate_module(conn, module_id):
    path = KNOWLEDGE_DIR / f"{module_id}.json"
    if not path.exists():
        print(f"  SKIP {module_id}")
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = 0

    conn.execute("INSERT OR REPLACE INTO modules VALUES (?,?,?,?,?)",
        (module_id, to_str(data.get("v") or data.get("version", "")),
         to_str(data.get("desc") or data.get("description", "")),
         ",".join(data.get("deps", [])), json.dumps(data.get("src_map", {}))))

    for d in data.get("defs", []):
        did = to_str(d.get("id", ""))
        term = to_str(d.get("term") or d.get("t", ""))
        desc = to_str(d.get("desc") or d.get("d", ""))
        formal = to_str(d.get("formal") or d.get("f", ""))
        src = to_str(d.get("source") or d.get("s", ""))
        conn.execute("INSERT OR REPLACE INTO definitions VALUES (?,?,?,?,?,?)", (did, module_id, term, desc, formal, src))
        conn.execute("INSERT INTO fts_knowledge VALUES (?,?,?,?,?)", (module_id, "def", term, desc + (" " + formal if formal else ""), src))
        rows += 1

    raw_facts = data.get("facts", [])
    if isinstance(raw_facts, dict):
        for topic, items in raw_facts.items():
            for item in items:
                name = to_str(item.get("name") or item.get("n", ""))
                desc = to_str(item.get("desc") or item.get("d", ""))
                src = to_str(item.get("source") or item.get("s", ""))
                conn.execute("INSERT INTO facts (module_id,topic,name,description,source) VALUES (?,?,?,?,?)", (module_id, topic, name, desc, src))
                conn.execute("INSERT INTO fts_knowledge VALUES (?,?,?,?,?)", (module_id, "fact", name, desc, src))
                rows += 1
    elif isinstance(raw_facts, list):
        for block in raw_facts:
            topic = to_str(block.get("topic", ""))
            for item in block.get("items", []):
                name = to_str(item.get("name") or item.get("n", ""))
                desc = to_str(item.get("desc") or item.get("d", ""))
                src = to_str(item.get("source") or item.get("s", ""))
                conn.execute("INSERT INTO facts (module_id,topic,name,description,source) VALUES (?,?,?,?,?)", (module_id, topic, name, desc, src))
                conn.execute("INSERT INTO fts_knowledge VALUES (?,?,?,?,?)", (module_id, "fact", name, desc, src))
                rows += 1

    for t in data.get("thms", []):
        tid = to_str(t.get("id") or t.get("name", ""))
        desc = to_str(t.get("desc") or t.get("stmt") or t.get("d", ""))
        formal = to_str(t.get("formal") or t.get("f", ""))
        src = to_str(t.get("source") or t.get("s", ""))
        conn.execute("INSERT OR REPLACE INTO theorems VALUES (?,?,?,?,?,?)", (tid, module_id, tid, desc, formal, src))
        rows += 1

    for xl in data.get("xlinks", []):
        fr = to_str(xl.get("fr") or xl.get("from", ""))
        to = xl.get("to", [])
        if isinstance(to, list):
            to = ",".join(str(v) for v in to)
        lt = to_str(xl.get("ty") or xl.get("type", ""))
        nt = to_str(xl.get("nt") or xl.get("note", ""))
        conn.execute("INSERT INTO crosslinks (from_id,to_ids,link_type,note,module_id) VALUES (?,?,?,?,?)", (fr, to, lt, nt, module_id))
        rows += 1

    return rows


def migrate_governance(conn):
    path = KNOWLEDGE_DIR / "GOVERNANCE.json"
    if not path.exists():
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = 0

    for gid, gate in data.get("autogate", {}).get("gates", {}).items():
        conn.execute("INSERT OR REPLACE INTO governance_gates VALUES (?,?,?,?)",
            (gid, to_str(gate.get("rule", "")), to_str(gate.get("fail") or gate.get("fail_action", "")), to_str(gate.get("note", ""))))
        rows += 1

    for tag, meaning in data.get("provenance", {}).get("tags", {}).items():
        conn.execute("INSERT OR REPLACE INTO governance_provenance VALUES (?,?)", (to_str(tag), to_str(meaning)))
        rows += 1

    conn.execute("INSERT OR REPLACE INTO modules VALUES (?,?,?,?,?)",
        ("GOVERNANCE", data.get("version", ""), "Governance gates, provenance, dialect", "", ""))

    return rows


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"Removed existing {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    create_schema(conn)

    total = 0
    for mod_id in DATA_MODULES:
        count = migrate_module(conn, mod_id)
        print(f"  {mod_id}: {count} rows")
        total += count

    gov_count = migrate_governance(conn)
    print(f"  GOVERNANCE: {gov_count} rows")
    total += gov_count

    conn.commit()
    conn.close()
    print(f"\nDone. {total} total rows in {DB_PATH}")
    print(f"Database size: {DB_PATH.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
