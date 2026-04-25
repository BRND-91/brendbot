"""Tests for brendbot.composition.abc_grammar — ABC notation
construction helpers.

These exercise the pure-string-building API. ABC parsing /
roundtripping through music21 is in test_music21_layer.py; this
file just pins that the strings we emit have the right shape.
"""
from __future__ import annotations

import pytest

from brendbot.composition import abc_grammar as ag


# ── Note token ──────────────────────────────────────────────────────────


def test_note_middle_c():
    """Middle-C-octave notes use uppercase letters with no marks."""
    assert ag.note("C") == "C"
    assert ag.note("D") == "D"
    assert ag.note("B", octave=4) == "B"


def test_note_octave_5_lowercase():
    """One octave above middle C uses lowercase letters."""
    assert ag.note("c", octave=5) == "c"
    assert ag.note("C", octave=5) == "c"
    assert ag.note("a", octave=5) == "a"


def test_note_octave_higher_apostrophes():
    """Octave 6 = lowercase + one apostrophe."""
    assert ag.note("C", octave=6) == "c'"
    assert ag.note("C", octave=7) == "c''"


def test_note_octave_lower_commas():
    """Octave 3 = uppercase + one comma."""
    assert ag.note("C", octave=3) == "C,"
    assert ag.note("C", octave=2) == "C,,"


def test_note_sharp_emits_caret():
    assert ag.note("F", accidental="#") == "^F"
    assert ag.note("c", octave=5, accidental="#") == "^c"


def test_note_flat_emits_underscore():
    assert ag.note("B", accidental="b") == "_B"


def test_note_natural_override():
    assert ag.note("F", accidental="=") == "=F"


def test_note_unicode_accidentals_accepted():
    """``♯`` and ``♭`` aliases for ``#`` and ``b`` — operators may
    paste them from notation programs."""
    assert ag.note("F", accidental="♯") == "^F"
    assert ag.note("B", accidental="♭") == "_B"


def test_note_duration_default_omitted():
    """Default duration of 1 doesn't append a number — matches
    standard ABC convention where bare ``C`` means one default unit."""
    assert ag.note("C") == "C"
    assert "1" not in ag.note("C")


def test_note_duration_integer():
    assert ag.note("C", duration=2) == "C2"
    assert ag.note("C", duration=4) == "C4"


def test_note_duration_fractional():
    """Dotted figures express as fractions: ``C3/2`` is a dotted
    eighth in an L:1/8 context."""
    assert ag.note("C", duration="3/2") == "C3/2"


def test_note_unknown_pitch_raises():
    with pytest.raises(ValueError):
        ag.note("H")


def test_note_unknown_accidental_raises():
    with pytest.raises(ValueError):
        ag.note("C", accidental="x")


def test_note_case_insensitive_pitch_letter():
    """``"c"`` and ``"C"`` both refer to the C note class — octave
    handles the case selection."""
    assert ag.note("c", octave=4) == ag.note("C", octave=4)


# ── Rest, chord, label ──────────────────────────────────────────────────


def test_rest_default():
    assert ag.rest() == "z"


def test_rest_long():
    assert ag.rest(4) == "z4"


def test_chord_stack():
    """Three-note triad: ``[ACE]`` for A minor or A major top voicing."""
    triad = ag.chord_stack([ag.note("A"), ag.note("c", 5), ag.note("e", 5)])
    assert triad == "[Ace]"


def test_chord_label_attached():
    assert ag.chord_label("Am", "A2 c2 e2") == '"Am"A2 c2 e2'


def test_chord_label_strips_double_quote():
    """A double-quote inside the label would close the annotation
    early. The helper sanitises."""
    assert '""' not in ag.chord_label('A"m', "A")


# ── Bar ──────────────────────────────────────────────────────────────────


def test_bar_appends_pipe():
    assert ag.bar("A2 B2 C2 D2") == "A2 B2 C2 D2 |"


# ── Header ──────────────────────────────────────────────────────────────


def test_header_minimal():
    """Required fields only — X, T, M, L, K — produces a valid
    ABC header."""
    h = ag.AbcHeader(reference=1, title="Test", meter="4/4",
                     default_note_length="1/8", key="C")
    lines = h.to_lines()
    assert lines == ["X:1", "T:Test", "M:4/4", "L:1/8", "K:C"]


def test_header_with_tempo_and_composer():
    h = ag.AbcHeader(
        reference=2, title="Another", composer="brendbot",
        meter="3/4", default_note_length="1/4", tempo_bpm=140,
        key="Am",
    )
    lines = h.to_lines()
    assert "X:2" in lines
    assert "T:Another" in lines
    assert "C:brendbot" in lines
    assert "M:3/4" in lines
    assert "L:1/4" in lines
    assert "Q:1/4=140" in lines
    assert "K:Am" in lines
    # Key must come last
    assert lines[-1] == "K:Am"


