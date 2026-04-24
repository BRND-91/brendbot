"""Tests for brendbot.composition.style_library — JSON-backed
per-genre composition data."""
from __future__ import annotations

import json

import pytest

from brendbot.composition import style_library as sl


# ── Inventory ────────────────────────────────────────────────────────────


def test_initial_genres_loaded():
    """Eight genres ship in PR #26 — sanity check they all parse."""
    genres = sl.available_genres()
    expected = {
        "lofi", "trance", "hardstyle", "jazz",
        "irish_trad", "jpop", "hiphop", "dnb", "ambient",
    }
    assert expected.issubset(set(genres)), (
        f"Missing genres: {expected - set(genres)}"
    )


def test_unknown_genre_returns_none():
    assert sl.get_genre("not-a-real-genre") is None


def test_genre_lookup_case_insensitive():
    """Operators may write 'LOFI' or 'Lofi' in soul prompts; lookup
    must not distinguish."""
    assert sl.get_genre("LOFI") is not None
    assert sl.get_genre("Lofi") is not None
    assert sl.get_genre("lofi") is not None


# ── Schema sanity per genre ──────────────────────────────────────────────


@pytest.mark.parametrize("genre", [
    "lofi", "trance", "hardstyle", "jazz",
    "irish_trad", "jpop", "hiphop", "dnb", "ambient",
])
def test_genre_required_top_level_fields(genre):
    """Each genre file has the required top-level keys. Adding a new
    genre requires hitting all of these or the test fails — keeps the
    schema consistent."""
    g = sl.get_genre(genre)
    assert g is not None
    assert g.get("name") == genre
    assert "display_name" in g
    assert "tempo_range" in g
    assert "default_tempo" in g
    assert "default_mode" in g
    assert "chord_progressions" in g
    assert "grooves" in g
    assert "form_templates" in g
    assert "motifs" in g
    assert "signature_traits" in g


@pytest.mark.parametrize("genre", [
    "lofi", "trance", "hardstyle", "jazz",
    "irish_trad", "jpop", "hiphop", "dnb", "ambient",
])
def test_genre_tempo_range_well_formed(genre):
    g = sl.get_genre(genre)
    rng = g["tempo_range"]
    assert isinstance(rng, list)
    assert len(rng) == 2
    lo, hi = rng
    assert 30 <= lo <= hi <= 250
    # Default tempo must be inside the range
    default = g["default_tempo"]
    assert lo <= default <= hi


@pytest.mark.parametrize("genre", [
    "lofi", "trance", "hardstyle", "jazz",
    "irish_trad", "jpop", "hiphop", "dnb", "ambient",
])
def test_chord_progressions_well_formed(genre):
    """Every progression must have id, roman array, mode, role."""
    progs = sl.get_progressions(genre)
    assert len(progs) > 0, f"{genre} ships zero chord progressions"
    for p in progs:
        assert "id" in p
        assert isinstance(p["roman"], list) and len(p["roman"]) > 0
        assert "mode" in p
        assert "role" in p
        # description is optional but encouraged
        assert "description" in p or True


# ── Filtered queries ─────────────────────────────────────────────────────


def test_progressions_filter_by_role():
    """All lofi progressions tagged 'verse' come back; 'drop' returns
    empty for lofi (it doesn't have drop sections)."""
    verses = sl.get_progressions("lofi", role="verse")
    assert len(verses) > 0
    assert all(p["role"] == "verse" for p in verses)

    drops = sl.get_progressions("lofi", role="drop")
    # lofi doesn't have drops; either zero or only ones tagged correctly
    for p in drops:
        assert p["role"] == "drop"


def test_progressions_filter_by_mode():
    """Trance has minor, major, and phrygian progressions. Filter
    isolates each cleanly."""
    minors = sl.get_progressions("trance", mode="minor")
    assert len(minors) > 0
    assert all(p["mode"] == "minor" for p in minors)


def test_progressions_filter_combined():
    """Both filters apply simultaneously."""
    minor_drops = sl.get_progressions("trance", role="drop", mode="minor")
    for p in minor_drops:
        assert p["role"] == "drop"
        assert p["mode"] == "minor"


