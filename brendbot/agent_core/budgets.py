"""Budgets — halting-problem defence for agent loops.

The halting problem (Theorem 5.10 of Hóu) says no static analysis can
decide whether an agent loop will terminate. The only sound defence is
a runtime bound: every loop, retry, sub-agent spawn, and tool chain
must carry an explicit step cap, wall-clock cap, and token cap, and
must surface a cancellation hook.

This module provides:

* :class:`Budget`           — immutable record of caps.
* :class:`BudgetState`      — mutable counters tracked within a scope.
* :class:`BudgetedScope`    — context manager that enforces caps and
                               raises :class:`BudgetExceeded` on violation.
* :func:`bounded`           — decorator that wraps a sync or async callable
                               in a BudgetedScope.

Scopes nest. A child scope inherits (and may tighten) the parent's
remaining budget. A child cannot loosen a parent's cap; if it tries,
the tighter value wins. This mirrors the UTM delegation pattern: every
sub-machine runs within a budget that is a prefix of its caller's.

Usage
-----

    from brendbot.agent_core.budgets import Budget, BudgetedScope, BudgetExceeded

    budget = Budget(step_cap=50, time_cap_s=10.0, token_cap=5_000)
    with BudgetedScope(budget) as scope:
        for item in things_to_process:
            scope.tick()              # one step; raises if over
            scope.add_tokens(cost)    # accumulate; raises if over
            ...

Rationale for each cap
----------------------

* ``step_cap`` catches infinite loops that do not consume tokens or
  wall-clock (pure Python spins, busy-waits on cached results).
* ``time_cap_s`` catches loops that consume real time on external I/O
  where step counting is not meaningful (network retries, DB polling).
* ``token_cap`` catches runaway LLM usage specifically, which is the
  dominant cost mode for brendbot.

All three are enforced because any one of them alone has a loophole.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional


class BudgetExceeded(RuntimeError):
    """Raised when any cap in the active scope is exceeded.

    The message identifies *which* cap tripped (steps, time, tokens) so
    the caller can decide whether to retry with a larger budget, fail
    out, or return a partial result.
    """

    def __init__(self, dimension: str, used: float, cap: float) -> None:
        self.dimension = dimension
        self.used = used
        self.cap = cap
        super().__init__(
            f"budget exceeded on {dimension}: used={used}, cap={cap}"
        )


@dataclass(frozen=True)
class Budget:
    """Immutable budget specification.

    A ``None`` value for any field disables that cap. In practice, at
    least one cap should always be set — an unbounded budget defeats
    the purpose of the module.
    """

    step_cap: Optional[int] = None
    time_cap_s: Optional[float] = None
    token_cap: Optional[int] = None

    def tighten(self, other: "Budget") -> "Budget":
        """Return the pointwise-minimum of two budgets.

        Used by nested scopes: a child's effective budget is the
        tightening of its declared budget against the parent's remaining
        budget. A ``None`` means "no cap on this dimension", so the
        other side wins unconditionally.
        """

        def _min(a: Any, b: Any) -> Any:
            if a is None:
                return b
            if b is None:
                return a
            return min(a, b)

        return Budget(
            step_cap=_min(self.step_cap, other.step_cap),
            time_cap_s=_min(self.time_cap_s, other.time_cap_s),
            token_cap=_min(self.token_cap, other.token_cap),
        )


@dataclass
class BudgetState:
    """Mutable counters tracked inside a :class:`BudgetedScope`.

    Not intended to be constructed directly — :class:`BudgetedScope`
    builds one when entered. Exposed for introspection (logging,
    metrics) and for tests.
    """

    budget: Budget
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    tokens: int = 0

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "tokens": self.tokens,
            "elapsed_s": round(self.elapsed_s, 4),
            "step_cap": self.budget.step_cap,
            "token_cap": self.budget.token_cap,
            "time_cap_s": self.budget.time_cap_s,
        }


# Context-var stack so nested `with BudgetedScope(...)` blocks can find
# their parent and tighten against it. A list is used (not a single ref)
# to make nesting explicit in debugging output.
_SCOPE_STACK: ContextVar[tuple["BudgetedScope", ...]] = ContextVar(
    "_agent_core_budget_stack", default=()
)


def current_scope() -> Optional["BudgetedScope"]:
    """Return the innermost active :class:`BudgetedScope`, or None."""
    stack = _SCOPE_STACK.get()
    return stack[-1] if stack else None


class BudgetedScope:
    """Context manager that enforces a :class:`Budget`.

    Enter a scope, call ``tick()`` at every logical step, and call
    ``add_tokens(n)`` every time the LLM consumes tokens. Both methods
    raise :class:`BudgetExceeded` if the corresponding cap is tripped.

    A scope also checks ``time_cap_s`` on every ``tick`` and
    ``add_tokens`` call — there is no separate ``check_time()`` method
    because a pure-time cap with no steps is a busy loop, which is
    itself a bug.

    Scopes nest. On entry, the declared budget is tightened against any
    parent's *remaining* budget (parent cap minus parent usage), so a
    child can never exceed a parent.
    """

    __slots__ = ("_declared", "_state", "_token")

    def __init__(self, budget: Budget) -> None:
        self._declared = budget
        self._state: Optional[BudgetState] = None
        self._token: Any = None  # contextvars.Token

    @property
    def state(self) -> BudgetState:
        if self._state is None:
            raise RuntimeError("BudgetedScope used outside a `with` block")
        return self._state

    def __enter__(self) -> "BudgetedScope":
        parent = current_scope()
        effective = self._declared
        if parent is not None:
            # Pass the parent's *remaining* budget down, not its original
            # budget. A child whose parent has already used most of its
            # steps cannot use more than what remains.
            remaining = parent._remaining_budget()
            effective = effective.tighten(remaining)
        self._state = BudgetState(budget=effective)
        stack = _SCOPE_STACK.get()
        self._token = _SCOPE_STACK.set(stack + (self,))
        return self

    def __exit__(self, *exc_info: Any) -> None:
        _SCOPE_STACK.reset(self._token)
        self._token = None

    def _remaining_budget(self) -> Budget:
        """Return a Budget capping the unused portion of this scope."""
        st = self._state
        if st is None:
            return self._declared
        b = st.budget
        return Budget(
            step_cap=None if b.step_cap is None else max(0, b.step_cap - st.steps),
            time_cap_s=(
                None
                if b.time_cap_s is None
                else max(0.0, b.time_cap_s - st.elapsed_s)
            ),
            token_cap=(
                None if b.token_cap is None else max(0, b.token_cap - st.tokens)
            ),
        )

    # --- enforcement -----------------------------------------------------

    def _check_time(self) -> None:
        cap = self.state.budget.time_cap_s
        if cap is not None and self.state.elapsed_s > cap:
            raise BudgetExceeded("time_s", self.state.elapsed_s, cap)

    def tick(self, n: int = 1) -> None:
        """Advance the step counter by ``n`` (default 1).

        Raises :class:`BudgetExceeded` if ``step_cap`` or ``time_cap_s``
        is tripped. Call this at every loop iteration, every tool call,
        and every sub-agent spawn.
        """
        st = self.state
        st.steps += n
        cap = st.budget.step_cap
        if cap is not None and st.steps > cap:
            raise BudgetExceeded("steps", st.steps, cap)
        self._check_time()

    def add_tokens(self, n: int) -> None:
        """Accumulate ``n`` tokens against the token cap."""
        st = self.state
        st.tokens += n
        cap = st.budget.token_cap
        if cap is not None and st.tokens > cap:
            raise BudgetExceeded("tokens", st.tokens, cap)
        self._check_time()


def bounded(
    budget: Budget,
    *,
    charge_step: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that runs the wrapped callable inside a BudgetedScope.

    ``charge_step=True`` (default) bumps the step counter once on entry,
    so that a bounded function counts as one step in its caller's
    scope. Set to False for cheap inner helpers.

    Works on both sync and async callables.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                parent = current_scope()
                if parent is not None and charge_step:
                    parent.tick()
                with BudgetedScope(budget):
                    return await fn(*args, **kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            parent = current_scope()
            if parent is not None and charge_step:
                parent.tick()
            with BudgetedScope(budget):
                return fn(*args, **kwargs)

        return sync_wrapper

    return decorator


__all__ = [
    "Budget",
    "BudgetedScope",
    "BudgetState",
    "BudgetExceeded",
    "bounded",
    "current_scope",
]
