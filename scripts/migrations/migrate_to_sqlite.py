"""
Migrate FUSED-CORE JSON knowledge modules to SQLite.

Usage (from project root):
    python scripts/migrations/migrate_to_sqlite.py

Creates knowledge.db inside brendbot/knowledge/. Each module's defs, facts, and
theorems become rows in normalized tables. A single kb-query SELECT returns
~200 bytes instead of reading an 11KB JSON file into context (55x improvement).
"""

import json
import re
import sqlite3
from pathlib import Path

# Script lives in scripts/migrations/; knowledge data lives in brendbot/knowledge/.
# Walk up two levels from this file to project root, then into the package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "brendbot" / "knowledge"
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
        CREATE TABLE theorems (id TEXT, module_id TEXT, name TEXT, description TEXT, formal TEXT, source TEXT, extended INTEGER DEFAULT 0, PRIMARY KEY (id, module_id));
        CREATE TABLE crosslinks (id INTEGER PRIMARY KEY AUTOINCREMENT, from_id TEXT, to_ids TEXT, link_type TEXT, note TEXT, module_id TEXT);
        CREATE TABLE governance_gates (id TEXT PRIMARY KEY, rule TEXT, fail_action TEXT, note TEXT);
        CREATE TABLE governance_provenance (tag TEXT PRIMARY KEY, meaning TEXT);
        CREATE VIRTUAL TABLE fts_knowledge USING fts5(module_id, entry_type, term, description, source);
        CREATE TABLE imagegen_config (section TEXT PRIMARY KEY, data JSON, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE memory_fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            tag TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_memory_source ON memory_fragments (source);
        CREATE INDEX idx_memory_tag ON memory_fragments (tag);

        -- Image generation prompt construction tables
        -- Named style → descriptor translation. Core terms are what the model
        -- reliably responds to; negative_space is what to explicitly exclude.
        CREATE TABLE prompt_styles (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            core_descriptors TEXT NOT NULL,
            supplemental_descriptors TEXT,
            negative_space TEXT,
            fail_modes TEXT,
            model_tier TEXT,
            notes TEXT
        );

        -- Known failure class → remediation strategy mapping.
        -- failure_class matches what the bot reports in render_outcomes.
        -- remediation is the concrete prompt adjustment to make on retry.
        CREATE TABLE render_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            failure_class TEXT NOT NULL UNIQUE,
            trigger_pattern TEXT,
            remediation TEXT NOT NULL,
            example TEXT
        );

        -- Empirical outcome log. Populated by the bot at generation time.
        -- Enables per-request history queries and long-term pattern detection.
        CREATE TABLE render_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_hash TEXT,
            attempt_number INTEGER DEFAULT 1,
            prompt_used TEXT,
            style_ids TEXT,
            constraint_score INTEGER,
            succeeded INTEGER DEFAULT 0,
            failure_class TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX idx_render_request ON render_outcomes (request_hash);
        CREATE INDEX idx_render_style ON render_outcomes (style_ids);

        -- FTS over prompt_styles for label/descriptor search
        CREATE VIRTUAL TABLE fts_prompts USING fts5(
            id, label, core_descriptors, supplemental_descriptors, notes
        );
    """)


def migrate_module(conn, module_id):
    path = KNOWLEDGE_DIR / f"{module_id}.json"
    if not path.exists():
        print(f"  SKIP {module_id}")
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = 0

    # src_map can be at top level or nested under prov
    src_map = data.get("src_map") or data.get("prov", {}).get("src_map", {})
    conn.execute("INSERT OR REPLACE INTO modules VALUES (?,?,?,?,?)",
        (module_id, to_str(data.get("v") or data.get("version", "")),
         to_str(data.get("desc") or data.get("description", "")),
         ",".join(data.get("deps", [])), json.dumps(src_map)))

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
        conn.execute("INSERT OR REPLACE INTO theorems VALUES (?,?,?,?,?,?,?)", (tid, module_id, tid, desc, formal, src, 0))
        rows += 1

    for t in data.get("thms_extended", []):
        tid = to_str(t.get("id") or t.get("name", ""))
        desc = to_str(t.get("desc") or t.get("stmt") or t.get("d", ""))
        formal = to_str(t.get("formal") or t.get("f", ""))
        src = to_str(t.get("source") or t.get("s", ""))
        conn.execute("INSERT OR REPLACE INTO theorems VALUES (?,?,?,?,?,?,?)", (tid, module_id, tid, desc, formal, src, 1))
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


def migrate_imagegen_config(conn):
    """Populate imagegen_config table from IMAGEGEN.json top-level keys."""
    path = KNOWLEDGE_DIR / "IMAGEGEN.json"
    if not path.exists():
        print("  SKIP imagegen_config (IMAGEGEN.json not found)")
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    skip_keys = {"id", "version"}
    rows = 0
    for key, value in data.items():
        if key in skip_keys:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO imagegen_config (section, data) VALUES (?, ?)",
            (key, json.dumps(value))
        )
        rows += 1
    return rows


def migrate_prompt_styles(conn):
    """Seed prompt_styles and render_failures from IMAGEGEN.json style_library.

    Converts the style_library entries into normalized prompt_styles rows.
    Each entry's core/supplemental lists are stored as comma-separated strings
    for compact kb-query output. render_failures seeded from known failure
    patterns documented across the style_library and element_dropping sections.
    """
    path = KNOWLEDGE_DIR / "IMAGEGEN.json"
    if not path.exists():
        print("  SKIP prompt_styles (IMAGEGEN.json not found)")
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    style_lib = data.get("style_library", {})
    rows = 0

    for style_id, entry in style_lib.items():
        if style_id.startswith("_"):
            continue  # skip _note keys
        core = entry.get("core", [])
        supplemental = entry.get("supplemental", [])
        fail_modes = entry.get("fail_modes", [])
        tier = entry.get("tier", "standard")
        notes = entry.get("note", "")

        # Build negative_space from fail_modes — these are what to steer away from
        negative_space = "; ".join(fail_modes) if fail_modes else ""

        # Human-readable label: replace underscores, title-case
        label = style_id.replace("_", " ").title()

        conn.execute(
            "INSERT OR REPLACE INTO prompt_styles "
            "(id, label, core_descriptors, supplemental_descriptors, negative_space, fail_modes, model_tier, notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                style_id,
                label,
                ", ".join(core),
                ", ".join(supplemental),
                negative_space,
                json.dumps(fail_modes),
                tier,
                notes,
            )
        )
        # FTS index entry
        conn.execute(
            "INSERT INTO fts_prompts (id, label, core_descriptors, supplemental_descriptors, notes) VALUES (?,?,?,?,?)",
            (style_id, label, ", ".join(core), ", ".join(supplemental), notes)
        )
        rows += 1

    # Seed render_failures from known patterns in the module
    # These are derived from style_library fail_modes, element_dropping, and
    # physical_interaction sections — the empirically documented failure classes.
    known_failures = [
        {
            "failure_class": "style_not_transferred",
            "trigger_pattern": "Named style reference without descriptor override terms",
            "remediation": "Replace named style reference with core descriptor set from prompt_styles. "
                           "Add 'non-photographic' explicitly. All three core terms required.",
            "example": "Replace 'JJK style' with 'manga linework, ink crosshatching, non-photographic'",
        },
        {
            "failure_class": "photorealism_bleed",
            "trigger_pattern": "Style directive present but named landmark or IP character also present",
            "remediation": "Replace named landmark with generic description. Replace IP character name "
                           "with physical description. Named references override style directives.",
            "example": "Replace 'Eiffel Tower' with 'enormous iron lattice tower'; replace character name with appearance desc",
        },
        {
            "failure_class": "element_dropped",
            "trigger_pattern": "3+ hard constraints (style + interaction + spatial) stacked in single prompt",
            "remediation": "ALL-CAPS the dropped element. Add 'PRIMARY SUBJECT:' prefix. "
                           "Reduce other constraints to their minimum viable terms. "
                           "Use multi_element_anchored template.",
            "example": "PRIMARY SUBJECT: ENORMOUS IRON LATTICE TOWER — do not drop",
        },
        {
            "failure_class": "text_malformed",
            "trigger_pattern": "Small-scale text overlay requested; font style unspecified",
            "remediation": "Describe text at larger implied scale. Specify font style explicitly "
                           "(bold sans-serif, hand-lettered, block capitals). "
                           "Isolate text element as last constraint in prompt.",
            "example": "Large bold hand-lettered sign reading [TEXT] — positioned lower right",
        },
        {
            "failure_class": "physical_interaction_failed",
            "trigger_pattern": "Carry, grip, punch, or direct physical contact between figures",
            "remediation": "Substitute reliable interaction: 'arm resting across shoulders', "
                           "'standing side by side', 'silhouette proximity'. "
                           "If contact is critical, isolate to single constraint — remove style or spatial.",
            "example": "Replace 'carrying over shoulder' with 'standing beside, arm across shoulders'",
        },
        {
            "failure_class": "safety_block_silent",
            "trigger_pattern": "Named real person in prompt; returns None with no error",
            "remediation": "Replace name with physical description. Block is name-keyed not appearance-keyed. "
                           "Disclose block to user before substituting — do not silently replace.",
            "example": "Replace 'Elon Musk' with 'tall thin man, short hair, casual clothing'",
        },
        {
            "failure_class": "constraint_budget_exceeded",
            "trigger_pattern": "3 constraint categories active (style + spatial + interaction)",
            "remediation": "Run --dry-run first to confirm category count. Drop lowest-priority category. "
                           "If all three required, set expectation with user before first call — "
                           "state high failure risk explicitly.",
            "example": "Drop spatial constraint or simplify to single directional term",
        },
    ]

    for f in known_failures:
        conn.execute(
            "INSERT OR REPLACE INTO render_failures "
            "(failure_class, trigger_pattern, remediation, example) VALUES (?,?,?,?)",
            (f["failure_class"], f["trigger_pattern"], f["remediation"], f["example"])
        )
        rows += 1

    return rows


def migrate_memory_fragments(conn):
    """Parse on-demand memory fragment .md files into memory_fragments table."""
    project_root = KNOWLEDGE_DIR.parent.parent
    transcript_discord = project_root / "transcripts" / "discord"
    if not transcript_discord.is_dir():
        print("  SKIP memory_fragments (transcripts/discord not found)")
        return 0

    _ESSENTIAL = {"behavior.md", "identity.md"}
    _TAG_PATTERN = re.compile(r'^- \[([^\]]+)\]\s+(.+)$', re.MULTILINE)

    rows = 0
    for memory_dir in sorted(transcript_discord.glob("*/memory")):
        if not memory_dir.is_dir():
            continue
        for md_file in sorted(memory_dir.glob("*.md")):
            if md_file.name in _ESSENTIAL:
                continue
            source = md_file.stem
            text = md_file.read_text(encoding="utf-8")
            for m in _TAG_PATTERN.finditer(text):
                tag, content = m.group(1), m.group(2).strip()
                conn.execute(
                    "INSERT INTO memory_fragments (source, tag, content) VALUES (?, ?, ?)",
                    (source, tag, content),
                )
                rows += 1
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

    ig_count = migrate_imagegen_config(conn)
    print(f"  imagegen_config: {ig_count} rows")
    total += ig_count

    ps_count = migrate_prompt_styles(conn)
    print(f"  prompt_styles + render_failures: {ps_count} rows")
    total += ps_count

    mem_count = migrate_memory_fragments(conn)
    print(f"  memory_fragments: {mem_count} rows")
    total += mem_count

    conn.commit()
    conn.close()
    print(f"\nDone. {total} total rows in {DB_PATH}")
    print(f"Database size: {DB_PATH.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
