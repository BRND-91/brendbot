"""Module-level constants for the ``Session`` runtime.

Previously colocated in ``session.py``; extracted in the Stage 7 repo
cleanup. Centralized here so the cognitive-load model, the context
thresholds, and the per-turn budgets can be found, tuned, and
cross-referenced in one place rather than as a scattered block near
the top of a 2000-line module.

``session.py``, ``session_handler.py``, and the test suite all re-import
these names. The test suite specifically reads them via
``brendbot.session`` (e.g. ``session_mod._LOAD_WEIGHT_TOKENS_PER_K``),
so ``session.py`` re-exports every name via the explicit import list
in its preamble.

No behavioural change — values are identical to the pre-extraction
constants. Tune via ``engagement.yaml`` if any of these start
showing up as load-bearing in feedback logs.
"""

from __future__ import annotations

# ── Context-threshold restart triggers ────────────────────────────────────

# Hard restart when input tokens exceed this value. Acts as the
# upper safety bound — if the soft warning below somehow misses,
# this catches it.
_CONTEXT_REFRESH_THRESHOLD = 400_000

# Soft warning threshold — promotes to threshold_hit so the receiver
# loop fires a clean restart at end-of-turn while there's still
# headroom, preempting the next turn's mid-turn ambush.
#
# Lowered 320_000 → 250_000 (PR #27) after the 2026-04-25 production
# log analysis: average context per turn was 173k, but the long-tail
# included a single turn that ballooned from below threshold to
# 2,672,096 tokens with $2.20 of API spend. The fix isn't just to
# restart sooner — that catches the turn-after — but to restart
# closer to the average so any single turn has less room to balloon
# from a "normal" context. At 250k, the next turn starts fresh
# whenever a turn ends at >250k, which is most non-trivial work.
#
# Trade-off: more frequent restarts (each ~$0.03 cold-start cost,
# typically 5-10 extra restarts per pilot) vs fewer multi-dollar
# runaway turns. The runaway-turn cost dominates by an order of
# magnitude in the production data.
_CONTEXT_SOFT_WARNING = 250_000


# ── Cognitive load model (Phase 3 #1A) ────────────────────────────────────
#
# Token count alone underweights high-intensity turns. A turn that runs
# 6 Bash calls + a haiku invocation costs more cognitively than a turn
# that emits 50 tokens of text, even though the token threshold treats
# them similarly. The load score below tracks compounding pressure across
# turns and triggers a preemptive restart when load exceeds budget,
# regardless of where token count sits.
#
# Weights are starting guesses. Calibrate against runtime feedback logs
# once enough data accumulates. Tune via engagement.yaml in a future pass
# if the values prove load-bearing.
_LOAD_WEIGHT_TOKENS_PER_K = 1.0       # 1 unit per 1000 input tokens
_LOAD_WEIGHT_BASH_CALL = 5.0          # each Bash call
_LOAD_WEIGHT_HAIKU_INVOCATION = 2.0   # each haiku gate call
_LOAD_WEIGHT_TOOL_OTHER = 1.0         # Read/Write/Edit/Grep/Glob etc.

# _LOAD_BUDGET_PREEMPTIVE and _LOAD_BUDGET_SHALLOW were deleted in the
# 2026-04-23 strip. The load-based preemptive restart never fired in
# any observed pilot — the pure token threshold at 400k always reached
# _CONTEXT_REFRESH_THRESHOLD first — and the shallow-rest cycle that
# would have fired between SHALLOW (280) and PREEMPTIVE (360) never
# activated either. The weighted load-score formula above is still
# computed and logged on every turn-complete line for observability,
# but it no longer triggers anything. If future operations reveal a
# distinct high-tool-low-token failure mode that warrants a separate
# restart trigger, this is where to reintroduce the budget constants
# — the computed load is already in session._cumulative_load.


# ── Per-turn caps and logging intervals ───────────────────────────────────

# Max turns to retain in the rolling turn log.
_MAX_TURN_LOG = 30

# Hard cap on total tool calls within a single user-message turn.
_TOOL_CALL_BUDGET = 8

# Separate hard cap on Bash calls per turn. Bash dumps full subprocess
# stdout into context — each call can add thousands of tokens. The
# overall _TOOL_CALL_BUDGET of 8 permits 8 Bash calls, but a cascade of
# git/gcloud/cat commands can balloon context by 200-300k tokens in one
# turn before the post-turn threshold check fires.
# Capped lower than _TOOL_CALL_BUDGET so budget is shared with Read/Edit.
_BASH_CALL_BUDGET = 5

# Wall-clock cap per turn (Patch 1a — agent_core.budgets halting-problem
# defence). Catches cases where the model stays under the step budget
# but loops on slow external I/O. Bash has its own per-call OS timeout
# (~2 min) so this is the aggregate whole-turn cap covering cascades of
# network calls, tool retries, and sub-agent spawns. Tripping this is
# logged as a BudgetExceeded with dimension='time_s'.
_TURN_TIME_CAP_S = 120.0

# Write a rolling checkpoint every N completed turns.
_CHECKPOINT_INTERVAL = 5
