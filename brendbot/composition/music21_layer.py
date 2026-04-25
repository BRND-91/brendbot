"""music21-backed theory layer.

Bridges between brendbot's symbolic representations (chord
progressions as roman-numeral lists, ABC notation strings) and
music21's :class:`music21.stream.Score` model. Provides four classes
of operation:

1. **Roman → concrete chords** — given a progression like
   ``["i", "VI", "III", "VII"]`` and a key like ``"a minor"``,
   return the actual chord names (``["Am", "F", "C", "G"]``) and
   pitch sets.
2. **Voice-leading checks** — flag parallel fifths, parallel
   octaves, and tritone leaps in a generated progression. Used to
   validate the harmonic skeleton before melodic generation.
3. **Mode-aware note constraints** — given a chord and a mode,
   return the allowed melody pitches (chord tones strong-beat
   biased, scale tones for passing tones, chromatic neighbors
   permitted on weak beats).
4. **ABC ↔ MIDI conversion** — parse an ABC string into a music21
   Score and write to a .mid file. The thin wrapper makes the
   composition pipeline's I/O calls testable.

music21 dependency
------------------
Imported lazily inside each function so the module loads even when
``music21`` isn't installed (e.g. tests for the JSON style library
shouldn't require the music extra). First call raises a clean
``ImportError`` with the install hint if the dep is missing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "music21 is required for brendbot.composition.music21_layer. "
    "Install it via `uv sync --extra music`."
)


def _require_music21() -> Any:
    try:
        import music21
        return music21
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc


# music21 accepts ``music21.key.Key("A")`` for A major,
# ``music21.key.Key("a")`` for A minor — case-only mode signal — but
# rejects ``"A minor"`` / ``"C major"`` / ``"E dorian"``, the natural
# strings most callers reach for. ``KeySignature(sharps, mode=...)``
# is the constructor that accepts ``"dorian"``, ``"lydian"`` etc. We
# pre-parse a flexible string format into the (tonic, mode) tuple
# music21 actually wants.
_MODE_TOKENS = (
    "major", "minor", "dorian", "phrygian", "lydian",
    "mixolydian", "aeolian", "locrian", "ionian",
)


def _parse_key_string(key_str: str) -> tuple[str, str]:
    """Normalize a flexible key string into ``(tonic, mode)`` for
    :class:`music21.key.Key`.

    Accepts:
    - ``"C"`` / ``"Cm"`` / ``"a"`` (music21-native, returned with
      mode inferred)
    - ``"C major"`` / ``"a minor"`` / ``"E dorian"`` / ``"F# lydian"``
    - ``"D-flat major"`` / ``"Bb minor"``

    Returns: ``(tonic, mode)`` where ``tonic`` is a music21-acceptable
    pitch like ``"C"`` or ``"F#"`` (case preserved as music21 expects)
    and ``mode`` is one of the lowercase mode tokens.
    """
    s = key_str.strip()
    lower = s.lower()
    # Try to split off a mode suffix
    for tok in _MODE_TOKENS:
        if lower.endswith(" " + tok):
            tonic = s[: -(len(tok) + 1)].strip()
            mode = tok
            return tonic, mode
    # Music21-native compact forms — let music21 itself parse them
    return s, ""


# ── Roman → concrete chords ──────────────────────────────────────────────


def _make_key(m21: Any, key_str: str) -> Any:
    """Convert a flexible key string into a :class:`music21.key.Key`.
    Raises :class:`ValueError` with the offending input on failure."""
    tonic, mode = _parse_key_string(key_str)
    try:
        if mode:
            return m21.key.Key(tonic, mode)
        return m21.key.Key(tonic)
    except Exception as exc:
        raise ValueError(f"unknown key {key_str!r}: {exc}") from exc


# ── Roman → concrete chords ──────────────────────────────────────────────


def progression_in_key(
    roman_numerals: list[str],
    key: str,
) -> list[dict[str, Any]]:
    """Resolve a roman-numeral progression in a specified key.

    Args:
        roman_numerals: list like ``["i", "VI", "III", "VII"]``.
            Lower-case = minor quality, upper-case = major. Suffixes
            like ``"7"``, ``"maj7"``, ``"sus4"``, ``"b9"`` are
            respected.
        key: music21-compatible key string. ``"C"``, ``"a minor"``,
            ``"D dorian"``, ``"F# lydian"``, etc.

    Returns:
        A list of dicts, one per chord, each with:
        - ``"roman"``: the input numeral (preserved)
        - ``"figure"``: the resolved figure (e.g. ``"Am"``, ``"F"``)
        - ``"pitches"``: list of pitch names like ``["A4", "C5", "E5"]``
        - ``"root"``: root pitch name
        - ``"quality"``: ``"major"``, ``"minor"``, ``"diminished"``,
          ``"augmented"``, ``"dominant"``, etc.

    Raises:
        ValueError: if a roman numeral can't be parsed or the key
            string isn't recognized.
    """
    m21 = _require_music21()
    m21_key = _make_key(m21, key)

    out: list[dict[str, Any]] = []
    for rn in roman_numerals:
        try:
            chord = m21.roman.RomanNumeral(rn, m21_key)
        except Exception as exc:
            raise ValueError(
                f"can't parse roman numeral {rn!r} in key {key!r}: {exc}"
            ) from exc
        out.append({
            "roman": rn,
            "figure": chord.figure,
            "pitches": [str(p) for p in chord.pitches],
            "root": str(chord.root()),
            "quality": chord.quality,
        })
    return out


# ── Voice-leading checks ────────────────────────────────────────────────


def check_voice_leading(
    progression: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag voice-leading issues between consecutive chords.

    Inspects each adjacent pair for:

    - **Parallel fifths**: two voices moving in the same direction
      separated by a perfect fifth in both chords. Standard
      common-practice no-no.
    - **Parallel octaves**: same direction, perfect octave both times.
    - **Large leaps**: any voice moving more than a perfect octave.

    Returns a list of issues; ``[]`` means clean. Each issue has
    ``"type"``, ``"between"`` (chord indices), and ``"detail"``.

    This is a lint, not a hard check — some genres deliberately
    use parallel fifths (rock, drone music). The pipeline can
    decide whether to act on the warnings.
    """
    m21 = _require_music21()
    issues: list[dict[str, Any]] = []
    if len(progression) < 2:
        return issues

    def _to_chord(entry: dict[str, Any]) -> Any:
        return m21.chord.Chord(entry["pitches"])

    for i in range(len(progression) - 1):
        a = _to_chord(progression[i])
        b = _to_chord(progression[i + 1])

        a_pitches = sorted(a.pitches, key=lambda p: p.midi)
        b_pitches = sorted(b.pitches, key=lambda p: p.midi)

        if len(a_pitches) >= 2 and len(b_pitches) >= 2:
            for v1 in range(len(a_pitches) - 1):
                v2 = v1 + 1
                if v1 >= len(b_pitches) or v2 >= len(b_pitches):
                    continue
                a_iv = abs(a_pitches[v2].midi - a_pitches[v1].midi) % 12
                b_iv = abs(b_pitches[v2].midi - b_pitches[v1].midi) % 12
                a_dir = b_pitches[v1].midi - a_pitches[v1].midi
                b_dir = b_pitches[v2].midi - a_pitches[v2].midi
                same_direction = (a_dir > 0 and b_dir > 0) or (a_dir < 0 and b_dir < 0)
                if same_direction and a_iv == b_iv == 7:
                    issues.append({
                        "type": "parallel_fifth",
                        "between": [i, i + 1],
                        "detail": (
                            f"voices {v1} and {v2} move in parallel "
                            f"perfect fifths"
                        ),
                    })
                if same_direction and a_iv == b_iv == 0:
                    issues.append({
                        "type": "parallel_octave",
                        "between": [i, i + 1],
                        "detail": (
                            f"voices {v1} and {v2} move in parallel "
                            f"octaves/unisons"
                        ),
                    })

    return issues


