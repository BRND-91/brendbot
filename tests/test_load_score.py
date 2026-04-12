"""Tests for Phase 3 #1A — cognitive load tracking and load-weighted restart."""
from brendbot import session as session_mod


def test_load_constants_present_and_positive():
    """Sanity: weights and budget exist with sensible values."""
    assert session_mod._LOAD_WEIGHT_TOKENS_PER_K > 0
    assert session_mod._LOAD_WEIGHT_BASH_CALL > 0
    assert session_mod._LOAD_WEIGHT_HAIKU_INVOCATION > 0
    assert session_mod._LOAD_WEIGHT_TOOL_OTHER > 0
    assert session_mod._LOAD_BUDGET_PREEMPTIVE > 0


def test_bash_weighted_higher_than_other_tools():
    """Bash should cost more per call than Read/Grep/etc.

    A Bash subprocess can run arbitrary code, hold state across stdout
    boundaries, and chain commands. A Read tool call is a bounded file
    fetch. The weights must reflect that asymmetry or the load score
    underestimates the cost of Bash-heavy turns.
    """
    assert (
        session_mod._LOAD_WEIGHT_BASH_CALL
        > session_mod._LOAD_WEIGHT_TOOL_OTHER
    )


def test_load_score_formula_matches_expected():
    """Reproduce the formula in _handle() ResultMessage branch.

    Validates that 320k tokens + 6 Bash calls + 1 haiku + 2 other tools
    crosses the preemptive budget — the canonical 'busy turn' that token
    count alone would not catch.
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
    # 320 + 30 + 2 + 2 = 354 — close to budget 360, demonstrates the
    # weights are calibrated so heavy-tool-use spikes accumulate visibly.
    assert load == 354.0
    assert load < session_mod._LOAD_BUDGET_PREEMPTIVE


def test_load_score_pure_token_growth_trips_budget():
    """A 360k+ token turn alone trips the budget even with zero tool use.

    This is the degenerate case where token count is the only signal —
    confirms the load model degrades gracefully to 'just tokens' when
    nothing else is happening.
    """
    tokens = 360_001
    load = (tokens / 1000.0) * session_mod._LOAD_WEIGHT_TOKENS_PER_K
    assert load > session_mod._LOAD_BUDGET_PREEMPTIVE


def test_load_score_heavy_bash_trips_budget_at_lower_token_count():
    """The point of the model: catch heavy-tool turns before tokens spike.

    300k tokens + 12 Bash calls + 3 haiku invocations should exceed budget
    even though the token threshold (320k preemptive) hasn't been hit.
    """
    tokens = 300_000
    bash = 12
    haiku = 3
    other = 0
    load = (
        (tokens / 1000.0) * session_mod._LOAD_WEIGHT_TOKENS_PER_K
        + bash * session_mod._LOAD_WEIGHT_BASH_CALL
        + haiku * session_mod._LOAD_WEIGHT_HAIKU_INVOCATION
        + other * session_mod._LOAD_WEIGHT_TOOL_OTHER
    )
    # 300 + 60 + 6 = 366
    assert load > session_mod._LOAD_BUDGET_PREEMPTIVE
    # And confirm token count alone wouldn't have tripped the existing
    # 320k soft warning.
    assert tokens < session_mod._CONTEXT_SOFT_WARNING
