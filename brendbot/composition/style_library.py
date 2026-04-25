"""Per-genre style data registry.

Loads JSON files from ``brendbot/knowledge/music_styles/`` and exposes
typed accessors. Each genre file declares: chord progressions (as
roman numeral sequences with mode + role tags), drum grooves
(abstract pattern descriptions), form templates (intro/verse/chorus
structures with bar counts), melodic motifs (short ABC fragments
tagged by role), and signature traits (validation criteria for
"does this generated track actually sound like the named genre").

The registry is cached at module import time. To reload mid-session,
call :func:`reload`.

Why JSON not SQLite
-------------------
Brendan's local deployment has its own ``music_styles`` SQLite table
(see ``migrate_to_sqlite.py``). This registry is the *source* — JSON
files in version control. Migration into SQLite happens via a
separate tool if anyone wants the database semantic; the canonical
representation stays as JSON because:

- It's diff-able. Adding a chord progression to ``trance.json``
  shows in ``git diff`` as a content change, not a database delta.
- It's inspectable without a tool. Operators can ``cat`` a file.
- It's the natural format for reading from inside an LLM context —
  the bot can read the JSON directly when reasoning about a genre.

Schema (per genre file)
-----------------------
.. code-block:: json

    {
      "name": "lofi",
      "display_name": "Lo-fi Hip Hop",
      "tempo_range": [70, 95],
      "default_tempo": 80,
      "default_mode": "minor",
      "common_modes": ["minor", "dorian", "aeolian"],
      "chord_progressions": [
        {
          "id": "lofi_sad_4",
          "roman": ["i", "VI", "III", "VII"],
          "mode": "minor",
          "role": "verse",
          "extensions": ["maj7", "9"],
          "description": "Classic melancholy lofi loop, jazz-extended"
        },
        ...
      ],
      "grooves": [
        {
          "id": "lofi_swing_basic",
          "tempo_range": [70, 90],
          "kick_pattern": "1.0,2.5",
          "snare_pattern": "2.0,4.0",
          "hat_pattern": "swung_8ths",
          "description": "Boom-bap with swung hats"
        },
        ...
      ],
      "form_templates": [
        {
          "id": "lofi_loop_basic",
          "sections": [
            {"name": "intro", "bars": 4, "density": "sparse"},
            {"name": "loop_a", "bars": 16, "density": "full"},
            ...
          ],
          "description": "Single-loop arrangement with intro and outro"
        }
      ],
      "motifs": [...],
      "signature_traits": {
        "must_have": ["swung_hats_or_8th_grid_with_swing", "extended_chords"],
        "must_avoid": ["four_on_floor_kick", "harsh_synth_lead"],
        "instrumentation_hints": ["rhodes", "muted_guitar", "tape_saturation"]
      }
    }
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Knowledge dir lives alongside the brendbot package.
_STYLES_DIR = (
    Path(__file__).resolve().parent.parent / "knowledge" / "music_styles"
)


@lru_cache(maxsize=1)
def _load_all_genres() -> dict[str, dict[str, Any]]:
    """Read every ``*.json`` from the styles dir and return a
    name-keyed dict. Cached for the process lifetime — call
    :func:`reload` if files change at runtime.

    Files that fail to parse are logged and skipped, not raised, so
    a single bad genre file doesn't break composition for every
    other genre.
    """
    out: dict[str, dict[str, Any]] = {}
    if not _STYLES_DIR.exists():
        logger.warning(
            "music style dir missing at %s — composition will run "
            "with no genre data",
            _STYLES_DIR,
        )
        return out
    for path in sorted(_STYLES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data.get("name") or path.stem
            out[name.lower()] = data
        except Exception as exc:
            logger.warning(
                "music style file %s failed to load: %s", path, exc,
            )
    logger.info(
        "music style library loaded: %d genres (%s)",
        len(out), ", ".join(sorted(out.keys())),
    )
    return out


def reload() -> None:
    """Drop the cache and re-read the styles dir on the next call.
    Useful if a JSON file is edited mid-session and the bot wants
    fresh data without restarting."""
    _load_all_genres.cache_clear()


# ── Public accessors ─────────────────────────────────────────────────────


def available_genres() -> list[str]:
    """Sorted list of genre names currently loaded."""
    return sorted(_load_all_genres().keys())


def get_genre(genre: str) -> dict[str, Any] | None:
    """Full genre data dict, or ``None`` if not found.

    Lookup is case-insensitive. ``get_genre("LOFI")`` and
    ``get_genre("lofi")`` both work."""
    return _load_all_genres().get(genre.lower())


def get_progressions(
    genre: str, *, role: str | None = None, mode: str | None = None,
) -> list[dict[str, Any]]:
    """All chord progressions for a genre, optionally filtered by
    role (``"verse"``, ``"drop"``, ``"bridge"``, etc.) or mode
    (``"minor"``, ``"dorian"``, etc.). Returns ``[]`` for unknown
    genres rather than raising — callers compose in fallback
    territory rather than crash."""
    g = get_genre(genre)
    if g is None:
        return []
    progs = g.get("chord_progressions", []) or []
    if role:
        progs = [p for p in progs if p.get("role") == role]
    if mode:
        progs = [p for p in progs if p.get("mode") == mode]
    return progs


def get_grooves(
    genre: str, *, tempo: float | None = None,
) -> list[dict[str, Any]]:
    """Drum grooves for a genre. If ``tempo`` is supplied, filter to
    grooves whose ``tempo_range`` brackets the requested tempo (or
    grooves with no declared range, which match anything)."""
    g = get_genre(genre)
    if g is None:
        return []
    grooves = g.get("grooves", []) or []
    if tempo is None:
        return grooves
    out = []
    for groove in grooves:
        rng = groove.get("tempo_range")
        if not rng:
            out.append(groove)
            continue
        try:
            lo, hi = rng
            if lo <= tempo <= hi:
                out.append(groove)
        except (TypeError, ValueError):
            out.append(groove)
    return out


def get_form_templates(genre: str) -> list[dict[str, Any]]:
    """Form templates (intro/verse/chorus structures) for a genre."""
    g = get_genre(genre)
    if g is None:
        return []
    return g.get("form_templates", []) or []


def get_motifs(
    genre: str, *, role: str | None = None,
) -> list[dict[str, Any]]:
    """Melodic motifs (short ABC fragments) for a genre, optionally
    filtered by role (``"hook"``, ``"verse_melody"``, ``"counter"``,
    etc.)."""
    g = get_genre(genre)
    if g is None:
        return []
    motifs = g.get("motifs", []) or []
    if role:
        motifs = [m for m in motifs if m.get("role") == role]
    return motifs


def get_signature_traits(genre: str) -> dict[str, Any]:
    """Validation criteria for "does this track sound like the named
    genre". Returns ``must_have`` / ``must_avoid`` / hints. Used by
    :func:`brendbot.composition.pipeline.validate_against_signature`.
    Returns ``{}`` for unknown genres."""
    g = get_genre(genre)
    if g is None:
        return {}
    return g.get("signature_traits", {}) or {}