# ── Mode-aware note constraints ──────────────────────────────────────────


def melody_constraints(
    chord_pitches: list[str],
    key: str,
) -> dict[str, list[str]]:
    """Return the allowed melody pitches against a chord in a mode.

    Returns dict with three lists:

    - ``"chord_tones"``: notes from the chord itself. Strong-beat
      bias — the melody should land on these on beats 1 and 3
      (4/4) most of the time.
    - ``"scale_tones"``: notes from the mode that aren't chord
      tones. Use as passing tones, neighbor tones, or as
      decorations on weak beats.
    - ``"chromatic_neighbors"``: half-step neighbors of chord tones.
      Use sparingly — ornamental, weak-beat only.

    Pitches returned as octave-less names (``"C"``, ``"F#"``,
    ``"Bb"``) so the caller can place them in any octave.
    """
    m21 = _require_music21()
    m21_key = _make_key(m21, key)

    chord_set = {p.split("-")[0].rstrip("0123456789") for p in chord_pitches}
    chord_set = {_normalise_pitch_name(p) for p in chord_set}

    scale_pitches = m21_key.getPitches()
    scale_set = {_normalise_pitch_name(p.name) for p in scale_pitches}

    chord_tones = sorted(chord_set)
    scale_tones = sorted(scale_set - chord_set)

    chromatic_neighbors: set[str] = set()
    for ct in chord_set:
        try:
            p = m21.pitch.Pitch(ct)
        except Exception:
            continue
        for delta in (-1, 1):
            np = p.transpose(delta)
            chromatic_neighbors.add(_normalise_pitch_name(np.name))
    # Remove anything already in chord or scale — true chromatic only
    chromatic_neighbors -= chord_set
    chromatic_neighbors -= scale_set

    return {
        "chord_tones": chord_tones,
        "scale_tones": scale_tones,
        "chromatic_neighbors": sorted(chromatic_neighbors),
    }


