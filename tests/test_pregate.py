"""Tests for the deterministic pregate short-circuit.

The pregate runs in front of the haiku ambiguity classifier for
middle-band messages. Its one heuristic (short_pleasantry) must be
exercised on both the firing and deferring sides so regressions don't
silently re-enable haiku latency on cases we already pinned.

SDK stubs are installed by tests/conftest.py before this file imports.
"""
from __future__ import annotations

import pytest

from brendbot import pregate as pg


# ── pregate_classify — short_pleasantry firing ───────────────────────────

class TestShortPleasantry:
    def test_bare_pleasantry_rejects(self) -> None:
        result = pg.pregate_classify("ok", has_domain=False, name_mentioned=False)
        assert result.decision is False
        assert result.reason == "short_pleasantry"

    def test_pleasantry_with_trailing_punct(self) -> None:
        result = pg.pregate_classify(
            "thanks!", has_domain=False, name_mentioned=False
        )
        assert result.decision is False
        assert result.reason == "short_pleasantry"

    def test_multi_word_trailing_pleasantry(self) -> None:
        # "yeah same" ≤ 15 chars, no '?', no domain, last token "same"
        # is in the pleasantries list → short_pleasantry.
        result = pg.pregate_classify(
            "yeah same", has_domain=False, name_mentioned=False
        )
        assert result.decision is False

    def test_mid_sentence_pleasantry_last_token_check(self) -> None:
        # The heuristic inspects the LAST token; "wow haha" ends in "haha"
        # which is in the list → short_pleasantry fires.
        result = pg.pregate_classify(
            "wow haha", has_domain=False, name_mentioned=False
        )
        assert result.decision is False

    def test_ellipsis_stripped(self) -> None:
        result = pg.pregate_classify(
            "nice...", has_domain=False, name_mentioned=False
        )
        assert result.decision is False


# ── pregate_classify — deferral (decision is None) ───────────────────────

class TestDeferralPaths:
    def test_too_long_defers(self) -> None:
        # Longer than max_pleasantry_length (15 chars). Last token "ok"
        # is a pleasantry, but the length gate must defer first.
        result = pg.pregate_classify(
            "that is definitely ok",
            has_domain=False, name_mentioned=False,
        )
        assert result.decision is None
        assert result.reason == ""

    def test_has_domain_defers(self) -> None:
        # Short, ends in pleasantry — but has_domain trips the gate.
        # Caller should hand this to haiku.
        result = pg.pregate_classify(
            "r-value ok", has_domain=True, name_mentioned=False
        )
        assert result.decision is None

    def test_name_mentioned_defers(self) -> None:
        result = pg.pregate_classify(
            "brend ok", has_domain=False, name_mentioned=True
        )
        assert result.decision is None

    def test_question_mark_defers(self) -> None:
        # A question is never a pleasantry, even if it ends in one.
        result = pg.pregate_classify(
            "ok?", has_domain=False, name_mentioned=False
        )
        assert result.decision is None

    def test_non_pleasantry_last_token_defers(self) -> None:
        result = pg.pregate_classify(
            "that works", has_domain=False, name_mentioned=False
        )
        assert result.decision is None

    def test_empty_text_defers(self) -> None:
        # Empty or whitespace-only text never fires a heuristic.
        result = pg.pregate_classify(
            "", has_domain=False, name_mentioned=False
        )
        assert result.decision is None

    def test_whitespace_only_defers(self) -> None:
        result = pg.pregate_classify(
            "   ", has_domain=False, name_mentioned=False
        )
        assert result.decision is None

    def test_punctuation_only_last_token_defers(self) -> None:
        # The last token strips to empty after trailing-punct removal.
        # The heuristic must defer rather than treat "" as a match.
        result = pg.pregate_classify(
            "hmm ...", has_domain=False, name_mentioned=False
        )
        # "..." strips to "" — defers regardless of what comes before.
        assert result.decision is None

    def test_case_insensitive_match(self) -> None:
        # "OK" and "Ok" must match "ok" in the lowercase pleasantries set.
        r1 = pg.pregate_classify("OK", has_domain=False, name_mentioned=False)
        r2 = pg.pregate_classify("Ok", has_domain=False, name_mentioned=False)
        assert r1.decision is False
        assert r2.decision is False


# ── Config loading ───────────────────────────────────────────────────────

class TestConfigLoad:
    def test_module_loads_pleasantries_from_yaml(self) -> None:
        # engagement.yaml ships with a known set; if the loader wires up
        # correctly a couple of canonical entries must be present.
        assert "ok" in pg._PLEASANTRIES
        assert "thanks" in pg._PLEASANTRIES
        assert "lol" in pg._PLEASANTRIES

    def test_max_length_populated(self) -> None:
        assert pg._MAX_LENGTH > 0

    def test_refresh_is_idempotent(self) -> None:
        # Calling refresh twice should leave config identical. Regression
        # guard against accidental accumulation (e.g. list-append instead
        # of frozenset-replace).
        before_plz = pg._PLEASANTRIES
        before_max = pg._MAX_LENGTH
        pg.refresh_pregate_config()
        pg.refresh_pregate_config()
        assert pg._PLEASANTRIES == before_plz
        assert pg._MAX_LENGTH == before_max

    def test_refresh_with_missing_block_keeps_previous(
        self, monkeypatch, tmp_path
    ) -> None:
        """If the yaml is rewritten without a pregate block, refresh
        warns and resets to never-fires rather than raising.
        A subsequent refresh against the real yaml must restore state."""
        yaml_path = tmp_path / "engagement.yaml"
        yaml_path.write_text("thresholds:\n  hard_pass: 0.85\n")
        original_path = pg._ENGAGEMENT_YAML
        monkeypatch.setattr(pg, "_ENGAGEMENT_YAML", yaml_path)
        pg.refresh_pregate_config()
        assert pg._MAX_LENGTH == 0
        # Result of pregate should always defer in this state.
        result = pg.pregate_classify(
            "ok", has_domain=False, name_mentioned=False
        )
        assert result.decision is None
        # Restore for the remaining tests in the session.
        monkeypatch.setattr(pg, "_ENGAGEMENT_YAML", original_path)
        pg.refresh_pregate_config()
        assert pg._MAX_LENGTH > 0

    def test_pleasantries_are_lowercased_on_load(self) -> None:
        # Guard against yaml authoring in mixed case leaking into the set.
        for p in pg._PLEASANTRIES:
            assert p == p.lower()


# ── PregateResult dataclass ──────────────────────────────────────────────

class TestPregateResult:
    def test_defer_sentinel_is_reused(self) -> None:
        # Both deferral paths should return the same object instance; the
        # module declares _DEFER once specifically to avoid per-call
        # allocation on the common path.
        r1 = pg.pregate_classify(
            "nothing here", has_domain=False, name_mentioned=False
        )
        r2 = pg.pregate_classify(
            "also nothing", has_domain=False, name_mentioned=False
        )
        assert r1 is pg._DEFER
        assert r2 is pg._DEFER

    def test_fire_allocates_new_result(self) -> None:
        # On firing, a new PregateResult is constructed — not the sentinel.
        r = pg.pregate_classify("ok", has_domain=False, name_mentioned=False)
        assert r is not pg._DEFER
        assert isinstance(r, pg.PregateResult)
