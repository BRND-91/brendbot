"""Tests for engagement scoring and address-level classification.

These tests pin the behavior described in engagement.yaml. If you change the
yaml thresholds or add a new domain, update the assertions here.

SDK stubs are installed by tests/conftest.py before this file imports.
"""
from __future__ import annotations

import time

import pytest

from brendbot import discord as bd


# ── _score_message ───────────────────────────────────────────────────────

class TestScoreMessage:
    def setup_method(self) -> None:
        # Reset recency state between tests so prior tests don't leak.
        bd._channel_last_spoke.clear()

    def test_pure_noise_token_drops(self) -> None:
        result = bd._score_message("lol", "ch1", False, None)
        assert result.score == 0.0
        assert result.domains == set()

    def test_two_word_noise_drops(self) -> None:
        result = bd._score_message("lol same", "ch1", False, None)
        assert result.score == 0.0

    def test_three_word_noise_does_not_short_circuit(self) -> None:
        # Three+ tokens bypass the short-circuit even if all noise — they
        # may still score 0 but the early-return path is gated to ≤2 words.
        result = bd._score_message("lol same bet", "ch1", False, None)
        # No domain, no recency, no reply — score stays 0.
        assert result.score == 0.0

    def test_reply_to_bot_strong_signal(self) -> None:
        result = bd._score_message("ok cool", "ch1", is_reply_to_bot=True)
        assert result.score >= bd._SCORE_REPLY_TO_BOT
        assert result.score >= bd.ENGAGE_HARD_PASS  # reply alone clears hard pass

    def test_buildsci_domain_match(self) -> None:
        result = bd._score_message(
            "what's the r-value of fiberglass insulation?",
            "ch1", False, None,
        )
        assert "BUILDSCI" in result.domains
        assert result.score >= bd._SCORE_DOMAIN

    def test_systems_multi_word_phrase(self) -> None:
        result = bd._score_message(
            "explain feedback loops in complex systems",
            "ch1", False, None,
        )
        assert "SYSTEMS" in result.domains

    def test_word_boundary_no_false_positive(self) -> None:
        # "stats" should not match the word "statistical" if it's not in the
        # domain list — and "delay" should not match "delayed". The compiled
        # regex uses \b boundaries so substring matches don't fire.
        result = bd._score_message("delayed reaction time", "ch1", False, None)
        # "delay" is in SYSTEMS but only as exact word — "delayed" should not match.
        assert "SYSTEMS" not in result.domains

    def test_recency_boost_only_with_word_count(self) -> None:
        bd._channel_last_spoke["ch1"] = time.time()
        # Two-word non-noise message: no recency boost (word_count < 3).
        r1 = bd._score_message("interesting point", "ch1", False, None)
        assert r1.score == 0.0
        # Three+ word message: gets recency boost.
        r2 = bd._score_message("that is an interesting point", "ch1", False, None)
        assert r2.score >= bd._SCORE_RECENCY

    def test_conversational_in_active_thread(self) -> None:
        bd._channel_last_spoke["ch1"] = time.time()
        result = bd._score_message(
            "what do you think about that",
            "ch1", False, None,
        )
        # recency (0.3) + conversational (0.2) = 0.5
        assert result.score >= 0.5

    def test_no_recency_when_stale(self) -> None:
        bd._channel_last_spoke["ch1"] = time.time() - bd.RECENCY_WINDOW_SECONDS - 60
        result = bd._score_message(
            "tell me about the weather",
            "ch1", False, None,
        )
        # No recency boost — only conversational starter doesn't fire either
        # because conversational is gated on recency_active. Score = 0.
        assert result.score == 0.0


# ── _classify_address ────────────────────────────────────────────────────

class TestClassifyAddress:
    def test_at_mention_always_high(self) -> None:
        assert bd._classify_address(0.0, is_at_mention=True) == "high"
        assert bd._classify_address(0.1, is_at_mention=True) == "high"

    def test_score_at_hard_pass_is_high(self) -> None:
        assert bd._classify_address(0.85, is_at_mention=False) == "high"
        assert bd._classify_address(0.9, is_at_mention=False) == "high"
        assert bd._classify_address(1.5, is_at_mention=False) == "high"

    def test_moderate_band(self) -> None:
        assert bd._classify_address(0.4, is_at_mention=False) == "moderate"
        assert bd._classify_address(0.7, is_at_mention=False) == "moderate"
        assert bd._classify_address(0.84, is_at_mention=False) == "moderate"

    def test_low_band(self) -> None:
        assert bd._classify_address(0.0, is_at_mention=False) == "low"
        assert bd._classify_address(0.39, is_at_mention=False) == "low"


# ── Domain pattern integrity ─────────────────────────────────────────────

class TestDomainPattern:
    def test_all_yaml_domains_compiled(self) -> None:
        # Every domain key in the yaml should appear at least once in the
        # keyword→module mapping. Catches typos like "BUILDSCI" vs "Buildsci".
        for module_id in bd._ENGAGEMENT_CFG["domains"]:
            assert module_id.upper() in bd.KEYWORD_TO_MODULE.values(), (
                f"Domain {module_id} from yaml not present in compiled mapping"
            )

    def test_keyword_count_matches_yaml(self) -> None:
        yaml_total = sum(len(terms) for terms in bd._ENGAGEMENT_CFG["domains"].values())
        # Some keywords may be deduped if they appear in multiple domains.
        assert len(bd.KEYWORD_TO_MODULE) <= yaml_total
        assert len(bd.KEYWORD_TO_MODULE) >= yaml_total * 0.9  # allow ≤10% dedup
