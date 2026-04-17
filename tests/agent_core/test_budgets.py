"""Tests for brendbot.agent_core.budgets.

Coverage targets:

* Each cap dimension (steps, time, tokens) trips independently.
* ``Budget.tighten`` is pointwise-min and handles ``None`` as "no cap".
* Nested scopes: a child cannot exceed a parent's *remaining* budget.
* ``bounded`` decorator works on sync and async functions and charges
  a step to the parent scope.
* ``current_scope`` returns None outside a ``with`` block and the
  innermost scope inside one.
* ``BudgetedScope.state`` raises outside a ``with`` block (misuse guard).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from brendbot.agent_core.budgets import (
    Budget,
    BudgetedScope,
    BudgetExceeded,
    BudgetState,
    bounded,
    current_scope,
)


# --- Budget.tighten ---------------------------------------------------------


def test_tighten_picks_pointwise_min() -> None:
    a = Budget(step_cap=10, time_cap_s=5.0, token_cap=100)
    b = Budget(step_cap=3, time_cap_s=9.0, token_cap=50)
    t = a.tighten(b)
    assert t == Budget(step_cap=3, time_cap_s=5.0, token_cap=50)


def test_tighten_none_means_no_cap_so_other_side_wins() -> None:
    a = Budget(step_cap=None, time_cap_s=5.0, token_cap=None)
    b = Budget(step_cap=7, time_cap_s=None, token_cap=42)
    t = a.tighten(b)
    assert t == Budget(step_cap=7, time_cap_s=5.0, token_cap=42)


def test_tighten_both_none_stays_none() -> None:
    a = Budget()
    b = Budget()
    assert a.tighten(b) == Budget()


# --- step cap ---------------------------------------------------------------


def test_tick_trips_step_cap() -> None:
    with BudgetedScope(Budget(step_cap=3)) as scope:
        scope.tick()
        scope.tick()
        scope.tick()
        with pytest.raises(BudgetExceeded) as exc:
            scope.tick()
        assert exc.value.dimension == "steps"
        assert exc.value.cap == 3


def test_tick_n_parameter_can_trip_cap_in_one_call() -> None:
    with BudgetedScope(Budget(step_cap=5)) as scope:
        with pytest.raises(BudgetExceeded) as exc:
            scope.tick(10)
        assert exc.value.dimension == "steps"


def test_no_step_cap_never_trips_on_steps() -> None:
    with BudgetedScope(Budget(token_cap=1_000)) as scope:
        for _ in range(10_000):
            scope.tick()
        # No exception — token and time caps untouched.


# --- token cap --------------------------------------------------------------


def test_add_tokens_trips_token_cap() -> None:
    with BudgetedScope(Budget(token_cap=100)) as scope:
        scope.add_tokens(60)
        scope.add_tokens(40)  # exactly at cap is allowed
        with pytest.raises(BudgetExceeded) as exc:
            scope.add_tokens(1)
        assert exc.value.dimension == "tokens"
        assert exc.value.cap == 100


# --- time cap ---------------------------------------------------------------


def test_time_cap_trips_on_tick() -> None:
    with BudgetedScope(Budget(time_cap_s=0.5)) as scope:
        scope.tick()  # no time passed yet
        # Simulate the clock having advanced past the cap by rewinding
        # the start time. time.monotonic() - started_at is what `elapsed_s`
        # returns, so moving started_at backwards is equivalent to moving
        # "now" forwards.
        scope.state.started_at -= 1.0
        with pytest.raises(BudgetExceeded) as exc:
            scope.tick()
        assert exc.value.dimension == "time_s"


def test_time_cap_trips_on_add_tokens() -> None:
    with BudgetedScope(Budget(time_cap_s=0.1)) as scope:
        scope.state.started_at -= 5.0
        with pytest.raises(BudgetExceeded) as exc:
            scope.add_tokens(1)
        assert exc.value.dimension == "time_s"


# --- nesting ----------------------------------------------------------------


def test_current_scope_outside_and_inside() -> None:
    assert current_scope() is None
    with BudgetedScope(Budget(step_cap=1)) as outer:
        assert current_scope() is outer
        with BudgetedScope(Budget(step_cap=1)) as inner:
            assert current_scope() is inner
        assert current_scope() is outer
    assert current_scope() is None


def test_child_inherits_parents_tighter_cap() -> None:
    # Parent allows 5 steps, child declares 100 — child is tightened to
    # parent's remaining (5).
    with BudgetedScope(Budget(step_cap=5)) as parent:
        with BudgetedScope(Budget(step_cap=100)) as child:
            assert child.state.budget.step_cap == 5
        # Parent untouched.
        assert parent.state.steps == 0


def test_child_sees_remaining_not_original_parent_budget() -> None:
    # Parent has used 3 of 5 steps; a child declaring 100 steps should
    # be tightened to 2.
    with BudgetedScope(Budget(step_cap=5)) as parent:
        parent.tick()
        parent.tick()
        parent.tick()
        with BudgetedScope(Budget(step_cap=100)) as child:
            assert child.state.budget.step_cap == 2


def test_child_cannot_exceed_parent_remaining_steps() -> None:
    with BudgetedScope(Budget(step_cap=3)):
        with BudgetedScope(Budget(step_cap=10)) as child:
            child.tick()
            child.tick()
            child.tick()
            with pytest.raises(BudgetExceeded):
                child.tick()


# --- bounded decorator ------------------------------------------------------


def test_bounded_sync_function_runs_inside_scope() -> None:
    @bounded(Budget(step_cap=2))
    def work() -> int:
        scope = current_scope()
        assert scope is not None
        scope.tick()
        scope.tick()
        return 42

    assert work() == 42


def test_bounded_sync_function_trips_own_cap() -> None:
    @bounded(Budget(step_cap=1))
    def work() -> None:
        scope = current_scope()
        assert scope is not None
        scope.tick()
        scope.tick()  # boom

    with pytest.raises(BudgetExceeded):
        work()


def test_bounded_async_function_runs_inside_scope() -> None:
    @bounded(Budget(step_cap=2))
    async def work() -> int:
        scope = current_scope()
        assert scope is not None
        scope.tick()
        return 7

    assert asyncio.run(work()) == 7


def test_bounded_charges_a_step_to_parent_scope() -> None:
    @bounded(Budget(step_cap=10))
    def child() -> None:
        pass

    with BudgetedScope(Budget(step_cap=5)) as parent:
        child()
        assert parent.state.steps == 1
        child()
        assert parent.state.steps == 2


def test_bounded_charge_step_false_does_not_charge_parent() -> None:
    @bounded(Budget(step_cap=10), charge_step=False)
    def child() -> None:
        pass

    with BudgetedScope(Budget(step_cap=5)) as parent:
        child()
        child()
        assert parent.state.steps == 0


# --- state & snapshot -------------------------------------------------------


def test_state_outside_with_block_raises() -> None:
    scope = BudgetedScope(Budget(step_cap=1))
    with pytest.raises(RuntimeError):
        _ = scope.state


def test_snapshot_fields_present() -> None:
    with BudgetedScope(Budget(step_cap=10, time_cap_s=1.0, token_cap=50)) as scope:
        scope.tick()
        scope.add_tokens(7)
        snap = scope.state.snapshot()
        assert snap["steps"] == 1
        assert snap["tokens"] == 7
        assert snap["step_cap"] == 10
        assert snap["token_cap"] == 50
        assert snap["time_cap_s"] == 1.0
        assert snap["elapsed_s"] >= 0.0


def test_budget_exceeded_carries_dimension_used_cap() -> None:
    err = BudgetExceeded("steps", 11, 10)
    assert err.dimension == "steps"
    assert err.used == 11
    assert err.cap == 10
    assert "steps" in str(err)
