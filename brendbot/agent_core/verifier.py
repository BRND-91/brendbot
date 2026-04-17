"""Verifier — generator/verifier split grounded in NP / Co-NP asymmetry.

Chapter 5 of Hou: a language is in NP iff there is a polynomial-time
verifier that, given an input ``x`` and a short certificate ``w``,
decides whether ``x`` is in the language. Generation is expensive
(exponential in the worst case); verification is cheap (polynomial).

The same asymmetry applies to LLM output. Generating a candidate is
the slow, expensive step. Checking whether the candidate satisfies a
set of cheap constraints is fast. Most reliability bugs in agents come
from skipping the check and trusting the generator.

This module provides a tiny, dependency-free framework for that split:

* :class:`Check`         — a single named predicate over a candidate.
* :class:`VerifierResult` — list of failed checks + convenience bool.
* :class:`Verifier`      — composable bundle of checks; ``verify(x)``.
* :func:`generate_and_verify` — retry loop with a budget: call a
  generator, verify the output, retry on failure up to ``max_attempts``.

Co-NP note
----------
A check that proves a *counterexample exists* (e.g., "this SQL is
unsafe because it drops a table") is a Co-NP verifier: easy to
falsify, hard to universally certify safe. Both modes compose here —
each :class:`Check` just returns True (pass) or False (fail) with an
optional human-readable reason.

The verifier is pure (no I/O, no LLM). Callers wire in LLM-based
checks by wrapping an LLM call in a :class:`Check`'s predicate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Iterable, Optional, TypeVar

from .budgets import BudgetedScope, current_scope

T = TypeVar("T")


@dataclass(frozen=True)
class CheckFailure:
    """Result of a single failed check."""

    name: str
    reason: str


@dataclass
class VerifierResult(Generic[T]):
    """Outcome of running a :class:`Verifier` against a candidate.

    ``ok`` is True iff zero checks failed. ``failures`` is a list of
    :class:`CheckFailure` records — preserved in the order checks were
    declared, so a caller can surface the first failure or all of them.
    ``candidate`` is the value that was checked.
    """

    candidate: T
    failures: list[CheckFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


@dataclass
class Check(Generic[T]):
    """A single named predicate over a candidate.

    ``predicate`` is a callable returning either a bool, or a tuple
    ``(bool, reason)``. A bare ``True`` means pass; a bare ``False``
    means fail with a generic reason. Returning a tuple lets the check
    explain *why* it failed, which is the whole point of a verifier.
    """

    name: str
    predicate: Callable[[T], Any]

    def run(self, candidate: T) -> Optional[CheckFailure]:
        result = self.predicate(candidate)
        # Accept either `bool` or `(bool, reason)`.
        if isinstance(result, tuple):
            ok, reason = result
        else:
            ok, reason = bool(result), ""
        if ok:
            return None
        return CheckFailure(
            name=self.name,
            reason=reason or f"check '{self.name}' returned False",
        )


class Verifier(Generic[T]):
    """A composable bundle of :class:`Check` instances.

    Checks run in declaration order. By default, the verifier runs
    every check and returns all failures (``fail_fast=False``). Set
    ``fail_fast=True`` to stop at the first failing check — useful
    when later checks depend on earlier ones passing.
    """

    def __init__(
        self,
        checks: Iterable[Check[T]] = (),
        *,
        fail_fast: bool = False,
    ) -> None:
        self._checks: list[Check[T]] = list(checks)
        self.fail_fast = fail_fast

    def add(self, check: Check[T]) -> "Verifier[T]":
        self._checks.append(check)
        return self

    @property
    def checks(self) -> tuple[Check[T], ...]:
        return tuple(self._checks)

    def verify(self, candidate: T) -> VerifierResult[T]:
        failures: list[CheckFailure] = []
        for check in self._checks:
            failure = check.run(candidate)
            if failure is not None:
                failures.append(failure)
                if self.fail_fast:
                    break
        return VerifierResult(candidate=candidate, failures=failures)


class VerificationFailed(RuntimeError):
    """Raised by :func:`generate_and_verify` when every attempt fails."""

    def __init__(self, attempts: int, last_result: VerifierResult[Any]) -> None:
        self.attempts = attempts
        self.last_result = last_result
        failure_names = ", ".join(f.name for f in last_result.failures) or "<none>"
        super().__init__(
            f"verification failed after {attempts} attempt(s); "
            f"last failures: {failure_names}"
        )


def generate_and_verify(
    generate: Callable[[int], T],
    verifier: Verifier[T],
    *,
    max_attempts: int = 3,
) -> T:
    """Generate, verify, retry up to ``max_attempts`` times.

    ``generate`` is called with the zero-indexed attempt number so it
    can, e.g., ratchet the prompt or vary temperature. If verification
    passes, the candidate is returned. If every attempt fails,
    :class:`VerificationFailed` is raised carrying the last result.

    If an ambient :class:`BudgetedScope` is active, every attempt
    charges one step to it — so a parent budget naturally caps the
    retry count regardless of ``max_attempts``.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last_result: Optional[VerifierResult[T]] = None
    scope: Optional[BudgetedScope] = current_scope()

    for attempt in range(max_attempts):
        if scope is not None:
            scope.tick()
        candidate = generate(attempt)
        result = verifier.verify(candidate)
        if result.ok:
            return candidate
        last_result = result

    assert last_result is not None  # max_attempts >= 1 ensures this
    raise VerificationFailed(attempts=max_attempts, last_result=last_result)


__all__ = [
    "Check",
    "CheckFailure",
    "Verifier",
    "VerifierResult",
    "VerificationFailed",
    "generate_and_verify",
]
