"""Complexity tier classification for agent tasks.

Chapter 5 of Hou organises problems into a hierarchy by the resources
needed to *decide* them:

* **P**       — polynomial-time deterministic. Lookups, arithmetic,
                regex matches, parsing well-formed JSON.
* **NP**      — polynomial-time verifiable. SAT, scheduling, graph
                colouring — hard to solve, easy to check a solution.
* **PSPACE**  — polynomial-space. Two-player game trees, QBF, long-
                horizon planning with state.
* **RE**      — recursively enumerable: the halting problem and its
                cousins. *Undecidable*, but confirming a terminating
                witness is still possible in finite time.
* **NON_RE**  — beyond recursively enumerable (Sigma_2 and up).
                "Does this program terminate on *all* inputs?" — not
                even semi-decidable.

Agents routinely accept tasks across all five tiers without noticing.
A P-tier task should never go to a solver; a non-RE task should never
be accepted at all. This module gives agents a typed classification
and a routing function so the right backend picks up the right work.

The classifier is intentionally small and conservative: it uses
keyword and structural hints, not heuristics that pretend to be more
than they are. Callers are encouraged to override by passing an
explicit ``tier`` when they already know it (e.g. "this is an
engagement-score lookup — P").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Tier(Enum):
    """Complexity tier of a task.

    Ordered from cheapest to most intractable. The numeric value is
    the Hou hierarchy position (1 = P, 5 = beyond RE); use ``>``
    comparisons with caution — tier is a classification, not a metric.
    """

    P = 1
    NP = 2
    PSPACE = 3
    RE = 4
    NON_RE = 5

    @property
    def decidable(self) -> bool:
        """True for tiers P, NP, PSPACE. RE is *semi*-decidable;
        NON_RE is neither."""
        return self in (Tier.P, Tier.NP, Tier.PSPACE)

    @property
    def semi_decidable(self) -> bool:
        return self is not Tier.NON_RE


class Route(Enum):
    """Backend routing decision for a classified task."""

    DIRECT = "direct"         # answer from cached facts / direct compute
    LLM = "llm"                # send to LLM with a verifier
    SOLVER = "solver"          # send to Z3 / SAT backend
    BOUNDED_SEARCH = "bounded_search"  # LLM or search with hard caps
    REJECT = "reject"          # refuse up front


@dataclass(frozen=True)
class Classification:
    """Result of classifying a task."""

    tier: Tier
    route: Route
    rationale: str


# --- keyword hints ----------------------------------------------------------
# Keywords are matched case-insensitively against the task description.
# Order matters only to the extent that ties are broken by first-match
# (most-specific tiers listed first).

_NON_RE_HINTS = (
    "total correctness",
    "terminates on every input",
    "terminates on all inputs",
    "always halts",
    "for all inputs",
    "prove no bug exists",
)

_RE_HINTS = (
    "will this program terminate",
    "does this program halt",
    "halting problem",
    "decide whether any",
    "find any counterexample",  # enumeration with no upper bound
)

_PSPACE_HINTS = (
    "optimal strategy",
    "game tree",
    "winning strategy",
    "multi-step plan",
    "long-horizon plan",
    "qbf",
    "quantified boolean",
    "alpha-beta",
)

_NP_HINTS = (
    "schedule",
    "scheduling",
    "assign",
    "assignment",
    "sat ",
    "sat,",
    "sat.",
    "3sat",
    "satisfiab",
    "constraint",
    "colouring",
    "coloring",
    "travelling salesman",
    "traveling salesman",
    "tsp",
    "knapsack",
    "subset sum",
    "vertex cover",
    "clique",
    "packing",
    "routing problem",
)

_P_HINTS = (
    "look up",
    "lookup",
    "fetch",
    "count ",
    "count of",
    "sum of",
    "average of",
    "parse json",
    "parse yaml",
    "regex match",
    "substring",
    "sort ",
    "sort the",
)


def _contains_any(text: str, hints: tuple[str, ...]) -> Optional[str]:
    low = text.lower()
    for h in hints:
        if h in low:
            return h
    return None


def classify(task: str) -> Classification:
    """Classify a free-form task description into a :class:`Tier` and :class:`Route`.

    The classification is intentionally conservative: when in doubt it
    returns NP with ``Route.BOUNDED_SEARCH``, so the task gets a cap
    rather than being trusted. Callers that know better should build
    a :class:`Classification` directly.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task description must be a non-empty string")

    # Order: most intractable first, so a task that mentions both
    # "halts" and "schedule" is flagged at the higher tier.
    hit = _contains_any(task, _NON_RE_HINTS)
    if hit is not None:
        return Classification(
            tier=Tier.NON_RE,
            route=Route.REJECT,
            rationale=f"non-RE hint: '{hit}' (undecidable; cannot accept)",
        )

    hit = _contains_any(task, _RE_HINTS)
    if hit is not None:
        return Classification(
            tier=Tier.RE,
            route=Route.REJECT,
            rationale=f"RE hint: '{hit}' (undecidable; cannot accept)",
        )

    hit = _contains_any(task, _PSPACE_HINTS)
    if hit is not None:
        return Classification(
            tier=Tier.PSPACE,
            route=Route.BOUNDED_SEARCH,
            rationale=f"PSPACE hint: '{hit}' (must cap depth/time)",
        )

    hit = _contains_any(task, _NP_HINTS)
    if hit is not None:
        return Classification(
            tier=Tier.NP,
            route=Route.SOLVER,
            rationale=f"NP hint: '{hit}' (route to SAT/SMT solver)",
        )

    hit = _contains_any(task, _P_HINTS)
    if hit is not None:
        return Classification(
            tier=Tier.P,
            route=Route.DIRECT,
            rationale=f"P hint: '{hit}' (direct compute)",
        )

    # No hint matched. Default: assume LLM-shaped task (open-ended
    # text generation) and route it through a verifier with a bounded
    # retry budget. Tier NP because "was this answer correct?" is the
    # standard verification mode for LLM output.
    return Classification(
        tier=Tier.NP,
        route=Route.BOUNDED_SEARCH,
        rationale="no tier hint matched; default to bounded LLM with verifier",
    )


# A rough word-count heuristic for "this task is long enough to
# justify a solver even without keyword hits". Not used by classify()
# directly, but exposed for callers that want it.
_LONG_TASK_WORDS = 80


def looks_long_horizon(task: str) -> bool:
    """True if the task has many clauses or words — a soft PSPACE hint.

    Useful as a post-classifier filter: if ``classify`` returned P or
    NP but the task is very long, a caller may want to escalate to
    BOUNDED_SEARCH out of caution.
    """
    words = len(re.findall(r"\w+", task))
    clause_markers = len(re.findall(r"[,;:]|\band\b|\bthen\b", task.lower()))
    return words >= _LONG_TASK_WORDS or clause_markers >= 10


__all__ = [
    "Tier",
    "Route",
    "Classification",
    "classify",
    "looks_long_horizon",
]
