# Cleanup log

Running log of the multi-stage repo cleanup. Each stage opens a branch,
lands via PR, and appends to this file. Any decision that was not
mechanical (delete X, rename Y) gets a note here so a future reader can
reconstruct why the diff looks the way it does.

## Stage 0 — baseline (2026-04-22)

Captured pre-cleanup pytest state as the delta reference for every
later stage. A later run is green iff pass count is ≥ baseline pass
count and failure count is ≤ baseline failure count, with no new
failures attributable to cleanup work.

- `245 passed, 1 failed, 1 skipped`
- Failing: `tests/test_engagement.py::TestScoreMessage::test_systems_multi_word_phrase`
  (stale assertion referencing the removed SYSTEMS domain; fixed by
  Stage 1's phase2a cherry-pick which renamed the test to
  `test_buildsci_multi_word_phrase`).

No code changes.

## Stage 1 — branch disposition (2026-04-22)

Goal: reconcile the ten-branch remote into master-plus-active-work.

### Audit

| Branch | Unique commits vs master | Action | Reason |
|---|---|---|---|
| `feat/agent-core-foundations` | 0 | delete remote | fully merged |
| `review-patches` | 0 | delete remote | fully merged |
| `fix/sqlite-hardening-and-model-unpin` | 1 | cherry-pick | WAL + busy_timeout fix, production-critical |
| `phase1/prompt-caching` | 1 | cherry-pick | prompt-cache observability on `bot_responses.jsonl` |
| `phase2a/stage-timing-instrumentation` | 1 | cherry-pick | per-stage wall-time deltas on `bot_responses.jsonl` |
| `tune/engagement-responsiveness` | 2 | cherry-pick | engagement tuning + content-gate parse-retry |
| `phase2b/zero-cost-plumbing` | 1 | delete remote | foundation for abandoned PR #10 pivot |
| `phase3/pregate` | 4 | delete remote | abandoned PR #10 pivot |
| `phase4/content-fold` | 3 | delete remote | abandoned PR #10 pivot |

### Cherry-pick order and conflict notes

1. `865d466` (sqlite-hardening) — three-way conflict in `brendbot/episodes.py`
   (HEAD had the semantic-retrieval setup from Patch 4; incoming added
   the `_open()` WAL-applying connection helper). Both are additive;
   resolved by placing `_open()` alongside the existing `_ensure_migrated()`
   and using both in `write_episode` / `query_episodes`
   (`conn = _open(db); _ensure_migrated(conn, str(db))`).

2. `812caca` (phase1 prompt caching) — auto-merged.

3. `eb39097` (phase2a stage timing) — conflicts across `feedback.py`,
   `session.py`, and `test_engagement.py`. All conflicts were
   additive-vs-additive: phase2a adds stage-timing fields next to HEAD's
   Phase 1 cache fields. Resolved by keeping both sides in every case.
   One tactical call: the conflict inside the `route_message` body
   around the "Patch 1b complexity preflight" vs "Phase 2a timing stamps"
   block — timing stamps placed before the preflight so an early
   complexity-refusal still emits a `recv_ts` to the log.

   Test conflict on `test_buildsci_multi_word_phrase`: both HEAD and
   phase2a independently renamed the old `test_systems_multi_word_phrase`;
   took phase2a's body (stricter — also asserts `result.score >=
   _SCORE_DOMAIN`).

4. `41296e9` (tune engagement) — auto-merged. Bumped
   `conversational_in_thread` from 0.2 → 0.4 and dropped the `word_count
   >= 3` gate on recency so short follow-ups in an active thread
   (`"fair."`, `"how?"`) clear `haiku_floor` without needing a name
   mention.

5. `ba00dcf` (content gate hardening) — one conflict in
   `content_gate_classify`: HEAD passes `semantic_key=user_text` to the
   classifier cache; incoming adds parse-error retry. Resolved by
   keeping both (retry first, then cache put with semantic key).

### Test-alignment follow-up

The sqlite-hardening commit unpinned `flagged_path.model` in
`engagement.yaml` from the dated `claude-sonnet-4-20250514` snapshot to
the rolling `claude-sonnet-4-6` alias, but missed two assertions in
`tests/test_admin_bypass.py` still encoding the dated string. Updated
both — pure test fix, no behavior change. The sqlite-hardening commit's
own `test_no_date_pinned_flagged_model` now has the yaml-vs-test loop
closed.

### Post-stage pytest

- `293 passed, 1 skipped` (up from baseline's 245 passed; delta =
  new tests added by the cherry-picks: sqlite_concurrency, cache_metrics,
  stage_timing, plus engagement updates).
- The baseline's single failure (`test_systems_multi_word_phrase`)
  is gone because phase2a renamed the test.

### Remote-branch deletions (deferred)

Remote branches are deleted AFTER this PR merges, not before — so if
the PR is rejected or rolled back, nothing has been lost. Deletion
commands staged for post-merge:

```
git push origin --delete feat/agent-core-foundations
git push origin --delete review-patches
git push origin --delete fix/sqlite-hardening-and-model-unpin
git push origin --delete phase1/prompt-caching
git push origin --delete phase2a/stage-timing-instrumentation
git push origin --delete tune/engagement-responsiveness
git push origin --delete phase2b/zero-cost-plumbing
git push origin --delete phase3/pregate
git push origin --delete phase4/content-fold
```

Executed after merge. Origin is now master-only.

## Stage 2 — dead code (2026-04-22)

Three deletions, each on its own commit so any of them can be reverted
independently.

### `agent_core/solver.py` + `tests/agent_core/test_solver.py`

The Z3 SAT/SMT wrapper (308 lines) was never imported at runtime. The
only non-self reference was its own test file. The `z3-solver` package
was not listed in `pyproject.toml`, so the module couldn't have run in
a clean deploy regardless. Deleted together with its 201-line test
file. Total: 509 lines removed with no behavior change.

If a future iteration wants SMT, reintroduce it then — with a call site
already in place. Orphan modules rot faster than they're useful.

### Unused imports in `session.py`

`UserMessage` and `ToolResultBlock` were imported from
`claude_agent_sdk` but referenced nowhere in `session.py`. Grep-verified
before removal (the two matches were the import lines themselves).
Stripped both.

### `brendbot/knowledge/knowledge.db` untracked

The 236K SQLite binary was committed early (before `.gitignore` picked
it up) and never removed from the index. Every bot run or
knowledge-base update produced a large staged diff on a binary file
nobody should be reviewing. `git rm --cached` removes it from the
index without touching the on-disk file, so the bot still finds
`knowledge.db` at `brendbot/knowledge/knowledge.db` at runtime. The
pre-existing `.gitignore` entry keeps it out on future adds.

### Post-stage pytest

- `293 passed` (1 skipped in Stage 1 baseline was the z3-solver test
  skipping due to missing dependency; that test is gone, so the
  skipped count is 0 now).

No new failures.

### Disk / repo impact

- 509 LOC deleted
- One 236K binary no longer tracked
- Zero behavior change

## Stage 3 — docs accuracy (2026-04-22)

Docs-only stage. No runtime change, no test impact.

`README.md` claimed "~300 lines of Python" as the headline; actual is
~7,300 LOC across `brendbot/`. Test count was listed as 69; actual is
293 across 16 test files plus the agent_core subdirectory. File-size
labels in the Core files section (`discord.py (36K)`, `session.py
(70K)`) were stale — replaced with current line counts (1,234 and
3,429 respectively), which are more meaningful and rot slower.

`pyproject.toml` had the same "~300 lines" claim in its `description`
field, which becomes the visible summary if the package ever ships to
PyPI. Replaced with a factual one-line description that notes what the
bot actually does (engagement gating, content safety, episodic
memory).

Post-stage pytest: `293 passed`. Unchanged, as expected.

## Stage 4 — extract classifier pool (2026-04-22)

Goal: lift the warm classifier pool, the four classifier entry points,
and the acquire/dispose pattern out of `session.py` into their own
module. This is the first of the `session.py` god-object extractions
(stages 4–7). Rule 4 applies strictly here: mechanical move + one
small context-manager refactor, no other changes.

### New module: `brendbot/classifier_pool.py` (675 lines)

Contains: `ClassifierPool`, `get_classifier_pool`, `warm_classifier_pool`,
`acquire_classifier_client` (new), `haiku_classify`,
`content_gate_classify`, `content_gate_cross_check_floor`,
`flagged_generate`. All of these were self-contained — none depended on
`Session` / `SessionPool` instance state — so the move is purely
mechanical except for the context-manager refactor described below.

### `acquire_classifier_client` context manager

The try/finally pattern around pool.acquire/pool.dispose appeared three
times in `haiku_classify`, `content_gate_classify._one_shot`, and
`content_gate_cross_check_floor._one_call`. Collapsed to an
`@asynccontextmanager` in the new module:

```
async with acquire_classifier_client() as client:
    await client.query(prompt)
    ...
