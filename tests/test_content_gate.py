"""Tests for brendbot.content_gate — the standalone content-gate primitives.

These tests cover the pure-logic side: classifier response parsing,
outcome routing, bypass token detection, refusal explanation generation.
Integration tests with Session / SessionPool are in test_admin_bypass.py
and test_content_gate_integration.py (if added later).

All tests are synchronous and import-only — no SDK dependency, no session
state, no network. The content_gate module is session-independent by
design so the logic can be tested in isolation.
"""
from __future__ import annotations

import pytest

from brendbot.content_gate import (
    ClassifierResult,
    Outcome,
    decide_outcome,
    detect_admin_bypass,
    format_refusal_explanation,
    parse_classifier_response,
)


# Standard hard-floor set from engagement.yaml. Duplicated here rather than
# imported so tests fail loudly if the yaml list drifts from what the
# gate code expects.
HARD_FLOORS = {
    "minor_sexual",
    "wmd_synth",
    "malware",
    "infra_attack",
    "extremist_recruit",
    "directed_incite",
}

PASS_T = 0.5
FLAG_T = 1.5
REFUSE_T = 1.5


class TestClassifierResultDataclass:
    """Behavior of the ClassifierResult dataclass itself."""

    def test_empty_is_benign(self) -> None:
        r = ClassifierResult()
        assert r.is_benign is True
        assert r.weighted_sum == 0.0
        assert r.hard_floor is None
        assert r.parse_error is False

    def test_with_criteria_not_benign(self) -> None:
        r = ClassifierResult(criteria={"tragedy_new": 0.9})
        assert r.is_benign is False
        assert r.weighted_sum == pytest.approx(0.9)

    def test_with_hard_floor_not_benign(self) -> None:
        r = ClassifierResult(hard_floor="malware")
        assert r.is_benign is False

    def test_weighted_sum_adds(self) -> None:
        r = ClassifierResult(criteria={"a": 0.5, "b": 0.9, "c": 0.2})
        assert r.weighted_sum == pytest.approx(1.6)

    def test_to_dict_serializable(self) -> None:
        import json
        r = ClassifierResult(
            criteria={"tragedy_new": 0.9},
            hard_floor=None,
            reasoning="test",
        )
        d = r.to_dict()
        # Must be json-serializable for audit logging
        json.dumps(d)
        assert d["weighted_sum"] == pytest.approx(0.9)
        assert d["criteria"] == {"tragedy_new": 0.9}
        assert d["parse_error"] is False


