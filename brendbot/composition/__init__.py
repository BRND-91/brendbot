"""Composition pipeline for brendbot — ABC-first symbolic music
generation with music21 theory constraints and a per-genre style
library.

This package replaces the prior "have the LLM write raw mido code"
workflow with a structured pipeline that:

1. Composes in ABC notation (compact, music-theory-aware, well-
   represented in LLM pretraining; see ChatMusician (arXiv
   2402.16153) and NotaGen (IJCAI 2025)).
2. Validates harmonic and melodic decisions through music21 (mode-
   bound note selection, voice-leading checks, transposition).
3. Pulls genre-specific data — chord progressions, grooves, form
   templates, motifs — from a JSON style library so the bot doesn't
   have to invent stylistic conventions from scratch each time.

Public surface:

- :mod:`.style_library` — load + query genre data
- :mod:`.abc_grammar` — ABC notation construction helpers
- :mod:`.music21_layer` — theory-aware constraints + ABC↔MIDI conversion
- :mod:`.pipeline` — multi-pass composition orchestration

Optional dependency: ``music21`` and ``pretty_midi``. Install via
``uv sync --extra music``. The package will import without them but
:mod:`.music21_layer` and :mod:`.pipeline` raise on first call if
they're missing.
"""

from brendbot.composition.style_library import (
    available_genres,
    get_genre,
    get_progressions,
    get_grooves,
    get_form_templates,
    get_motifs,
    get_signature_traits,
)

__all__ = [
    "available_genres",
    "get_genre",
    "get_progressions",
    "get_grooves",
    "get_form_templates",
    "get_motifs",
    "get_signature_traits",
]
