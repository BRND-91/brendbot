"""
Migrate FUSED-CORE JSON knowledge modules to SQLite.

Usage (from project root):
    python scripts/migrations/migrate_to_sqlite.py

Creates knowledge.db inside brendbot/knowledge/. Each module's defs, facts, and
theorems become rows in normalized tables. A single kb-query SELECT returns
~200 bytes instead of reading an 11KB JSON file into context (55x improvement).
"""

import csv
import json
import re
import sqlite3
from pathlib import Path

# Script lives in scripts/migrations/; knowledge data lives in brendbot/knowledge/.
# Walk up two levels from this file to project root, then into the package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "brendbot" / "knowledge"
ENVELOPE_DIR = KNOWLEDGE_DIR / "envelope" / "data"
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

        -- ─── ENVELOPE / ENERGY MODEL TABLES (added v3.1) ───────────────────
        -- Authoritative-source data for residential building envelope effective
        -- R-values, fenestration, MA code minima, and MA climate stations.
        -- CSV source-of-truth lives in brendbot/knowledge/envelope/data/.

        -- Source citations for every envelope row. source_id is referenced by
        -- envelope_assemblies.source_id, fenestration_assemblies.source_id, etc.
        CREATE TABLE envelope_sources (
            source_id TEXT PRIMARY KEY,         -- e.g. BBRS10, IECC2021, NFRC100, BPI1100, ASHRAE_HOF
            full_name TEXT NOT NULL,
            edition TEXT,                        -- year/edition
            publisher TEXT,
            kind TEXT,                           -- code|standard|test_method|guidance|product_data
            url TEXT,
            note TEXT
        );

        -- Parent envelope assembly. Each row is a citable, ready-to-use assembly
        -- (e.g., "2x6 16 o.c., R-21 fiberglass cavity, 1/2 in. OSB, vinyl siding").
        -- r_effective is precomputed using ASHRAE parallel-path so brendbot can
        -- present the result without recomputing.
        CREATE TABLE envelope_assemblies (
            assembly_id TEXT PRIMARY KEY,        -- ENV.WALL.2x6_16OC_R21FG_VNYL
            type TEXT NOT NULL,                  -- wall|roof|floor|rim|slab_edge|basement_wall|crawlspace_wall
            name TEXT NOT NULL,                  -- short label
            description TEXT,                    -- prose description
            framing TEXT,                        -- 2x4|2x6|2x8|2x10|none|steel|trussed
            spacing_in INTEGER,                  -- 16 or 24
            cavity_material TEXT,                -- fiberglass|cellulose|mineral_wool|cc_spf|oc_spf|none
            cavity_r_nominal REAL,               -- as labeled
            ci_material TEXT,                    -- xps|eps|polyiso|mineral_wool|cc_spf|none
            ci_r REAL,                           -- continuous insulation R
            sheathing_material TEXT,             -- osb|plywood|fiberboard|gypsum|none
            sheathing_r REAL,
            exterior_finish TEXT,                -- vinyl|wood|fiber_cement|brick|stucco|metal|asphalt_shingle|none
            interior_finish TEXT,                -- drywall_half|drywall_5_8|none
            framing_factor REAL,                 -- ASHRAE FF, 0..1
            r_clear_path REAL,                   -- cavity-only path R total
            r_framing_path REAL,                 -- stud path R total
            r_effective REAL,                    -- parallel-path effective R
            u_effective REAL,                    -- 1/r_effective
            calc_method TEXT,                    -- ASHRAE_parallel|isothermal_planes|hot_box|finite_element
            source_id TEXT,                      -- FK -> envelope_sources.source_id
            source_section TEXT,                 -- e.g. BBRS10 §N1102.1.3
            ma_compliant_base TEXT,              -- yes|no|na (MA base code, BBRS 10th)
            ma_compliant_stretch TEXT,           -- yes|no|na (MA Stretch, 225 CMR 22)
            ma_compliant_specialized TEXT,       -- yes|no|na (MA Specialized, 225 CMR 23)
            note TEXT
        );
        CREATE INDEX idx_env_asm_type ON envelope_assemblies (type);
        CREATE INDEX idx_env_asm_framing ON envelope_assemblies (framing);

        -- Ordered layer list per assembly (interior to exterior). Lets brendbot
        -- answer layer-level questions and recompute when a layer changes.
        CREATE TABLE envelope_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assembly_id TEXT NOT NULL,           -- FK -> envelope_assemblies.assembly_id
            layer_order INTEGER NOT NULL,        -- 1=innermost
            layer_role TEXT NOT NULL,            -- film_int|interior_finish|cavity|framing|sheathing|ci|exterior_finish|film_ext
            material TEXT NOT NULL,
            thickness_in REAL,
            r_per_in REAL,
            r_layer REAL,
            note TEXT
        );
        CREATE INDEX idx_env_lay_asm ON envelope_layers (assembly_id);

        -- Fenestration: NFRC-style whole-window/door performance data.
        CREATE TABLE fenestration_assemblies (
            fen_id TEXT PRIMARY KEY,             -- FEN.WIN.VINYL_DBL_LOWE_ARGON
            type TEXT NOT NULL,                  -- window|door|skylight|sliding_glass
            name TEXT NOT NULL,
            description TEXT,
            frame_material TEXT,                 -- vinyl|wood|wood_clad|aluminum|fiberglass|steel|none
            frame_thermal_break TEXT,            -- yes|no|na
            glazing_layers INTEGER,              -- 1, 2, 3
            low_e TEXT,                          -- none|hard_coat|soft_coat|spectrally_selective
            gas_fill TEXT,                       -- air|argon|krypton|vacuum
            spacer_type TEXT,                    -- aluminum|warm_edge|none
            u_factor REAL,                       -- BTU/(hr·ft²·°F), whole unit NFRC
            shgc REAL,                           -- 0..1
            vt REAL,                             -- visible transmittance, 0..1
            air_leakage REAL,                    -- CFM/ft² @ 75 Pa, NFRC 400
            condensation_resistance REAL,        -- NFRC 500 CR
            nfrc_class TEXT,                     -- nominal NFRC product class
            calc_method TEXT,                    -- NFRC100|NFRC200|NFRC400|NFRC500|hot_box|simulation
            source_id TEXT,
            source_section TEXT,
            ma_compliant_base TEXT,
            ma_compliant_stretch TEXT,
            ma_compliant_specialized TEXT,
            note TEXT
        );
        CREATE INDEX idx_fen_type ON fenestration_assemblies (type);

        -- Continuous insulation product library.
        CREATE TABLE ci_materials (
            ci_id TEXT PRIMARY KEY,              -- CI.XPS_AGED, CI.POLYISO_AGED, CI.MINWOOL
            name TEXT NOT NULL,
            material TEXT NOT NULL,              -- xps|eps|polyiso|mineral_wool|cc_spf|oc_spf|wood_fiber|cork
            r_per_in_nominal REAL,
            r_per_in_aged REAL,                  -- LTTR if applicable, else same as nominal
            density_pcf REAL,
            vapor_perm REAL,                     -- US perms
            water_absorb_pct REAL,
            flame_spread INTEGER,                -- ASTM E84
            smoke_developed INTEGER,             -- ASTM E84
            typical_use TEXT,
            source_id TEXT,
            source_section TEXT,
            note TEXT
        );

        -- Massachusetts code minimums by edition × climate zone × component.
        CREATE TABLE ma_code_minimums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_edition TEXT NOT NULL,          -- BBRS10|IECC2021|MA_STRETCH|MA_SPECIALIZED
            code_section TEXT,                   -- e.g. R402.1.3
            climate_zone TEXT NOT NULL,          -- 4|5|5A|6|6A
            component TEXT NOT NULL,             -- ceiling|wood_frame_wall|mass_wall|floor|basement_wall|slab_edge|crawlspace_wall|window_u|window_shgc|door_u|skylight_u
            metric TEXT NOT NULL,                -- R_min|U_max|F_max|SHGC_max
            value REAL NOT NULL,
            unit TEXT,                           -- R|U|F|dimensionless
            depth_in INTEGER,                    -- for slab edge insulation depth
            note TEXT,
            source_id TEXT,
            UNIQUE(code_edition, climate_zone, component, metric, depth_in)
        );
        CREATE INDEX idx_ma_code_zone ON ma_code_minimums (climate_zone, component);

        -- 11 MA weather stations mirrored from energy-calc.html.
        CREATE TABLE ma_climate_stations (
            station_code TEXT PRIMARY KEY,       -- BOS, ORH, BDL, PSF, EWB, HYA, ACK, GBR, FIT, LWM, GHG
            label TEXT NOT NULL,
            climate_zone TEXT NOT NULL,
            hdd65 INTEGER,                       -- annual HDD base 65 °F
            cdd65 INTEGER,                       -- annual CDD base 65 °F
            design_temp_heating REAL,            -- 99% winter dry-bulb, °F
            design_temp_cooling REAL,            -- 1% summer dry-bulb, °F
            covers TEXT,                         -- comma-sep list of representative towns
            source_id TEXT,
            note TEXT
        );

        -- Full-text search across all envelope content.
        CREATE VIRTUAL TABLE fts_envelope USING fts5(
            entity_kind, entity_id, name, description, source_id
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



# ─── ENVELOPE / ENERGY MODEL DATA LOADER ─────────────────────────────────────

def _read_csv(path):
    """Read CSV as list of dict rows. Skip blank rows; strip whitespace."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        rows = []
        for r in rdr:
            r = {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            # Skip empty rows
            if not any(v for v in r.values()):
                continue
            rows.append(r)
        return rows


def _to_real(v):
    """Best-effort cast to float, treating empty/'NA' as None."""
    if v is None or v == "" or str(v).upper() in ("NA", "N/A", "NONE"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    if v is None or v == "" or str(v).upper() in ("NA", "N/A", "NONE"):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def migrate_envelope(conn):
    """Load envelope CSV files into SQLite.

    Idempotent: each CSV is the source-of-truth; this loader replaces matching
    rows in-place. Order matters because of FK references to envelope_sources.
    """
    if not ENVELOPE_DIR.is_dir():
        print("  SKIP envelope (envelope/data not found)")
        return 0

    rows = 0

    # 1) sources.csv
    for r in _read_csv(ENVELOPE_DIR / "sources.csv"):
        conn.execute(
            "INSERT OR REPLACE INTO envelope_sources VALUES (?,?,?,?,?,?,?)",
            (r.get("source_id"), r.get("full_name"), r.get("edition"),
             r.get("publisher"), r.get("kind"), r.get("url"), r.get("note"))
        )
        conn.execute(
            "INSERT INTO fts_envelope VALUES (?,?,?,?,?)",
            ("source", r.get("source_id"), r.get("full_name", ""), r.get("note", ""), r.get("source_id", ""))
        )
        rows += 1

    # 2) ci_materials.csv
    for r in _read_csv(ENVELOPE_DIR / "ci_materials.csv"):
        conn.execute(
            "INSERT OR REPLACE INTO ci_materials "
            "(ci_id, name, material, r_per_in_nominal, r_per_in_aged, density_pcf, "
            " vapor_perm, water_absorb_pct, flame_spread, smoke_developed, typical_use, "
            " source_id, source_section, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("ci_id"), r.get("name"), r.get("material"),
             _to_real(r.get("r_per_in_nominal")), _to_real(r.get("r_per_in_aged")),
             _to_real(r.get("density_pcf")), _to_real(r.get("vapor_perm")),
             _to_real(r.get("water_absorb_pct")), _to_int(r.get("flame_spread")),
             _to_int(r.get("smoke_developed")), r.get("typical_use"),
             r.get("source_id"), r.get("source_section"), r.get("note"))
        )
        conn.execute(
            "INSERT INTO fts_envelope VALUES (?,?,?,?,?)",
            ("ci", r.get("ci_id"), r.get("name", ""),
             f"{r.get('material','')} R/in {r.get('r_per_in_nominal','')} {r.get('typical_use','')}",
             r.get("source_id", ""))
        )
        rows += 1

    # 3) ma_climate_stations.csv
    for r in _read_csv(ENVELOPE_DIR / "ma_climate_stations.csv"):
        conn.execute(
            "INSERT OR REPLACE INTO ma_climate_stations "
            "(station_code, label, climate_zone, hdd65, cdd65, design_temp_heating, "
            " design_temp_cooling, covers, source_id, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.get("station_code"), r.get("label"), r.get("climate_zone"),
             _to_int(r.get("hdd65")), _to_int(r.get("cdd65")),
             _to_real(r.get("design_temp_heating")), _to_real(r.get("design_temp_cooling")),
             r.get("covers"), r.get("source_id"), r.get("note"))
        )
        conn.execute(
            "INSERT INTO fts_envelope VALUES (?,?,?,?,?)",
            ("station", r.get("station_code"), r.get("label", ""),
             f"CZ{r.get('climate_zone','')} HDD{r.get('hdd65','')} CDD{r.get('cdd65','')} {r.get('covers','')}",
             r.get("source_id", ""))
        )
        rows += 1

    # 4) envelope_assemblies parent + envelope_layers child
    for fname in ("walls.csv", "roofs.csv", "floors.csv", "foundations.csv"):
        for r in _read_csv(ENVELOPE_DIR / fname):
            conn.execute(
                "INSERT OR REPLACE INTO envelope_assemblies "
                "(assembly_id, type, name, description, framing, spacing_in, "
                " cavity_material, cavity_r_nominal, ci_material, ci_r, "
                " sheathing_material, sheathing_r, exterior_finish, interior_finish, "
                " framing_factor, r_clear_path, r_framing_path, r_effective, u_effective, "
                " calc_method, source_id, source_section, "
                " ma_compliant_base, ma_compliant_stretch, ma_compliant_specialized, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("assembly_id"), r.get("type"), r.get("name"), r.get("description"),
                 r.get("framing"), _to_int(r.get("spacing_in")),
                 r.get("cavity_material"), _to_real(r.get("cavity_r_nominal")),
                 r.get("ci_material"), _to_real(r.get("ci_r")),
                 r.get("sheathing_material"), _to_real(r.get("sheathing_r")),
                 r.get("exterior_finish"), r.get("interior_finish"),
                 _to_real(r.get("framing_factor")),
                 _to_real(r.get("r_clear_path")), _to_real(r.get("r_framing_path")),
                 _to_real(r.get("r_effective")), _to_real(r.get("u_effective")),
                 r.get("calc_method"), r.get("source_id"), r.get("source_section"),
                 r.get("ma_compliant_base"), r.get("ma_compliant_stretch"),
                 r.get("ma_compliant_specialized"), r.get("note"))
            )
            conn.execute(
                "INSERT INTO fts_envelope VALUES (?,?,?,?,?)",
                ("assembly", r.get("assembly_id"), r.get("name", ""),
                 (r.get("description","") + " " + (r.get("note","") or "")),
                 r.get("source_id",""))
            )
            rows += 1

    # 5) envelope_layers (one CSV with assembly_id FK)
    for r in _read_csv(ENVELOPE_DIR / "layers.csv"):
        conn.execute(
            "INSERT INTO envelope_layers "
            "(assembly_id, layer_order, layer_role, material, thickness_in, r_per_in, r_layer, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (r.get("assembly_id"), _to_int(r.get("layer_order")), r.get("layer_role"),
             r.get("material"), _to_real(r.get("thickness_in")),
             _to_real(r.get("r_per_in")), _to_real(r.get("r_layer")), r.get("note"))
        )
        rows += 1

    # 6) fenestration_assemblies (windows.csv + doors.csv)
    for fname in ("windows.csv", "doors.csv"):
        for r in _read_csv(ENVELOPE_DIR / fname):
            conn.execute(
                "INSERT OR REPLACE INTO fenestration_assemblies "
                "(fen_id, type, name, description, frame_material, frame_thermal_break, "
                " glazing_layers, low_e, gas_fill, spacer_type, "
                " u_factor, shgc, vt, air_leakage, condensation_resistance, "
                " nfrc_class, calc_method, source_id, source_section, "
                " ma_compliant_base, ma_compliant_stretch, ma_compliant_specialized, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("fen_id"), r.get("type"), r.get("name"), r.get("description"),
                 r.get("frame_material"), r.get("frame_thermal_break"),
                 _to_int(r.get("glazing_layers")), r.get("low_e"), r.get("gas_fill"),
                 r.get("spacer_type"),
                 _to_real(r.get("u_factor")), _to_real(r.get("shgc")),
                 _to_real(r.get("vt")), _to_real(r.get("air_leakage")),
                 _to_real(r.get("condensation_resistance")),
                 r.get("nfrc_class"), r.get("calc_method"),
                 r.get("source_id"), r.get("source_section"),
                 r.get("ma_compliant_base"), r.get("ma_compliant_stretch"),
                 r.get("ma_compliant_specialized"), r.get("note"))
            )
            conn.execute(
                "INSERT INTO fts_envelope VALUES (?,?,?,?,?)",
                ("fenestration", r.get("fen_id"), r.get("name", ""),
                 (r.get("description","") + " " + (r.get("note","") or "")),
                 r.get("source_id",""))
            )
            rows += 1

    # 7) ma_code_minimums.csv
    for r in _read_csv(ENVELOPE_DIR / "ma_code_minimums.csv"):
        conn.execute(
            "INSERT OR REPLACE INTO ma_code_minimums "
            "(code_edition, code_section, climate_zone, component, metric, value, unit, depth_in, note, source_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.get("code_edition"), r.get("code_section"), r.get("climate_zone"),
             r.get("component"), r.get("metric"),
             _to_real(r.get("value")), r.get("unit"),
             _to_int(r.get("depth_in")), r.get("note"), r.get("source_id"))
        )
        conn.execute(
            "INSERT INTO fts_envelope VALUES (?,?,?,?,?)",
            ("code", f"{r.get('code_edition','')}.{r.get('climate_zone','')}.{r.get('component','')}.{r.get('metric','')}",
             f"{r.get('code_edition','')} CZ{r.get('climate_zone','')} {r.get('component','')}",
             f"{r.get('metric','')} {r.get('value','')} {r.get('unit','')} {r.get('note','')}",
             r.get("source_id",""))
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

    env_count = migrate_envelope(conn)
    print(f"  envelope: {env_count} rows")
    total += env_count

    conn.commit()
    conn.close()
    print(f"\nDone. {total} total rows in {DB_PATH}")
    print(f"Database size: {DB_PATH.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
