"""ABC notation construction helpers.

ABC notation (https://abcnotation.com) is a compact text-based music
representation. A short tune fits in a few hundred characters where
the equivalent MIDI byte stream or python-mido call sequence runs to
thousands. Importantly for our use, LLMs see millions of ABC examples
during pretraining (folk-tune archives, music-education corpora) and
already understand the grammar without fine-tuning. This module
gives the bot a typed builder API so it doesn't have to remember
ABC's escape rules and ordering constraints.

Example output of :func:`build_abc`::

    X:1
    T:An Example
    M:4/4
    L:1/8
    Q:1/4=120
    K:Am
    V:1 name="lead"
    "Am"A2 c2 e2 c2 | "F"f4 e4 |
    V:2 name="bass"
    [V:2] "Am"A,4 "F"F,4 |

The result can be fed to ``music21.converter.parse`` (with format
hint ``"abc"``) to obtain a :class:`music21.stream.Score` for further
manipulation, or to ``abc2midi`` (a CLI tool, if installed) for
direct MIDI conversion. Most of our pipeline goes through music21.

Reference grammar coverage in this module:

- Header lines: X (reference), T (title), C (composer), M (meter),
  L (default note length), Q (tempo), K (key signature)
- Voices: V:N declarations with optional ``name=`` and ``clef=``
- Note tokens: pitch + accidental + octave markers + duration
- Bar lines: ``|``, ``||``, ``|]``
- Chord symbols above the staff: ``"Am"``, ``"F#m7"``
- Polyphonic chord stacks: ``[ACE]``
- Dynamics and decorations: not yet covered (deferred to v2)
- Repeats and section markers: not yet covered (deferred to v2)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Header ───────────────────────────────────────────────────────────────


@dataclass
class AbcHeader:
    """Top-of-tune metadata. ABC requires X, T, M, L, K in roughly
    this order; everything else is optional."""

    reference: int = 1               # X: — must be int, unique per file
    title: str = "Untitled"          # T:
    composer: str | None = None      # C:
    meter: str = "4/4"               # M:
    default_note_length: str = "1/8"  # L: — note tokens default to this
    tempo_bpm: int | None = None     # Q: as 1/4=BPM
    key: str = "C"                   # K: — major key by default; minor as "Am"

    def to_lines(self) -> list[str]:
        """Render as ABC header lines, one per output line. Order is
        X → T → C? → M → L → Q? → K, which is what most ABC parsers
        expect (key signature must come last in header)."""
        out = [f"X:{self.reference}", f"T:{self.title}"]
        if self.composer:
            out.append(f"C:{self.composer}")
        out.append(f"M:{self.meter}")
        out.append(f"L:{self.default_note_length}")
        if self.tempo_bpm:
            out.append(f"Q:1/4={self.tempo_bpm}")
        out.append(f"K:{self.key}")
        return out


# ── Voice / track ────────────────────────────────────────────────────────


@dataclass
class AbcVoice:
    """One musical line inside a tune. ABC supports multiple
    simultaneous voices — typically lead, bass, drums (as a special
    track), and any number of pads or counter-melodies.

    The body is a string of ABC note tokens. We don't tokenize at
    insert time; callers can use the helper functions below to
    construct the string, or paste raw ABC if they know what
    they're doing."""

    voice_id: int
    body: str = ""                            # raw ABC body
    name: str | None = None                   # V: name="lead"
    clef: str | None = None                   # V: clef=bass
    midi_program: int | None = None           # GM 0-127
    midi_channel: int | None = None           # 0-15

    def to_lines(self) -> list[str]:
        decl = f"V:{self.voice_id}"
        if self.name:
            decl += f' name="{self.name}"'
        if self.clef:
            decl += f" clef={self.clef}"
        # ABC convention: %%MIDI directives sit on their own lines
        # below the voice declaration; abc2midi and music21 both
        # respect them.
        out = [decl]
        if self.midi_program is not None:
            out.append(f"%%MIDI program {self.midi_program}")
        if self.midi_channel is not None:
            out.append(f"%%MIDI channel {self.midi_channel + 1}")  # ABC is 1-indexed
        out.append(f"[V:{self.voice_id}]{self.body}")
        return out


# ── Note token helpers ──────────────────────────────────────────────────


# ABC pitch convention:
#   C, D, E, F, G, A, B  → octave 4 (middle C and up to B above)
#   c, d, e, f, g, a, b  → octave 5
#   c', d', e', f', ...  → octave 6 (apostrophes raise an octave)
#   C,, D,, E,, F,, ...  → octaves 3 and below (commas drop)
#
# Accidentals: ^ = sharp, _ = flat, = = natural; come before the pitch
# letter and are persistent within the bar.
#
# Duration: the L: header sets the default unit. A note "C" lasts the
# default length; "C2" lasts twice that; "C/2" lasts half.