class TestParseClassifierResponse:
    """Behavior of the classifier response parser. Covers expected
    well-formed responses, hard-floor matches, and fail-loud on broken
    input."""

    def test_benign_none(self) -> None:
        raw = "TRIGGERED: none\nREASONING: benign request"
        r = parse_classifier_response(raw)
        assert r.is_benign
        assert r.parse_error is False
        assert r.reasoning == "benign request"

    def test_single_criterion(self) -> None:
        raw = "TRIGGERED: tragedy_new=0.9\nREASONING: recent tragedy reference"
        r = parse_classifier_response(raw)
        assert r.criteria == {"tragedy_new": 0.9}
        assert r.hard_floor is None
        assert r.parse_error is False

    def test_multiple_criteria(self) -> None:
        raw = (
            "TRIGGERED: tragedy_new=0.9, person_neutral=0.5, frame_ambiguous=0.8\n"
            "REASONING: stacks three signals"
        )
        r = parse_classifier_response(raw)
        assert r.criteria == {
            "tragedy_new": 0.9,
            "person_neutral": 0.5,
            "frame_ambiguous": 0.8,
        }
        assert r.weighted_sum == pytest.approx(2.2)

    def test_hard_floor(self) -> None:
        raw = "TRIGGERED: hard_floor=malware\nREASONING: asked for ransomware"
        r = parse_classifier_response(raw)
        assert r.hard_floor == "malware"
        assert r.criteria == {}
        assert r.parse_error is False

    def test_case_insensitive_headers(self) -> None:
        raw = "triggered: none\nreasoning: case test"
        r = parse_classifier_response(raw)
        assert r.is_benign
        assert r.parse_error is False

    def test_empty_raw_fails_loud(self) -> None:
        r = parse_classifier_response("")
        assert r.parse_error is True
        assert r.weighted_sum >= REFUSE_T  # must exceed refuse threshold
        assert "_parse_error" in r.criteria

    def test_whitespace_only_fails_loud(self) -> None:
        r = parse_classifier_response("   \n  \t  ")
        assert r.parse_error is True

    def test_no_triggered_line_fails_loud(self) -> None:
        raw = "just some garbage output without the expected format"
        r = parse_classifier_response(raw)
        assert r.parse_error is True

    def test_triggered_present_but_unparseable(self) -> None:
        raw = "TRIGGERED: some random text with no equals signs\nREASONING: x"
        r = parse_classifier_response(raw)
        assert r.parse_error is True
        assert r.weighted_sum >= REFUSE_T

    def test_missing_reasoning_ok(self) -> None:
        raw = "TRIGGERED: tragedy_old=0.2"
        r = parse_classifier_response(raw)
        # Missing reasoning is not a parse error — the classifier just
        # omitted the rationale. Valid criteria still populate.
        assert r.criteria == {"tragedy_old": 0.2}
        assert r.parse_error is False

    def test_malformed_weight(self) -> None:
        raw = "TRIGGERED: tragedy_new=notanumber, person_neutral=0.5\nREASONING: x"
        r = parse_classifier_response(raw)
        # The regex only matches digit-bearing tokens, so 'notanumber'
        # is skipped entirely. person_neutral parses normally.
        assert r.criteria == {"person_neutral": 0.5}

    def test_noise_tokens_filtered(self) -> None:
        # If the classifier emits 'reasoning=0.5' by accident, it must
        # not become a scored criterion.
        raw = "TRIGGERED: reasoning=0.5, triggered=0.9, tragedy_old=0.2\nREASONING: x"
        r = parse_classifier_response(raw)
        assert r.criteria == {"tragedy_old": 0.2}

    def test_long_reasoning_truncated_in_dict(self) -> None:
        long_reason = "x" * 500
        raw = f"TRIGGERED: tragedy_old=0.2\nREASONING: {long_reason}"
        r = parse_classifier_response(raw)
        d = r.to_dict()
        assert len(d["reasoning"]) <= 200


