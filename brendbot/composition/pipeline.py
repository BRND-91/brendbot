"""Multi-pass composition pipeline.

Orchestrates the structured workflow that the bot follows when
composing a song from a natural-language request. The literature
this is built on (Chord-Transformer 2025, MusicGen-Chord 2024)
shows that high-level semantic features as constraints — chord
progressions in particular — improve long-sequence coherence
significantly over end-to-end generation. This module gives the
LLM a multi-step scaffold so it doesn't have to hold the full
song in working memory at once.

Pipeline stages
---------------
1. **plan_form** — pick a form template (intro/verse/chorus
   structure with bar counts) for the genre + duration.
2. **plan_harmony** — pick or refine a chord progression in roman
   numerals; resolve to concrete chords in the key via music21.
3. **lint_harmony** — run voice-leading checks; warnings flow
   forward but don't block.
4. **plan_melody** — derive melody constraints from each chord +
   mode; this is the constraint envelope the LLM works inside.
5. **realize** — assemble the ABC document. Caller can pass an
   ABC body string they generated separately, or use the
   pipeline's free-form ``melody_abc`` field.
6. **render** — convert the ABC to MIDI via music21.

Each stage returns a :class:`PipelineState` you pass to the next.
The intent is *not* to fully automate composition end-to-end —
genre flavor and motivic invention still benefit from the LLM in
the loop — but to remove the bookkeeping (key resolution, voice-
leading checks, form structuring) so the bot's compositional
attention goes to the parts that matter.

Usage from a Bash session
-------------------------
The bot calls ``scripts/compose-song`` which wraps this pipeline
with command-line argument parsing. Direct programmatic use is
also supported — see ``test_composition_pipeline.py`` for
end-to-end examples.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brendbot.composition import abc_grammar as ag
from brendbot.composition import style_library

logger = logging.getLogger(__name__)


# ── State container ──────────────────────────────────────────────────────


@dataclass
class PipelineState:
    """Snapshot of the composition in progress.

    Each stage reads + mutates this. Keeping it as a flat dataclass
    rather than threading kwargs through every function makes the
    pipeline easy to inspect mid-run (e.g. log the current state to
    obs.log_tool_call so the bot can read back what it decided)."""

    # Inputs
    genre: str
    title: str = "Untitled"
    key: str = "C"                  # music21-flexible: "C major", "a minor", "E dorian"
    tempo_bpm: int | None = None    # default from genre if None
    target_duration_s: float = 120.0

    # Stage outputs
    form_template: dict[str, Any] | None = None
    progression_record: dict[str, Any] | None = None
    progression_resolved: list[dict[str, Any]] | None = None
    voice_leading_issues: list[dict[str, Any]] = field(default_factory=list)
    melody_envelopes: list[dict[str, list[str]]] = field(default_factory=list)
    abc_score: str | None = None
    midi_path: Path | None = None

    # Diagnostics
    notes: list[str] = field(default_factory=list)


def _emit(state: PipelineState, msg: str) -> None:
    """Append a diagnostic note to the state and log it."""
    state.notes.append(msg)
    logger.info("[composition pipeline] %s", msg)


# ── Stage 1: form ────────────────────────────────────────────────────────


def plan_form(state: PipelineState, *, prefer_id: str | None = None) -> PipelineState:
    """Pick a form template from the genre's library. If
    ``prefer_id`` is supplied, use that template; otherwise pick the
    one whose total bar count best matches the requested duration."""
    templates = style_library.get_form_templates(state.genre)
    if not templates:
        _emit(state, f"no form templates for genre {state.genre!r}; using single-section fallback")
        state.form_template = {
            "id": "fallback_single",
            "sections": [
                {"name": "intro", "bars": 4, "density": "sparse"},
                {"name": "main", "bars": 16, "density": "full"},
                {"name": "outro", "bars": 4, "density": "sparse"},
            ],
            "description": "Generated fallback — no genre data available",
        }
        return state

    if prefer_id:
        for t in templates:
            if t.get("id") == prefer_id:
                state.form_template = t
                _emit(state, f"form: prefer_id matched {prefer_id!r}")
                return state
        _emit(state, f"form: prefer_id {prefer_id!r} not found; falling through to duration match")

    # Pick by closest fit to target duration, given the genre's
    # default tempo. bars × 4 beats × 60/tempo seconds-per-beat ≈
    # section duration in seconds; we want sum(sections) closest to
    # target.
    genre_data = style_library.get_genre(state.genre) or {}
    tempo = state.tempo_bpm or genre_data.get("default_tempo", 120)
    seconds_per_bar = (60.0 / tempo) * 4

    def _template_duration(t: dict[str, Any]) -> float:
        return sum(s.get("bars", 0) for s in t.get("sections", [])) * seconds_per_bar

    best = min(
        templates,
        key=lambda t: abs(_template_duration(t) - state.target_duration_s),
    )
    state.form_template = best
    _emit(
        state,
        f"form: picked {best['id']!r} "
        f"(~{_template_duration(best):.0f}s vs target {state.target_duration_s:.0f}s)",
    )
    return state


# ── Stage 2: harmony ─────────────────────────────────────────────────────


def plan_harmony(
    state: PipelineState,
    *,
    role: str = "verse",
    prefer_id: str | None = None,
    rng: random.Random | None = None,
) -> PipelineState:
    """Pick a chord progression for the genre + role. Resolve it in
    the configured key via music21 to concrete chord names + pitches.

    ``role`` filters by progression role tag (``"verse"``, ``"drop"``,
    ``"breakdown"``, etc.). ``prefer_id`` overrides random selection.
    ``rng`` allows deterministic test output."""
    rng = rng or random.Random()

    progs = style_library.get_progressions(state.genre, role=role)
    if not progs:
        # Fall back to any progression for the genre
        progs = style_library.get_progressions(state.genre)
    if not progs:
        _emit(
            state,
            f"no progressions for genre {state.genre!r}; using "
            f"i-VI-III-VII fallback",
        )
        state.progression_record = {
            "id": "fallback_minor_loop",
            "roman": ["i", "VI", "III", "VII"],
            "mode": "minor",
            "role": role,
            "extensions": [],
            "description": "Generated fallback",
        }
    elif prefer_id:
        for p in progs:
            if p.get("id") == prefer_id:
                state.progression_record = p
                _emit(state, f"harmony: prefer_id matched {prefer_id!r}")
                break
        else:
            state.progression_record = rng.choice(progs)
            _emit(
                state,
                f"harmony: prefer_id {prefer_id!r} not found; randomly chose "
                f"{state.progression_record['id']!r}",
            )
    else:
        state.progression_record = rng.choice(progs)
        _emit(
            state,
            f"harmony: chose {state.progression_record['id']!r} "
            f"(roman={state.progression_record['roman']})",
        )

    # Resolve in key via music21. Imported lazily so this module
    # works without the music extra at import time.
    from brendbot.composition import music21_layer as m21l

    # Strip ABC-style chord-symbol extensions that music21's roman
    # numeral parser doesn't accept (sus2, sus4, add9, etc.). They're
    # valuable as voicing hints in the JSON but music21 wants
    # functional figures only.
    def _clean_roman(rn: str) -> str:
        for tok in ("sus2", "sus4", "add9", "add6", "add4", "add2"):
            rn = rn.replace(tok, "")
        return rn

    cleaned_roman = [_clean_roman(rn) for rn in state.progression_record["roman"]]
    try:
        state.progression_resolved = m21l.progression_in_key(
            cleaned_roman, state.key,
        )
    except ValueError as exc:
        _emit(
            state,
            f"harmony: progression {state.progression_record['id']!r} "
            f"failed music21 parse ({exc}); falling back to "
            f"i-VI-III-VII fallback",
        )
        state.progression_record = {
            "id": "fallback_minor_loop",
            "roman": ["i", "VI", "III", "VII"],
            "mode": "minor",
            "role": role,
            "extensions": [],
            "description": "Fallback after music21 parse failure",
        }
        state.progression_resolved = m21l.progression_in_key(
            state.progression_record["roman"], state.key,
        )
    _emit(
        state,
        f"harmony resolved: "
        f"{[p['figure'] for p in state.progression_resolved]}",
    )
    return state


# ── Stage 3: voice-leading lint ──────────────────────────────────────────


def lint_harmony(state: PipelineState) -> PipelineState:
    """Run voice-leading checks against the resolved progression.
    Issues are recorded on state but don't block — the pipeline
    surfaces them as diagnostics for the LLM to consider."""
    if not state.progression_resolved:
        _emit(state, "lint_harmony: no progression resolved yet; skipping")
        return state

    from brendbot.composition import music21_layer as m21l
    issues = m21l.check_voice_leading(state.progression_resolved)
    state.voice_leading_issues = issues
    if issues:
        _emit(state, f"voice-leading: {len(issues)} issue(s) flagged")
    else:
        _emit(state, "voice-leading: clean")
    return state


# ── Stage 4: melody envelope ─────────────────────────────────────────────


def plan_melody(state: PipelineState) -> PipelineState:
    """Derive per-chord melody constraints (chord tones, scale
    tones, chromatic neighbors) from the resolved progression. The
    LLM uses these to bias melody generation."""
    if not state.progression_resolved:
        _emit(state, "plan_melody: no progression resolved; skipping")
        return state

    from brendbot.composition import music21_layer as m21l
    envelopes = []
    for chord_record in state.progression_resolved:
        env = m21l.melody_constraints(chord_record["pitches"], state.key)
        envelopes.append(env)
    state.melody_envelopes = envelopes
    _emit(
        state,
        f"melody envelopes: "
        f"{len(envelopes)} chords × "
        f"({len(envelopes[0]['chord_tones']) if envelopes else 0} chord, "
        f"{len(envelopes[0]['scale_tones']) if envelopes else 0} scale, "
        f"{len(envelopes[0]['chromatic_neighbors']) if envelopes else 0} chrom)",
    )
    return state


# ── Stage 5: ABC realization ─────────────────────────────────────────────


def realize(
    state: PipelineState,
    *,
    melody_abc_body: str | None = None,
    bass_abc_body: str | None = None,
    lead_program: int = 4,        # 4 = Electric Piano 1 (Rhodes)
    bass_program: int = 33,       # 33 = Electric Bass (finger)
) -> PipelineState:
    """Assemble the final ABC document from form + harmony +
    optionally caller-supplied melody body. If no melody_abc_body is
    provided, emit a stub melody using only chord roots so callers
    can render-and-iterate before composing the lead."""
    if not state.progression_record:
        _emit(state, "realize: no harmony planned; emitting bare header")
        state.abc_score = ag.build_abc(
            title=state.title, key=state.key,
        )
        return state

    if melody_abc_body is None:
        # Stub melody — rests at whole notes per chord. The chord
        # symbol displayed is a human-readable one derived from the
        # root + quality (Am, F, C7) rather than the roman-numeral
        # figure (i, VI, I7) that music21's .figure attribute returns.
        melody_abc_body = " ".join(
            ag.chord_label(_display_chord(p), "z8") + " |"
            for p in state.progression_resolved or []
        )

    if bass_abc_body is None:
        # Stub bass — chord roots in low octave
        bass_abc_body = " ".join(
            ag.chord_label(
                _display_chord(p),
                ag.note(_root_letter(p["root"]), octave=2, duration=8),
            ) + " |"
            for p in state.progression_resolved or []
        )

    voices = [
        ag.AbcVoice(
            voice_id=1, name="lead", body=melody_abc_body,
            midi_program=lead_program, midi_channel=0,
        ),
        ag.AbcVoice(
            voice_id=2, name="bass", clef="bass", body=bass_abc_body,
            midi_program=bass_program, midi_channel=1,
        ),
    ]

    genre_data = style_library.get_genre(state.genre) or {}
    tempo = state.tempo_bpm or genre_data.get("default_tempo", 120)

    state.abc_score = ag.build_abc(
        title=state.title,
        key=state.key.split()[0] if " " in state.key else state.key,
        tempo_bpm=tempo,
        voices=voices,
    )
    _emit(state, f"realize: ABC document built ({len(state.abc_score)} chars)")
    return state


def _root_letter(root_pitch: str) -> str:
    """Extract the natural-letter root from a music21 pitch name like
    'F#4' → 'F' or 'A4' → 'A'. Defensive — falls back to 'C' on
    parse failure (e.g. unexpected input shape)."""
    if not root_pitch:
        return "C"
    letter = root_pitch[0].upper()
    if letter not in "CDEFGAB":
        return "C"
    return letter


def _display_chord(progression_entry: dict[str, Any]) -> str:
    """Turn a music21 RomanNumeral resolution into a human-readable
    chord name suitable for ABC chord-symbol annotation.

    Examples:
    - {"root": "A4", "quality": "minor"} → "Am"
    - {"root": "F4", "quality": "major"} → "F"
    - {"root": "G4", "quality": "dominant"} → "G7"
    - {"root": "B-4", "quality": "minor"} → "Bbm"

    The output is what the bot wants the user to see in chord-symbol
    annotations above the staff, not music21's internal figure."""
    root = progression_entry.get("root", "C4")
    quality = progression_entry.get("quality", "major")
    # Strip octave digits and convert music21 flat ('-') to ABC flat ('b')
    pitch_only = "".join(c for c in root if not c.isdigit())
    pitch_only = pitch_only.replace("-", "b")
    suffix_map = {
        "major": "",
        "minor": "m",
        "diminished": "dim",
        "augmented": "aug",
        "dominant": "7",
        "half-diminished": "m7b5",
    }
    return pitch_only + suffix_map.get(quality, "")


