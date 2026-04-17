"""Solver — Z3 SAT/SMT as a first-class tool for NP-hard subproblems.

Chapter 5 of Hou notes that NP-hard problems do not, in general, have
a polynomial-time algorithm — but we have decades of heavily tuned
SMT solvers that handle real-world instances in acceptable time.
Handing an NP-tier subproblem to an LLM to "figure out" is the wrong
backend: the LLM guesses, the solver decides.

This module wraps Z3 in a thin, budget-aware interface. The goal is
not to expose all of Z3 — callers who need the full API can import
``z3`` directly. Instead, this module ships the *boring common cases*
that come up in agent plumbing:

* :func:`solve_int`     — boolean satisfiability / simple-integer
                           constraints expressed as Z3 Python AST.
* :func:`solve_scheduling` — a scheduling helper: assign each item one
                              of a finite set of slots subject to
                              per-item allowed-slot sets and pairwise
                              conflict constraints.
* :class:`SolverResult` — a typed result carrying model + status.

Z3 is an optional dependency. If it is not installed, importing this
module still works; only the functions raise at call time. This keeps
``agent_core`` importable in test environments that have not pulled
in ``z3-solver``.

Budget integration
------------------
All solver calls honour the ambient :class:`BudgetedScope`:

* One step is charged on entry (the solver call counts as a step).
* ``time_cap_s`` from the scope, if set, is translated into a Z3
  per-check timeout. This means a runaway constraint search cannot
  outrun the scope's wall-clock cap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .budgets import BudgetedScope, current_scope

try:
    import z3  # type: ignore[import-not-found]
    _Z3_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in envs without z3
    z3 = None  # type: ignore[assignment]
    _Z3_AVAILABLE = False


class SolverUnavailable(RuntimeError):
    """Raised when a solver call is made without z3-solver installed."""


def _require_z3() -> None:
    if not _Z3_AVAILABLE:
        raise SolverUnavailable(
            "z3-solver is not installed; `pip install z3-solver` to enable "
            "brendbot.agent_core.solver"
        )


@dataclass
class SolverResult:
    """Outcome of a solver call.

    ``status`` is one of ``"sat"``, ``"unsat"``, or ``"unknown"``
    (Z3's three outcomes — ``unknown`` means the solver ran out of
    resources, not that the problem is undecidable).

    ``model`` is a dict mapping variable name -> Python value (int,
    bool, or string). Populated only when ``status == "sat"``.

    ``elapsed_s`` is wall-clock time the solver spent. Useful for
    logging and for telling a retry loop whether to try a different
    formulation next time.
    """

    status: str
    model: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0

    @property
    def sat(self) -> bool:
        return self.status == "sat"

    @property
    def unsat(self) -> bool:
        return self.status == "unsat"

    @property
    def unknown(self) -> bool:
        return self.status == "unknown"


def _z3_value_to_python(value: Any) -> Any:
    """Convert a Z3 model value to a plain Python value where possible."""
    # Z3 exposes IntNumRef / BoolRef / RatNumRef / StringVal; fall back
    # to str() for unsupported sorts (the caller can parse).
    if value is None:
        return None
    if z3.is_int_value(value):
        return value.as_long()
    if z3.is_rational_value(value):
        num = value.numerator_as_long()
        den = value.denominator_as_long()
        return num / den if den != 1 else num
    if z3.is_bool(value):
        # Note: check for True/False constants specifically.
        if z3.is_true(value):
            return True
        if z3.is_false(value):
            return False
    try:
        return value.as_string()
    except Exception:  # pragma: no cover
        return str(value)


def _apply_time_budget(solver: Any) -> None:
    """If an ambient scope has a time_cap_s, translate it to a Z3 timeout."""
    scope: Optional[BudgetedScope] = current_scope()
    if scope is None:
        return
    remaining = scope.state.budget.time_cap_s
    if remaining is None:
        return
    elapsed = scope.state.elapsed_s
    budget_left_s = max(0.0, remaining - elapsed)
    # Z3 wants milliseconds, integer.
    solver.set("timeout", max(1, int(budget_left_s * 1000)))


def solve_int(
    variables: Iterable[str],
    constraints: Iterable[str],
    *,
    allow_negative: bool = True,
) -> SolverResult:
    """Solve a set of integer constraints expressed as Python-ish strings.

    Each variable in ``variables`` becomes a Z3 ``Int``. Each string
    in ``constraints`` is evaluated against a namespace containing
    those variables and Z3's own operators, so the caller writes
    natural-looking expressions:

        solve_int(
            ["x", "y"],
            ["x + y == 10", "x > 0", "y > 0", "x != y"],
        )

    Returns a :class:`SolverResult`. On success, ``result.model`` maps
    each variable name to its integer value.

    ``allow_negative=False`` adds an implicit ``v >= 0`` clause for
    every variable — useful for counts, indices, durations.

    Raises :class:`SolverUnavailable` if z3-solver is not installed.
    """
    _require_z3()
    import time as _time

    parent = current_scope()
    if parent is not None:
        parent.tick()

    var_names = list(variables)
    if not var_names:
        raise ValueError("must declare at least one variable")
    constraint_list = list(constraints)

    z3_vars: dict[str, Any] = {name: z3.Int(name) for name in var_names}
    # Build the eval namespace. We intentionally keep it narrow: the
    # Z3 variables and a handful of constructors. No builtins, no
    # generic eval footguns.
    eval_globals = {"__builtins__": {}}
    eval_locals = {
        **z3_vars,
        "And": z3.And,
        "Or": z3.Or,
        "Not": z3.Not,
        "Implies": z3.Implies,
        "Xor": z3.Xor,
        "If": z3.If,
    }

    solver = z3.Solver()
    _apply_time_budget(solver)
    if not allow_negative:
        for v in z3_vars.values():
            solver.add(v >= 0)
    for src in constraint_list:
        try:
            clause = eval(src, eval_globals, eval_locals)  # noqa: S307
        except Exception as exc:
            raise ValueError(
                f"could not parse constraint {src!r}: {exc}"
            ) from exc
        solver.add(clause)

    t0 = _time.monotonic()
    status_ref = solver.check()
    elapsed = _time.monotonic() - t0

    status = str(status_ref)
    model: dict[str, Any] = {}
    if status == "sat":
        m = solver.model()
        for name, var in z3_vars.items():
            val = m.evaluate(var, model_completion=True)
            model[name] = _z3_value_to_python(val)
    return SolverResult(status=status, model=model, elapsed_s=elapsed)


def solve_scheduling(
    items: Iterable[str],
    slots: Iterable[str],
    *,
    allowed: Optional[Mapping[str, Iterable[str]]] = None,
    conflicts: Iterable[tuple[str, str]] = (),
) -> SolverResult:
    """Assign each item to exactly one slot under constraints.

    ``items``: the things to schedule (e.g., employee names, tasks).
    ``slots``: the slots each item might occupy (e.g., shifts, rooms).
    ``allowed``: optional per-item whitelist of allowed slots. If
        omitted or missing a key, the item may take any slot.
    ``conflicts``: pairs ``(item_a, item_b)`` that must not share a
        slot.

    Returns a :class:`SolverResult`. When ``status == "sat"`` the
    model maps each item to its assigned slot name.

    This is a typical NP-complete problem (reducible to graph
    colouring when every slot pair is constrained); Z3 handles it
    directly via Int variables with bounded range.

    Raises :class:`SolverUnavailable` if z3-solver is not installed.
    """
    _require_z3()
    import time as _time

    parent = current_scope()
    if parent is not None:
        parent.tick()

    item_list = list(items)
    slot_list = list(slots)
    if not item_list:
        raise ValueError("must schedule at least one item")
    if not slot_list:
        raise ValueError("must provide at least one slot")
    slot_index = {name: i for i, name in enumerate(slot_list)}

    solver = z3.Solver()
    _apply_time_budget(solver)

    # One Int per item; value is the slot index.
    item_vars: dict[str, Any] = {
        name: z3.Int(f"item_{name}") for name in item_list
    }
    for name, v in item_vars.items():
        allowed_slots = (
            list(allowed[name]) if allowed is not None and name in allowed
            else slot_list
        )
        # Validate allowed against the declared slot universe.
        for s in allowed_slots:
            if s not in slot_index:
                raise ValueError(
                    f"allowed slot {s!r} for item {name!r} is not "
                    f"in the slot list"
                )
        allowed_indices = [slot_index[s] for s in allowed_slots]
        if not allowed_indices:
            # No slots permitted -> immediate unsat.
            solver.add(z3.BoolVal(False))
            continue
        solver.add(z3.Or([v == i for i in allowed_indices]))

    # Conflicts: no two items sharing a slot.
    for a, b in conflicts:
        if a not in item_vars or b not in item_vars:
            raise ValueError(
                f"conflict pair ({a!r}, {b!r}) references unknown item"
            )
        solver.add(item_vars[a] != item_vars[b])

    t0 = _time.monotonic()
    status_ref = solver.check()
    elapsed = _time.monotonic() - t0

    status = str(status_ref)
    model: dict[str, Any] = {}
    if status == "sat":
        m = solver.model()
        for name, var in item_vars.items():
            idx = _z3_value_to_python(m.evaluate(var, model_completion=True))
            model[name] = slot_list[int(idx)]
    return SolverResult(status=status, model=model, elapsed_s=elapsed)


__all__ = [
    "SolverResult",
    "SolverUnavailable",
    "solve_int",
    "solve_scheduling",
]