class TestParserDriftHardening:
    """Regression tests for the 2026-04-16 parser hardening. flag_audit
    analysis showed a ~40% parse-error rate in production caused by
    classifier formatting drift. Each of these patterns was observed in
    the raw_classifier_output field. The parser must now tolerate all
    of them without tripping the fail-conservative REFUSE path.
    """

    def test_markdown_bold_keyword(self) -> None:
        """**TRIGGERED:** none — bolded keyword was the most common drift."""
        raw = "**TRIGGERED:** none\n**REASONING:** benign"
        r = parse_classifier_response(raw)
        assert r.is_benign
        assert r.parse_error is False

    def test_markdown_bold_with_criteria(self) -> None:
        raw = "**TRIGGERED:** tragedy_new=0.9\n**REASONING:** recent event"
        r = parse_classifier_response(raw)
        assert r.criteria == {"tragedy_new": 0.9}
        assert r.parse_error is False

    def test_code_fence_wrapped(self) -> None:
        """```\nTRIGGERED: none\nREASONING: benign\n```"""
        raw = "```\nTRIGGERED: none\nREASONING: benign\n```"
        r = parse_classifier_response(raw)
        assert r.is_benign
        assert r.parse_error is False

    def test_code_fence_with_language_tag(self) -> None:
        """```text\nTRIGGERED: tragedy_old=0.2\n```"""
        raw = "```text\nTRIGGERED: tragedy_old=0.2\nREASONING: historical\n```"
        r = parse_classifier_response(raw)
        assert r.criteria == {"tragedy_old": 0.2}
        assert r.parse_error is False

    def test_leading_preamble_tolerated(self) -> None:
        """Here is my classification:\n\nTRIGGERED: ... — preamble prose."""
        raw = (
            "Here is my classification of the request:\n\n"
            "TRIGGERED: frame_fictional=0.3\n"
            "REASONING: fictional villain dialogue"
        )
        r = parse_classifier_response(raw)
        assert r.criteria == {"frame_fictional": 0.3}
        assert r.parse_error is False

    def test_trailing_postamble_tolerated(self) -> None:
        """TRIGGERED: ... REASONING: ...\n\nLet me know if you need anything else."""
        raw = (
            "TRIGGERED: tragedy_mid=0.5\n"
            "REASONING: 1970s political event\n\n"
            "Let me know if you'd like me to reconsider."
        )
        r = parse_classifier_response(raw)
        assert r.criteria == {"tragedy_mid": 0.5}
        assert r.parse_error is False

    def test_italic_keyword_single_asterisk(self) -> None:
        raw = "*TRIGGERED:* none\n*REASONING:* benign"
        r = parse_classifier_response(raw)
        assert r.is_benign
        assert r.parse_error is False

    def test_markdown_hard_floor(self) -> None:
        raw = "**TRIGGERED:** hard_floor=malware\n**REASONING:** ransomware request"
        r = parse_classifier_response(raw)
        assert r.hard_floor == "malware"
        assert r.parse_error is False

    def test_combined_drift_fence_and_bold(self) -> None:
        """Worst-case observed drift: code fence + bold keyword + preamble."""
        raw = (
            "Sure, here's my analysis:\n\n"
            "```\n"
            "**TRIGGERED:** person_neutral=0.5, frame_ambiguous=0.8\n"
            "**REASONING:** depicts named real person in unclear framing\n"
            "```\n"
        )
        r = parse_classifier_response(raw)
        assert r.criteria == {"person_neutral": 0.5, "frame_ambiguous": 0.8}
        assert r.parse_error is False

    def test_multiline_reasoning_captured(self) -> None:
        raw = (
            "TRIGGERED: tragedy_new=0.9\n"
            "REASONING: this is a long reasoning\n"
            "that spans multiple lines"
        )
        r = parse_classifier_response(raw)
        assert r.criteria == {"tragedy_new": 0.9}
        assert r.parse_error is False
        # Reasoning capture should pull at least the first line.
        assert "long reasoning" in r.reasoning


