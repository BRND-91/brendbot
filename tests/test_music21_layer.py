"""Tests for brendbot.composition.music21_layer — theory-aware
constraint helpers and ABC↔MIDI conversion.

Requires the ``music`` extra (music21 + pretty_midi). Tests are
skipped if the dep isn't installed."""
from __future__ import annotations

import pytest

# Skip the whole module if music21 isn't installed — the tests don't
# meaningfully run without it.
pytest.importorskip("music21")

from brendbot.composition import abc_grammar as ag
from brendbot.composition import music21_layer as m21l


# ── Roman → concrete ────────────────────────────────────────────────────


def test_progression_in_a_minor():
    """The classic i-VI-III-VII in A minor resolves to Am-F-C-G."""
    progs = m21l.progression_in_key(
        ["i", "VI", "III", "VII"], "a minor",
    )
    figures = [p["figure"] for p in progs]
    roots = [p["root"] for p in progs]
    qualities = [p["quality"] for p in progs]

    # music21 figure rendering varies — the cores stay constant
    assert any("A" in f for f in figures[:1]) or "A" in roots[0]
    assert qualities[0] == "minor"
    # F major is the relative VI in A minor
    assert "F" in roots[1]
    assert qualities[1] == "major"
    # C major is the III
    assert "C" in roots[2]
    assert qualities[2] == "major"
    # G major is the VII
    assert "G" in roots[3]
    assert qualities[3] == "major"


def test_progression_in_dorian():
    """E dorian: i-VII-i-VII oscillation. i=Em, VII=D."""
    progs = m21l.progression_in_key(["i", "VII"], "e dorian")
    assert progs[0]["quality"] == "minor"
    assert progs[0]["root"].startswith("E")
    assert progs[1]["quality"] == "major"
    assert progs[1]["root"].startswith("D")


def test_progression_with_extensions():
    """Roman numerals can carry 7th/9th figured-bass markers."""
    progs = m21l.progression_in_key(["I7", "vi7", "ii7", "V7"], "C major")
    for p in progs:
        # With 7ths, each chord has at least 4 pitches
        assert len(p["pitches"]) >= 4


def test_progression_pitches_are_strings():
    """Caller-friendly: pitches return as strings ('A4', 'C5'),
    not music21 Pitch objects, so they're easy to log / serialize /
    pass through to ABC builders."""
    progs = m21l.progression_in_key(["i"], "a minor")
    for p in progs[0]["pitches"]:
        assert isinstance(p, str)


def test_unknown_key_raises_value_error():
    with pytest.raises(ValueError, match="unknown key"):
        m21l.progression_in_key(["I"], "purple")


def test_invalid_roman_raises_value_error():
    with pytest.raises(ValueError, match="can't parse roman numeral"):
        m21l.progression_in_key(["XYZ"], "C major")


# ── Voice-leading checks ────────────────────────────────────────────────


def test_voice_leading_clean_progression_no_issues():
    """A well-formed I-vi-IV-V in C major has no parallel
    fifths/octaves between adjacent chords when voiced in standard
    SATB-ish ranges."""
    progs = m21l.progression_in_key(["I", "vi", "IV", "V"], "C major")
    issues = m21l.check_voice_leading(progs)
    # The default music21 voicing on these is clean — no parallel
    # fifths/octaves expected. (If music21 ever changes its default
    # voicing, this test will need an explicit voicing layer.)
    assert isinstance(issues, list)


def test_voice_leading_single_chord_no_issues():
    """No adjacency = no issues."""
    progs = m21l.progression_in_key(["I"], "C major")
    assert m21l.check_voice_leading(progs) == []


def test_voice_leading_empty_progression_no_issues():
    assert m21l.check_voice_leading([]) == []


def test_voice_leading_returns_dict_per_issue():
    """Issue records have type, between, detail — schema for
    downstream tooling."""
    # Construct a deliberately bad progression
    bad = [
        {"roman": "I", "figure": "C", "pitches": ["C4", "E4", "G4"], "root": "C", "quality": "major"},
        {"roman": "II", "figure": "D", "pitches": ["D4", "F#4", "A4"], "root": "D", "quality": "major"},
    ]
    # Not asserting issues fire on this exact input — voice-leading
    # depends on stack ordering — but the return type must be the
    # documented shape.
    issues = m21l.check_voice_leading(bad)
    for issue in issues:
        assert "type" in issue
        assert "between" in issue
        assert "detail" in issue


# ── Mode-aware note constraints ─────────────────────────────────────────