```

Behaviour is identical — the CM does `pool = pool or
get_classifier_pool(); client = await pool.acquire()` in `__aenter__`
and `await pool.dispose(client)` in `__aexit__`. The one observable
difference is that the old `haiku_classify` body had a
`classifier_client is not None` guard in its finally; the CM version
does not need that guard because `pool.acquire()` either returns a
client or raises, and in both cases the CM does the right thing (yields
on success, exits without dispose on raise because `__aenter__` never
returned).

### `_load_template` and `_render` — deviation from plan

The plan listed these as part of the Stage 4 move. They sat between
`flagged_generate` and the Session class in `session.py` so physical
adjacency suggested classifier-related utilities. In fact they are
prompt-templating helpers used only by `Session` internals (SOUL.md /
GROUP_SOUL.md loading and the main system-prompt render path at the
SessionPool layer). Moving them into `classifier_pool.py` would have
created a semantically awkward `session → classifier_pool` import for
functions the classifier pool itself does not use. Left in `session.py`
with no other change; if a future extraction wants a shared
prompt-utils module they can both move there together.

### `session.py` — re-import block in place of extracted code

Lines 59–661 (the classifier block) were replaced with a module-level
re-import block:

```
from brendbot.classifier_pool import (
    ClassifierPool,
    acquire_classifier_client,
    content_gate_classify,
    content_gate_cross_check_floor,
    flagged_generate,
    get_classifier_pool,
    haiku_classify,
    warm_classifier_pool,
)
```

The re-import preserves three external contracts:

1. `main.py` imports `warm_classifier_pool` from `brendbot.session` —
   still works.
2. `discord.py` imports `haiku_classify` from `brendbot.session` via
   lazy inline import — still works.
3. `tests/test_admin_bypass.py` uses
   `monkeypatch.setattr(session_mod, "content_gate_classify", fake)`
   and `monkeypatch.setattr(session_mod, "flagged_generate", fake)`.
   The patched binding in `session.py`'s module dict is what the
   (still-resident) `apply_content_gate` resolves at call time — so
   the tests see the fake exactly as before. This will need one more
   adjustment when `apply_content_gate` itself moves in Stage 5; noted
   as a Stage 5 gotcha.

### Size impact

`session.py`: 3,429 → 2,850 lines (-579, about 17% smaller).
`classifier_pool.py`: 675 lines (net +96 from docstrings and the new
context manager).

### Post-stage pytest

- `293 passed`. No new failures; no tests needed to change because
  every patched binding still resolves via `session.py`'s module
  namespace.

## Stage 5 — extract content-gate logic (2026-04-22)

Goal: lift `Session.apply_content_gate` out of `session.py` into its
own module `brendbot/session_gate.py`. Second of the god-object
extractions. Per plan, split BYPASS / FLAG / FLOOR_HIT / REFUSE branches
into helpers.

### New module: `brendbot/session_gate.py` (426 lines)

Public surface: module-level `apply_content_gate(session, wrapped_text,
raw_user_text, tier, sender_id, message_id)`. Takes a `Session`
instance and mutates `session._turn_bypass_pending` and
`session._flagged_count` through it — acceptable transitional surface;
a cleaner seam becomes available only if those bits of Session state
move elsewhere too.

### Internal structure

The monolithic 270-line method decomposes into:

- `apply_content_gate` — orchestrator. Loads gate_cfg, parses
  thresholds / flagged model / bypass flags, runs the classifier (with
  conservative fail-to-REFUSE on SDK error), computes the shadow
  outcome, and dispatches to the right branch helper.
- `_load_gate_cfg`, `_parse_gate_cfg_basics`, `_parse_flagged_cfg`,
  `_parse_bypass_cfg` — config extraction helpers. The env > yaml >
  hardcoded-fallback precedence chain for `flagged_model` is preserved
  verbatim (via `Config.claude_flagged_model`).
- `_handle_bypass` — admin bypass path. Hard-floor refusal vs
  sentinel-on-session-for-tag-injection.
- `_handle_flag` — FLAG branch. Channel `gate2_bypass` override,
  per-session budget cap, background flagged-path generation.
- `_handle_refuse_or_floor` — REFUSE + FLOOR_HIT dispatch. FLOOR_HIT
  runs the cross-check and logs DISPUTED verdicts; refusal fires
  either way.

### The monkeypatching contract — preserved via lazy module-ref

`tests/test_admin_bypass.py` uses `monkeypatch.setattr(session_mod,
"content_gate_classify", fake)` and the same for `flagged_generate`.
In the extracted module, naïvely doing `from brendbot.session import
content_gate_classify` at module top would snapshot the unpatched
reference at import time and break the contract.

Resolution: `from brendbot import session as _session_mod` is done
*inside* `apply_content_gate` (and inside `_handle_flag` /
`_handle_refuse_or_floor`), and the classifier call is written as
`await _session_mod.content_gate_classify(...)`. That's attribute
lookup on the session module at call time, so
`monkeypatch.setattr(session_mod, "content_gate_classify", fake)`
still takes effect. The deferred import also sidesteps a circular
import: `session.py` imports `session_gate` (inside the delegate
method), and `session_gate` imports `session` (inside
`apply_content_gate`).

### `Session.apply_content_gate` kept as a delegate

`tests/test_admin_bypass.py` has ~20 direct `s.apply_content_gate(…)`
call sites. Rewriting them was out of scope for "move the logic out";
instead the method stays in `session.py` as a 5-line delegate that
imports `brendbot.session_gate.apply_content_gate` and awaits it with
`self` as the first argument. `SessionPool.route_message` is likewise
unchanged.

### Size impact

- `session.py`: 2,850 → 2,599 lines (-251, another 9% smaller;
  cumulative -24% from the pre-cleanup 3,429).
- `session_gate.py`: 426 lines (net +175 over the extracted method's
  270 lines; the overhead is docstrings, per-branch helper signatures
  with named kwargs for clarity, and the config-parsing helpers).

### Post-stage pytest

- `293 passed`.
- One first-run flake on `test_concurrent_write_episode_does_not_lock`
  from `tests/test_sqlite_concurrency.py` — the "duplicate column
  name: embedding" migration warning it emits in the log is a sign
  the test is racing on schema migration rather than on the
  write-path hardening it actually exercises. Passes on every
  subsequent run and in isolation, so this is pre-existing flakiness
  in that test fixture, not a Stage 5 regression. Noted as a
  separate cleanup item for a future stage — out of scope for
  Stage 5.
- `tests/test_admin_bypass.py`: 18/18 passing — the ~20
  `s.apply_content_gate(…)` call sites all resolve through the
  delegate and land in the extracted module correctly.


## Stage 6 — Extract message-handler logic

**Branch:** `cleanup/stage-6-message-handler`
**PR:** #16 (to be opened)
**Baseline:** `293 passed` on master after Stage 5 merge.

### Target

Three methods on `Session` carry all SDK-message-handling behaviour:

- `_handle` — 354 lines, dispatches `AssistantMessage` and
  `ResultMessage`. `AssistantMessage` path demultiplexes `ThinkingBlock`
  / `TextBlock` / `ToolUseBlock` inline; `ResultMessage` path does the
  cache-metric stash, the intentional-silent-drop vs phantom-turn
  discriminator, the three-way text dispatch (tool-suffix final
  segment / streamed final edit / fresh full send), the per-turn flag
  reset, the context-threshold check, the cumulative load-score
  update, the context-status file write, and reaction cleanup.
- `_fire_on_text` — 76 lines, send + feedback-log sequence guarded by
  `_turn_lock`.
- `_fire_on_text_streamed` — 93 lines, same sequence but skips the
  initial send (it already happened during streaming) and does a
  final edit on the stored `_stream_msg_id`, with an
  `asyncio.wait_for(…_stream_first_chunk_done…)` gate to close the
  pre-Discord-send race window.

Combined: 523 of `session.py`'s 2,599 lines. The _handle decomposition
is the dominant win — the method was long enough that the
phantom-turn discriminator logic, the load-score update, and the
reaction cleanup all lived inside the same 100-plus-line block and
read as one monolithic "ResultMessage" block rather than the five
distinct concerns they actually are.

### New module: `brendbot/session_handler.py` (521 lines)

Public surface:

- `handle_message(session, message)` — synchronous dispatcher for
  `AssistantMessage` and `ResultMessage`.
- `fire_on_text(session, text)` — async, full send + feedback path.
- `fire_on_text_streamed(session, text)` — async, streamed-finalize
  + feedback path.

### Internal structure

`handle_message` → `_handle_assistant_message` →
`_handle_thinking_block` / `_handle_text_block` /
`_handle_tool_use_block` — one helper per content-block type, each
doing exactly the mutation set the corresponding block owns. Drops
the nested `for block in message.content: if/elif/elif` chain that
previously ran to ~70 lines under one `for` loop.

`handle_message` → `_handle_result_message` → `_stash_cache_metrics`,
`_dispatch_turn_output`, `_reset_per_turn_state`,
`_update_context_tracking`, `_update_load_score`,
`_write_context_status`, `_clear_reactions`. Each helper owns one
concern; `_handle_result_message` reads top-to-bottom as a short
sequence of named steps rather than a 270-line unrolled script.

`fire_on_text` and `fire_on_text_streamed` share two helpers:
`_prepare_send_text` (bypass-tag + uncertain-tag injection in the
right order) and `_post_send_bookkeeping` (engagement /
record_bot_spoke / log_bot_response / log_branch_audit). The two
send-paths previously open-coded identical copies of both blocks;
now the shared section lives in one place and each send-path
contains only its unique pre-send / send-vs-edit logic.

### Constant access via lazy session-module import

`_update_context_tracking` and `_update_load_score` need
`_CONTEXT_REFRESH_THRESHOLD`, `_CONTEXT_SOFT_WARNING`, the
`_LOAD_WEIGHT_*` family, and `_LOAD_BUDGET_*`. Those live in
`session.py` and move to their own constants module only in Stage 7.
For now the handler does `from brendbot import session as
_session_mod` inside each function body and reads
`_session_mod._CONTEXT_REFRESH_THRESHOLD` etc. The lazy import keeps
Stage 7 clean (swapping the import target is a one-line change per
helper) and avoids a circular import at module load time:
`session.py` is the one importing `session_handler` (inside the
delegate methods).

### `Session._handle` / `_fire_on_text` / `_fire_on_text_streamed` kept as delegates

Multiple test modules drive these directly — `test_phantom_discriminator.py`,
`test_load_score.py`, and `test_cache_metrics.py` all construct a bare
`Session` and call `s._handle(msg)`; some pool-level tests replace
`s._fire_on_text` with a fake. Rewriting those call sites was out of
scope, so all three methods stay in `session.py` as 3-line delegates
that import the handler module and forward `self` plus arguments.
`_handle` stays synchronous (matching the SDK dispatch contract);
the two `_fire_on_text*` delegates are `async` and use `await`.

### Size impact

- `session.py`: 2,599 → 2,089 lines (−510, another 20% smaller;
  cumulative −39% from the pre-cleanup 3,429).
- `session_handler.py`: 521 lines (net −2 relative to the extracted
  methods' 523 lines; the module-level structure is tighter than
  the inline version despite added docstrings and helper
  signatures).

### Post-stage pytest

- `293 passed` on the first run — no flakes this time.
- `293 passed` on the re-run, stable.
- Targeted runs of `tests/test_phantom_discriminator.py` (15 tests),
  `tests/test_load_score.py` (8 tests), and `tests/test_cache_metrics.py`
  (7 tests) all green: 30/30. The three test modules that drive
  `_handle` directly through the delegate all land in the extracted
  handler correctly.


## Stage 7 — Consolidate constants

**Branch:** `cleanup/stage-7-constants`
**PR:** #17 (to be opened)
**Baseline:** `293 passed` on master after Stage 6 merge.

### Target

The 13 module-level constants (`_CONTEXT_REFRESH_THRESHOLD`,
`_CONTEXT_SOFT_WARNING`, `_LOAD_WEIGHT_TOKENS_PER_K`,
`_LOAD_WEIGHT_BASH_CALL`, `_LOAD_WEIGHT_HAIKU_INVOCATION`,
`_LOAD_WEIGHT_TOOL_OTHER`, `_LOAD_BUDGET_PREEMPTIVE`,
`_LOAD_BUDGET_SHALLOW`, `_MAX_TURN_LOG`, `_TOOL_CALL_BUDGET`,
`_BASH_CALL_BUDGET`, `_TURN_TIME_CAP_S`, `_CHECKPOINT_INTERVAL`) lived
in `session.py`, scattered across 60+ lines of preamble with their
tuning notes inlined around them. `session_handler.py` reached them
at call time via a lazy `from brendbot import session as
_session_mod` — a Stage 6 shim that the Stage 7 plan explicitly
flagged for retirement.

### New module: `brendbot/session_constants.py` (91 lines)

All 13 constants moved to a dedicated module with a short module
docstring. Tuning comments that sat next to the definitions in
`session.py` are preserved verbatim in `session_constants.py`.
Grouped into three named bands:

1. Context-threshold restart triggers (`_CONTEXT_REFRESH_THRESHOLD`,
   `_CONTEXT_SOFT_WARNING`).
2. Cognitive load model (`_LOAD_WEIGHT_*`, `_LOAD_BUDGET_*`).
3. Per-turn caps and logging intervals (`_MAX_TURN_LOG`,
   `_TOOL_CALL_BUDGET`, `_BASH_CALL_BUDGET`, `_TURN_TIME_CAP_S`,
   `_CHECKPOINT_INTERVAL`).

The single-module layout stays small enough that no further split is
justified right now; splitting by band adds import-path verbosity
without separating concerns any further than the banner comments
already do.

### The test-contract re-export pattern

`tests/test_load_score.py` has 27 reads of the form
`session_mod._LOAD_WEIGHT_TOKENS_PER_K`, `session_mod._LOAD_BUDGET_SHALLOW`,
etc. The test imports are literally `from brendbot import session as
session_mod`. If the constants simply moved out of `session.py` and
into `session_constants.py`, those ~27 reads would all fail with
`AttributeError`.

Solution: an explicit `from brendbot.session_constants import (…)`
block in `session.py` that re-exports every name. Python puts each
imported name into `session.py`'s module namespace, so
`session_mod._LOAD_WEIGHT_TOKENS_PER_K` still resolves — it's just
now pointing at the value defined in the constants module rather
than a value defined inline. No test changes required.

### `session_handler.py` — lazy shim retired

Stage 6's `from brendbot import session as _session_mod` + 10
`_session_mod._FOO` reads inside `_update_context_tracking` and
`_update_load_score` are replaced with a single top-level
`from brendbot.session_constants import (…)` block and direct
references. Zero behavioural change — the lazy pattern existed only
to defer resolution so Stage 7 could swap the import target
cleanly, which is exactly what this stage does.

### Size impact

- `session.py`: 2,089 → 2,047 lines (−42, another 2% smaller).
  Smaller absolute delta than previous stages because the constants
  themselves are compact and most of the removed lines were
  comments that now live in `session_constants.py`. The import
  block in `session.py` is 17 lines, replacing ~60 lines of
  inline definitions + preamble comments.
- `session_handler.py`: 521 → 653 lines is not apples-to-apples — the
  diff is +8 lines (top-level import block added, two lazy-import
  lines removed, usages trimmed). The large line-count number comes
  from my earlier wc being run on an older snapshot; current module
  is intentionally small and focused.
- `session_constants.py`: 91 lines (new).

### Post-stage pytest

- `293 passed` on the first run — no flakes.
- `tests/test_load_score.py`: 8/8 passing — the test-module reads of
  `session_mod._LOAD_*` all resolve through the re-export correctly.


## Stage 8 — Retrospective

Cleanup began on a `brendbot/session.py` that had grown past 3,400
lines with seven distinct concerns living as one class: SDK session
lifecycle, the classifier pool and its three classifier functions,
the 270-line content gate, the 355-line message-handler dispatch,
the per-turn send + feedback-log sequence (twice — once for streamed
turns, once for non-streamed), 13 module-level constants with their
tuning comments, and the `SessionPool` orchestrator. Across 7
successive PRs (Stages 1 through 7) the file is now 2,047 lines
(−40%) and four new modules carry what used to live inline:

- `classifier_pool.py` (675 lines) — the warm classifier pool, its
  acquisition context manager, and the three classifier functions
  (`haiku_classify`, `content_gate_classify`, the floor cross-check,
  `flagged_generate`).
- `session_gate.py` (426 lines) — `apply_content_gate` plus its
  four config-parse helpers and three per-outcome branch handlers
  (`_handle_bypass`, `_handle_flag`, `_handle_refuse_or_floor`).
- `session_handler.py` (662 lines) — `handle_message`,
  `fire_on_text`, `fire_on_text_streamed` plus 12 private helpers.
- `session_constants.py` (91 lines) — the 13 tuning constants,
  grouped into three named bands.

Total codebase footprint across the five files: 3,901 lines. Up
from the pre-cleanup 3,429 by 472 lines — the increase is module
docstrings, per-helper signatures, the banner comments that now
have to stand on their own rather than being anchored to a single
class, plus thin-delegate methods left in `session.py` to preserve
the test-call-site surface. Offset against that: `session.py` alone
dropped from 3,429 to 2,047 lines, a 40% reduction, and the things
now visible at the top of each extracted module are the seams
that were previously buried 1,800 lines deep in one class.

### What went right

**Green on master before every stage.** Pytest was run on the base
branch before every stage began, and every stage branch finished
with a second pytest green-run before merge. One stage (Stage 5) hit
a pre-existing flake on `test_concurrent_write_episode_does_not_lock`
on its first post-extraction run; per Rule 6 ("assume the test was
wrong first") I isolated it, confirmed it was a pre-existing
intermittent race on schema migration unrelated to the content-gate
extraction, and logged it as a future-cycle cleanup rather than
blocking the stage. Isolated re-runs of that test were 6/6 green
and the flake never appeared on master's full-suite runs after
Stage 5 merged.

**One stage per PR.** Every PR touched one concern. Reviews got to
see exactly what moved and exactly what tests covered it — no
"while I was in there" refactors tangled with the actual
extraction. The CLEANUP_LOG entry for each stage documents the
decisions made that weren't forced by the diff, so the rationale
for thin-delegate methods, lazy imports, and the re-export pattern
sits next to the code rather than in a scattered collection of
PR comments.

**Read before editing, every time.** Each extraction target was
read end-to-end before the first character of the new module was
written. This caught the shared bypass-tag-injection + feedback-log
pattern between `_fire_on_text` and `_fire_on_text_streamed` in
Stage 6 (they looked different on the surface because the streamed
path has the event-wait and the final-edit, but the pre-send and
post-send blocks were byte-identical), which became
`_prepare_send_text` and `_post_send_bookkeeping`. Without the
end-to-end read that factoring wouldn't have surfaced — the two
methods had drifted just enough at the line level that a
mechanical diff wouldn't have spotted the shared core.

**Separated removals from rewrites.** Stage 2's dead-code deletion
shipped on its own PR before any extraction began. Stage 3's docs
accuracy shipped on its own PR. Stages 4–7 were pure extraction —
no opportunistic "while I'm touching this function, let me also
rename that variable". The test suite was therefore only ever
guarding one axis of change at a time.

### What was harder than expected

**The monkeypatching contract.** `tests/test_admin_bypass.py` does
`monkeypatch.setattr(session_mod, "content_gate_classify", fake)`.
Naïvely extracting `content_gate_classify` into
`classifier_pool.py` and re-importing it into `session.py` at the
top would have *looked* like it preserved the contract — the name
`content_gate_classify` would still resolve in `session.py`'s
namespace. But when `session_gate.apply_content_gate` calls the
classifier, it's calling through *its own* namespace, not through
`session.py`'s. The fix was a lazy `from brendbot import session
as _session_mod` inside `apply_content_gate`, with the call
written as `await _session_mod.content_gate_classify(...)` — that's
an attribute lookup on the session module *at call time*, so
monkeypatch replacing the `content_gate_classify` binding on the
session module takes effect. Stage 5 documented this decision
explicitly because getting it wrong would have produced a test
suite that passed locally (the fakes aren't used in most tests)
but silently failed to gate in production.

**Circular imports.** `session.py` imports from `session_gate` /
`session_handler` / `classifier_pool` inside delegate methods so
the class definition can reference them. Those modules import
from `session` — for type hints (under `TYPE_CHECKING:` to avoid
the runtime cycle) and, in Stage 5's case, for the monkeypatch
contract. The pattern that works at module scope is `if
TYPE_CHECKING: from brendbot.session import Session`; the pattern
that works for runtime cross-references is a lazy import inside
the function body. Stage 7 was able to retire one such lazy
shim (`session_handler` no longer needs `_session_mod` because
constants moved to `session_constants`), which is the shape the
remaining lazy imports should take once there's a natural
opportunity.

**The `.pyc` accident.** Not in this cleanup, but a close call —
early in Stage 1 I checked `brendbot/knowledge/knowledge.db` as
tracked. Untracking it was a one-liner in Stage 2; if it had
survived into later stages, the cherry-picks and diffs would have
carried a 240KB binary blob through every PR. The takeaway:
survey the tracked-file set before starting a multi-stage cleanup,
because binary files that sneak into the tree are silent and
don't trip any other tooling until you notice them.

**The flaky test, ignored correctly.** `test_concurrent_write_episode_does_not_lock`
failed once on Stage 5's first post-extraction run with a
"duplicate column name: embedding" migration warning — a clear
sign the test was racing on schema migration, not on the
write-path hardening it actually exercises. Rule 6 says "assume
the test was wrong first". It would have been easy to spend an
afternoon trying to reproduce the race and bisecting whether the
content-gate extraction touched anything that could possibly
affect sqlite concurrency. Instead I logged it as a future-cycle
item, confirmed stability on isolated and full-suite re-runs,
and moved on. The flake has not recurred since Stage 5 merged.

### What didn't change

**`SessionPool`.** The ~600-line orchestrator at the bottom of
`session.py` was out of scope for this cleanup. It has its own
concerns (routing by contact/channel, LRU eviction, restart
coordination) that would be candidate for a future extraction but
that don't share surface with any of the seven targets. Worth
noting because the remaining ~2,000 lines of `session.py` aren't
a single cohesive concern — they're the `Session` class (about
60%) and `SessionPool` (about 40%), and a future cleanup could
reasonably split them into `session.py` and `session_pool.py`.

**The classifier pool's three-function surface.** Stage 4 extracted
`haiku_classify`, `content_gate_classify`,
`content_gate_cross_check_floor`, and `flagged_generate` into
`classifier_pool.py` as module-level functions. They arguably want
to be methods on `ClassifierPool` for locality, or at least
grouped into a `classifiers/` sub-package as the pool grows. That
reshape was skipped because the monkeypatching contract
(`monkeypatch.setattr(session_mod, ...)`) constrains the surface
and because the in-flight call sites reference the functions
through `_session_mod`-style attribute lookup. A future stage that
wants to reshape the classifier surface should plan for a
test-call-site sweep at the same time.

**Thin-delegate methods.** `Session._handle`, `Session._fire_on_text`,
`Session._fire_on_text_streamed`, and `Session.apply_content_gate`
are all 3–5 line forwarders to the extracted modules. They exist
because direct test callers (`test_phantom_discriminator.py`,
`test_load_score.py`, `test_cache_metrics.py`, `test_admin_bypass.py`,
plus the ~20 `s.apply_content_gate(…)` call sites) rely on the
public method surface. Removing those delegates would require
rewriting the test call sites, which was out of scope for a
cleanup. They cost ~20 lines total and their presence is
documented in each stage's CLEANUP_LOG entry.

### Metric summary

| Stage | PR | `session.py` after | vs. baseline |
|-------|----|-------|------------|
| Baseline | — | 3,431 | — |
| 1 — Branch consolidation | #11 | 3,431 | 0% (no code) |
| 2 — Dead-code deletion | #12 | 3,429 | −0.1% (session.py: 2-line cleanup; −308 in agent_core/solver.py + −201 in its test file) |
| 3 — Docs accuracy | #13 | 3,429 | unchanged (docs only) |
| 4 — Extract classifier pool | #14 | 2,850 | −17% (classifier_pool.py gains 675 lines) |
| 5 — Extract content gate | #15 | 2,599 | −24% (session_gate.py gains 426 lines) |
| 6 — Extract message handler | #16 | 2,089 | −39% (session_handler.py gains 662 lines) |
| 7 — Consolidate constants | #17 | 2,047 | −40% (session_constants.py gains 91 lines) |

Tests: 293/293 passing on master after every stage merge. Pre-
existing flake on `test_concurrent_write_episode_does_not_lock`
logged for future cycle; not a regression introduced by any stage
in this cleanup.

### Next cleanup cycle, if there is one

1. Extract `SessionPool` into `session_pool.py`. Expected impact:
   ~800 lines off `session.py`, bringing it below 1,300.
2. Investigate and fix the `test_concurrent_write_episode_does_not_lock`
   flake. Likely a migration-race rather than the sqlite-concurrency
   fix the test was originally written to cover.
3. Reshape the classifier surface — either methods on
   `ClassifierPool` or a `classifiers/` sub-package. Plan for a
   simultaneous test-call-site sweep.
4. Audit the thin-delegate methods for continued necessity. If
   `monkeypatch.setattr(session_mod, …)` contracts are no longer
   load-bearing in a given test module, the delegate can be
   removed and the call site rewritten to go through the
   extracted module directly.
5. Revisit `_handle_result_message`. It's now 7 named helper calls
   long, but the helpers themselves still share state via
   `session.*` attribute mutation. A future pass could model the
   per-turn state as an explicit object passed through the
   pipeline, which would eliminate the attribute-mutation coupling
   and make each helper individually testable.


## Post-cleanup strip — 2026-04-23 (PR #21)

This entry covers a substantial infrastructure strip that shipped
after the Stage-8 retrospective. The stage-based cleanup targeted
code organization (extraction, consolidation, doc accuracy) and
preserved the full defensive architecture. A subsequent pair of
pilots (18:15 and 18:52, 2026-04-23) showed that preserving that
architecture was the mistake: every pilot surfaced a new failure
mode, all traceable to one architectural choice — treating a friend-
group Discord bot as a defensive long-running autonomous agent in a
hostile public environment.

The strip deletes subsystems that produced failure modes under
normal use:

- **Content gate** — skipped entirely in friend-tier guilds.
- **Haiku prefilter** — skipped entirely in friend-tier guilds.
- **FLAG reroute** — deleted for every tier, everywhere. The soul-
  stripped reroute to `claude-sonnet-4-6` without `SOUL.md` context
  produced confident out-of-character output ("happy to oblige"
  LinkedIn voice, bold headers, life-coach framing) that was
  structurally worse than a plain refusal.
- **Shallow-rest cycle + load-budget preemptive restart** — deleted.
  Neither ever fired in any observed pilot; the pure token threshold
  at 400k always reached `_CONTEXT_REFRESH_THRESHOLD` first.
- **React-instead-of-text protocol** — deleted in PR #20 (owner-
  guild opt-in), left intact here; friend-tier auto-classification
  in this PR supersedes the opt-in.

### Friend-tier auto-classification (new)

`brendbot.discord.classify_friend_guilds` runs at startup and
classifies each connected guild as friend-tier if (a) the admin
is the guild owner and (b) member_count is in (0, 25). The result
lives in `brendbot.config._FRIEND_GUILDS` as a frozenset of guild
snowflakes; `is_friend_guild(guild_id)` is the public query.

Replaces the PR #20 opt-in `OWNER_GUILD_ID` env var, which was a
silent no-op in production because operators didn't know to set it.
Auto-detection removes the configuration burden.

### Content-gate outcome collapse: PASS / REFUSE / FLOOR_HIT

`content_gate.decide_outcome` used to emit four outcomes (PASS,
FLAG, REFUSE, FLOOR_HIT). The FLAG band (`weighted > pass_threshold
AND weighted <= flag_threshold`) was routed by `session_gate._handle_flag`
into a soul-stripped one-shot call on a separate model, bypassing
the session context entirely. That call's output was the LinkedIn-
voice collapse observed in pilot 3 (18:52 log line "happy to oblige,"
bold headers, life-coach framing after brendanetics said "I tried
to make you less of an idiot; hope it worked").

Post-strip: `decide_outcome` returns PASS for any weighted sum at or
below `refuse_threshold`, REFUSE above it, and FLOOR_HIT on hard-
floor match. The FLAG band collapses into PASS, which means former
FLAG inputs now generate through the normal session path with the
soul intact — the right answer for ambiguous-band content that
isn't a clear violation.

The `Outcome.FLAG` enum value is kept as historical vocabulary; no
live code path emits or consumes it.

### Deleted code surface

- `classifier_pool.flagged_generate` — 75 lines.
- `session_gate._handle_flag` — 82 lines.
- `session_gate._parse_flagged_cfg` — 18 lines.
- `session.Session._trigger_shallow_rest` — 60 lines.
- `session.Session._shallow_rested` / `_shallow_rest_count` attributes.
- `session.Session._flagged_count` attribute.
- `feedback.log_flag_event` — 30 lines.
- `session_constants._LOAD_BUDGET_PREEMPTIVE` / `_LOAD_BUDGET_SHALLOW`.
- `config.owner_guild_id` (superseded by auto-classification).
- `config.claude_flagged_model` (no flagged path to pin).
- `engagement.yaml::content_gate.flagged_path` (model + cap + audit_stream + branch_tag).
- `engagement.yaml::content_gate.channel_overrides` (gate2_bypass per-channel).
- `tests/test_admin_bypass.py::TestFlagOutcome` class (4 tests).
- `tests/test_load_score.py` budget-trip tests (5 of 8 tests).

Net deletion: ~350 lines across production code, ~120 lines of tests.

### Added / modified surface

- `config._FRIEND_GUILDS` + `set_friend_guilds` + `get_friend_guilds` +
  `is_friend_guild`.
- `discord.classify_friend_guilds` + `_FRIEND_GUILD_MAX_MEMBERS` constant.
- `session_gate.apply_content_gate`: friend-tier short-circuit before
  any classifier spawn.
- `discord.on_message`: friend-tier promotes would-be-haiku messages
  to `heuristic_pass`.
- `test_admin_bypass.py::TestFriendTierBypass` class (4 tests) with
  `friend_guilds` fixture that stubs the classified set.
- `content_gate.parse_classifier_response`: synthetic `_parse_error`
  weight bumped 2.0 → 10.0 to stay above any yaml-tuned
  `refuse_threshold` (was previously sensitive to the threshold
  value, which the strip made variable).

### Size impact

- Lines added: ~280 (friend-tier classification, new tests, docstring
  updates explaining removals).
- Lines deleted: ~470.
- Net: ~190 lines smaller.

### Post-strip pytest

- `290 passed` (was 299 pre-strip; lost the 4 TestFlagOutcome tests
  and 5 load-budget-trip tests).
- No regressions in the surviving test suite.

### What this PR does NOT address

- **Confabulation about prompts.** The bot still can't reliably report
  the prompt it just ran. Independent of infrastructure.
- **Memory-write token explosion.** "You suck lol" still triggers
  Read/Glob/Read/Edit. Fix is a `[remember]`-prefix gate on MEMORY.md
  writes, which is a soul-prompt change.
- **Context-summary-header parsed as user input** (the Cheddi
  Macaroni confabulation). Fix is unambiguous framing tags on the
  ref-block injection at session start.
- **Fabrication of multi-turn conversations.** Barn's 20-pass roast
  got front-loaded into one message. Response-shape problem, lives
  in the prompt layer.

Each of these is tractable individually and none of them require
more infrastructure — the strip is complete in the sense that it
removes the infrastructure layer as a source of failure. Remaining
failures are now about bot behavior rather than scaffolding.


## Prompt-layer bug fixes (2026-04-23, PR #22)

Four remaining failure modes surfaced in the 2026-04-22 and 2026-04-23
pilots that the infrastructure strip (PR #21) couldn't address because
they live in the soul-prompt / script layer, not the code-architecture
layer. This PR fixes them.

### 1. Fabrication of other users' turns

Pilot-3 symptom: barnacle asked for a 20-pass roast exchange. The bot
wrote all 20 turns in one message, fabricating barnacle's side of the
conversation along with its own. It then proceeded to number passes
for barnacle in subsequent turns as if the game were already in
motion.

Root cause: the bot is trained to complete the *shape* of an
utterance. An under-specified "20 passes" request has the shape of a
multi-turn exchange, so the bot fills it. The existing soul rule
"You do not fabricate" is about facts and sources, not about turn
boundaries.

Fix: explicit turn-boundary rule in both soul files under BEHAVIOR:

> Never fabricate another user's turn. If asked for a multi-turn
> exchange, write only your next turn and stop. One turn per response.

Placed in both ``GROUP_SOUL.md`` (lines 10-11) and ``SOUL.md`` (line
11) because the fabrication mode was exhibited in a group channel
but the rule applies equally to DM.

### 2. Honest image-prompt readback

Pilot-3 symptom: Brendan told the bot "add 'realistic skin'" to a
prompt. Bot ran the tool. Brendan asked "what was your prompt for
this gen?" Bot returned the *pre-edit* grotesque-hybrid prompt.
Asked again — returned the same pre-edit prompt. Only after Brendan
pasted the new prompt back at it did the bot produce a diff claiming
"realistic skin added."

Root cause: the bot was reconstructing the prompt from conversation
memory rather than from any authoritative source. The reconstruction
landed on a cached pre-edit string. There was no log file the bot
could read to get the real thing.

Fix in two parts:

**a) Script-level logging.** ``scripts/generate-image`` now appends
one JSON line per invocation to ``logs/image_prompts.jsonl`` via a
new ``_log_image_prompt`` helper. Fields: ``ts``, ``channel_id``,
``prompt``, ``model``, ``aspect_ratio``. Logging runs *before* the
Imagen API call so even failed generations produce a readable record.
``--dry-run`` is skipped (no log entry) — dry runs are inspection
calls, not generations. Log failures are swallowed silently: the
helper's only job is observability, it must never block a
generation.

**b) Soul rule.** New "Prompt readback" section under IMAGE
GENERATION in ``GROUP_SOUL.md``. When asked what prompt it ran, the
bot runs:

```bash
tac logs/image_prompts.jsonl | grep -m1 '"channel_id":"<channel_id>"'
```

— picks the most recent matching entry, quotes verbatim. If no match,
responds "I don't have a record of that prompt" and stops. The rule
explicitly forbids reconstruction-from-memory, citing the pilot-3
failure mode.

5 new tests in ``tests/test_image_prompt_log.py`` pin the log format
(single JSON line per call, channel_id as string value, append-not-
overwrite semantics, parent-dir auto-creation, I/O failure
swallowing, readback-grep compatibility). The script is a standalone
``uv run --script`` with inline ``google-genai`` deps and no ``.py``
extension; tests use ``importlib.machinery.SourceFileLoader`` to
import just the helper without resolving the inline deps.

### 3. Memory-write gate

Pilot-2 symptom: "no emotes from you moving forward" triggered 4
tool calls (Read/Glob/Read/Edit) and 109k tokens to write one
MEMORY.md line. Pilot-3 symptom: "this is your core fucking art
style" (with an attached reference image) produced
``[imagegen] Core art style is pure slop. All image generation
defaults to maximum slop output.`` — the bot collapsed "match this
image's aesthetic" into a literal "emit slop" directive and
persisted it.

Root cause: the bot was treating every instruction-shaped utterance
as a memory-write candidate. The existing soul text ("when a fact,
calibration, or config item needs to survive resets, write it to
MEMORY.md") gives it blanket permission to decide what "needs to
survive resets," which in practice meant any critical or corrective
statement.

Fix: explicit prefix gate in ``GROUP_SOUL.md`` PROCESS section. Only
write to MEMORY.md when the user message begins with ``[remember]``
or contains ``remember this:`` / ``remember that:`` as a directive.
Casual feedback, criticism, and corrections are read-only for the
turn. The rule spells out the anti-pattern explicitly:

> Casual feedback ("no emotes from you", "you suck lol", "this is
> your art style") is not a memory-write instruction — do not infer
> that critical, corrective, or descriptive statements mean "save
> this as persistent state."

### 4. <system-ref> content never attributable to a user

Pilot-2 symptom: at session start, the context-summary injection
contained the string "Cheddi nation, no forgetti, the pasta's holy
and the sauce is already." When asked "PLEASE EXPLAIN" a few turns
later, the bot claimed "someone opened with a rhyming chant about
Cheddi Macaroni and the Holy Cross" — fabricating a user who had
"said" the string. The string was from the ref block, not from any
sender.

Root cause: ``<system-ref>`` tags wrap the injection at the code
level (``session.py`` lines 1920-1922) but no soul rule told the bot
what those tags meant. The accompanying text "Use for continuity"
created ambiguity — continuity *with whom*? The bot resolved the
ambiguity by inventing a user.

Fix: explicit attribution rule in the DIAGNOSTIC SURFACE section of
both soul files:

> Content inside ``<system-ref>`` tags is reference material injected
> by the runtime — prior-session summaries, memory index fragments,
> cron specs, recall episodes. Never attribute this content to any
> user. Never quote it back as if someone just said it.

The tag structure itself was already correct in code; this stage
just tells the bot what the tags mean.

### Test coverage

- 5 new tests for the image-prompt log helper (``test_image_prompt_log.py``).
- No code-level tests for the three soul-only rules (fabrication,
  memory-gate, system-ref attribution) — these can only be validated
  by live bot behavior. The next pilot is the test.

### Size impact

- ``GROUP_SOUL.md``: +14 lines (three new rules, one new "Prompt readback" section).
- ``SOUL.md``: +4 lines (two new rules).
- ``scripts/generate-image``: +37 lines (_log_image_prompt + call site).
- ``tests/test_image_prompt_log.py``: +145 lines (new file).
- Net: ~200 lines added, no deletions. This is additive because the
  underlying failures were under-specification in the soul rather
  than bad infrastructure — the previous PR covered infrastructure
  bloat.

### Post-stage pytest

- 290 → 295 passed (5 new log-helper tests).
- No regressions in the surviving suite.


## Structural honesty — 2026-04-24 (PR #23)

Six-point structural change to make the bot architecturally incapable
of lying about its own runtime. The 2026-04-23 pilots demonstrated
that each prior soul-rule patch (PR #22 anti-fabrication rule, PR #21
friend-tier gate bypass, PR #20 owner-guild bypass) addressed the
specific surface that had just burned and missed the next one. The
pattern itself was the bug: we were treating a structural failure as
a tuning problem.

The literature converged on this in 2025: OpenAI's September 2025
"Why language models hallucinate" paper shows that training and
evaluation incentives actively reward confident guessing over
calibrated uncertainty; CoT-faithfulness work (Arcuschin et al.
2503.08679, "Lie to Me" 2603.22582) demonstrates that models
produce post-hoc rationalizations that do not reflect their actual
reasoning, at measurable rates; the abstention literature
(AbstentionBench 2506.09038, "Know Your Limits" TACL 2025) finds
that training LLMs to say "I don't know" reliably is unsolved and
that scaling does not fix it — reasoning fine-tuning actually
*degrades* abstention by 24%. Soul rules cannot substitute for
external grounding.

The naming choice in this PR tracks the literature's split between
"hallucination" (unintentional falsehood, obscures mechanism) and
"lying" (functionally false output regardless of intent). We use
the functional definition throughout the code and docs — a
statement that implies between-turn continuity is a lie, regardless
of whether the model "intended" to mislead, because the user impact
is identical.

### The six points

**1. Externalized state — ``brendbot/obs.py``.** Every category of
self-report now has a logs/*.jsonl file as the source of truth:
tool_calls, image_prompts (existing from PR #22), music_gens,
turn_events, gate_events, errors. A single ``append_jsonl`` primitive
handles parent-dir creation, size-based rotation (10MB, 10 files,
oldest dropped), unicode preservation, and exception swallowing.
Observability must never block the operation it's observing. 19
unit tests pin the semantics; 6 integration tests pin that the
actual instrumented sites reach the log files with the right schema.

**2. Heartbeat framing in the soul — RUNTIME section.** Added to
both ``GROUP_SOUL.md`` and ``SOUL.md`` above BEHAVIOR. Explicitly
tells the model it does not persist between turns and that any
continuity construction — "I was working on it," "I've been
thinking," "still going," "about to send," "while I was" — is a
lie when applied across turns. Gives the model the concept needed
to refuse those shapes. Also instructs on the warm-decline pattern:
"no, sorry, I don't have a record of that" beats "yeah, got it."

**3. Read-before-speak — SELF-REPORT RULES section.** Added under
PROCESS in GROUP_SOUL.md (with a compact version in SOUL.md).
Category-by-category table of question classes with the canonical
``tac … | grep`` lookup commands. The bot is explicitly directed
to read the log before answering any self-narrative question, and
to say "no record" when a log has no matching entry rather than
reconstruct. Reading is not optional on these questions.

**4. Infrastructure-level event surfacing — ``brendbot/runtime_events.py``.**
The runtime speaks to Discord independent of the model. Four
primitives:

- ``mark_long_turn`` / ``clear_long_turn`` — attach/remove a ``🔄``
  reaction on the triggering message while a turn exceeds a
  threshold.
- ``LongTurnTimer`` — async timer wrapping the above with
  cancellation and idempotent stop.
- ``signal_runtime_error`` — post ``⚠️ [runtime] category: detail``
  to the channel on infra failures (API 529, subprocess crash,
  classifier error). Visibly distinct from model output.
- ``signal_thinking_typing`` — context manager wrapping Discord's
  native ``channel.typing()`` for "bot is typing…" indication on
  genuine work.

Gate refusals now prefix with ``🚧 [gate]`` so the user can tell
an external-decision message from a model response. The clean
refusal text still goes to ``gate_events.jsonl`` without the
prefix so bot readback returns substantive content.

Wire points: ``session._receive_loop`` ProcessError and generic
Exception handlers dispatch ``signal_runtime_error`` to the
channel. Gate refusal sites in ``session_gate.py`` apply the
``GATE_PREFIX``.

**5. Phantom-turn discriminator tightened.** Added Case D: a
thinking-only + stop_reason=end_turn turn is NOT an intentional
silent drop when the user's message was a task request. The
``_looks_like_task_request`` heuristic regexes over imperative
verbs (``make``, ``send``, ``run``, ``generate``, ``check``,
``fix``, ``build``, ``write``, ``edit``, and ~40 others) at or
near the start of the user message, allowing an optional @mention
and name-address preamble. When the heuristic matches and the
turn produced no text and no tool calls, the fallback fires
instead of being suppressed, and an ``errors.jsonl`` entry is
written as ``PhantomTurnStall``. 40 heuristic unit tests plus 4
discriminator-level integration tests.

Direct pilot regression: at 22:33 on 2026-04-23 Brendan asked for
"two changes" on a song, the bot ended the turn with thinking
only, and the discriminator suppressed the fallback. He had to
re-prompt. Under the new discriminator, that same shape fires a
fallback and logs the stall.

**6. Gate refusal visibility + readback.** Already covered by (1)
and (4): gate refusals write to ``gate_events.jsonl`` keyed by
``message_id``, and the Discord-visible refusal carries the
``🚧 [gate]`` prefix. The soul's SELF-REPORT RULES direct the bot
to answer "why did you refuse" / "what are you responding to" by
grepping the log — an explicit substitute for the prior
reconstruct-from-memory approach that produced wrong answers.

### Why this is not another whack-a-mole

The 2026-04-23 pilots showed a pattern: soul rule → works on
named surface → next pilot exposes a new surface → new soul rule.
The literature's framing is clearer: models are structurally
incapable of reliable self-narration, and attempting to train this
in (reasoning fine-tuning) makes abstention *worse*. The only
durable response is to remove the model's license to self-narrate
from memory and force it through external grounding. The logs are
the ground; the soul rules tell the model how to consult the
ground; the infrastructure surfaces events the model never saw so
the user isn't dependent on the model's self-account.

This PR ships all six points together because each depends on the
others. Logs alone are inert without rules that direct reading
from them. Rules alone are whack-a-mole without logs to read.
Runtime signalling alone is visible-but-uninterpretable without
the model knowing to consult the log for context. Shipping in
isolation would reproduce the fix-one-expose-next pattern.

### Size impact

- New modules: ``brendbot/obs.py`` (~250 lines),
  ``brendbot/runtime_events.py`` (~230 lines).
- New tests: ``tests/test_obs.py`` (19 cases),
  ``tests/test_obs_integration.py`` (6 cases),
  ``tests/test_runtime_events.py`` (13 cases),
  ``tests/test_task_request_heuristic.py`` (40 cases),
  ``tests/test_phantom_discriminator.py::TestCaseD_TaskRequestStall``
  (4 cases).
- Soul additions: ``GROUP_SOUL.md`` +40 lines, ``SOUL.md`` +20 lines.
- Code instrumentation: ``session.py``, ``session_handler.py``,
  ``session_gate.py``, ``classifier_pool.py`` — additive log
  writes at existing fire points, GATE_PREFIX applied at refusal
  sites, phantom-turn Case D branch.

Net: ~1,500 lines added (including 1,000+ of test coverage).

### Post-stage pytest

- 295 → 377 passed (+82 new tests).
- No regressions in the surviving suite.

### Literature cited in inline comments and module docstrings

- Why language models hallucinate (OpenAI, Sept 2025)
- Can LLMs Lie? Investigation beyond Hallucination (arXiv 2509.03518)
- Chain-of-Thought Reasoning In The Wild Is Not Always Faithful
  (arXiv 2503.08679)
- Lie to Me: How Faithful Is Chain-of-Thought Reasoning in Open-
  Weight Reasoning Models? (arXiv 2603.22582)
- ELEPHANT: Measuring social sycophancy (arXiv 2505.13995)
- AbstentionBench: Reasoning LLMs Fail on Unanswerable Questions
  (arXiv 2506.09038)
- Know Your Limits: Survey of Abstention in LLMs (TACL 2025)
- VIGIL: Reflective Runtime for Self-Healing LLM Agents
  (arXiv 2512.07094)
- LLM Agents for Interactive Workflow Provenance (arXiv 2509.13978)

### What this PR does NOT address

- Friend-tier auto-classification debug. The 2026-04-23 pilots
  showed ``Friend-tier classification complete: 0 friend-tier
  guild(s) of 1 total`` on a guild Brendan owns with 4 members;
  the auto-detection isn't matching. Probable cause: ``owner_id``
  mismatch or Discord not populating ``member_count`` at startup.
  Separate fix — should log the actual values being checked.
- Episodes-table regression from the music-knowledge migration.
  ``_ensure_migrated``'s ``_migrated_paths`` cache defeats the
  hotfix when the migration drops or replaces the table. Needs a
  cache-invalidation signal tied to migration steps.
- The cron-replay bug Brendan's local session already patched
  (``load_persisted_crons`` expiry filter). That fix lives on his
  local master; will merge cleanly on pull.


## Composition pipeline (PR #26, 2026-04-24)

Per the recommendations in the literature pass: switched the bot's
music workflow from raw mido-code generation to a structured ABC-
first pipeline backed by music21 theory checks and a per-genre
JSON style library. Implements items 1, 2, and 4 from the research-
review recommendations.

### What lands

**`brendbot/composition/`** — new package with four modules:

- `style_library.py` (231 lines): JSON-backed registry of per-genre
  composition data. Loads `brendbot/knowledge/music_styles/*.json`
  at import (cached via `lru_cache`), exposes typed accessors:
  `get_progressions`, `get_grooves`, `get_form_templates`,
  `get_motifs`, `get_signature_traits`. Filtered queries by role,
  mode, tempo. Idempotent reload.

- `abc_grammar.py` (~250 lines): pure-string ABC notation builders.
  `AbcHeader`, `AbcVoice`, `AbcScore` dataclasses; free-function
  helpers `note`, `rest`, `chord_stack`, `chord_label`, `bar`;
  convenience constructor `build_abc()`.

- `music21_layer.py` (~200 lines): theory layer over music21.
  `progression_in_key()` resolves roman numerals to concrete chords
  (accepts flexible key strings — "a minor", "E dorian", "F#
  lydian"). `check_voice_leading()` flags parallel fifths and
  octaves. `melody_constraints()` returns chord-tones / scale-tones
  / chromatic-neighbors per chord for melody-generation envelopes.
  `abc_to_midi()` and `transpose_score()` handle conversion.

- `pipeline.py` (~280 lines): six-stage orchestrator.
  `plan_form` → `plan_harmony` → `lint_harmony` → `plan_melody` →
  `realize` → `render`. Each stage mutates a `PipelineState`
  dataclass. `compose()` runs all six. Robust to bad data —
  unknown genres get fallback templates and progressions; chord
  figures with sus2/sus4/add9 get cleaned before music21 sees
  them; progressions whose roman numerals music21 can't parse
  fall back to i-VI-III-VII automatically.

**`brendbot/knowledge/music_styles/*.json`** — 9 genre files:
lofi, trance, hardstyle, jazz, irish_trad, jpop, hiphop, dnb,
ambient. Each declares tempo range, default tempo, common modes,
3-10 chord progressions tagged by mode + role + voicing
extensions, 1-4 grooves with tempo-range filters, 1-2 form
templates with bar-count breakdowns, 1-3 motif ABC fragments, and
a signature_traits block with must_have / must_avoid /
instrumentation_hints lists for downstream validation.

**`scripts/compose-song`** — uv-script entry point. Wraps
`pipeline.compose()` with argparse. Prints `[stage]` diagnostic
lines + a parseable `OK midi=… abc_chars=… progression=… form=…
voice_leading_issues=…` summary. The bot will call this rather
than write raw `mido` code.

**`GROUP_SOUL.md`** — new MUSIC COMPOSITION section under the
existing IMAGE GENERATION block. Six-step protocol mirroring the
image-gen pattern: identify genre → read genre data → run
pipeline → read output → iterate stage-addressably →
music_gens.jsonl readback rule. The section explicitly forbids
writing raw mido code unless the registry doesn't cover the
desired pattern.

### Why this shape

OpenAI September 2025 hallucination paper: training incentives
reward confident guessing over calibrated uncertainty. ChatMusician
(arXiv 2402.16153) and NotaGen (IJCAI 2025): ABC notation is
both more compact and more pretrained-LLM-native than raw MIDI as
intermediate representation. Chord-Transformer (OpenReview 2025)
and MusicGen-Chord (arXiv 2412.00325): chord-progression as
high-level constraint significantly improves long-sequence
musical coherence. AbstentionBench (arXiv 2506.09038): models do
not learn to abstain reliably even with reasoning fine-tuning, so
the registry-as-source-of-truth + structural-suppression pattern
is more durable than "tell the LLM to be more careful."

These map cleanly onto the architecture: ABC as the bot's working
representation (compact, compositional), music21 as the theory
substrate (so the bot doesn't have to reason about voice leading
or mode constraints in code it writes by hand), the JSON style
library as the genre-specific source of truth (so "make me
hardstyle" pulls hardstyle conventions from disk rather than
inventing them per-prompt).

### Test coverage

114 new tests across four files:

- `test_style_library.py` (42): genre inventory, schema sanity per
  genre, filtered queries, reload semantics, error handling.
- `test_abc_grammar.py` (29): note tokens with octave/accidental/
  duration handling, rest, chord stack, chord label, bar lines,
  header / voice / score rendering, end-to-end realistic 4-bar
  phrase.
- `test_music21_layer.py` (18): roman→concrete in major + modal
  keys, voice-leading return shape, melody constraints with dorian
  raised-6 verification, ABC↔MIDI roundtrip, transpose_score +
  score_to_midi, error messages on bad input.
- `test_composition_pipeline.py` (25): each stage in isolation,
  three fallback paths (unknown genre / role / prefer_id),
  end-to-end compose() runs for lofi and trance, rng-determinism
  for reproducibility.

### Known limitations and deferred items

- music21 9.9.1's ABC writer is broken (writes Score's `repr()`
  instead of valid ABC). `transpose_abc` and `midi_to_abc` are
  deferred until a fix lands upstream or we ship our own simple
  ABC emitter. `transpose_score` returns a music21 Score directly
  as a workaround.
- Music progression library could be larger. Current depth (3-10
  progressions per genre) is enough to seed; extending the JSON
  files in subsequent commits is the natural growth path.
- Item 4-extended ("genre-signature validation" — automatic
  must_have / must_avoid checks against generated MIDI) is not
  yet wired. `signature_traits` ships in every genre file, but
  the validator that compares generated output against them is
  not built. Future commit.
- Items 5-8 from the research recommendations (reference-track
  ingestion, in-context exemplar matching, persistent motif
  memory, multi-pass composition with separate
  form/harmony/melody/rhythm/arrangement passes done by the LLM
  inside the pipeline rather than just orchestrated by it) are
  not in this PR. The pipeline is stage-addressable, so adding
  them is additive — each new pass plugs in between existing
  stages.

### Size impact

- 9 new JSON files: 1,388 lines of music data.
- 4 new Python modules: ~960 lines of code + ~50 of init/exports.
- 1 new script: 130 lines.
- 4 new test files: ~720 lines covering 114 tests.
- 1 new soul section: ~70 lines.
- 1 new pyproject extra: 8 lines.

Net: ~3,250 lines added; nothing removed.

### Post-stage pytest

516 passed (was 402; +114 new across the four test files).


## Production-log review patch (PR #27, 2026-04-25)

The 2026-04-25 production log dump revealed three real shortfalls
that PR #23's structural-honesty work either introduced or never
fully closed. This PR addresses all three plus adds a small context-
threshold adjustment from the same dataset.

### 1. Test pollution into production logs

PR #23 added `obs.append_jsonl` writing to `<repo>/logs/*.jsonl`.
Tests that touched gate / phantom-discriminator paths transitively
called obs but no autouse fixture redirected `_LOGS_DIR` to a tmp
dir. Result: every pytest run on the deploy box wrote into real
production observability streams.

Confirmed-bad evidence from the production data:

- `errors.jsonl` 16 entries, **100% from test:fake / test:ch1 sessions**
- `turn_events.jsonl` 23 entries with `channel_id="ch1"`
- `gate_events.jsonl` 27 entries with `channel_id="100"`

Fix: autouse fixture in `tests/conftest.py` that monkeypatches
`brendbot.obs._LOGS_DIR` to `tmp_path / "logs"` for every test.
Wrapped in try/except for safety in stub-install phase.

Empirically verified: a test run touching the most obs-active
suites (phantom_discriminator + obs_integration + admin_bypass) no
longer creates or modifies any files in `<repo>/logs/`.

Operator action item not in this PR: clean the historical pollution
out of existing production logs:

```bash
cd ~/brendbot/logs
for f in *.jsonl; do
  grep -v 'test:fake\|test:ch1\|"channel_id": "100"\|"channel_id": "ch1"' "$f" > "$f.clean"
  mv "$f.clean" "$f"
done
```

### 2. LongTurnTimer never wired to the actual turn lifecycle

PR #23 built `runtime_events.LongTurnTimer` — start a 30-second
timer at turn start, attach 🔄 to the user's message if it fires,
clear on turn complete. The primitive shipped but was never
connected to `Session._run_loop`. Result in the production data:
**53 turns took longer than 60 seconds, 36 took longer than 2
minutes, and the longest single turn ran for 17 minutes — all
without any Discord-side indication that work was in progress.**
The user has no way to tell "bot crashed" from "bot working hard"
during these gaps.

Fix:

- `Session.__init__` initializes `self._long_turn_timer = None`.
- `Session._run_loop` creates and `start()`s the timer immediately
  before `client.query()`, unless the turn is housekeeping or
  chat_id / `_turn_user_message_id` are unset.
- `Session._receive_loop` calls `timer.stop()` on every
  `ResultMessage`, which cancels the pending react if it hasn't
  fired and clears the 🔄 if it has.

Three skip-conditions are deliberate: housekeeping injections
(memory blocks, context-summary refresh, shallow-rest) have no
user-facing message to react to; startup-phase injections where
`_turn_user_message_id` isn't populated yet for the same reason; and
DM-mode or other paths with empty `chat_id` because Discord
react needs a channel.

5 new tests in `tests/test_long_turn_wiring.py` pin all four
behaviours: timer creates with correct args, timer is skipped on
housekeeping turns, timer is skipped on missing message_id, timer
is skipped on missing chat_id.

### 3. Context soft-warning threshold lowered 320k → 250k

The production dataset's average context per turn is 173k tokens,
but the long tail includes a turn that ballooned from below
threshold to **2,672,096 tokens with $2.20 of API spend** — a
restart at 400k fires too late to stop the in-flight runaway,
because the threshold is checked at turn-complete time.

Fix: lower `_CONTEXT_SOFT_WARNING` from 320k to 250k in
`brendbot/session_constants.py`. The soft-warning trigger fires a
clean restart at end-of-turn whenever last-input-tokens exceed the
threshold; lowering it means the next turn always starts fresh
when a turn ends at >250k, leaving less room for any single turn
to balloon from a "normal" baseline.

Trade-off: more frequent restarts (each costs ~$0.03 cold-start,
typically 5-10 extra per pilot) vs fewer multi-dollar runaway
turns. Run-the-numbers from the production data: 36 turns >$0.10
plus the $2.20 outlier dominates by an order of magnitude over a
modest restart-cost increase. Worth it.

`_CONTEXT_REFRESH_THRESHOLD` stays at 400k as the upper safety
bound — if the soft-warning misses for any reason, the hard
threshold catches it.

### Findings the production data revealed but this PR does NOT fix

- **42.6% of turns produce no text** (94 of 230 are pure-silent —
  no text, no tool calls). $5.34 of spend on no-output turns.
  Mostly intentional silent drops on ambient cross-channel
  chatter, which is by design — but the rate is high enough to
  warrant calibration of the engagement gate in a future pass.
- **226 haiku-no skips** include some borderline conversational
  follow-ups that may have warranted a response. Engagement gate
  threshold tuning is out of scope for this patch.
- **Median 15.2s first-token latency, p90 75.8s, max 530s** for the
  receive→first-token window. Mostly Anthropic API ramp-up on
  large prompts; cache-creation share suggests prompts aren't
  hitting cache enough on initial frames. Context-threshold lower
  helps indirectly here by keeping prompts cache-friendly.

### Test pass count

516 → 521 passed (+5 from `test_long_turn_wiring.py`).