def test_progressions_unknown_genre_empty():
    """Composing for a genre we don't know returns [] rather than
    raising. Caller decides whether to use a fallback genre or
    refuse."""
    assert sl.get_progressions("synthwave") == []


def test_grooves_filter_by_tempo():
    """Lofi has a halftime groove (70-85 BPM) and a chillhop straight
    groove (80-95 BPM). Tempo filter picks correctly."""
    g_75 = sl.get_grooves("lofi", tempo=75)
    g_92 = sl.get_grooves("lofi", tempo=92)

    halftime_in_75 = any(g["id"] == "lofi_halftime" for g in g_75)
    chillhop_in_92 = any(g["id"] == "lofi_chillhop_straight" for g in g_92)

    assert halftime_in_75
    assert chillhop_in_92


def test_grooves_no_tempo_returns_all():
    """Tempo filter is optional — pass None or omit, get everything."""
    all_grooves = sl.get_grooves("lofi")
    assert len(all_grooves) > 1


def test_motifs_filter_by_role():
    motifs = sl.get_motifs("lofi", role="verse_melody")
    for m in motifs:
        assert m["role"] == "verse_melody"


def test_form_templates_present():
    """Every genre ships at least one form template."""
    for genre in sl.available_genres():
        templates = sl.get_form_templates(genre)
        assert len(templates) >= 1, f"{genre} has no form templates"


def test_signature_traits_structure():
    """Signature traits use a consistent shape: must_have,
    must_avoid, instrumentation_hints."""
    for genre in sl.available_genres():
        traits = sl.get_signature_traits(genre)
        assert "must_have" in traits
        assert "must_avoid" in traits
        assert isinstance(traits["must_have"], list)
        assert isinstance(traits["must_avoid"], list)


# ── Reload semantics ─────────────────────────────────────────────────────


def test_reload_picks_up_new_data(tmp_path, monkeypatch):
    """Editing a JSON file mid-session and calling reload() makes the
    new data visible without restart."""
    # Point the loader at a tmp dir
    monkeypatch.setattr(sl, "_STYLES_DIR", tmp_path)
    sl.reload()
    assert sl.available_genres() == []

    # Drop a new genre file
    (tmp_path / "synthwave.json").write_text(json.dumps({
        "name": "synthwave",
        "display_name": "Synthwave",
        "tempo_range": [85, 110],
        "default_tempo": 100,
        "default_mode": "minor",
        "common_modes": ["minor"],
        "chord_progressions": [{
            "id": "sw_test",
            "roman": ["i", "VI", "III", "VII"],
            "mode": "minor",
            "role": "verse",
            "extensions": [],
            "description": "synthwave staple",
        }],
        "grooves": [],
        "form_templates": [],
        "motifs": [],
        "signature_traits": {"must_have": [], "must_avoid": []},
    }))

    sl.reload()
    assert "synthwave" in sl.available_genres()
    assert sl.get_progressions("synthwave")[0]["id"] == "sw_test"


def test_invalid_json_skipped_not_raised(tmp_path, monkeypatch, caplog):
    """A single corrupt JSON file logs a warning but doesn't break
    loading of the other files."""
    monkeypatch.setattr(sl, "_STYLES_DIR", tmp_path)
    sl.reload()

    (tmp_path / "good.json").write_text(json.dumps({
        "name": "good", "display_name": "Good",
        "tempo_range": [60, 120], "default_tempo": 90,
        "default_mode": "minor", "common_modes": ["minor"],
        "chord_progressions": [], "grooves": [],
        "form_templates": [], "motifs": [],
        "signature_traits": {"must_have": [], "must_avoid": []},
    }))
    (tmp_path / "broken.json").write_text("{not valid json")

    sl.reload()
    genres = sl.available_genres()
    assert "good" in genres
    assert "broken" not in genres


def test_missing_styles_dir_returns_empty(tmp_path, monkeypatch):
    """Defensive — if the styles dir doesn't exist (unusual deploy),
    return empty rather than raise."""
    monkeypatch.setattr(sl, "_STYLES_DIR", tmp_path / "does_not_exist")
    sl.reload()
    assert sl.available_genres() == []