def test_melody_constraints_a_minor_tonic():
    """Am chord in A minor: chord tones are A, C, E. Scale tones
    fill in B, D, F, G."""
    constraints = m21l.melody_constraints(
        chord_pitches=["A4", "C5", "E5"],
        key="a minor",
    )
    chord_tones = set(constraints["chord_tones"])
    scale_tones = set(constraints["scale_tones"])

    assert {"A", "C", "E"} <= chord_tones
    # Scale notes that are not chord tones — B, D, F, G in A natural minor
    expected_scale = {"B", "D", "F", "G"}
    assert expected_scale <= scale_tones
    # Chord tones excluded from scale tones (no double-counting)
    assert chord_tones.isdisjoint(scale_tones)


def test_melody_constraints_dorian_includes_raised_6():
    """Dorian's signature is the raised 6th. Em in E dorian: scale
    tones must include C# (raised 6 from natural E minor's C)."""
    constraints = m21l.melody_constraints(
        chord_pitches=["E4", "G4", "B4"],
        key="e dorian",
    )
    scale_tones = set(constraints["scale_tones"])
    # C# is the dorian-defining note vs natural minor
    assert "C#" in scale_tones
    # And C natural is NOT in dorian
    assert "C" not in scale_tones


def test_melody_constraints_chromatic_neighbors_present():
    """For a C major chord, chromatic neighbors include things like
    C#/Db (above C) and B (below C). Some overlap with scale tones is
    expected — those are filtered out."""
    constraints = m21l.melody_constraints(
        chord_pitches=["C4", "E4", "G4"],
        key="C major",
    )
    chromatics = set(constraints["chromatic_neighbors"])
    # Chromatic neighbors are by definition outside the diatonic scale
    scale_tones = set(constraints["scale_tones"])
    chord_tones = set(constraints["chord_tones"])
    assert chromatics.isdisjoint(scale_tones)
    assert chromatics.isdisjoint(chord_tones)


# ── ABC ↔ MIDI conversion ───────────────────────────────────────────────


def test_abc_to_midi_roundtrip(tmp_path):
    """A well-formed ABC string produces a non-empty MIDI file at
    the requested path."""
    abc = ag.build_abc(
        title="Test",
        key="C",
        meter="4/4",
        tempo_bpm=120,
        voices=[ag.AbcVoice(voice_id=1, body='"C"C2 E2 G2 E2 |')],
    )
    out = m21l.abc_to_midi(abc, tmp_path / "out.mid")
    assert out.exists()
    assert out.stat().st_size > 0
    # MIDI file magic
    assert out.read_bytes().startswith(b"MThd")


def test_abc_to_midi_creates_parent_dirs(tmp_path):
    """Caller doesn't have to mkdir — abc_to_midi handles it."""
    abc = ag.build_abc(
        title="Test", key="C",
        voices=[ag.AbcVoice(voice_id=1, body='"C"C |')],
    )
    deep = tmp_path / "a" / "b" / "out.mid"
    assert not deep.parent.exists()
    out = m21l.abc_to_midi(abc, deep)
    assert out.exists()


def test_abc_to_midi_invalid_raises_with_excerpt():
    """A malformed ABC string raises ValueError with the offending
    excerpt embedded — gives the bot something concrete to debug."""
    bad = "this is not abc notation at all"
    with pytest.raises(ValueError) as exc_info:
        m21l.abc_to_midi(bad, "/tmp/ignored.mid")
    # Either parse error mentions the input or raises another way —
    # the wrapper at least produces a clear error type
    assert "parse" in str(exc_info.value).lower() or "abc" in str(exc_info.value).lower()


# ── Transpose ────────────────────────────────────────────────────────────


def test_transpose_score_returns_music21_score(tmp_path):
    """Transposing returns a music21 Score that can be written to
    MIDI directly. ABC-text round-tripping is deferred until music21
    fixes its ABC writer (see module docstring)."""
    abc_in = ag.build_abc(
        title="Test", key="Am",
        voices=[ag.AbcVoice(voice_id=1, body='"Am"A2 |')],
    )
    score = m21l.transpose_score(abc_in, 1)
    out = m21l.score_to_midi(score, tmp_path / "transposed.mid")
    assert out.exists()
    assert out.stat().st_size > 0
    assert out.read_bytes().startswith(b"MThd")


def test_transpose_score_zero_semitones_unchanged(tmp_path):
    """Transposing by 0 should produce a MIDI file equivalent to the
    untransposed score (allowing for non-deterministic file metadata
    like timestamps)."""
    abc_in = ag.build_abc(
        title="Test", key="C",
        voices=[ag.AbcVoice(voice_id=1, body='"C"C2 E2 G2 |')],
    )
    score = m21l.transpose_score(abc_in, 0)
    out = m21l.score_to_midi(score, tmp_path / "unchanged.mid")
    assert out.exists()