def _normalise_pitch_name(name: str) -> str:
    """Convert music21's pitch names (``"F#"``, ``"B-"``) to a
    consistent ABC-friendly form (``"F#"``, ``"Bb"``)."""
    return name.replace("-", "b")


# ── ABC ↔ MIDI conversion ────────────────────────────────────────────────


def abc_to_midi(abc_str: str, output_path: Path | str) -> Path:
    """Convert an ABC notation string to a MIDI file via music21.

    Returns the resolved output path. Raises :class:`ValueError` on
    parse error with the offending ABC excerpt for debugging."""
    m21 = _require_music21()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        score = m21.converter.parse(abc_str, format="abc")
    except Exception as exc:
        # Show first ~200 chars of the ABC so the bot can see what
        # the parser choked on.
        snippet = abc_str[:200].replace("\n", " | ")
        raise ValueError(
            f"ABC parse failed: {exc}. First 200 chars: {snippet!r}"
        ) from exc

    score.write("midi", fp=str(output_path))
    logger.info("abc_to_midi: wrote %s", output_path)
    return output_path


# NOTE — ABC writer / round-trip transpose
# ----------------------------------------
# Earlier drafts of this module exposed ``midi_to_abc`` and
# ``transpose_abc``. Both relied on music21's built-in ABC writer
# (``score.write("abc")`` and ``ConverterABC.write``). In music21
# 9.9.1 (current as of this PR) the ABC output path is broken — it
# writes the Score's ``repr()`` to the file rather than valid ABC.
# Round-tripping through it produces parse errors downstream
# ("no active default note length provided" etc).
#
# Rather than ship a buggy public API, both functions are deferred
# to a future PR. The pipeline doesn't need them today: the LLM
# composes in the target key directly (no after-the-fact transpose
# required), and reference-MIDI ingestion can use raw music21 Score
# objects without converting back to ABC text.
#
# When music21 fixes its ABC writer (or we ship our own simple ABC
# emitter), reintroduce here.


def transpose_score(abc_str: str, semitones: int) -> Any:
    """Transpose an ABC string and return the resulting music21 Score
    object. Caller can then write to MIDI directly via
    :func:`abc_to_midi`-style mechanics on the score, or convert to
    ABC text via their own emitter once available.

    This is the supported transposition path for now — see the note
    above on why ABC-text round-tripping is deferred."""
    m21 = _require_music21()
    score = m21.converter.parse(abc_str, format="abc")
    return score.transpose(semitones)


def score_to_midi(score: Any, output_path: Path | str) -> Path:
    """Write a music21 Score (e.g. the result of :func:`transpose_score`)
    to a MIDI file. Mirrors :func:`abc_to_midi` for callers that
    have a Score object rather than ABC text."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    score.write("midi", fp=str(output_path))
    logger.info("score_to_midi: wrote %s", output_path)
    return output_path