# ── Voice ────────────────────────────────────────────────────────────────


def test_voice_minimal():
    v = ag.AbcVoice(voice_id=1, body="A2 c2 e2 c2")
    lines = v.to_lines()
    assert lines[0] == "V:1"
    assert lines[-1] == "[V:1]A2 c2 e2 c2"


def test_voice_with_name_and_clef():
    v = ag.AbcVoice(voice_id=2, body="A,4 F,4", name="bass", clef="bass")
    lines = v.to_lines()
    assert lines[0] == 'V:2 name="bass" clef=bass'


def test_voice_with_midi_program():
    """MIDI program directives sit on their own lines after V:.
    abc2midi and music21 both honour these for fluidsynth routing."""
    v = ag.AbcVoice(voice_id=1, body="A2", midi_program=80, midi_channel=0)
    lines = v.to_lines()
    assert any("MIDI program 80" in l for l in lines)
    # ABC midi channels are 1-indexed; our API takes 0-indexed
    assert any("MIDI channel 1" in l for l in lines)


# ── Score assembly ──────────────────────────────────────────────────────


def test_build_abc_minimal():
    """End-to-end: tempo, key, single voice, a couple of bars."""
    abc = ag.build_abc(
        title="Hello",
        key="Am",
        meter="4/4",
        tempo_bpm=120,
        voices=[ag.AbcVoice(voice_id=1, body='"Am"A2 c2 e2 c2 |')],
    )
    assert "X:1" in abc
    assert "T:Hello" in abc
    assert "K:Am" in abc
    assert "Q:1/4=120" in abc
    assert "V:1" in abc
    assert '"Am"A2 c2 e2 c2 |' in abc
    # Newline-terminated for clean append/pipe
    assert abc.endswith("\n")


def test_build_abc_two_voices():
    """Lead + bass over the same chord progression."""
    abc = ag.build_abc(
        title="TwoVoice",
        key="C",
        voices=[
            ag.AbcVoice(voice_id=1, name="lead",
                        body='"C"c2 e2 g2 e2 |'),
            ag.AbcVoice(voice_id=2, name="bass", clef="bass",
                        body='"C"C,4 G,,4 |'),
        ],
    )
    assert "V:1" in abc
    assert "V:2" in abc
    assert 'name="lead"' in abc
    assert 'name="bass"' in abc
    assert "clef=bass" in abc


def test_build_abc_no_voices_renders_header_only():
    """An empty piece is still a valid ABC document — useful for
    pipeline stages that emit just the header before the body is
    composed."""
    abc = ag.build_abc(title="Skeleton", key="D")
    assert "X:1" in abc
    assert "T:Skeleton" in abc
    assert "K:D" in abc
    assert "V:" not in abc


# ── Integration: realistic 4-bar phrase ─────────────────────────────────


def test_realistic_4_bar_lofi_phrase():
    """A representative output the pipeline might emit for a lofi
    verse: chord-labeled lead voice + bass voice, four bars of a
    Am-F-C-G progression."""
    lead_body = (
        " ".join([
            ag.chord_label("Am", "A2 c2 e2 c2") + " |",
            ag.chord_label("F", "f4 e4") + " |",
            ag.chord_label("C", "e2 g2 c2 G2") + " |",
            ag.chord_label("G", "d2 B2 G2 B2") + " |",
        ])
    )
    bass_body = (
        " ".join([
            ag.chord_label("Am", "A,4 E,4") + " |",
            ag.chord_label("F", "F,4 C,4") + " |",
            ag.chord_label("C", "C,4 G,,4") + " |",
            ag.chord_label("G", "G,,4 D,4") + " |",
        ])
    )
    abc = ag.build_abc(
        title="Lofi 4-bar",
        key="Am",
        meter="4/4",
        tempo_bpm=80,
        voices=[
            ag.AbcVoice(voice_id=1, name="lead", body=lead_body,
                        midi_program=4),  # Rhodes
            ag.AbcVoice(voice_id=2, name="bass", clef="bass",
                        body=bass_body, midi_program=33),  # finger bass
        ],
    )
    # Content checks — every chord, every bar present
    for chord in ("Am", "F", "C", "G"):
        assert f'"{chord}"' in abc
    # Four bars in each voice = 4 bar-lines per voice = 8 total
    assert abc.count(" |") == 8
    # MIDI program assignments propagated
    assert "MIDI program 4" in abc
    assert "MIDI program 33" in abc
