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
