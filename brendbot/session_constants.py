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

# Hard restart when input tokens exceed this value.
_CONTEXT_REFRESH_THRESHOLD = 400_000

# Soft warning threshold — promotes to threshold_hit so the receiver
# loop fires a clean restart at ~320k while there's still headroom,
# preempting the 400k mid-turn ambush.
_CONTEXT_SOFT_WARNING = 320_000


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

# Load budget — tuned so a "normal" 320k-token session sits around budget
# with light tool use, but a session with heavy Bash activity hits budget
# earlier. 320 (from _LOAD_WEIGHT_TOKENS_PER_K × 320k) + ~40 headroom for
# accumulated tool work = 360.
_LOAD_BUDGET_PREEMPTIVE = 360.0

# Shallow rest threshold (Phase 3 #1B). When cumulative load crosses this
# but stays below preemptive, fire a rest cycle that flushes per-turn tool
# counters and injects a brief "rest" system message — without respawning
# the subprocess. Cheaper than a full restart and addresses tool-load
# accumulation without paying the cold-start cost. Note: this does NOT
# reduce input tokens (only a real restart does), it only resets the
# non-token components of the load score.
_LOAD_BUDGET_SHALLOW = 280.0


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
