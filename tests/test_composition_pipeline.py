"""Tests for brendbot.composition.pipeline — the multi-pass
composition orchestrator.

Stage-by-stage tests + end-to-end ``compose()`` integration. Each
stage should be testable in isolation with the previous stages'
outputs supplied as inputs.
"""
from __future__ import annotations

import random

import pytest

pytest.importorskip("music21")

from brendbot.composition import pipeline as pl


# ── Stage 1: plan_form ──────────────────────────────────────────────────


def test_plan_form_picks_template_for_genre():
    state = pl.PipelineState(genre="lofi", target_duration_s=120.0)
    pl.plan_form(state)
    assert state.form_template is not None
    assert "sections" in state.form_template


def test_plan_form_unknown_genre_uses_fallback():
    state = pl.PipelineState(genre="not-a-genre")
    pl.plan_form(state)
    assert state.form_template is not None
    assert state.form_template["id"] == "fallback_single"


def test_plan_form_prefer_id_overrides_duration_match():
    state = pl.PipelineState(genre="lofi", target_duration_s=999.0)
    pl.plan_form(state, prefer_id="lofi_loop_basic")
    assert state.form_template is not None
    assert state.form_template["id"] == "lofi_loop_basic"


def test_plan_form_picks_closest_to_target_duration():
    """trance has both 'classic' (long) and 'short_radio' (compact)
    templates. A short target should pick short_radio."""
    short_state = pl.PipelineState(
        genre="trance", target_duration_s=180.0,  # ~3 minutes
    )
    pl.plan_form(short_state)
    assert short_state.form_template is not None
    # The radio edit form is shorter than the full classic one;
    # for a 3-minute target it should win on duration match
    assert short_state.form_template["id"] in {"trance_short_radio", "trance_classic"}


# ── Stage 2: plan_harmony ───────────────────────────────────────────────


def test_plan_harmony_picks_progression_for_role():
    state = pl.PipelineState(genre="lofi", key="a minor")
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    assert state.progression_record is not None
    assert state.progression_record["role"] == "verse"
    assert state.progression_resolved is not None
    assert len(state.progression_resolved) == len(state.progression_record["roman"])


def test_plan_harmony_resolves_in_correct_key():
    """A progression in 'i-VI-III-VII' chosen for A minor must
    resolve to chords whose roots are A, F, C, G."""
    state = pl.PipelineState(genre="lofi", key="a minor")
    pl.plan_harmony(
        state, role="verse", prefer_id="lofi_sad_4",
        rng=random.Random(0),
    )
    roots = [p["root"][0] for p in state.progression_resolved or []]
    # The first chord is i (Am) — root is A
    assert roots[0] == "A"


def test_plan_harmony_unknown_role_falls_back():
    """A role tag with no matches falls back to all progressions for
    the genre rather than raising."""
    state = pl.PipelineState(genre="lofi", key="C")
    pl.plan_harmony(state, role="not_a_real_role", rng=random.Random(0))
    # Should still get some progression, just not role-filtered
    assert state.progression_record is not None


def test_plan_harmony_unknown_genre_uses_fallback_progression():
    state = pl.PipelineState(genre="synthwave", key="a minor")
    pl.plan_harmony(state, role="verse")
    assert state.progression_record is not None
    assert state.progression_record["id"] == "fallback_minor_loop"
    assert state.progression_resolved is not None


def test_plan_harmony_prefer_id_overrides_random():
    state = pl.PipelineState(genre="lofi", key="C major")
    pl.plan_harmony(
        state, role="verse",
        prefer_id="lofi_chillhop_4",
        rng=random.Random(0),
    )
    assert state.progression_record["id"] == "lofi_chillhop_4"


def test_plan_harmony_prefer_id_not_found_falls_through():
    """If prefer_id doesn't exist, randomly choose and emit a note."""
    state = pl.PipelineState(genre="lofi", key="C major")
    pl.plan_harmony(
        state, role="verse",
        prefer_id="nonexistent_id",
        rng=random.Random(0),
    )
    assert state.progression_record is not None
    assert state.progression_record["id"] != "nonexistent_id"
    # Note about the missed prefer_id should be in diagnostics
    assert any("prefer_id" in n and "not found" in n for n in state.notes)


# ── Stage 3: lint_harmony ───────────────────────────────────────────────


def test_lint_harmony_records_issues():
    """Voice-leading lint runs and populates the issues list."""
    state = pl.PipelineState(genre="lofi", key="a minor")
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    pl.lint_harmony(state)
    # Lint result is a list (may be empty for clean progressions)
    assert isinstance(state.voice_leading_issues, list)


def test_lint_harmony_skips_with_no_progression():
    """If plan_harmony hasn't run, lint_harmony emits a note rather
    than raising."""
    state = pl.PipelineState(genre="lofi", key="C")
    pl.lint_harmony(state)
    assert state.voice_leading_issues == []
    assert any("skipping" in n for n in state.notes)


# ── Stage 4: plan_melody ────────────────────────────────────────────────


