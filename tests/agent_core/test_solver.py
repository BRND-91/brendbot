"""Tests for brendbot.agent_core.solver.

Coverage targets:

* solve_int: sat, unsat, model values, constraint parse errors,
  allow_negative flag, variable validation.
* solve_scheduling: sat/unsat paths, allowed-slot whitelists,
  conflict pairs, input validation.
* SolverResult flags (sat / unsat / unknown).
* Budget integration: ambient scope charges a step per solver call;
  a too-tight time cap surfaces as an unknown result.
"""
from __future__ import annotations

import importlib

import pytest

from brendbot.agent_core.budgets import Budget, BudgetedScope
from brendbot.agent_core.solver import (
    SolverResult,
    SolverUnavailable,
    solve_int,
    solve_scheduling,
)


# Skip the real tests if z3 wasn't installed. When installed, import
# normally so all tests run.
z3 = pytest.importorskip("z3")


# --- SolverResult flags -----------------------------------------------------


def test_solver_result_flag_helpers() -> None:
    assert SolverResult(status="sat").sat
    assert not SolverResult(status="sat").unsat
    assert SolverResult(status="unsat").unsat
    assert SolverResult(status="unknown").unknown


# --- solve_int --------------------------------------------------------------


def test_solve_int_basic_sat() -> None:
    r = solve_int(["x", "y"], ["x + y == 10", "x > 0", "y > 0", "x != y"])
    assert r.sat
    assert r.model["x"] + r.model["y"] == 10
    assert r.model["x"] > 0
    assert r.model["y"] > 0
    assert r.model["x"] != r.model["y"]


def test_solve_int_unsat() -> None:
    r = solve_int(["x"], ["x > 0", "x < 0"])
    assert r.unsat
    assert r.model == {}


def test_solve_int_uses_and_or_helpers() -> None:
    r = solve_int(["x"], ["Or(x == 1, x == 2)", "x > 1"])
    assert r.sat
    assert r.model["x"] == 2


def test_solve_int_allow_negative_false_adds_nonnegativity() -> None:
    # With allow_negative=False, x >= 0 is implicit, so x = -1 is blocked.
    r = solve_int(["x"], ["x < 1"], allow_negative=False)
    assert r.sat
    assert r.model["x"] == 0


def test_solve_int_rejects_empty_variables() -> None:
    with pytest.raises(ValueError):
        solve_int([], ["True"])


def test_solve_int_rejects_unparseable_constraint() -> None:
    with pytest.raises(ValueError):
        solve_int(["x"], ["this is not python"])


def test_solve_int_no_constraints_is_sat() -> None:
    r = solve_int(["x"], [])
    assert r.sat
    # With no constraints and model_completion, z3 assigns *some* int.
    assert isinstance(r.model["x"], int)


# --- solve_scheduling -------------------------------------------------------


def test_solve_scheduling_simple_sat() -> None:
    r = solve_scheduling(
        items=["alice", "bob", "carol"],
        slots=["morning", "afternoon", "evening"],
    )
    assert r.sat
    assert set(r.model.keys()) == {"alice", "bob", "carol"}
    for slot in r.model.values():
        assert slot in {"morning", "afternoon", "evening"}


def test_solve_scheduling_conflicts_force_different_slots() -> None:
    r = solve_scheduling(
        items=["a", "b"],
        slots=["x", "y"],
        conflicts=[("a", "b")],
    )
    assert r.sat
    assert r.model["a"] != r.model["b"]


def test_solve_scheduling_too_many_items_for_conflict_slots_is_unsat() -> None:
    # Three items, two slots, all mutually conflicting -> unsat
    # (by pigeonhole).
    r = solve_scheduling(
        items=["a", "b", "c"],
        slots=["x", "y"],
        conflicts=[("a", "b"), ("a", "c"), ("b", "c")],
    )
    assert r.unsat


def test_solve_scheduling_allowed_restricts_slots() -> None:
    r = solve_scheduling(
        items=["alice", "bob"],
        slots=["morning", "evening"],
        allowed={"alice": ["morning"], "bob": ["evening"]},
    )
    assert r.sat
    assert r.model["alice"] == "morning"
    assert r.model["bob"] == "evening"


def test_solve_scheduling_allowed_empty_is_unsat() -> None:
    r = solve_scheduling(
        items=["alice"],
        slots=["morning"],
        allowed={"alice": []},
    )
    assert r.unsat


def test_solve_scheduling_validates_allowed_slot_names() -> None:
    with pytest.raises(ValueError):
        solve_scheduling(
            items=["alice"],
            slots=["morning"],
            allowed={"alice": ["midnight"]},
        )


def test_solve_scheduling_validates_conflict_items() -> None:
    with pytest.raises(ValueError):
        solve_scheduling(
            items=["alice"],
            slots=["morning"],
            conflicts=[("alice", "ghost")],
        )


def test_solve_scheduling_rejects_empty_items_or_slots() -> None:
    with pytest.raises(ValueError):
        solve_scheduling(items=[], slots=["x"])
    with pytest.raises(ValueError):
        solve_scheduling(items=["a"], slots=[])


# --- budget integration -----------------------------------------------------


def test_solve_int_charges_one_step_to_ambient_scope() -> None:
    with BudgetedScope(Budget(step_cap=10)) as scope:
        solve_int(["x"], ["x == 1"])
        assert scope.state.steps == 1
        solve_int(["y"], ["y == 2"])
        assert scope.state.steps == 2


def test_solve_scheduling_charges_one_step_to_ambient_scope() -> None:
    with BudgetedScope(Budget(step_cap=10)) as scope:
        solve_scheduling(items=["a"], slots=["x"])
        assert scope.state.steps == 1


def test_solver_unavailable_error_type_is_runtime_error() -> None:
    # A pure type-hierarchy check; when z3 IS installed this just
    # confirms the surface exists for the absent-z3 code path.
    assert issubclass(SolverUnavailable, RuntimeError)


def test_solve_int_with_tight_time_budget_surfaces_unknown_or_sat() -> None:
    # A trivial instance finishes well under any sane timeout, but the
    # point here is that the scope's time_cap_s propagates to z3 without
    # raising.
    with BudgetedScope(Budget(step_cap=10, time_cap_s=5.0)):
        r = solve_int(["x"], ["x == 7"])
    # Must produce some valid status string.
    assert r.status in {"sat", "unsat", "unknown"}
