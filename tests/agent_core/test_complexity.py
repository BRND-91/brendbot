"""Tests for brendbot.agent_core.complexity.

Coverage targets:

* Tier precedence: NON_RE > RE > PSPACE > NP > P > default.
* Each tier's route wiring (REJECT for undecidable, SOLVER for NP,
  DIRECT for P, BOUNDED_SEARCH for PSPACE and defaults).
* Default path when no hint matches.
* Input validation on empty/non-string tasks.
* ``looks_long_horizon`` triggers on long text and on many clauses.
* Tier properties (decidable, semi_decidable).
"""
from __future__ import annotations

import pytest

from brendbot.agent_core.complexity import (
    Classification,
    Route,
    Tier,
    classify,
    looks_long_horizon,
)


# --- tier properties --------------------------------------------------------


def test_tier_ordering_by_value() -> None:
    assert Tier.P.value < Tier.NP.value < Tier.PSPACE.value
    assert Tier.PSPACE.value < Tier.RE.value < Tier.NON_RE.value


def test_decidable_and_semi_decidable_flags() -> None:
    assert Tier.P.decidable
    assert Tier.NP.decidable
    assert Tier.PSPACE.decidable
    assert not Tier.RE.decidable
    assert not Tier.NON_RE.decidable

    assert Tier.P.semi_decidable
    assert Tier.NP.semi_decidable
    assert Tier.PSPACE.semi_decidable
    assert Tier.RE.semi_decidable
    assert not Tier.NON_RE.semi_decidable


# --- classification --------------------------------------------------------


def test_classify_non_re_rejects() -> None:
    c = classify("Prove that this program terminates on all inputs.")
    assert c.tier is Tier.NON_RE
    assert c.route is Route.REJECT


def test_classify_re_rejects() -> None:
    c = classify("Does this program halt on the given input?")
    assert c.tier is Tier.RE
    assert c.route is Route.REJECT


def test_classify_pspace_goes_to_bounded_search() -> None:
    c = classify("Find the optimal strategy for this two-player game.")
    assert c.tier is Tier.PSPACE
    assert c.route is Route.BOUNDED_SEARCH


def test_classify_np_schedule_goes_to_solver() -> None:
    c = classify("Build a schedule for 20 employees across 3 shifts.")
    assert c.tier is Tier.NP
    assert c.route is Route.SOLVER


def test_classify_np_sat_goes_to_solver() -> None:
    c = classify("Is this 3SAT instance satisfiable?")
    assert c.tier is Tier.NP
    assert c.route is Route.SOLVER


def test_classify_p_lookup_goes_direct() -> None:
    c = classify("Look up the engagement score for user 42.")
    assert c.tier is Tier.P
    assert c.route is Route.DIRECT


def test_classify_p_sort_goes_direct() -> None:
    c = classify("Sort the list of message IDs ascending.")
    assert c.tier is Tier.P
    assert c.route is Route.DIRECT


def test_classify_default_is_bounded_np() -> None:
    # No keywords that match any tier — falls through to the conservative
    # default.
    c = classify("Write a friendly one-line reply to this user.")
    assert c.tier is Tier.NP
    assert c.route is Route.BOUNDED_SEARCH
    assert "default" in c.rationale.lower()


# --- precedence ------------------------------------------------------------


def test_non_re_wins_over_np_when_both_keywords_present() -> None:
    # "schedule" would match NP on its own.
    c = classify(
        "Prove for all inputs that our schedule generator terminates."
    )
    assert c.tier is Tier.NON_RE


def test_re_wins_over_np_when_both_keywords_present() -> None:
    c = classify(
        "Does this program halt when you give it a schedule constraint?"
    )
    assert c.tier is Tier.RE


def test_pspace_wins_over_np() -> None:
    c = classify(
        "Find the optimal strategy, even if it has a scheduling component."
    )
    assert c.tier is Tier.PSPACE


def test_np_wins_over_p() -> None:
    c = classify(
        "Look up the user's preferences and build a schedule around them."
    )
    # "schedule" trips NP before "look up" trips P.
    assert c.tier is Tier.NP


# --- validation ------------------------------------------------------------


def test_empty_task_raises() -> None:
    with pytest.raises(ValueError):
        classify("")


def test_whitespace_task_raises() -> None:
    with pytest.raises(ValueError):
        classify("   \n  ")


def test_non_string_task_raises() -> None:
    with pytest.raises(ValueError):
        classify(None)  # type: ignore[arg-type]


# --- looks_long_horizon ----------------------------------------------------


def test_looks_long_horizon_word_threshold() -> None:
    long_task = " ".join(["word"] * 100)
    assert looks_long_horizon(long_task) is True


def test_looks_long_horizon_short_task_false() -> None:
    assert looks_long_horizon("short task") is False


def test_looks_long_horizon_many_clauses_trips() -> None:
    # Short in words but full of clause markers -> still long-horizon.
    task = "a, b, c, d, e, f, g, h, i, j, k"
    assert looks_long_horizon(task) is True


# --- Classification value object -------------------------------------------


def test_classification_is_frozen() -> None:
    c = classify("Sort this list.")
    with pytest.raises(Exception):
        c.tier = Tier.NP  # type: ignore[misc]


def test_classification_carries_rationale() -> None:
    c = classify("Sort this list.")
    assert c.rationale
    assert isinstance(c.rationale, str)