class TestDecideOutcome:
    """Outcome routing logic. Covers every band boundary and hard-floor
    precedence."""

    def _result(self, **criteria) -> ClassifierResult:
        return ClassifierResult(criteria=dict(criteria))

    def test_benign_passes(self) -> None:
        r = ClassifierResult()
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS

    def test_below_pass_threshold_passes(self) -> None:
        r = self._result(tragedy_old=0.2)  # 0.2 ≤ 0.5
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS

    def test_at_pass_threshold_passes(self) -> None:
        r = self._result(person_neutral=0.5)  # exactly 0.5, inclusive
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS

    def test_just_above_pass_passes_post_flag_strip(self) -> None:
        """After the 2026-04-23 FLAG strip, sums in the former flag band
        collapse into PASS. The soul-stripped reroute that FLAG used to
        trigger was producing confident out-of-character output; a plain
        PASS on ambiguous-band messages is less harmful than the reroute."""
        r = self._result(frame_ambiguous=0.8)  # > 0.5 and ≤ 1.5
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS

    def test_at_flag_threshold_passes_post_flag_strip(self) -> None:
        r = self._result(tragedy_mid=0.5, person_targeted=1.0)  # sum = 1.5
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS

    def test_just_above_refuse_refuses(self) -> None:
        r = self._result(tragedy_new=0.9, person_neutral=0.5, frame_ambiguous=0.8)
        # sum = 2.2, > 1.5 refuse_threshold
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.REFUSE

    def test_far_above_refuse_refuses(self) -> None:
        r = self._result(frame_directed=2.0, tragedy_live=1.5)  # sum = 3.5
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.REFUSE

    def test_hard_floor_beats_weighted_sum(self) -> None:
        """Hard-floor match forces FLOOR_HIT even when weighted sum is 0."""
        r = ClassifierResult(hard_floor="malware")
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.FLOOR_HIT

    def test_hard_floor_beats_high_weighted_sum(self) -> None:
        """Hard-floor precedence holds even when weighted sum would refuse."""
        r = ClassifierResult(
            criteria={"frame_directed": 2.0, "tragedy_live": 1.5},
            hard_floor="wmd_synth",
        )
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.FLOOR_HIT

    def test_unknown_hard_floor_name_no_floor_hit(self) -> None:
        """Defensive: if the classifier invents a hard-floor name not in
        the configured set, it does NOT produce FLOOR_HIT. Scored criteria
        still route normally."""
        r = ClassifierResult(hard_floor="some_made_up_floor")
        # No criteria, no recognized floor → PASS
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS

    def test_parse_error_fails_conservative(self) -> None:
        """A parse-error ClassifierResult with synthetic _parse_error=2.0
        must land above refuse_threshold → REFUSE."""
        r = parse_classifier_response("")  # empty input → parse error
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.REFUSE

    def test_integration_nine_eleven_sandwich(self) -> None:
        """Specific case from 2026-04-13 00:30 log. The "9/11 sandwich"
        deli pun should NOT trip tragedy criteria (classifier prompt
        explicitly notes this). If classifier does its job right, this
        parses as benign or frame_fictional only."""
        # Benign case
        r = parse_classifier_response("TRIGGERED: none\nREASONING: deli order number pun")
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS
        # frame_fictional alone also passes (≤ 0.5)
        r2 = parse_classifier_response("TRIGGERED: frame_fictional=0.3\nREASONING: pun framing")
        # 0.3 ≤ 0.5 → PASS
        assert decide_outcome(r2, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS

    def test_integration_historical_satire_passes_post_flag_strip(self) -> None:
        """Historical figure satire (tragedy_mid + person_satire = 0.7) now
        PASSes after the 2026-04-23 FLAG strip. Previously this would
        have rerouted to a soul-stripped model via the FLAG outcome; now
        it just generates normally through the session soul."""
        r = parse_classifier_response(
            "TRIGGERED: tragedy_mid=0.5, person_satire=0.2\nREASONING: 70s-era political satire"
        )
        assert decide_outcome(r, HARD_FLOORS, PASS_T, FLAG_T, REFUSE_T) == Outcome.PASS


class TestDetectAdminBypass:
    """Italic *brend* token detection for admin bypass. The pattern must
    be strict enough to not false-positive on mid-sentence emphasis but
    loose enough to catch intentional invocations at message edges."""

    def test_token_at_start(self) -> None:
        assert detect_admin_bypass("*brend* make me X", "admin") is True

    def test_token_at_end(self) -> None:
        assert detect_admin_bypass("make me X *brend*", "admin") is True

    def test_token_at_end_with_period(self) -> None:
        assert detect_admin_bypass("make me X, *brend*.", "admin") is True

    def test_token_at_end_with_exclamation(self) -> None:
        assert detect_admin_bypass("go go go *brend*!", "admin") is True

    def test_token_at_end_with_question(self) -> None:
        assert detect_admin_bypass("can you do it *brend*?", "admin") is True

    def test_standalone_whole_word(self) -> None:
        # Standalone with trailing punctuation
        assert detect_admin_bypass("*brend*. make it happen", "admin") is True

    def test_mid_sentence_comma_emphasis_does_not_trigger(self) -> None:
        """'not quite, *brend*, try again' is mid-sentence emphasis
        using italic for tone, NOT a bypass invocation. Comma before
        the token attaches it to the sentence clause."""
        assert detect_admin_bypass("not quite, *brend*, try again", "admin") is False

    def test_no_token_no_bypass(self) -> None:
        assert detect_admin_bypass("brend make me X", "admin") is False

    def test_plain_brend_not_italicized_no_bypass(self) -> None:
        assert detect_admin_bypass("hey brend what's up", "admin") is False

    def test_non_admin_tier_never_triggers(self) -> None:
        """Non-admin senders cannot invoke bypass regardless of token."""
        assert detect_admin_bypass("*brend* make me X", "default") is False
        assert detect_admin_bypass("*brend* make me X", "trusted") is False
        assert detect_admin_bypass("*brend* make me X", "") is False

    def test_case_insensitive(self) -> None:
        assert detect_admin_bypass("*BREND* make me X", "admin") is True
        assert detect_admin_bypass("*Brend* make me X", "admin") is True

    def test_empty_text_no_bypass(self) -> None:
        assert detect_admin_bypass("", "admin") is False

    def test_none_text_no_bypass(self) -> None:
        assert detect_admin_bypass(None, "admin") is False  # type: ignore

    def test_any_mode_matches_mid_sentence(self) -> None:
        """The 'any' pattern mode is looser and catches mid-sentence
        emphasis. Available as fallback if edge mode proves too strict."""
        assert detect_admin_bypass(
            "not quite, *brend*, try again", "admin", pattern_mode="any"
        ) is True

    def test_any_mode_still_requires_admin(self) -> None:
        """Even 'any' mode enforces the admin tier check."""
        assert detect_admin_bypass(
            "*brend* make me X", "default", pattern_mode="any"
        ) is False


class TestFormatRefusalExplanation:
    """Plain-language refusal text generation. Must name tripped criteria
    without mechanism jargon."""

    def test_hard_floor_plain_name(self) -> None:
        r = ClassifierResult(hard_floor="malware")
        msg = format_refusal_explanation(r)
        assert "malware" in msg.lower() or "exploit" in msg.lower()
        assert "hard floor" in msg.lower()

    def test_hard_floor_minor_sexual(self) -> None:
        r = ClassifierResult(hard_floor="minor_sexual")
        msg = format_refusal_explanation(r)
        assert "minor" in msg.lower()

    def test_hard_floor_wmd(self) -> None:
        r = ClassifierResult(hard_floor="wmd_synth")
        msg = format_refusal_explanation(r)
        assert "mass destruction" in msg.lower() or "wmd" in msg.lower()

    def test_parse_error_message(self) -> None:
        r = parse_classifier_response("")
        msg = format_refusal_explanation(r)
        assert "classifier" in msg.lower() or "conservative" in msg.lower()

    def test_tragedy_plus_person_stacks(self) -> None:
        r = ClassifierResult(
            criteria={"tragedy_new": 0.9, "person_neutral": 0.5, "frame_ambiguous": 0.8}
        )
        msg = format_refusal_explanation(r)
        assert "tragedy" in msg.lower()
        assert "person" in msg.lower()
        assert "stacks" in msg.lower() or "gate" in msg.lower()

    def test_targeted_person_named(self) -> None:
        r = ClassifierResult(criteria={"person_targeted": 1.5})
        msg = format_refusal_explanation(r)
        assert "targeted" in msg.lower()

    def test_directed_framing_named(self) -> None:
        r = ClassifierResult(criteria={"frame_directed": 2.0})
        msg = format_refusal_explanation(r)
        assert "directed" in msg.lower()

    def test_no_mechanism_jargon_in_hard_floor(self) -> None:
        """The refusal text should not leak internal mechanism terms like
        'classifier', 'outcome', 'threshold', 'weighted_sum' into chat."""
        r = ClassifierResult(hard_floor="malware")
        msg = format_refusal_explanation(r)
        forbidden = ["weighted_sum", "threshold", "outcome"]
        for term in forbidden:
            assert term not in msg.lower(), f"leaked '{term}' in {msg!r}"
