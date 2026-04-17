"""Tests for brendbot.agent_core.verifier.

Coverage targets:

* A ``Check`` accepts both bare-bool and ``(bool, reason)`` predicate
  returns, and failed checks carry their reason.
* A ``Verifier`` runs checks in order, collects failures, and
  short-circuits when ``fail_fast=True``.
* ``generate_and_verify`` returns on first pass, retries on failure,
  raises ``VerificationFailed`` when no attempt passes, and charges a
  step to an ambient ``BudgetedScope``.
"""
from __future__ import annotations

import pytest

from brendbot.agent_core.budgets import Budget, BudgetedScope, BudgetExceeded
from brendbot.agent_core.verifier import (
    Check,
    CheckFailure,
    VerificationFailed,
    Verifier,
    VerifierResult,
    generate_and_verify,
)


# --- Check ------------------------------------------------------------------


def test_check_pass_returns_none() -> None:
    chk = Check[int](name="positive", predicate=lambda x: x > 0)
    assert chk.run(3) is None


def test_check_bool_false_returns_generic_reason() -> None:
    chk = Check[int](name="positive", predicate=lambda x: x > 0)
    failure = chk.run(-1)
    assert failure is not None
    assert failure.name == "positive"
    assert "positive" in failure.reason


def test_check_tuple_return_carries_reason() -> None:
    def predicate(x: int) -> tuple[bool, str]:
        if x > 0:
            return True, ""
        return False, f"expected positive, got {x}"

    chk = Check[int](name="positive", predicate=predicate)
    failure = chk.run(-1)
    assert failure is not None
    assert failure.reason == "expected positive, got -1"


# --- Verifier ---------------------------------------------------------------


def _verifier_two_checks() -> Verifier[int]:
    return Verifier[int](
        [
            Check(name="positive", predicate=lambda x: (x > 0, "not positive")),
            Check(name="even", predicate=lambda x: (x % 2 == 0, "not even")),
        ]
    )


def test_verifier_passes_when_all_checks_pass() -> None:
    v = _verifier_two_checks()
    result = v.verify(4)
    assert result.ok
    assert result.failures == []
    assert bool(result) is True
    assert result.candidate == 4


def test_verifier_collects_all_failures_by_default() -> None:
    v = _verifier_two_checks()
    result = v.verify(-3)  # fails both
    assert not result.ok
    names = [f.name for f in result.failures]
    assert names == ["positive", "even"]


def test_verifier_fail_fast_stops_at_first_failure() -> None:
    v = Verifier[int](
        [
            Check(name="positive", predicate=lambda x: (x > 0, "not positive")),
            Check(name="even", predicate=lambda x: (x % 2 == 0, "not even")),
        ],
        fail_fast=True,
    )
    result = v.verify(-3)
    assert len(result.failures) == 1
    assert result.failures[0].name == "positive"


def test_verifier_add_appends_check() -> None:
    v = Verifier[int]()
    v.add(Check(name="positive", predicate=lambda x: x > 0))
    assert len(v.checks) == 1
    assert v.verify(1).ok
    assert not v.verify(-1).ok


# --- generate_and_verify ----------------------------------------------------


def test_generate_and_verify_returns_first_passing_candidate() -> None:
    v = Verifier[int]([Check(name="positive", predicate=lambda x: x > 0)])
    calls: list[int] = []

    def gen(attempt: int) -> int:
        calls.append(attempt)
        return 5  # always passes

    assert generate_and_verify(gen, v, max_attempts=3) == 5
    assert calls == [0]  # returned after first attempt


def test_generate_and_verify_retries_on_failure() -> None:
    v = Verifier[int]([Check(name="positive", predicate=lambda x: x > 0)])
    sequence = [-1, -1, 7]

    def gen(attempt: int) -> int:
        return sequence[attempt]

    assert generate_and_verify(gen, v, max_attempts=3) == 7


def test_generate_and_verify_raises_when_all_attempts_fail() -> None:
    v = Verifier[int](
        [Check(name="positive", predicate=lambda x: (x > 0, "not positive"))]
    )

    def gen(attempt: int) -> int:
        return -1

    with pytest.raises(VerificationFailed) as exc:
        generate_and_verify(gen, v, max_attempts=2)
    assert exc.value.attempts == 2
    assert exc.value.last_result.failures[0].name == "positive"


def test_generate_and_verify_rejects_zero_attempts() -> None:
    v = Verifier[int]()
    with pytest.raises(ValueError):
        generate_and_verify(lambda _: 1, v, max_attempts=0)


def test_generate_and_verify_charges_step_to_ambient_scope() -> None:
    v = Verifier[int]([Check(name="positive", predicate=lambda x: x > 0)])

    sequence = [-1, -1, 5]

    def gen(attempt: int) -> int:
        return sequence[attempt]

    with BudgetedScope(Budget(step_cap=10)) as scope:
        generate_and_verify(gen, v, max_attempts=3)
        # 3 attempts = 3 steps charged to the scope.
        assert scope.state.steps == 3


def test_generate_and_verify_respects_ambient_budget() -> None:
    # Parent caps to 2 steps, but generator never produces a pass.
    # The budget trips before max_attempts=10 is reached.
    v = Verifier[int](
        [Check(name="positive", predicate=lambda x: (x > 0, "not positive"))]
    )

    def gen(attempt: int) -> int:
        return -1

    with BudgetedScope(Budget(step_cap=2)):
        with pytest.raises(BudgetExceeded):
            generate_and_verify(gen, v, max_attempts=10)


# --- VerifierResult / CheckFailure misc -------------------------------------


def test_verifier_result_truthy_reflects_ok() -> None:
    r_pass = VerifierResult(candidate=1, failures=[])
    r_fail = VerifierResult(
        candidate=1, failures=[CheckFailure(name="x", reason="r")]
    )
    assert bool(r_pass) is True
    assert bool(r_fail) is False


def test_check_failure_is_hashable_and_frozen() -> None:
    # Frozen dataclass guarantee: failures can be put in a set.
    f1 = CheckFailure(name="a", reason="r")
    f2 = CheckFailure(name="a", reason="r")
    assert f1 == f2
    assert {f1, f2} == {f1}
