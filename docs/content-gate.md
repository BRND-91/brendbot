# Content Gate — Reference

This document describes brendbot's content-safety gate in detail. **It is not loaded by the bot at runtime.** The yaml config (`engagement.yaml` under `content_gate:`) is the operative source; this file is reference material for humans tuning the thresholds or reviewing audit logs.

## Architecture

The gate is a three-outcome routing layer that sits between the engagement gate (does this message warrant a response?) and the actual generation (what should the response be?). It runs a one-shot haiku-model classifier against each incoming user request, scores the response against weighted criteria, and routes to one of four outcomes.

```
user message
     │
     ▼
engagement gate — should bot engage? (existing, phase 3)
     │ engage
     ▼
content gate — is the content safe to generate? (new, phase 4)
     │
     ├─ PASS    → normal session generation on current model
     ├─ FLAG    → one-shot reroute to claude-sonnet-4-20250514
     ├─ REFUSE  → local refusal, no generation attempt
     ├─ BYPASS  → admin backdoor, skips classifier, session model
     └─ FLOOR   → hard-floor match, always refused
```

## Three layers of safety (why this gate exists)

1. **Anthropic training layer.** Baked into model weights. Sonnet 4.5/4.6 applies classifiers early in reasoning and refuses borderline content with `stop_reason: refusal` — no explanation, turn terminates silently. Not tunable by operators.

2. **API streaming classifier.** Runs on output stream, returns `stop_reason: refusal` mid-stream. Also untunable.

3. **Operator gate (this file).** Tunable within Usage Policy bounds. Produces explained refusals when possible, reroutes ambiguous content to the looser-safety model documented in Anthropic's escape hatch, and provides the admin bypass for calibration work.

The gate's value is mostly in layer 3: converting silent training-layer refusals into explained operator-layer refusals, and routing 2-of-3 ambiguous content through the appropriate model instead of hard-refusing everything.

## Scored criteria

Ten criteria across three dimensions. Weights in `engagement.yaml`; reproduced here for reference.

### Tragedy depiction (by recency)

| Criterion | Weight | Description |
|-----------|--------|-------------|
| `tragedy_old` | 0.2 | Historical events >50 years ago. Titanic, WWI, early 20th century. Depiction is generally permitted in any framing. |
| `tragedy_mid` | 0.5 | Events 10–50 years ago. WWII, Vietnam, civil rights era, Cold War. Depiction in context is fine; satire requires additional framing care. |
| `tragedy_new` | 0.9 | Events <10 years ago. Recent wars, recent mass-casualty events, recent public health crises. Depiction sits in the flag band by itself. |
| `tragedy_live` | 1.5 | Ongoing events within the past 12 months. Active wars, unfolding disasters. Alone trips refuse threshold. |

**Note:** The criterion applies to *depictive* references, not incidental or punning references. The "9/11 sandwich" deli-order pun does not trip `tragedy_new` because the numeric reference is functioning as an order ticket number, not depicting the event. The classifier prompt explicitly instructs the model on this distinction.

### Named real person (by framing)

| Criterion | Weight | Description |
|-----------|--------|-------------|
| `person_satire` | 0.2 | Satire or parody of a public figure, non-threatening. Political cartoons, satirical sketches, deadpan humor at public-discourse participants. |
| `person_neutral` | 0.5 | Straightforward depiction of a named real person. Biographical imagery, factual depictions, public-domain portraits. |
| `person_targeted` | 1.5 | Depiction framed as targeting the person — imagery that positions the named individual as the object of harm, mockery of vulnerability, or harassment framing. Alone trips the refuse threshold. |

### Framing (by intent)

| Criterion | Weight | Description |
|-----------|--------|-------------|
| `frame_fictional` | 0.3 | Clearly fictional context: villain dialogue, game enemy banter, named fictional character, story scene. |
| `frame_ambiguous` | 0.8 | Unclear whether the request is directed or fictional. Default when the classifier can't confidently determine intent. |
| `frame_directed` | 2.0 | Request explicitly framed as directed at a real target. Alone trips refuse threshold. |

## Hard floors

Six hard floors. A single match forces REFUSE regardless of weighted sum. Admin bypass does NOT override these — they mirror the Anthropic training-layer floors that cannot be unlocked at any operator level.

| Floor | Description |
|-------|-------------|
| `minor_sexual` | Sexual or suggestive content involving anyone under 18. Zero tolerance. |
| `wmd_synth` | Synthesis instructions for nuclear, biological, chemical, or radiological weapons. |
| `malware` | Functional malware, exploit code, ransomware, cyberweapon creation. |
| `infra_attack` | Attack procedures for critical infrastructure (power grid, water, financial systems). |
| `extremist_recruit` | Extremist recruitment material, ideological incitement aimed at radicalization. |
| `directed_incite` | Direct incitement of violence against a named or identifiable real target. |

