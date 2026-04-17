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

    def test_recency_boost_ignores_word_count(self) -> None:
        # word_count gate dropped 2026-04-16: short non-noise follow-ups in
        # an active thread should still receive the recency boost so the
        # bot responds to "fair." or "how?" instead of silently ignoring.
        bd._channel_last_spoke["ch1"] = time.time()
        r1 = bd._score_message("interesting point", "ch1", False, None)
        assert r1.score == pytest.approx(bd._SCORE_RECENCY)
        r2 = bd._score_message("that is an interesting point", "ch1", False, None)
        assert r2.score == pytest.approx(bd._SCORE_RECENCY)

    def test_short_conversational_in_active_thread_clears_floor(self) -> None:
        # Regression pin for the 2026-04-16 tuning: a 2-word question in an
        # active thread should score above haiku_floor (0.4) on its own,
        # without needing name_mention or @mention. This is the whole point
        # of bumping conversational_in_thread to 0.4 and dropping the
        # word_count gate on both recency and conversational.
        bd._channel_last_spoke["ch1"] = time.time()
        result = bd._score_message("how now", "ch1", False, None)
        # "how " is a question starter, recency is active -> 0.3 + 0.4 = 0.7.
        assert result.score >= bd._ENGAGEMENT_CFG["thresholds"]["haiku_floor"]
        assert result.score == pytest.approx(
            bd._SCORE_RECENCY + bd._SCORE_CONVERSATIONAL
        )

    def test_conversational_in_active_thread(self) -> None:
        bd._channel_last_spoke["ch1"] = time.time()
        result = bd._score_message(
            "what do you think about that",
            "ch1", False, None,
        )
        # recency (0.3) + conversational (0.4, bumped from 0.2 on 2026-04-16) = 0.7
        assert result.score == pytest.approx(
            bd._SCORE_RECENCY + bd._SCORE_CONVERSATIONAL
        )

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


# ── Context domain tracking (Phase 3 fix) ────────────────────────────────

class TestContextDomainTracking:
    """Verify that context_domains correctly distinguishes domains matched
    via the recent-channel-context fallback from domains matched in the
    current message. Regression test for the IMAGEGEN false-positive on
    'hey brend; how is it going?' observed 2026-04-12.
    """
    def setup_method(self) -> None:
        bd._channel_last_spoke.clear()

    def test_current_message_match_not_in_context_domains(self) -> None:
        """A domain matched by the current message is NOT flagged as context-only."""
        result = bd._score_message(
            "can you draw me a picture of a dragon",
            "ch1", False, None,
        )
        # "draw" and "picture" are both IMAGEGEN keywords in the current message
        assert "IMAGEGEN" in result.domains
        assert result.context_domains == set()  # none from context fallback

    def test_context_fallback_populates_context_domains(self) -> None:
        """When the current message has no domain match but recent context
        does, the matched domain lands in BOTH domains AND context_domains.
        This is the exact false-positive path: 'hey brend how is it going'
        has no domain keywords; the fallback picks up IMAGEGEN from a prior
        image-gen discussion and tags it — correctly — as context-only.
        """
        recent = [
            {"text": "can you make me a picture of a dragon", "has_keyword": True},
        ]
        result = bd._score_message(
            "hey brend how is it going",
            "ch1", False, recent,
        )
        assert "IMAGEGEN" in result.domains
        assert "IMAGEGEN" in result.context_domains
        # And the score got the smaller context-fallback boost, not the full
        # domain_match boost.
        assert result.score == bd._SCORE_DOMAIN_CTX

    def test_no_match_anywhere_leaves_context_domains_empty(self) -> None:
        """No keywords in message or context → no domain match at all."""
        recent = [
            {"text": "hello world", "has_keyword": True},
        ]
        result = bd._score_message(
            "hey brend",
            "ch1", False, recent,
        )
        assert result.domains == set()
        assert result.context_domains == set()

    def test_current_match_beats_context_match(self) -> None:
        """If the current message matches, the context fallback is skipped
        entirely — domain_scored gate blocks it. context_domains stays empty.
        """
        recent = [
            {"text": "logic argument proof", "has_keyword": True},  # LOGIC
        ]
        result = bd._score_message(
            "can you draw me a cat",  # IMAGEGEN (draw)
            "ch1", False, recent,
        )
        assert "IMAGEGEN" in result.domains
        assert result.context_domains == set()  # context fallback didn't run
