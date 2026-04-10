"""
fused_core_loader.py
────────────────────
Loads the FUSED-CORE v2.0 JSON knowledge base and renders it as a
structured markdown block suitable for injection into CLAUDE.md.

Usage
-----
    from fused_core_loader import build_knowledge_block
    kb = build_knowledge_block()          # uses default knowledge/ path
    kb = build_knowledge_block("/path")   # explicit path

The returned string is empty-string-safe: if the knowledge dir or
MANIFEST is missing, the function logs a warning and returns "".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── default location: knowledge/ sibling to this file ────────────────────────
_DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

# ── modules to load (in dependency order) ────────────────────────────────────
_MODULE_ORDER = ["LOGIC", "STATS", "SYSTEMS", "PERSONALITY", "BUILDSCI", "GOVERNANCE"]

# ── per-module def/fact caps to keep the context window manageable ────────────
_DEF_LIMIT    = 20   # defs shown per module
_FACT_LIMIT   = 10   # facts shown per module
_THM_LIMIT    = 8    # theorems shown per module


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_knowledge_block(knowledge_dir: str | Path | None = None) -> str:
    """Return a markdown string representing the FUSED-CORE knowledge base.

    Returns an empty string if the knowledge directory or MANIFEST is missing.
    """
    kdir = Path(knowledge_dir) if knowledge_dir else _DEFAULT_KNOWLEDGE_DIR

    manifest_path = kdir / "MANIFEST.json"
    if not manifest_path.exists():
        logger.warning("FUSED-CORE: MANIFEST.json not found at %s — skipping knowledge injection", kdir)
        return ""

    try:
        manifest = _load_json(manifest_path)
    except Exception as exc:
        logger.error("FUSED-CORE: failed to parse MANIFEST.json: %s", exc)
        return ""

    sections: list[str] = []
    sections.append(_render_header(manifest))

    # ── Governance first (rules apply to all responses) ───────────────────────
    gov_path = kdir / "GOVERNANCE.json"
    if gov_path.exists():
        try:
            gov = _load_json(gov_path)
            sections.append(_render_governance(gov))
        except Exception as exc:
            logger.warning("FUSED-CORE: could not load GOVERNANCE.json: %s", exc)

    # ── Domain modules ────────────────────────────────────────────────────────
    module_registry = {m["id"]: m for m in manifest.get("modules", [])}
    for mod_id in _MODULE_ORDER:
        if mod_id == "GOVERNANCE":
            continue  # already rendered above
        meta = module_registry.get(mod_id, {})
        file_name = meta.get("file", f"{mod_id}.json")
        mod_path = kdir / file_name
        if not mod_path.exists():
            logger.warning("FUSED-CORE: module file not found: %s", mod_path)
            continue
        try:
            module = _load_json(mod_path)
            sections.append(_render_module(module))
        except Exception as exc:
            logger.warning("FUSED-CORE: could not load %s: %s", file_name, exc)

    # ── Crosslinks ────────────────────────────────────────────────────────────
    crosslinks = manifest.get("crosslinks", {})
    if crosslinks:
        sections.append(_render_crosslinks(crosslinks))

    return "\n\n".join(sections)


# ─────────────────────────────────────────────────────────────────────────────
# Render helpers
# ─────────────────────────────────────────────────────────────────────────────

def _render_header(manifest: dict) -> str:
    version = manifest.get("version", "v2.0")
    author  = manifest.get("author", "BREND")
    desc    = manifest.get("description", "FUSED-CORE knowledge system")
    return (
        "---\n"
        f"## FUSED-CORE KNOWLEDGE BASE ({version})\n"
        f"*{desc}*\n"
        f"Author: {author}  |  Load order: {', '.join(_MODULE_ORDER)}"
    )


def _render_governance(gov: dict) -> str:
    lines: list[str] = ["### GOVERNANCE — Rules & Gates"]

    # FabricationGate is the most critical rule
    fab = gov.get("autogate", {}).get("gates", {}).get("FabricationGate", {})
    if fab:
        lines.append(
            "**FabricationGate (MANDATORY):** "
            + fab.get("rule", "")
            + "  \n*Fail action:* "
            + fab.get("fail_action", "")
            + "  \n> "
            + fab.get("note", "")
        )

    # Other gates (compact)
    lines.append("\n**AutoGate rules:**")
    for gate_id, gate in gov.get("autogate", {}).get("gates", {}).items():
        if gate_id == "FabricationGate":
            continue
        lines.append(f"- **{gate_id}:** {gate.get('rule', '')}  *(fail → {gate.get('fail_action', '')})*")

    # Dialect controls
    dialect = gov.get("dialect", {})
    ambi = dialect.get("ambiguity_controls", {})
    if ambi:
        lines.append("\n**Dialect controls:**")
        for key, val in ambi.items():
            lines.append(f"- **{key}:** {val}")

    # Provenance tags
    tags = gov.get("provenance_policy", {}).get("tags", {})
    if tags:
        lines.append("\n**Provenance tags:**")
        for tag, meaning in tags.items():
            lines.append(f"- `{tag}` — {meaning}")

    return "\n".join(lines)


def _render_module(module: dict) -> str:
    mod_id  = module.get("id", "UNKNOWN")
    version = module.get("version", "")
    desc    = module.get("desc", "")
    lines: list[str] = [f"### {mod_id} ({version})", f"*{desc}*"]

    # Definitions
    defs = module.get("defs", [])
    if defs:
        lines.append("\n**Definitions** (key terms):")
        for d in defs[:_DEF_LIMIT]:
            term = d.get("term", d.get("id", ""))
            description = d.get("desc", "")
            formal = d.get("formal", "")
            formal_part = f" — `{formal}`" if formal else ""
            lines.append(f"- **{term}**{formal_part}: {description}")
        if len(defs) > _DEF_LIMIT:
            lines.append(f"- *…{len(defs) - _DEF_LIMIT} more definitions in {mod_id}.json*")

    # Facts
    facts = module.get("facts", [])
    if facts:
        lines.append("\n**Key facts:**")
        count = 0
        for topic_block in facts:
            topic = topic_block.get("topic", "")
            items = topic_block.get("items", [])
            for item in items:
                if count >= _FACT_LIMIT:
                    break
                name = item.get("name", "")
                fdesc = item.get("desc", "")
                lines.append(f"- [{topic}] **{name}**: {fdesc}")
                count += 1
            if count >= _FACT_LIMIT:
                break
        remaining = sum(len(b.get("items", [])) for b in facts) - count
        if remaining > 0:
            lines.append(f"- *…{remaining} more facts in {mod_id}.json*")

    # Theorems
    thms = module.get("thms", [])
    if thms:
        lines.append("\n**Theorems:**")
        for t in thms[:_THM_LIMIT]:
            name  = t.get("name", t.get("id", ""))
            tdesc = t.get("desc", t.get("stmt", ""))
            lines.append(f"- **{name}**: {tdesc}")
        if len(thms) > _THM_LIMIT:
            lines.append(f"- *…{len(thms) - _THM_LIMIT} more theorems in {mod_id}.json*")

    return "\n".join(lines)


def _render_crosslinks(crosslinks: dict) -> str:
    lines: list[str] = ["### CROSS-DOMAIN BINDINGS"]
    for key, items in crosslinks.items():
        lines.append(f"\n**{key}:**")
        for item in items:
            lines.append(f"- {item}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke-test: python fused_core_loader.py [path/to/knowledge]
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    kdir_arg = sys.argv[1] if len(sys.argv) > 1 else None
    logging.basicConfig(level=logging.DEBUG)
    block = build_knowledge_block(kdir_arg)
    if block:
        print(block)
        print(f"\n\n[OK] {len(block)} characters generated.")
    else:
        print("[WARN] No knowledge block generated — check path and files.")