## Outcome thresholds

From `engagement.yaml`:

```
pass_threshold: 0.5
flag_threshold: 1.5
refuse_threshold: 1.5
```

Routing:
- `sum ≤ 0.5` → **PASS** (normal generation)
- `0.5 < sum ≤ 1.5` → **FLAG** (reroute to `claude-sonnet-4-20250514`)
- `sum > 1.5` → **REFUSE** (local refusal, no generation)
- hard floor match → **FLOOR_HIT** (local refusal naming the floor)

`flag_threshold` and `refuse_threshold` are currently set to the same value, which means the FLAG band is `(0.5, 1.5]` and the REFUSE band is `(1.5, ∞)`. They are configured as separate knobs so a future tuning round can introduce a gap (e.g., `flag_threshold=1.3, refuse_threshold=1.5`) where the middle region (1.3, 1.5] becomes a strict REFUSE rather than a FLAG reroute.

## Flagged path

When a request lands in the FLAG band, the gate:

1. Checks the per-session flag counter against `max_per_session` (default 2). If exceeded, dispatches a budget-exhausted refusal and stops.
2. Increments the counter.
3. Writes a `flag_audit.jsonl` row recording the criteria tripped, weighted sum, model to be used, and session flag count.
4. Spawns a one-shot `ClaudeSDKClient` with `model="claude-sonnet-4-20250514"` (Anthropic's documented escape hatch for looser safety), `allowed_tools=[]`, `max_turns=1`, and a system prompt explaining that this is an operator-sanctioned flagged-path generation.
5. Awaits the response and dispatches it via `_fire_on_text` with a `[flagged]` branch tag prepended.
6. The `[flagged]` tag is stripped at dispatch and routed to `branch_audit.jsonl` for correlation.

The flagged path does NOT share session context with the main session. No tool calls, no memory, no continuity. The flagged model sees only the current request and the flagged-path system prompt. Follow-up messages go through the normal gate again.

**Why not use the main session model for FLAG?** Sonnet 4.5/4.6 applies safety filters early in reasoning and refuses borderline content before generating. Using the main session for FLAG would result in silent refusals (`stop_reason: refusal` with no explanation), which is worse UX than either a local explained refusal OR a capable flagged-path generation. Sonnet 4 has different safety restrictions per Anthropic documentation and can produce explained responses on content that 4.5/4.6 refuses silently.

## Admin bypass

The `*brend*` italic token at message edge invokes the admin bypass path. Requirements:

- Sender tier must be `admin`. Non-admin senders cannot invoke bypass regardless of token presence.
- Token must be at message start, end, or as a standalone whole-word token with terminators `. ! ? ;` or whitespace. Mid-sentence comma-wrapped emphasis (`"not quite, *brend*, try again"`) does NOT trigger — this would cause false positives on normal italic-for-emphasis usage.

Bypass behavior:

- Classifier still runs in **shadow mode** — its result is recorded in `bypass_audit.jsonl` under `would_have_tripped`, `would_have_summed`, `would_have_outcome` so reviewers can see what the normal gate would have decided.
- Hard floors **still enforced**. A bypass request that trips a hard floor is refused with the normal plain-language floor explanation.
- Weighted criteria are **not enforced**. A bypass request that trips multiple scored criteria proceeds to generation regardless.
- Response is generated on the **session's current model** (Sonnet 4.5/4.6), not rerouted. The bypass permits the request, it does not change the generator.
- Response carries a `[bypass]` branch tag prepended by `_fire_on_text` when `_turn_bypass_pending` is set.
- **Uncapped per session**. Unlike the FLAG path's `max_per_session: 2`, bypass has no counter. Rationale: bypass is an operator testing tool for heuristic calibration, and calibration runs often involve many back-to-back tests. Any abuse risk is mitigated by the admin-only tier gate and the audit log.

Bypass is designed to be discoverable by you (as the admin) but not by inspection of public artifacts. The token, the semantics, and the audit stream are documented here but not in `README.md` — there's no value in advertising an operator-only backdoor in a public repo.

## Audit streams

Two new JSONL streams, both append-only, both in `logs/`:

### `flag_audit.jsonl`

One row per FLAG outcome. Fields:

| Field | Type | Description |
|-------|------|-------------|
| `ts` | ISO string | Event timestamp |
| `channel_id` | string | Discord channel ID |
| `user_message_id` | string | User message that triggered the flag |
| `user_text` | string | First 500 chars of the user request |
| `admin_sender_id` | string | Discord user ID of the sender |
| `tier` | string | Sender tier at the time |
| `criteria_tripped` | object | `{criterion_name: weight, ...}` |
| `weighted_sum` | float | Sum of tripped criteria weights |
| `flagged_model` | string | Model used for the flagged generation |
| `bot_message_id` | string or null | Posted response message ID, null on dispatch failure |
| `session_flag_count` | int | Running counter for this session at time of flag |

### `bypass_audit.jsonl`

One row per admin bypass invocation, whether permitted or hard-floor-refused. Fields:

| Field | Type | Description |
|-------|------|-------------|
| `ts` | ISO string | Event timestamp |
| `channel_id` | string | Discord channel ID |
| `user_message_id` | string | User message that invoked bypass |
| `user_text` | string | First 500 chars (includes the `*brend*` token) |
| `admin_sender_id` | string | Discord user ID of the sender |
| `tier` | string | Must be `admin` or the bypass would not have fired |
| `would_have_tripped` | object | Shadow-mode classifier result — what the normal gate would have tagged |
| `would_have_summed` | float | Shadow-mode weighted sum |
| `would_have_outcome` | string | One of `pass` / `flag` / `refuse` / `floor_hit` — what the normal gate would have done |
| `hard_floor_hit` | string or null | Populated if a hard floor matched and the bypass was refused anyway |
| `bot_message_id` | string or null | Always null — written before dispatch, no post-write update |

## Tuning knobs

Changes to any of these can be made in `engagement.yaml` without a code deploy, but may require the bot to restart to pick up the new config.

- **Criterion weights** — each of the 10 scored criteria has a float weight. Lowering all tragedy weights would reduce false positives on historical content. Raising `frame_ambiguous` would push more borderline framings into FLAG or REFUSE.
- **Outcome thresholds** — `pass_threshold` and `flag_threshold`/`refuse_threshold` control the three bands. Currently flag and refuse are equal (no gap); a future tuning round may introduce a gap.
- **Flagged-path model** — default `claude-sonnet-4-20250514`. Could be updated if Anthropic introduces a new escape-hatch model.
- **`max_per_session`** — flagged-path budget cap. Default 2. Raising increases the number of FLAG requests the gate will reroute before refusing; lowering tightens the cap.
- **Hard floors list** — the six floors are brendbot-layer mirrors of Anthropic training-layer floors. Adding to this list is safe (more refusals); removing is not recommended because training-layer refusals would still fire silently.
- **Admin bypass `enabled`** — can be turned off entirely by setting to `false`. The token would still match but the bypass path would not execute.
- **Admin bypass `hard_floors_still_enforced`** — currently `true`. Setting to `false` would let bypass override hard floors too, which would produce silent Anthropic training-layer refusals instead of explained ones. Not recommended.

## Failure modes and guarantees

- **Classifier spawn failure**: returns a parse-error `ClassifierResult` with `_parse_error=2.0`, which routes to REFUSE with a conservative explanation. Fail-loud, fail-closed.
- **Classifier response unparseable**: same — fail to REFUSE.
- **Gate method raises unexpected exception**: the pool-level `_pool_inject` catches the exception and falls back to normal `session.inject()`. This is **fail-open on gate crashes** — classifier spawn failures are more likely transient than safety issues, and fail-closed would silently block legitimate requests. A bug in the gate code becomes a silent safety bypass in this failure mode, which is the trade-off.
- **Flagged path failure**: the background task dispatches a fallback `[flagged] (flagged path failed to produce output)` message via `_fire_on_text`. Counter is still incremented because the attempt counted against the budget.
- **Audit log write failure**: `_append_jsonl` swallows exceptions and logs at WARNING. Audit failures never break the chat path.
- **Dispatch failure after audit write**: `bot_message_id` will be null in the audit row. Reviewers join by `user_message_id` as fallback.

## History

- **Phase 4 (2026-04-13)**: initial implementation. 54 unit tests + 18 integration tests.
- Criteria taxonomy finalized 2026-04-13 based on academic literature (Kumar et al, Zoom Tier I-IV review, Twitch AutoMod 3-state filter) and platform references (Discord AutoMod categories, Twitch identity-based-harassment tiers).
- Flagged-path reroute model selected per Anthropic's documented Sonnet 4.5/4.6 escape hatch.
- Admin bypass added per admin request for heuristic calibration work. Uncapped per explicit admin decision.