_NATURAL_ORDER = "CDEFGAB"


def note(
    pitch: str,
    octave: int = 4,
    accidental: str = "",
    duration: int | str = 1,
) -> str:
    """Build a single ABC note token.

    Args:
        pitch: One of ``"C"``, ``"D"``, ..., ``"B"``. Case-insensitive.
        octave: 0 = C0 (subcontra), 4 = middle C octave, 9 = top.
            ABC's reference octave is 4 (uppercase) and 5 (lowercase);
            this helper handles the case conversion and adds commas
            or apostrophes as needed.
        accidental: ``""``, ``"#"`` for sharp, ``"b"`` for flat,
            ``"="`` for natural override. ABC sigils ``^`` and ``_``
            are emitted automatically.
        duration: integer multiple of the default note length, or a
            string like ``"3/2"`` for dotted figures.
    """
    pitch_upper = pitch.upper()
    if pitch_upper not in _NATURAL_ORDER:
        raise ValueError(f"Unknown pitch letter: {pitch!r}")

    # Accidental → ABC sigil
    if accidental in ("#", "♯"):
        sigil = "^"
    elif accidental in ("b", "♭"):
        sigil = "_"
    elif accidental == "=":
        sigil = "="
    elif accidental == "":
        sigil = ""
    else:
        raise ValueError(f"Unknown accidental: {accidental!r}")

    # Pitch letter — uppercase for octave 4, lowercase for octave 5,
    # with commas (octave-down) or apostrophes (octave-up) appended.
    if octave <= 4:
        letter = pitch_upper
        marks = "," * (4 - octave)
    else:
        letter = pitch_upper.lower()
        marks = "'" * (octave - 5)

    dur_str = "" if duration == 1 else str(duration)
    return f"{sigil}{letter}{marks}{dur_str}"


def rest(duration: int | str = 1) -> str:
    """ABC rest token. ``z`` is the standard rest character; ``Z`` is
    a multi-bar rest but we prefer explicit ``z`` per beat for
    clarity."""
    return f"z{'' if duration == 1 else duration}"


def chord_stack(notes: Iterable[str]) -> str:
    """Stack multiple notes into a single beat as ``[ACE]``. Each
    note token must already have its accidental, octave, and
    duration applied. For uniform-duration chords the duration goes
    after the closing bracket: ``[ACE]2``."""
    inner = "".join(notes)
    return f"[{inner}]"


def chord_label(symbol: str, body: str) -> str:
    """Attach a chord symbol above a staff position: ``"Am"A2`` etc.
    ABC parsers display these as guitar-chord annotations and music21
    reads them back as :class:`music21.harmony.ChordSymbol` objects."""
    # Escape any embedded double-quotes in the chord symbol — they'd
    # close the label early and produce garbage downstream.
    safe = symbol.replace('"', '')
    return f'"{safe}"{body}'


def bar(content: str) -> str:
    """Append a bar line. ABC bar lines: ``|`` standard, ``||`` double,
    ``|]`` end-of-piece. We use the standard form throughout."""
    return f"{content} |"


# ── Score assembly ───────────────────────────────────────────────────────


@dataclass
class AbcScore:
    """A complete ABC document: header + one or more voices."""

    header: AbcHeader
    voices: list[AbcVoice] = field(default_factory=list)

    def add_voice(self, voice: AbcVoice) -> None:
        self.voices.append(voice)

    def to_text(self) -> str:
        """Render the full ABC document as a single string."""
        lines = self.header.to_lines()
        # Voice declarations come AFTER the K: line. Order matters:
        # ABC parsers stop reading the header on the first non-header
        # line, and V: declarations are body-section content.
        for v in self.voices:
            lines.extend(v.to_lines())
        return "\n".join(lines) + "\n"


def build_abc(
    *,
    title: str,
    key: str,
    meter: str = "4/4",
    tempo_bpm: int = 120,
    default_note_length: str = "1/8",
    voices: list[AbcVoice] | None = None,
    composer: str | None = None,
    reference: int = 1,
) -> str:
    """Convenience constructor — builds an :class:`AbcScore` and
    returns its text. Most callers will use this rather than
    instantiating ``AbcHeader`` and ``AbcScore`` directly."""
    header = AbcHeader(
        reference=reference,
        title=title,
        composer=composer,
        meter=meter,
        default_note_length=default_note_length,
        tempo_bpm=tempo_bpm,
        key=key,
    )
    score = AbcScore(header=header, voices=voices or [])
    return score.to_text()