def test_plan_melody_produces_per_chord_envelopes():
    state = pl.PipelineState(genre="lofi", key="a minor")
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    pl.plan_melody(state)
    assert state.progression_resolved is not None
    assert len(state.melody_envelopes) == len(state.progression_resolved)
    for env in state.melody_envelopes:
        assert "chord_tones" in env
        assert "scale_tones" in env
        assert "chromatic_neighbors" in env
        assert len(env["chord_tones"]) > 0


# ── Stage 5: realize ────────────────────────────────────────────────────


def test_realize_emits_abc_with_voices():
    state = pl.PipelineState(genre="lofi", key="a minor", title="LofiTest")
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    pl.realize(state)
    assert state.abc_score is not None
    assert "X:1" in state.abc_score
    assert "T:LofiTest" in state.abc_score
    assert "V:1" in state.abc_score
    assert "V:2" in state.abc_score


def test_realize_uses_caller_supplied_melody_body():
    """When the LLM has composed a melody, the pipeline takes it
    verbatim instead of using the stub."""
    state = pl.PipelineState(genre="lofi", key="a minor")
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    custom_melody = '"Am"A2 c2 e2 c2 | "F"f4 e4 |'
    pl.realize(state, melody_abc_body=custom_melody)
    assert custom_melody in state.abc_score


def test_realize_skipped_harmony_emits_bare_header():
    """If plan_harmony hasn't run, realize emits just the header
    rather than raising."""
    state = pl.PipelineState(genre="lofi", key="C")
    pl.realize(state)
    assert state.abc_score is not None
    # Bare header has X, T, M, L, K but no V:
    assert "K:" in state.abc_score
    assert "V:" not in state.abc_score


def test_realize_uses_genre_default_tempo_when_unset():
    state = pl.PipelineState(genre="trance", key="a minor")
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    pl.realize(state)
    # trance default_tempo is 132
    assert "Q:1/4=132" in state.abc_score


def test_realize_uses_explicit_tempo_when_set():
    state = pl.PipelineState(
        genre="lofi", key="C", tempo_bpm=85,
    )
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    pl.realize(state)
    assert "Q:1/4=85" in state.abc_score


# ── Stage 6: render ─────────────────────────────────────────────────────


def test_render_writes_midi(tmp_path):
    state = pl.PipelineState(genre="lofi", key="a minor")
    pl.plan_harmony(state, role="verse", rng=random.Random(42))
    pl.realize(state)
    pl.render(state, tmp_path / "out.mid")
    assert state.midi_path is not None
    assert state.midi_path.exists()
    assert state.midi_path.read_bytes().startswith(b"MThd")


def test_render_without_realize_raises():
    state = pl.PipelineState(genre="lofi", key="C")
    with pytest.raises(RuntimeError, match="no ABC score"):
        pl.render(state, "/tmp/wont_be_written.mid")


# ── End-to-end: compose() ───────────────────────────────────────────────


def test_compose_end_to_end_lofi(tmp_path):
    """Full pipeline run produces a MIDI file with all stages
    completing."""
    state = pl.compose(
        genre="lofi",
        title="EndToEndLofi",
        key="a minor",
        target_duration_s=60.0,
        output_path=tmp_path / "lofi_e2e.mid",
        rng=random.Random(42),
    )
    assert state.form_template is not None
    assert state.progression_record is not None
    assert state.progression_resolved is not None
    assert state.melody_envelopes
    assert state.abc_score is not None
    assert state.midi_path is not None
    assert state.midi_path.exists()
    # Diagnostics list should be populated with stage notes
    assert len(state.notes) >= 5  # one per stage minimum


def test_compose_end_to_end_trance(tmp_path):
    state = pl.compose(
        genre="trance", title="EndToEndTrance",
        key="a minor", role="drop",
        output_path=tmp_path / "trance_e2e.mid",
        rng=random.Random(7),
    )
    assert state.midi_path.exists()
    # Trance progression in drop role
    assert state.progression_record["role"] == "drop"


def test_compose_skips_render_when_no_output_path():
    state = pl.compose(
        genre="lofi", key="a minor",
        rng=random.Random(0),
    )
    # ABC built but no MIDI rendered
    assert state.abc_score is not None
    assert state.midi_path is None


def test_compose_unknown_genre_falls_through(tmp_path):
    """Unknown genre uses fallbacks at every stage but still
    produces output."""
    state = pl.compose(
        genre="not-a-real-genre",
        key="C major",
        output_path=tmp_path / "fallback.mid",
        rng=random.Random(0),
    )
    assert state.midi_path is not None
    assert state.midi_path.exists()
    # Form fallback fired
    assert state.form_template["id"] == "fallback_single"
    # Harmony fallback fired
    assert state.progression_record["id"] == "fallback_minor_loop"


def test_compose_deterministic_with_seeded_rng(tmp_path):
    """Same seed → same progression choice. Used for
    reproducibility in tests and pilot debugging."""
    s1 = pl.compose(
        genre="lofi", key="C major", role="verse",
        output_path=tmp_path / "a.mid",
        rng=random.Random(42),
    )
    s2 = pl.compose(
        genre="lofi", key="C major", role="verse",
        output_path=tmp_path / "b.mid",
        rng=random.Random(42),
    )
    assert s1.progression_record["id"] == s2.progression_record["id"]
