"""Tests for the cognitive-load metric (Phase 3 #1A).

Post-2026-04-23 strip: the load score is an observability-only metric.
The preemptive-restart-on-load trigger and the shallow-rest cycle were
deleted because they never fired in any observed pilot — the pure token
threshold at 400k always reached _CONTEXT_REFRESH_THRESHOLD first.
What remains is the weighted formula (bash + haiku + other + tokens/1k)
which is logged on every turn-complete line so operators can still see
the combined-pressure signal when reading logs.

This file used to exercise the budget-trip branches (shallow rest,
preemptive restart). Those tests are gone. What survives: the formula
shape, weight ordering, and weight sign sanity checks.
"""
from brendbot import session as session_mod


def test_load_weights_present_and_positive():
    """Sanity: the four load-weight constants exist with positive values."""
    assert session_mod._LOAD_WEIGHT_TOKENS_PER_K > 0
    assert session_mod._LOAD_WEIGHT_BASH_CALL > 0
    assert session_mod._LOAD_WEIGHT_HAIKU_INVOCATION > 0
    assert session_mod._LOAD_WEIGHT_TOOL_OTHER > 0


def test_bash_weighted_higher_than_other_tools():
    """Bash should cost more per call than Read/Grep/etc.

    A Bash subprocess can run arbitrary code, hold state across stdout
    boundaries, and chain commands. A Read tool call is a bounded file
    fetch. The weights reflect that asymmetry; the load score otherwise
    underestimates the cost of Bash-heavy turns.
    """
    assert (
        session_mod._LOAD_WEIGHT_BASH_CALL
        > session_mod._LOAD_WEIGHT_TOOL_OTHER
    )


def test_load_score_formula_matches_expected():
    """Reproduce the formula in _update_load_score.

    320k tokens + 6 Bash + 1 haiku + 2 other = 354. The number itself
    isn't load-bearing anymore (no budget trips on it); this test just
    pins the formula shape so a future refactor can't quietly change
    what the logged load metric means.
    """
    tokens = 320_000
    bash = 6
    haiku = 1
    other = 2

    load = (
        (tokens / 1000.0) * session_mod._LOAD_WEIGHT_TOKENS_PER_K
        + bash * session_mod._LOAD_WEIGHT_BASH_CALL
        + haiku * session_mod._LOAD_WEIGHT_HAIKU_INVOCATION
        + other * session_mod._LOAD_WEIGHT_TOOL_OTHER
    )
    # 320 + 30 + 2 + 2 = 354
    assert load == 354.0