# ── Stage 6: render ──────────────────────────────────────────────────────


def render(state: PipelineState, output_path: Path | str) -> PipelineState:
    """Convert the assembled ABC to a MIDI file at ``output_path``."""
    if not state.abc_score:
        raise RuntimeError("render: no ABC score; call realize() first")

    from brendbot.composition import music21_layer as m21l
    state.midi_path = m21l.abc_to_midi(state.abc_score, output_path)
    _emit(state, f"render: wrote {state.midi_path}")
    return state


# ── Convenience: run all stages ──────────────────────────────────────────


def compose(
    genre: str,
    *,
    title: str = "Untitled",
    key: str = "C major",
    tempo_bpm: int | None = None,
    target_duration_s: float = 120.0,
    role: str = "verse",
    output_path: Path | str | None = None,
    melody_abc_body: str | None = None,
    bass_abc_body: str | None = None,
    rng: random.Random | None = None,
) -> PipelineState:
    """Run the full pipeline end-to-end. Returns the final state for
    inspection / logging. If ``output_path`` is None, the render
    stage is skipped — useful when the caller wants ABC-only and
    will MIDI it themselves."""
    state = PipelineState(
        genre=genre,
        title=title,
        key=key,
        tempo_bpm=tempo_bpm,
        target_duration_s=target_duration_s,
    )

    plan_form(state)
    plan_harmony(state, role=role, rng=rng)
    lint_harmony(state)
    plan_melody(state)
    realize(state, melody_abc_body=melody_abc_body, bass_abc_body=bass_abc_body)

    if output_path is not None:
        render(state, output_path)

    return state
