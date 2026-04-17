"""agent_core — foundations for agent architecture.

Four modules grounded in Chapter 5 of Hóu's *Fundamentals of Logic and
Computation*:

* :mod:`budgets`    — halting-problem defence: step, time, and token caps.
* :mod:`verifier`   — NP/Co-NP asymmetry: cheap generator + fast verifier.
* :mod:`complexity` — task tier classification (P / NP / PSPACE / RE / non-RE)
                       and routing.
* :mod:`solver`     — Z3 SAT/SMT as a first-class tool for NP-hard subproblems.

These modules are pure Python with no brendbot-runtime dependencies
(no discord, no claude_agent_sdk). They are importable on their own and
are intended to be composed into the engagement/session pipeline over
time rather than dropped in all at once.
"""
from __future__ import annotations

__all__ = ["budgets", "verifier", "complexity", "solver"]
