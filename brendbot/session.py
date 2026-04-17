"""Claude Agent SDK session manager.

Each contact/channel gets one persistent Claude session. The SDK spawns a
Claude Code subprocess — no API key needed, uses OAuth via `claude login`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape, quoteattr

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# Patch SDK parser to handle unknown message types gracefully
try:
    import claude_agent_sdk._internal.message_parser as _mp
    import claude_agent_sdk._internal.client as _client

    _original_parse = _mp.parse_message

    def _tolerant_parse(data):
        try:
            return _original_parse(data)
        except Exception:
            return SystemMessage(subtype=data.get("type", "unknown"), data=data)

    _client.parse_message = _tolerant_parse
except (ImportError, AttributeError):
    pass

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"

CRON_FILE = "crons.json"  # per-session cron persistence file (lives in session cwd)

# ---------------------------------------------------------------------------
# Warm classifier pool — pre-spawned ClaudeSDKClient instances
# ---------------------------------------------------------------------------
#
# Both the engagement classifier (haiku_classify) and the content-gate
# classifier (content_gate_classify) previously spawned a fresh
# ClaudeSDKClient per call. Subprocess spawn + connect is ~18s cold,
# ~7-8s warm.  ClassifierPool pre-spawns N connected clients at boot
# and replenishes in the background after each acquisition, eliminating
# the cold-start penalty for classifier calls.
#
# The pool is a module-level singleton initialised by SessionPool or
# by calling warm_classifier_pool() from main.py during boot-split.
# ---------------------------------------------------------------------------

_CLASSIFIER_POOL_SIZE = 3  # target warm clients in the pool


class ClassifierPool:
    """Maintains a rotating pool of pre-connected haiku ClaudeSDKClient
    instances for one-shot classifier calls.

    Usage:
        client = await pool.acquire()   # returns a connected client
        try:
            await client.query(prompt)
            ... read response ...
        finally:
            await pool.dispose(client)  # tear down used client, replenish
    """

    # Consecutive-failure threshold for a one-time admin alert. Below this,
    # the pool silently falls back to cold-spawn (adding ~18s to every
    # ambiguous message) which was previously invisible to the operator
    # until manual inspection of logs. Above it, we post once to the
    # admin alert channel so the breakage is not silent. Counter resets
    # on any successful spawn.
    _LIVENESS_FAILURE_THRESHOLD = 5

    def __init__(self, pool_size: int = _CLASSIFIER_POOL_SIZE) -> None:
        self._target_size = pool_size
        self._ready: asyncio.Queue[ClaudeSDKClient] = asyncio.Queue()
        self._spawning = 0  # number of in-flight background spawns
        self._closed = False
        self._replenish_tasks: list[asyncio.Task] = []
        # Liveness tracking — consecutive spawn failures since the last
        # successful spawn. Reset to 0 by _spawn_one on success. The alert
        # fires exactly once per crossing of _LIVENESS_FAILURE_THRESHOLD;
        # _liveness_alerted prevents re-firing every subsequent failure.
        self._consecutive_spawn_failures: int = 0
        self._liveness_alerted: bool = False

    async def warm_up(self) -> None:
        """Pre-spawn pool_size clients concurrently. Call at startup."""
        tasks = [self._spawn_one() for _ in range(self._target_size)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        logger.info(
            "ClassifierPool warmed: %d/%d clients ready", ok, self._target_size
        )

    async def _spawn_one(self) -> bool:
        """Create and connect one haiku client. Returns True on success."""
        if self._closed:
            return False
        self._spawning += 1
        try:
            opts = ClaudeAgentOptions(
                model="haiku",
                allowed_tools=[],
                permission_mode="default",
                setting_sources=[],
                max_turns=1,
            )
            client = ClaudeSDKClient(options=opts)
            await client.connect()
            if self._closed:
                await self._teardown(client)
                return False
            self._ready.put_nowait(client)
            # Success — reset liveness counter so a spurious earlier failure
            # does not carry forward forever.
            self._consecutive_spawn_failures = 0
            self._liveness_alerted = False
            return True
        except Exception as exc:
            logger.warning("ClassifierPool spawn failed: %s", exc)
            self._consecutive_spawn_failures += 1
            # Fire a one-time admin alert after N consecutive failures.
            # _liveness_alerted guard prevents re-firing on every subsequent
            # failure while the outage persists; the flag clears on any
            # success. The alert itself is rate-limited independently inside
            # discord._admin_alert (1/hr per category) so even without the
            # guard the user-visible noise would be bounded, but the guard
            # keeps the log-level intent explicit.
            if (
                self._consecutive_spawn_failures >= self._LIVENESS_FAILURE_THRESHOLD
                and not self._liveness_alerted
            ):
                self._liveness_alerted = True
                try:
                    # Lazy import to avoid circular import at module load —
                    # discord.py imports session.py via haiku_classify.
                    from brendbot.discord import _admin_alert
                    _admin_alert(
                        "classifier_pool_outage",
                        f"ClassifierPool: {self._consecutive_spawn_failures} "
                        f"consecutive spawn failures. Falling back to "
                        f"cold-spawn per call (+~18s latency each). "
                        f"Last error: {type(exc).__name__}: {exc}"[:500],
                    )
                except Exception as alert_exc:
                    logger.debug(
                        "ClassifierPool liveness alert dispatch failed: %s",
                        alert_exc,
                    )
            return False
        finally:
            self._spawning -= 1

    async def acquire(self) -> ClaudeSDKClient:
        """Return a warm, connected client. If the pool is empty, spawns
        one on-demand (fallback to cold path)."""
        try:
            return self._ready.get_nowait()
        except asyncio.QueueEmpty:
            logger.debug("ClassifierPool empty — cold-spawning on demand")
            opts = ClaudeAgentOptions(
                model="haiku",
                allowed_tools=[],
                permission_mode="default",
                setting_sources=[],
                max_turns=1,
            )
            client = ClaudeSDKClient(options=opts)
            await client.connect()
            return client

    async def dispose(self, client: ClaudeSDKClient) -> None:
        """Tear down a used client and kick off background replenishment."""
        await self._teardown(client)
        self._schedule_replenish()

    def _schedule_replenish(self) -> None:
        """Ensure the pool stays topped up without blocking the caller."""
        deficit = self._target_size - (self._ready.qsize() + self._spawning)
        for _ in range(max(0, deficit)):
            task = asyncio.create_task(self._spawn_one(), name="classifier-pool-replenish")
            self._replenish_tasks.append(task)
            # Clean up finished tasks periodically
            self._replenish_tasks = [t for t in self._replenish_tasks if not t.done()]

    @staticmethod
    async def _teardown(client: ClaudeSDKClient) -> None:
        """Best-effort transport close for a consumed one-shot client."""
        try:
            transport = getattr(client, "_transport", None)
            if transport and hasattr(transport, "close"):
                close_result = transport.close()
                if asyncio.iscoroutine(close_result):
                    await close_result
        except Exception:
            pass

    async def drain(self) -> None:
        """Shut down all pooled clients. Call at process exit."""
        self._closed = True
        # Cancel pending replenish tasks
        for task in self._replenish_tasks:
            task.cancel()
        # Drain ready queue
        while not self._ready.empty():
            try:
                client = self._ready.get_nowait()
                await self._teardown(client)
            except asyncio.QueueEmpty:
                break
        logger.info("ClassifierPool drained")


# Module-level singleton — initialised lazily or at boot via warm_classifier_pool()
_classifier_pool: ClassifierPool | None = None


def get_classifier_pool() -> ClassifierPool:
    """Return the module-level ClassifierPool, creating it if needed.
    The pool is not yet warmed — call pool.warm_up() separately."""
    global _classifier_pool
    if _classifier_pool is None:
        _classifier_pool = ClassifierPool()
    return _classifier_pool


async def warm_classifier_pool() -> None:
    """Public entry point for boot-split: create the pool and pre-spawn
    clients. Safe to call from main.py concurrently with gateway connect."""
    pool = get_classifier_pool()
    await pool.warm_up()


# ---------------------------------------------------------------------------
# Haiku ambiguity classifier — warm-pool path
# ---------------------------------------------------------------------------


async def haiku_classify(payload: dict) -> dict:
    """
    Engagement classifier via the Claude Agent SDK (OAuth/subscription path).

    Draws a pre-connected client from the ClassifierPool instead of spawning
    a fresh subprocess per call. If the pool is empty, falls back to cold
    spawn so behaviour is never worse than before.

    Returns {"engage": bool, "reason": str, "tone": str}.
    """
    text = payload.get("message", "")
    recent = payload.get("recent_context", [])
    context_lines = "\n".join(
        f"{m.get('display_name', 'unknown')}: {m.get('text', '')}"
        for m in recent[-5:]
    )

    try:
        from brendbot.discord import _ENGAGEMENT_CFG
        classifier_rules = _ENGAGEMENT_CFG.get("classifier_prompt", "").strip()
    except Exception:
        classifier_rules = ""
    if not classifier_rules:
        classifier_rules = (
            "Should brendbot respond? YES if directly addressed or domain question. "
            "NO if casual chat between others. Reply YES or NO only."
        )

    prompt = (
        f"{classifier_rules}\n\n"
        f"Recent context:\n{context_lines}\n"
        f"New message: {text}\n"
        "Reply YES or NO followed by one tone word from: "
        "funny hype sad weird dumb wholesome neutral. "
        "Example: YES funny  or  NO neutral. Nothing else."
    )

    # Check engagement cache before hitting the classifier pool. The
    # user's inbound `text` is the semantic key — the rules block and
    # context block vary much less, so embedding the whole prompt
    # would over-match on shared boilerplate. Passing `text` alone
    # restricts semantic hits to genuinely similar user messages.
    from brendbot.classifier_cache import get_engage_cache
    engage_cache = get_engage_cache()
    cached = engage_cache.get(prompt, semantic_key=text)
    if cached is not None:
        logger.debug("haiku_classify cache hit")
        return cached

    pool = get_classifier_pool()
    classifier_client: ClaudeSDKClient | None = None
    try:
        classifier_client = await pool.acquire()
        await classifier_client.query(prompt)

        answer_text = ""
        async for msg in classifier_client.receive_messages():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        answer_text = block.text.strip().upper()
                        break
                if answer_text:
                    break
            if isinstance(msg, ResultMessage):
                break

        if not answer_text:
            return {"engage": False, "reason": "sdk:empty", "tone": "neutral"}
        tokens = answer_text.split()
        engage = tokens[0].startswith("Y")
        _valid_tones = {"funny", "hype", "sad", "weird", "dumb", "wholesome", "neutral"}
        tone = tokens[1].lower() if len(tokens) > 1 and tokens[1].lower() in _valid_tones else "neutral"
        result = {"engage": engage, "reason": f"sdk:{answer_text[:8]}", "tone": tone}
        engage_cache.put(prompt, result, semantic_key=text)
        return result
    except Exception as e:
        logger.warning("haiku_classify SDK error: %s", e)
        return {"engage": False, "reason": "error", "tone": "neutral"}
    finally:
        if classifier_client is not None:
            await pool.dispose(classifier_client)


async def content_gate_classify(user_text: str) -> "ContentGateResult":
    """Content-gate classifier: one-shot haiku call that tags which
    content-safety criteria a request trips.

    Draws from the ClassifierPool for reduced latency. On SDK error,
    returns a parse_error result which will fail conservative to REFUSE.
    """
    from brendbot.content_gate import parse_classifier_response, ClassifierResult

    try:
        from brendbot.discord import _ENGAGEMENT_CFG
        classifier_rules = _ENGAGEMENT_CFG.get("content_classifier_prompt", "").strip()
    except Exception:
        classifier_rules = ""
    if not classifier_rules:
        classifier_rules = (
            "Return TRIGGERED: <criteria or none> and REASONING: <explanation>. "
            "Criteria: tragedy_old/mid/new/live, person_satire/neutral/targeted, "
            "frame_fictional/ambiguous/directed. Hard floors: minor_sexual, "
            "wmd_synth, malware, infra_attack, extremist_recruit, directed_incite."
        )

    prompt = (
        f"{classifier_rules}\n\n"
        f"Request to classify:\n{user_text[:2000]}"
    )

    # Check content-gate cache before hitting the classifier pool.
    # `user_text` is the semantic key — the rules block is constant
    # across calls and would dominate a full-prompt embedding.
    from brendbot.classifier_cache import get_content_cache
    content_cache = get_content_cache()
    cached = content_cache.get(prompt, semantic_key=user_text)
    if cached is not None:
        logger.debug("content_gate_classify cache hit")
        return cached

    async def _one_shot(query: str) -> str:
        """Single classifier round-trip. Returns raw text. Callers handle
        parse. Pool acquire/dispose handled per call so the retry path
        can get a fresh client if the first one drifted off-format."""
        classifier_client: ClaudeSDKClient | None = None
        try:
            classifier_client = await get_classifier_pool().acquire()
            await classifier_client.query(query)
            raw = ""
            async for msg in classifier_client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            raw += block.text
                if isinstance(msg, ResultMessage):
                    break
            return raw
        finally:
            if classifier_client is not None:
                await get_classifier_pool().dispose(classifier_client)

    try:
        raw_text = await _one_shot(prompt)
        result = parse_classifier_response(raw_text)

        # 2026-04-16: one retry with explicit format-reminder on parse
        # errors. flag_audit showed the classifier is mostly right on
        # content but drifts on formatting (preamble, code fences,
        # markdown headers). A single reminder typically recovers — this
        # is much cheaper than failing-conservative to REFUSE every time.
        if result.parse_error:
            logger.warning(
                "content_gate_classify parse_error on first attempt; retrying. raw=%r",
                raw_text[:200],
            )
            retry_prompt = (
                f"{classifier_rules}\n\n"
                f"Request to classify:\n{user_text[:2000]}\n\n"
                f"REMINDER: your previous response was unparseable. "
                f"Return EXACTLY two lines with no preamble, no code "
                f"fences, no markdown. First line starts with "
                f"'TRIGGERED:', second line starts with 'REASONING:'."
            )
            retry_raw = await _one_shot(retry_prompt)
            retry_result = parse_classifier_response(retry_raw)
            if not retry_result.parse_error:
                logger.info("content_gate_classify retry succeeded")
                result = retry_result
            else:
                logger.warning(
                    "content_gate_classify retry still parse_error; failing conservative. raw=%r",
                    retry_raw[:200],
                )

        content_cache.put(prompt, result, semantic_key=user_text)
        return result
    except Exception as e:
        logger.warning("content_gate_classify SDK error: %s", e)
        return ClassifierResult(
            criteria={"_parse_error": 2.0},
            reasoning=f"classifier SDK error: {type(e).__name__}",
            parse_error=True,
        )


async def content_gate_cross_check_floor(
    user_text: str,
    suspected_floor: str,
) -> tuple[bool, str]:
    """Second-pass verification for a hard-floor classification.

    When the primary content-gate classifier returns TRIGGERED:
    hard_floor=<name>, a single haiku call is making an irreversible
    refusal decision. Haiku can free-associate on coincidences (e.g.
    marking technical-vocabulary "trigger" as directed incitement, or
    flagging medication dosages as wmd_synth). A dedicated cross-check
    pass — same model, different prompt framing — catches obvious
    false positives without a second model tier.

    Returns (confirmed, raw_response):
      confirmed=True  → the cross-check agrees the text trips the floor.
                        Caller should refuse as planned.
      confirmed=False → the cross-check disputes the floor. Caller should
                        still refuse conservative (floor hits are the
                        strongest gate; erring on the side of refusal is
                        correct), but log the dispute for admin review.

    Bypasses the content-cache deliberately — the whole point is an
    independent check, and the cache would return the same verdict the
    primary call produced.
    """
    # Plain-language floor definitions mirror the classifier prompt so
    # the cross-check evaluates against the same rubric.
    floor_defs = {
        "minor_sexual": "sexual content involving anyone under 18",
        "wmd_synth": "synthesis instructions for nuclear/biological/chemical weapons",
        "malware": "functional malware, exploit code, or ransomware",
        "infra_attack": "attack procedures for critical infrastructure",
        "extremist_recruit": "extremist recruitment or incitement materials",
        "directed_incite": "direct incitement of violence against a real named target",
    }
    definition = floor_defs.get(
        suspected_floor,
        suspected_floor.replace("_", " "),
    )

    prompt = (
        f"A prior content-safety classifier flagged the following text "
        f"as a hard-floor violation of type '{suspected_floor}' "
        f"({definition}).\n\n"
        f"You are the verification pass. Does the text ACTUALLY match "
        f"that floor definition, or was the prior classifier wrong? "
        f"Technical vocabulary (e.g. 'trigger' in software context, "
        f"'payload' in networking, 'exploit' in gaming), historical "
        f"references, fiction framing, and bug reports about the bot "
        f"itself are NOT violations. Only match when the text would "
        f"materially enable or direct the harm the floor is designed "
        f"to prevent.\n\n"
        f"Reply CONFIRMED on one line if the text matches the floor. "
        f"Reply DISPUTED on one line if it does not. No other output.\n\n"
        f"Text to verify:\n{user_text[:2000]}"
    )

    # Patch 1c — agent_core.verifier: wrap the cross-check as a
    # generate/verify pair. The generator runs the SDK call; the
    # verifier checks the response is shaped correctly (first token is
    # CONFIRMED or DISPUTED). Unshaped responses (model repeated the
    # prompt, returned commentary, empty string) are retried up to
    # _MAX_ATTEMPTS times. The NP/Co-NP asymmetry is literal here:
    # generating a classification is expensive; checking it's
    # well-formed is a two-line predicate.
    from brendbot.agent_core.verifier import Check, Verifier

    _MAX_ATTEMPTS = 2

    def _is_shaped(candidate: str) -> "tuple[bool, str]":
        """Verifier predicate: response must lead with a recognised verdict."""
        if not candidate:
            return False, "empty response"
        first = candidate.strip().upper().split()[0] if candidate.strip() else ""
        if first.startswith("CONFIRMED") or first.startswith("DISPUTED"):
            return True, ""
        return False, f"unrecognised verdict token: {first[:20]!r}"

    verifier = Verifier[str]([
        Check(name="verdict_shape", predicate=_is_shaped),
    ])

    pool = get_classifier_pool()

    async def _one_call() -> str:
        classifier_client: ClaudeSDKClient | None = None
        try:
            classifier_client = await pool.acquire()
            await classifier_client.query(prompt)
            raw_text = ""
            async for msg in classifier_client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            raw_text += block.text
                if isinstance(msg, ResultMessage):
                    break
            return raw_text.strip()[:400]
        finally:
            if classifier_client is not None:
                await pool.dispose(classifier_client)

    last_candidate = ""
    try:
        for attempt in range(_MAX_ATTEMPTS):
            candidate = await _one_call()
            last_candidate = candidate
            result = verifier.verify(candidate)
            if result.ok:
                first_token = candidate.upper().split()[0] if candidate else ""
                confirmed = not first_token.startswith("DISPUTED")
                return confirmed, candidate
            logger.debug(
                "content_gate_cross_check_floor attempt %d failed verifier: %s",
                attempt + 1,
                ", ".join(f.reason for f in result.failures),
            )
    except Exception as exc:
        logger.warning("content_gate_cross_check_floor SDK error: %s", exc)
        # Fail-conservative: keep the refusal on classifier error.
        return True, f"cross_check_error:{type(exc).__name__}"

    # All attempts produced unshaped responses. Fail-conservative:
    # treat as CONFIRMED so the refusal stands. Dispute log captures
    # the unparseable last candidate for audit.
    logger.warning(
        "content_gate_cross_check_floor exhausted %d attempts without a "
        "shaped verdict; defaulting to CONFIRMED",
        _MAX_ATTEMPTS,
    )
    return True, f"unshaped_after_{_MAX_ATTEMPTS}_attempts:{last_candidate[:80]}"


async def flagged_generate(
    wrapped_message: str,
    model: str,
    cwd: str | None = None,
) -> str:
    """One-shot generation via the flagged-path model (default
    claude-sonnet-4-20250514). Used by the content gate's FLAG outcome
    when a request lands in the 2-of-3 weighted band and needs the
    looser-safety model per Anthropic's documented escape hatch for
    Sonnet 4.5/4.6 training-layer refusals.

    Returns the generated text (joined across any TextBlocks), prefixed
    with the [flagged] branch tag. On SDK error or empty response,
    returns a fallback explanation string so the caller never gets None.
    The branch tag is stripped by _fire_on_text at dispatch time and
    routed to branch_audit.jsonl.

    NOT a full session — no tool calls, no context continuity, no
    memory. The flagged model only sees the current request and the
    flagged-path system prompt telling it this is an operator-sanctioned
    generation. Follow-up messages from the same user go through the
    normal gate again.
    """
    system_prompt = (
        "You are responding on a flagged content path. An operator-level "
        "content gate has reviewed this request and determined it lands "
        "in a band that warrants the looser-safety model (you) rather "
        "than outright refusal. The request may involve historical "
        "figures, fictional framings, or other content that requires "
        "nuanced judgment. Generate a direct response to the request. "
        "Do not re-refuse based on general safety concerns — the gate "
        "already made that decision. Apply your own judgment only if "
        "the request crosses into content you genuinely cannot produce. "
        "Keep output concise."
    )

    flagged_client: ClaudeSDKClient | None = None
    try:
        opts = ClaudeAgentOptions(
            model=model,
            allowed_tools=[],
            permission_mode="default",
            setting_sources=[],
            max_turns=1,
            system_prompt=system_prompt,
        )
        if cwd:
            opts.cwd = cwd  # type: ignore
        flagged_client = ClaudeSDKClient(options=opts)
        await flagged_client.connect()
        await flagged_client.query(wrapped_message)

        raw_parts: list[str] = []
        async for msg in flagged_client.receive_messages():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        raw_parts.append(block.text)
            if isinstance(msg, ResultMessage):
                break

        raw = "".join(raw_parts).strip()
        if not raw:
            return "[flagged] (flagged path produced no output)"
        return f"[flagged] {raw}"
    except Exception as e:
        logger.warning("flagged_generate SDK error: %s", e)
        return f"[flagged] (flagged path error: {type(e).__name__})"
    finally:
        if flagged_client is not None:
            try:
                transport = getattr(flagged_client, "_transport", None)
                if transport and hasattr(transport, "close"):
                    close_result = transport.close()
                    if asyncio.iscoroutine(close_result):
                        await close_result
            except Exception:
                pass


def _load_template(name: str) -> str:
    """Load a soul template file."""
    path = PROJECT_ROOT / name
    if path.exists():
        return path.read_text()
    return "You are a helpful Discord bot.\nTo reply, run: {{ send_command }}"


def _render(template: str, variables: dict[str, str]) -> str:
    """Simple {{ var }} substitution."""
    def replacer(m: re.Match) -> str:
        return variables.get(m.group(1).strip(), m.group(0))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, template)


# ---------------------------------------------------------------------------
# Single SDK session
# ---------------------------------------------------------------------------

# Hard restart when input tokens exceed this value.
_CONTEXT_REFRESH_THRESHOLD = 400_000

# Soft warning threshold.
_CONTEXT_SOFT_WARNING = 320_000

# ── Cognitive load model (Phase 3 #1A) ────────────────────────────────────
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


class Session:
    """One Claude Agent SDK session for a contact or channel."""

    def __init__(
        self,
        key: str,
        tier: str,
        cwd: str,
        model: str = "sonnet",
        on_text: Optional[Any] = None,
        on_text_edit: Optional[Any] = None,
        chat_id: str = "",
        guild_id: str = "",
    ) -> None:
        self.key = key
        self.tier = tier
        self.cwd = cwd
        self._model = model
        self._on_text = on_text
        self._on_text_edit = on_text_edit
        self._chat_id = chat_id
        # Guild snowflake for multi-server user_registry filtering.
        # Stashed here so _restart_session can preserve it across restarts
        # without re-plumbing it through every caller.
        self._guild_id: str = guild_id
        self._client: Optional[ClaudeSDKClient] = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._error_count = 0
        self.running = False
        self.current_sender_tier: str | None = None
        # Address level for the current turn (low/moderate/high). Set by
        # SessionPool.route_message before each inject. Read by
        # _permission_check to enforce FUSED-CORE Budget Throttle caps.
        self.current_address_level: str = "high"
        self._last_input_tokens: int = 0
        self._context_state: str = "normal"
        self._soft_warning_sent: bool = False
        self._on_needs_restart: Optional[Any] = None
        self._react_fn: Optional[Any] = None
        self._react_channel: str = ""
        self._react_msg_id: str = ""
        self._react_phase: int = 0
        self._HOUSEKEEPING_PATH_FRAGMENTS = frozenset({
            "session.py", "discord.py", "__init__.py",
            "CLAUDE.md", "MEMORY.md", "ai-image-shortfalls.md",
            "CONTEXT_SUMMARY.md",
        })
        self._turn_text_buffer: list[str] = []
        self._turn_used_send_discord: bool = False
        self._turn_tool_called: bool = False  # True once any ToolUseBlock seen this turn
        # Phantom-turn discriminator flags (Phase 3 phantom-fix-v2).
        # The silent-drop fallback was firing on two distinct failure modes:
        # (a) intentional silent drops where the model correctly declined
        # to respond per the soul rules (case A), and (b) true broken turns
        # where no output arrived at all (case B). These two flags plus
        # ResultMessage.stop_reason are the three-way discriminator:
        #   Case A: any_assistant_msg_seen=True, content_block_seen=True,
        #           stop_reason='end_turn' → suppress fallback
        #   Case B: any_assistant_msg_seen=False OR content_block_seen=False,
        #           stop_reason=None or error → fire fallback
        #   Case C: is_housekeeping=True → suppress (existing behavior)
        self._turn_any_assistant_msg_seen: bool = False
        self._turn_any_content_block_seen: bool = False
        # Discord sender id of the user whose message triggered the current
        # turn. Set by _pool_inject just before session.inject(); consumed in
        # the ResultMessage handler if _turn_tool_called was True, to record
        # the follow-up signal state so iteration replies ("no, try again")
        # from the same user get a score boost. None means no follow-up
        # signal will be recorded for this turn (housekeeping, restart, etc).
        self._turn_sender_id: str | None = None
        # Content gate state (phase 4)
        self._flagged_count: int = 0
        # Per-turn: set by apply_content_gate when an admin bypass was
        # invoked, consumed by _fire_on_text to prepend [bypass] tag to
        # the response text before extract_branch_tag strips and logs.
        self._turn_bypass_pending: bool = False
        self._MODULE_EMOTES: dict[str, str] = {
            "BUILDSCI":    "🏗️",
            "IMAGEGEN":    "🎨",
        }
        self._NEUTRAL_EMOTE = "⬛"
        self._MAX_MODULE_EMOTES = 3
        self._unreact_fn: Optional[Any] = None
        self._active_reactions: set[str] = set()
        self._module_emote_count: int = 0
        self._turn_log: list[dict] = []
        self._tool_call_count: int = 0
        self._completed_turn_count: int = 0
        self._turn_modules_queried: set[str] = set()
        self._turn_kb_query_used: bool = False
        # ── Cognitive load tracking (Phase 3 #1A) ─────────────────────
        # Running cumulative load across turns. Reset only on full restart.
        # Updated in _handle() ResultMessage branch from per-turn counts.
        self._cumulative_load: float = 0.0
        self._cumulative_bash_calls: int = 0
        self._cumulative_haiku_invocations: int = 0
        self._cumulative_other_tools: int = 0
        # Per-turn counters reset before each inject in route_message.
        self._turn_bash_calls: int = 0
        self._turn_other_tool_calls: int = 0
        # Patch 1a — agent_core.budgets halting-problem defence. A typed
        # per-turn BudgetState enforced in _permission_check on every
        # tool call. step_cap duplicates _tool_call_count enforcement
        # (kept separately for back-compat) and time_cap_s is the new
        # wall-clock cap with no prior enforcement in the response path.
        # Reset at turn boundary in SessionPool.route_message.
        self._turn_budget: Optional[Any] = None
        # Shallow rest tracking (Phase 3 #1B). Set True after a shallow rest
        # cycle fires. Cleared when cumulative load grows by at least
        # SHALLOW_RECOVERY_DELTA past the previous trigger point — prevents
        # the rest cycle from re-firing on every subsequent turn.
        self._shallow_rested: bool = False
        self._shallow_rest_count: int = 0
        # Flag for the next inject: if True, suppress both the response
        # dispatch AND the silent-drop fallback for the resulting turn.
        # Used by _trigger_shallow_rest to inject housekeeping that the
        # model is told not to respond to. Cleared after one turn.
        self._next_turn_is_housekeeping: bool = False
        # Phase 3 #2A — session-start timestamp, used as ts_start when an
        # episode row is written at restart time. Reset implicitly on every
        # restart because SessionPool._restart_session creates a fresh
        # Session instance via _create(), which re-runs __init__.
        from datetime import datetime as _dt
        self._session_started_at: str = _dt.now().isoformat(timespec="seconds")
        # Aggregate domains seen across this session segment, for the
        # episode write at restart time.
        self._session_domains_seen: set[str] = set()
        # Turn metadata for feedback logging — set by SessionPool.route_message
        # before each inject and read by _fire_on_text after send_message
        # returns the bot_message_id.
        self._turn_user_message_id: str = ""
        self._turn_user_text: str = ""
        self._turn_score: float | None = None
        self._turn_domains: list[str] = []
        # Serializes turn boundaries: ensures _fire_on_text + state reset
        # for turn N completes before query() for turn N+1 dispatches.
        # Fixes the duplicate `turn complete` race observed when two messages
        # land while a result is still being handled.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        # ── Streaming response state ──────────────────────────────────
        # On text-only turns (no tool calls), TextBlocks are streamed to
        # Discord via message edits as they arrive. The first chunk creates
        # a new message; subsequent chunks edit it on a 400ms timer to
        # stay under Discord's rate limit (~5 edits / 5s / channel).
        self._stream_msg_id: str | None = None   # Discord message being streamed
        self._stream_buffer: str = ""             # accumulated text so far
        self._stream_timer: Optional[asyncio.Task] = None  # pending edit task
        self._stream_dirty: bool = False          # buffer changed since last edit
        # Synchronous flag set the instant the first _stream_chunk task is
        # created — before the async Discord send completes and populates
        # _stream_msg_id. The ResultMessage handler checks this instead of
        # _stream_msg_id to decide whether streaming was initiated, closing
        # the race window where ResultMessage arrives before the first-chunk
        # send returns.
        # Per-turn haiku-invoked flag. Set by route_message before each
        # inject from the haiku_invoked parameter passed through from
        # discord.py. Read by _fire_on_text / _fire_on_text_streamed for
        # flow-class and fabrication-risk diagnostics in log_bot_response.
        self._turn_haiku_invoked: bool = False
        # Phase 1 — prompt-cache observability. The CLI surfaces these on
        # ResultMessage.usage; we stash them at result time so both
        # _fire_on_text and _fire_on_text_streamed can log per-turn cache
        # behaviour to bot_responses.jsonl. None means the SDK/API didn't
        # return a usage block for this turn (e.g. housekeeping, restart,
        # SDK error); log_bot_response omits the fields entirely in that
        # case so downstream consumers never see partial rows.
        self._turn_input_tokens: int | None = None
        self._turn_cache_read_tokens: int | None = None
        self._turn_cache_creation_tokens: int | None = None
        # Phase 2a — stage-timing instrumentation. time.monotonic() stamps
        # at four points in the turn lifecycle; _fire_on_text computes
        # deltas in ms for log_bot_response. None means the stage didn't
        # execute this turn (e.g. DMs skip the engagement gate, housekeeping
        # turns skip first-token). Reset in route_message at turn start.
        self._turn_t_received: float | None = None           # on_message entry in discord.py
        self._turn_t_engage_gate_done: float | None = None   # engagement decision complete
        self._turn_t_content_gate_done: float | None = None  # content gate classifier returned
        self._turn_t_first_token: float | None = None        # first AssistantMessage TextBlock
        # Patch A — deduplicated recall injection.
        # Tracks episode IDs (SQLite rowid from episodes table) that have
        # already been injected into this session. On subsequent turns,
        # query_episodes may return the same episode(s) — skipping them
        # prevents re-injecting identical recall fragments every message,
        # which was adding 200–600 chars of context to every turn with
        # zero marginal information gain. Reset on session restart via
        # fresh Session.__init__ in _restart_session → _create.
        self._recalled_episode_ids: set[int] = set()
        self._stream_initiated: bool = False
        # Future that resolves once the first-chunk send has completed
        # (successfully or not). _fire_on_text_streamed awaits this so it
        # doesn't finalize before _stream_msg_id is populated.
        self._stream_first_chunk_done: Optional[asyncio.Event] = None
        # Stage 4 — deferred KB index expansion. True after the full
        # FUSED-CORE MODULES + KNOWLEDGE QUERY TOOL block has been
        # injected into this session (happens on the first domain-matched
        # message). Sessions that never touch a knowledge module never
        # incur the full-index cost.
        self._kb_index_expanded: bool = False
        # Premise Check enforcement — tracks which knowledge modules have
        # had their structured content (defs/facts/thms) pre-fetched and
        # injected into this session. On every message whose domain_hint
        # contains a module NOT in this set, the module's content is
        # auto-injected as <grounded_facts> housekeeping before the user
        # message is processed. This converts the documented Premise Check
        # gate from honor-system ("the model will query kb-query") to
        # enforced-by-construction ("the module content is already in the
        # context window when the model generates the answer").
        self._grounded_modules: set[str] = set()

    async def start(self) -> None:
        opts = self._build_options()
        logger.info(
            "Creating session %s (tier=%s, model=%s, perm=%s, cwd=%s)",
            self.key, self.tier, self._model, opts.permission_mode, self.cwd,
        )
        try:
            self._client = ClaudeSDKClient(options=opts)
            await self._client.connect()
        except Exception as e:
            logger.error("Failed to start Claude session %s: %s", self.key, e)
            raise
        self.running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"session:{self.key}")
        logger.info("Session started: %s", self.key)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._kill()

    async def inject(self, text: str, housekeeping: bool = False) -> None:
        """Queue a message for the next turn.

        housekeeping=True marks this inject as a context-only injection
        that the model is told not to respond to. The result handler will
        suppress both the normal text dispatch and the silent-drop fallback
        for the resulting turn. Used for: startup memory fragments, ref
        block injection, shallow rest cycles. The flag is consumed atomically
        by _run_loop right before query(), so multiple housekeeping injects
        queued in a row don't race against each other.
        """
        await self._queue.put((text, housekeeping))

    async def apply_content_gate(
        self,
        wrapped_text: str,
        raw_user_text: str,
        tier: str,
        sender_id: str,
        message_id: str,
    ) -> str:
        """Run the content gate on an incoming user message and return
        one of: 'inject' (caller should call session.inject(wrapped)),
        'handled' (caller should NOT inject — response already dispatched
        via _fire_on_text or the flagged path). Non-returning paths call
        _fire_on_text directly so the normal inject+query cycle is
        bypassed entirely for REFUSE / FLOOR_HIT / FLAG / BYPASS.

        Gate routing:
          - BYPASS (admin italic *brend* token): run classifier in shadow
            mode, check hard floors, if clean inject normally with a
            [bypass] tag prepended to wrapped; if hard floor hit, refuse.
          - normal path: run classifier, decide outcome:
              PASS → return 'inject' (normal flow)
              FLAG → reroute to flagged_generate on sonnet-4, decrement
                     session budget, dispatch via _fire_on_text, return
                     'handled'
              REFUSE or FLOOR_HIT → dispatch local refusal explanation
                     via _fire_on_text, return 'handled'

        Gate failures (classifier spawn errors etc) fail conservative to
        REFUSE with an explanation noting the classifier error.
        """
        from brendbot.content_gate import (
            detect_admin_bypass,
            decide_outcome,
            format_refusal_explanation,
            Outcome,
        )
        from brendbot.feedback import (
            log_flag_event,
            log_bypass_event,
            log_disputed_floor_event,
        )

        try:
            from brendbot.discord import _ENGAGEMENT_CFG
            gate_cfg = _ENGAGEMENT_CFG.get("content_gate", {}) or {}
        except Exception:
            gate_cfg = {}

        hard_floors_list = gate_cfg.get("hard_floors", []) or []
        hard_floors: set[str] = set(hard_floors_list)
        outcomes_cfg = gate_cfg.get("outcomes", {}) or {}
        pass_thr = float(outcomes_cfg.get("pass_threshold", 0.5))
        flag_thr = float(outcomes_cfg.get("flag_threshold", 1.5))
        refuse_thr = float(outcomes_cfg.get("refuse_threshold", 1.5))

        flagged_cfg = gate_cfg.get("flagged_path", {}) or {}
        # Resolution order: env var (via Config.claude_flagged_model) > yaml >
        # hardcoded fallback. The env var lets operators pin a fresh model
        # without editing yaml and prevents silently-unreachable dated model
        # strings from pinning FLAG-band traffic to a 410 Gone revision.
        from brendbot.config import get_config as _get_cfg
        _cfg = _get_cfg()
        flagged_model = (
            _cfg.claude_flagged_model
            or flagged_cfg.get("model")
            or "claude-sonnet-4-20250514"
        )
        flagged_cap = int(flagged_cfg.get("max_per_session", 2))

        bypass_cfg = gate_cfg.get("admin_bypass", {}) or {}
        bypass_enabled = bool(bypass_cfg.get("enabled", True))
        bypass_enforces_floors = bool(bypass_cfg.get("hard_floors_still_enforced", True))

        # Admin bypass detection runs before the classifier spawn so the
        # common non-bypass path skips the extra check cost.
        is_bypass = (
            bypass_enabled
            and detect_admin_bypass(raw_user_text, tier)
        )

        # Run the classifier. For bypass path, this is shadow-mode
        # (recording would-have-been decisions for audit). For normal
        # path, this is the primary gate decision.
        try:
            classifier_result = await content_gate_classify(raw_user_text)
        except Exception as exc:
            logger.warning(
                "[%s] content_gate_classify unexpected error: %s",
                self.key, exc,
            )
            from brendbot.content_gate import ClassifierResult
            classifier_result = ClassifierResult(
                criteria={"_parse_error": 2.0},
                reasoning=f"classifier error: {type(exc).__name__}",
                parse_error=True,
            )

        shadow_outcome = decide_outcome(
            classifier_result, hard_floors, pass_thr, flag_thr, refuse_thr,
        )

        if is_bypass:
            # Admin bypass path. Hard floors are the only thing that can
            # still refuse. Everything else generates on session model
            # with a [bypass] branch tag.
            hard_floor_hit: str | None = None
            if bypass_enforces_floors and classifier_result.hard_floor in hard_floors:
                hard_floor_hit = classifier_result.hard_floor

            if hard_floor_hit:
                refusal = format_refusal_explanation(classifier_result)
                logger.info(
                    "[%s] admin bypass + hard floor hit (%s) — refusing",
                    self.key, hard_floor_hit,
                )
                asyncio.create_task(self._fire_on_text(refusal))
                log_bypass_event(
                    channel_id=self._chat_id,
                    user_message_id=message_id,
                    user_text=raw_user_text,
                    admin_sender_id=sender_id,
                    tier=tier,
                    would_have_tripped=classifier_result.to_dict()["criteria"],
                    would_have_summed=classifier_result.weighted_sum,
                    would_have_outcome=shadow_outcome.value,
                    hard_floor_hit=hard_floor_hit,
                    bot_message_id=None,
                )
                return "handled"

            # Bypass permitted — prepend [bypass] tag to the wrapped message
            # so the normal session generates with the tag. _fire_on_text
            # will strip and route to branch_audit.jsonl automatically
            # via the extract_branch_tag flow that already handles
            # [rejected]/[searching]/[unverified].
            # The session model stays the same (NOT rerouted).
            logger.info(
                "[%s] admin bypass invoked (shadow outcome=%s, sum=%.2f)",
                self.key, shadow_outcome.value, classifier_result.weighted_sum,
            )
            log_bypass_event(
                channel_id=self._chat_id,
                user_message_id=message_id,
                user_text=raw_user_text,
                admin_sender_id=sender_id,
                tier=tier,
                would_have_tripped=classifier_result.to_dict()["criteria"],
                would_have_summed=classifier_result.weighted_sum,
                would_have_outcome=shadow_outcome.value,
                hard_floor_hit=None,
                bot_message_id=None,  # will be known after dispatch; audit
                                       # entry is written pre-dispatch so
                                       # mid-pipeline failures still log
            )
            # Caller will inject the wrapped text — we don't tag the
            # wrapped payload itself because the tag convention is on
            # the bot's response, not the injected user message. The
            # session's next turn will produce a normal response; the
            # [bypass] branch tag is applied by _fire_on_text via a
            # sentinel we set on the session for this turn.
            self._turn_bypass_pending = True
            return "inject"

        # Normal path: no bypass token, decide by classifier outcome.
        if shadow_outcome == Outcome.PASS:
            return "inject"

        if shadow_outcome == Outcome.FLAG:
            # Check channel-level gate2_bypass before routing to flagged path.
            channel_overrides = gate_cfg.get("channel_overrides", {}) or {}
            ch_cfg = channel_overrides.get(str(self._chat_id), {}) or {}
            if ch_cfg.get("gate2_bypass", False):
                logger.info(
                    "[%s] FLAG outcome but gate2_bypass active for channel — treating as PASS",
                    self.key,
                )
                return "inject"

            # Budget cap check.
            if self._flagged_count >= flagged_cap:
                refusal = (
                    "can't do that one — flagged-path budget exhausted "
                    f"for this session ({flagged_cap}/session). "
                    "restart the session to reset."
                )
                logger.info(
                    "[%s] FLAG outcome but budget exhausted (%d/%d)",
                    self.key, self._flagged_count, flagged_cap,
                )
                asyncio.create_task(self._fire_on_text(refusal))
                return "handled"

            self._flagged_count += 1
            logger.info(
                "[%s] FLAG outcome (sum=%.2f, count=%d/%d) — rerouting to %s",
                self.key, classifier_result.weighted_sum,
                self._flagged_count, flagged_cap, flagged_model,
            )

            # One-shot flagged generation in the background. Audit is
            # written before dispatch so any failure in flagged_generate
            # still produces a log entry.
            log_flag_event(
                channel_id=self._chat_id,
                user_message_id=message_id,
                user_text=raw_user_text,
                admin_sender_id=sender_id,
                tier=tier,
                criteria_tripped=dict(classifier_result.criteria),
                weighted_sum=classifier_result.weighted_sum,
                flagged_model=flagged_model,
                bot_message_id=None,
                session_flag_count=self._flagged_count,
            )

            async def _flagged_task() -> None:
                try:
                    response = await flagged_generate(
                        wrapped_message=wrapped_text,
                        model=flagged_model,
                        cwd=None,
                    )
                    await self._fire_on_text(response)
                except Exception as exc:
                    logger.warning(
                        "[%s] flagged path failed: %s", self.key, exc,
                    )
                    await self._fire_on_text(
                        "[flagged] (flagged path failed to produce output)"
                    )
            asyncio.create_task(_flagged_task())
            return "handled"

        # REFUSE or FLOOR_HIT. FLOOR_HIT in particular is a single-model
        # irreversible refusal based on a list-match classification — the
        # class of error most worth catching with a second pass. Run the
        # cross-check; the refusal still fires regardless, but dispute
        # gets logged for admin review so false-positive floor matches
        # (technical-vocabulary "trigger", "payload", "exploit" etc.)
        # can be tuned out of the primary classifier.
        suspected_floor = classifier_result.hard_floor
        if shadow_outcome == Outcome.FLOOR_HIT and suspected_floor:
            confirmed, cross_text = await content_gate_cross_check_floor(
                raw_user_text, suspected_floor,
            )
            if not confirmed:
                logger.info(
                    "[%s] floor-hit cross-check DISPUTED (floor=%s)",
                    self.key, suspected_floor,
                )
                log_disputed_floor_event(
                    channel_id=self._chat_id,
                    user_message_id=message_id,
                    user_text=raw_user_text,
                    sender_id=sender_id,
                    tier=tier,
                    suspected_floor=suspected_floor,
                    cross_check_response=cross_text,
                    bot_message_id=None,
                )

        refusal = format_refusal_explanation(classifier_result)
        logger.info(
            "[%s] content gate %s (sum=%.2f, floor=%s)",
            self.key, shadow_outcome.value,
            classifier_result.weighted_sum,
            classifier_result.hard_floor,
        )
        asyncio.create_task(self._fire_on_text(refusal))
        return "handled"

    async def _stream_chunk(self, text: str) -> None:
        """Handle an incoming TextBlock for the streaming path.

        First chunk: send a new Discord message via _on_text to get a
        message ID. Subsequent chunks: schedule a 400ms debounced edit.
        Only active for text-only turns (no tool calls yet this turn).
        """
        self._stream_buffer += text
        self._stream_dirty = True

        if self._stream_msg_id is None and self._on_text and self._chat_id:
            # First chunk — send immediately to get message ID and show
            # the user that the bot is responding. Truncate to 2000 chars
            # (Discord limit); the final edit will handle overflow.
            try:
                msg_id = await self._on_text(self._chat_id, self._stream_buffer[:2000])
                if msg_id:
                    self._stream_msg_id = msg_id
                    self._stream_dirty = False
            except Exception as exc:
                logger.debug("[%s] stream first-chunk send failed: %s", self.key, exc)
            finally:
                # Signal that the first-chunk send has resolved (success or
                # failure). _fire_on_text_streamed awaits this event so it
                # doesn't finalize before _stream_msg_id is populated.
                if self._stream_first_chunk_done:
                    self._stream_first_chunk_done.set()
        elif self._stream_msg_id and self._on_text_edit:
            # Subsequent chunks — debounce edits at 400ms intervals
            if self._stream_timer is None or self._stream_timer.done():
                self._stream_timer = asyncio.create_task(
                    self._stream_flush_after_delay(0.4)
                )

    async def _stream_flush_after_delay(self, delay: float) -> None:
        """Wait `delay` seconds then edit the streamed message if dirty."""
        await asyncio.sleep(delay)
        await self._stream_flush()

    async def _stream_flush(self) -> None:
        """Edit the streamed Discord message with the current buffer."""
        if (self._stream_msg_id
                and self._stream_dirty
                and self._on_text_edit
                and self._chat_id):
            try:
                await self._on_text_edit(
                    self._chat_id,
                    self._stream_msg_id,
                    self._stream_buffer[:2000],
                )
                self._stream_dirty = False
            except Exception as exc:
                logger.debug("[%s] stream edit failed: %s", self.key, exc)

    def _stream_reset(self) -> None:
        """Reset streaming state between turns."""
        if self._stream_timer and not self._stream_timer.done():
            self._stream_timer.cancel()
        self._stream_msg_id = None
        self._stream_buffer = ""
        self._stream_timer = None
        self._stream_dirty = False
        self._stream_initiated = False
        self._stream_first_chunk_done = None

    async def _stream_reset_with_cleanup(self) -> None:
        """Reset streaming state, cleaning up any orphaned Discord message.

        When a tool fires after streaming has started, there's already a
        partial message on Discord. Wait for the first-chunk send to
        resolve, then edit the orphaned message to indicate a tool-augmented
        response is coming. This prevents stale partial text from lingering.
        """
        if self._stream_first_chunk_done:
            try:
                await asyncio.wait_for(self._stream_first_chunk_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.debug("[%s] stream cleanup: first-chunk send timed out", self.key)
        if self._stream_msg_id and self._on_text_edit and self._chat_id:
            try:
                await self._on_text_edit(
                    self._chat_id,
                    self._stream_msg_id,
                    "\u2026",  # ellipsis — will be replaced by the tool-turn response
                )
            except Exception:
                pass
        self._stream_reset()

    def _compute_stage_timings_ms(self) -> dict[str, float] | None:
        """Phase 2a — compute per-stage wall-time deltas in milliseconds
        from the four _turn_t_* stamps. Returns None if no stamps were
        collected (e.g. legacy caller that didn't pass recv_ts). Missing
        stamps produce None for the affected delta, which log_bot_response
        filters out before writing. t_first_token_to_complete is stamped
        here (at log time) rather than earlier so it captures the full
        stream-finalize + Discord-send latency."""
        now = time.monotonic()
        t_recv = self._turn_t_received
        t_engage = self._turn_t_engage_gate_done
        t_gate = self._turn_t_content_gate_done
        t_first = self._turn_t_first_token
        timings: dict[str, float | None] = {
            "t_receive_to_engage_gate": (
                (t_engage - t_recv) * 1000.0
                if (t_recv is not None and t_engage is not None) else None
            ),
            "t_engage_gate_to_content_gate": (
                (t_gate - t_engage) * 1000.0
                if (t_engage is not None and t_gate is not None) else None
            ),
            "t_content_gate_to_first_token": (
                (t_first - t_gate) * 1000.0
                if (t_gate is not None and t_first is not None) else None
            ),
            "t_first_token_to_complete": (
                (now - t_first) * 1000.0
                if t_first is not None else None
            ),
        }
        # Drop None entries; if every entry is None return None so callers
        # can skip the field entirely.
        result = {k: v for k, v in timings.items() if v is not None}
        return result or None

    async def _fire_on_text_streamed(self, text: str) -> None:
        """Finalize a streamed response: do the last edit, then audit-log.

        Like _fire_on_text but skips the initial send because the message
        was already posted during streaming. Uses the stored _stream_msg_id
        for audit correlation.

        Awaits _stream_first_chunk_done so that if ResultMessage arrives
        before the first-chunk Discord send completes, we don't race past
        it and accidentally fall through to _fire_on_text (the pre-fix
        double-message bug).
        """
        from brendbot.feedback import (
            extract_branch_tag,
            log_bot_response,
            log_branch_audit,
        )
        # Wait for the first-chunk send to complete so _stream_msg_id is
        # populated. Without this, a fast ResultMessage can arrive before
        # the Discord API returns, and _stream_msg_id would be None.
        if self._stream_first_chunk_done:
            try:
                await asyncio.wait_for(self._stream_first_chunk_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("[%s] stream finalize: first-chunk send timed out", self.key)

        # Cancel any pending edit timer and do one final flush
        if self._stream_timer and not self._stream_timer.done():
            self._stream_timer.cancel()

        if self._turn_bypass_pending and not text.lstrip().startswith("["):
            text = f"[bypass] {text}"
        # Pre-send fabrication-risk injection — same policy as the
        # non-streaming path in _fire_on_text.
        text = self._maybe_prepend_uncertain(text)

        async with self._turn_lock:
            branch_tag, stripped = extract_branch_tag(text)
            # Final edit with the complete, tag-stripped text
            if self._on_text_edit and self._stream_msg_id and self._chat_id:
                try:
                    await self._on_text_edit(
                        self._chat_id,
                        self._stream_msg_id,
                        stripped[:2000],
                    )
                except Exception:
                    pass
            bot_message_id = self._stream_msg_id
            self._stream_reset()
            if not bot_message_id:
                # First-chunk send failed — stream never reached Discord.
                # Fall back to a fresh send so the response isn't lost.
                logger.debug("[%s] stream finalize: no msg_id, falling back to fresh send", self.key)
                try:
                    bot_message_id = await self._on_text(self._chat_id, stripped[:2000])
                except Exception as exc:
                    logger.error("[%s] stream fallback send failed: %s", self.key, exc)
                    return
                if not bot_message_id:
                    return
            if self._turn_sender_id:
                try:
                    from brendbot.user_registry import record_engagement
                    record_engagement(self._turn_sender_id)
                except Exception:
                    pass
            from brendbot.discord import record_bot_spoke
            record_bot_spoke(self._chat_id)
            log_bot_response(
                channel_id=self._chat_id,
                bot_message_id=bot_message_id,
                user_message_id=self._turn_user_message_id,
                user_text=self._turn_user_text,
                score=self._turn_score,
                domains=self._turn_domains,
                address_level=self.current_address_level,
                branch_tag=branch_tag,
                modules_queried=sorted(self._turn_modules_queried),
                haiku_invoked=self._turn_haiku_invoked,
                input_tokens=self._turn_input_tokens,
                cache_read_input_tokens=self._turn_cache_read_tokens,
                cache_creation_input_tokens=self._turn_cache_creation_tokens,
                stage_timings_ms=self._compute_stage_timings_ms(),
            )
            if branch_tag:
                log_branch_audit(
                    channel_id=self._chat_id,
                    bot_message_id=bot_message_id,
                    branch=branch_tag,
                    response_text=stripped,
                )

    def _maybe_prepend_uncertain(self, text: str) -> str:
        """Pre-send fabrication-risk check. If the triadic pattern fires
        (haiku_invoked + domain match + no kb-query + no branch tag), the
        response sits on the highest-risk profile for fabrication documented
        in feedback.log_bot_response. Prepend [uncertain] so the response
        reaches Discord with the audit tag applied rather than relying on
        retrospective log review.

        The retrospective audit record in bot_responses.jsonl is still
        written downstream by log_bot_response — the derived
        fabrication_risk flag there preserves the training signal on the
        un-intervened pattern. The chat-bound text just gets the
        [uncertain] prefix that the model would have self-applied if it
        were reliably applying the three-branch classifier.

        Called from both _fire_on_text and _fire_on_text_streamed after
        the bypass-tag check and before extract_branch_tag, so if the
        injected tag makes it to extract_branch_tag it gets stripped
        from the visible text and routed to branch_audit.jsonl normally.
        """
        if text.lstrip().startswith("["):
            return text
        triadic = (
            self._turn_haiku_invoked
            and bool(self._turn_domains)
            and not self._turn_modules_queried
        )
        if triadic:
            return f"[uncertain] {text}"
        return text

    async def _fire_on_text(self, text: str) -> None:
        """Send a turn's response to Discord with feedback-log side effects.

        Order: extract leading branch tag → strip from chat-bound text →
        call on_text(chat_id, stripped_text) which returns the posted
        message ID → log to bot_responses.jsonl → if there was a tag,
        also log to branch_audit.jsonl.

        Holds self._turn_lock for the entire send + log sequence so the
        next query() in _run_loop cannot dispatch turn N+1 until turn N's
        feedback writes have completed. This is the actual serialization
        the duplicate `turn complete` race fix requires — wrapping
        _client.query() alone in _run_loop is insufficient because
        _fire_on_text is fire-and-forget from _receive_loop.

        All log writes are best-effort; failures never break the chat path.
        """
        from brendbot.feedback import (
            extract_branch_tag,
            log_bot_response,
            log_branch_audit,
        )
        # Admin bypass tag injection: if apply_content_gate detected the
        # *brend* token and set _turn_bypass_pending, prepend [bypass] to
        # any response that doesn't already carry a branch tag. Consumed
        # once per turn — reset by the ResultMessage handler alongside
        # other per-turn flags.
        if self._turn_bypass_pending and not text.lstrip().startswith("["):
            text = f"[bypass] {text}"
        # Pre-send fabrication-risk injection (see _maybe_prepend_uncertain
        # docstring). Runs after the bypass tag so [bypass] wins when both
        # conditions are true.
        text = self._maybe_prepend_uncertain(text)
        async with self._turn_lock:
            branch_tag, stripped = extract_branch_tag(text)
            try:
                bot_message_id = await self._on_text(self._chat_id, stripped)
            except Exception as exc:
                logger.error("[%s] on_text callback failed: %s", self.key, exc)
                return
            if not bot_message_id:
                # Send failed or pre-ready dispatch — nothing to correlate against.
                return
            # Increment per-user engaged_count in the registry so the model's
            # compact user table reflects actual interaction history over time.
            if self._turn_sender_id:
                try:
                    from brendbot.user_registry import record_engagement
                    record_engagement(self._turn_sender_id)
                except Exception:
                    pass
            from brendbot.discord import record_bot_spoke
            record_bot_spoke(self._chat_id)
            log_bot_response(
                channel_id=self._chat_id,
                bot_message_id=bot_message_id,
                user_message_id=self._turn_user_message_id,
                user_text=self._turn_user_text,
                score=self._turn_score,
                domains=self._turn_domains,
                address_level=self.current_address_level,
                branch_tag=branch_tag,
                modules_queried=sorted(self._turn_modules_queried),
                haiku_invoked=self._turn_haiku_invoked,
                input_tokens=self._turn_input_tokens,
                cache_read_input_tokens=self._turn_cache_read_tokens,
                cache_creation_input_tokens=self._turn_cache_creation_tokens,
                stage_timings_ms=self._compute_stage_timings_ms(),
            )
            if branch_tag:
                log_branch_audit(
                    channel_id=self._chat_id,
                    bot_message_id=bot_message_id,
                    branch=branch_tag,
                    response_text=stripped,
                )

    def log_turn(self, role: str, text: str, max_chars: int = 800) -> None:
        self._turn_log.append({"role": role, "text": text[:max_chars]})
        if len(self._turn_log) > _MAX_TURN_LOG:
            self._turn_log = self._turn_log[-_MAX_TURN_LOG:]

    def is_alive(self) -> bool:
        return self.running and self._task is not None and not self._task.done()

    async def _kill(self) -> None:
        if not self._client:
            return
        try:
            transport = getattr(self._client, "_transport", None)
            if transport and hasattr(transport, "close"):
                close_result = transport.close()
                if asyncio.iscoroutine(close_result):
                    await close_result
        except Exception as e:
            logger.warning("Kill error for %s: %s", self.key, e)
        finally:
            self._client = None

    async def _run_loop(self) -> None:
        receiver = asyncio.create_task(self._receive_loop())
        try:
            while self.running:
                if receiver.done():
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                # Backwards compat: queue may contain raw strings (legacy
                # callers that pre-date the housekeeping tuple) or
                # (text, housekeeping) tuples. Unpack accordingly.
                if isinstance(item, tuple):
                    msg, housekeeping = item
                else:
                    msg, housekeeping = item, False
                if self._context_state == "restarting":
                    continue
                try:
                    assert self._client is not None
                    async with self._turn_lock:
                        # Set housekeeping flag inside the lock so it's
                        # paired with this specific query() call. Consumed
                        # by the result handler when the ResultMessage for
                        # this turn arrives.
                        if housekeeping:
                            self._next_turn_is_housekeeping = True
                        await self._client.query(msg)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._error_count += 1
                    logger.error("Session %s query error: %s", self.key, e)
                    if self._error_count >= 3:
                        self.running = False
                        break
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        finally:
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass
            await self._kill()

    async def _receive_loop(self) -> None:
        try:
            assert self._client is not None
            async for message in self._client.receive_messages():
                try:
                    self._handle(message)
                except Exception as e:
                    logger.exception("Error handling message: %s", e)
                if isinstance(message, ResultMessage):
                    self._error_count = 0
                    self._completed_turn_count += 1
                    if self._completed_turn_count == 1 or self._completed_turn_count % _CHECKPOINT_INTERVAL == 0:
                        self._write_checkpoint()
                    if self._context_state == "threshold_hit":
                        await self._trigger_clean_restart()
        except asyncio.CancelledError:
            pass
        except ProcessError as e:
            if getattr(e, "exit_code", None) in (137, 143):
                logger.debug("Session %s subprocess exited via signal (exit %d) — clean shutdown", self.key, e.exit_code)
            else:
                logger.error("Session %s subprocess error (exit %s): %s", self.key, getattr(e, "exit_code", "?"), e)
            self.running = False
        except Exception as e:
            logger.error("Session %s receiver error: %s", self.key, e)
            self.running = False

    def _handle(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            # Discriminator flag: an AssistantMessage arrived this turn
            # at all. Combined with any_content_block_seen below, this
            # distinguishes true broken turns (no message) from thinking-only
            # turns where the model correctly declined to respond.
            self._turn_any_assistant_msg_seen = True
            for block in message.content:
                # Discriminator flag: any content block at all (Text,
                # Thinking, ToolUse, ToolResult). An empty content list
                # combined with an AssistantMessage still means the SDK
                # gave us nothing actionable. A thinking-only content list
                # is the canonical shape of an intentional silent drop.
                self._turn_any_content_block_seen = True
                if isinstance(block, ThinkingBlock):
                    try:
                        import datetime
                        thoughts_path = Path(self.cwd) / "thoughts.log"
                        ts = datetime.datetime.now().isoformat(timespec="seconds")
                        with thoughts_path.open("a") as f:
                            f.write(f"\n--- [{ts}] turn {self._completed_turn_count} ---\n")
                            f.write(block.thinking)
                            f.write("\n")
                    except Exception as exc:
                        logger.warning("[%s] thoughts.log write failed: %s", self.key, exc)
                elif isinstance(block, TextBlock):
                    logger.info("[%s] %s", self.key, block.text[:200])
                    self.log_turn("assistant", block.text)
                    if block.text.strip():
                        # Phase 2a — stamp first-token time on the first
                        # non-empty TextBlock this turn. Subsequent blocks
                        # don't re-stamp. Non-empty guard prevents empty
                        # SDK-drift blocks from registering as first token.
                        if self._turn_t_first_token is None:
                            self._turn_t_first_token = time.monotonic()
                        self._turn_text_buffer.append(block.text)
                        # Stream text to Discord as it arrives (text-only
                        # turns only). Once a tool fires, streaming stops
                        # and the final-segment-only dispatch in
                        # ResultMessage takes over.
                        if (not self._turn_tool_called
                                and not self._turn_used_send_discord
                                and self._on_text_edit):
                            if not self._stream_initiated:
                                # First streaming TextBlock this turn — set
                                # the synchronous flag BEFORE creating the
                                # async task so ResultMessage handler can't
                                # race past it.
                                self._stream_initiated = True
                                self._stream_first_chunk_done = asyncio.Event()
                            asyncio.create_task(self._stream_chunk(block.text))
                elif isinstance(block, ToolUseBlock):
                    logger.info("[%s] tool: %s", self.key, block.name)
                    self._turn_tool_called = True  # mark that tool use occurred this turn
                    if block.name == "Bash":
                        self._turn_bash_calls += 1
                        tool_cmd = (block.input or {}).get("command", "")
                        if "send-discord" in tool_cmd:
                            self._turn_used_send_discord = True
                        if "kb-query" in tool_cmd:
                            self._turn_kb_query_used = True
                            import re as _re
                            _mod_match = _re.search(
                                r'kb-query\s+(?:defs|facts|thms|topics|xlinks)\s+(\w+)',
                                tool_cmd,
                            )
                            if _mod_match:
                                self._turn_modules_queried.add(_mod_match.group(1).upper())
                    elif block.name == "CronCreate":
                        # Persist cron definition so it survives session restarts.
                        self._persist_cron(block.input or {})
                    elif block.name == "CronDelete":
                        # Remove persisted cron entry.
                        self._remove_cron(block.input or {})
                    else:
                        # Non-Bash tool use (Read, Write, Edit, Grep, Glob,
                        # WebSearch, WebFetch, Task, NotebookEdit). Counted
                        # at lower weight than Bash for cognitive load —
                        # they're typically faster, narrower, less stateful.
                        self._turn_other_tool_calls += 1
        elif isinstance(message, ResultMessage):
            # ── Phase 1: cache-metric stash ──────────────────────────
            # Read usage up-front so the _turn_*_tokens fields are set
            # before _fire_on_text / _fire_on_text_streamed get scheduled
            # below. asyncio.create_task doesn't run the task immediately
            # (the receive loop must yield first), so this is belt-and-
            # braces — the values would be set in time anyway, but pulling
            # them here makes the ordering explicit rather than relying
            # on scheduler internals. The same `usage` dict is reused for
            # context / load tracking further down.
            _usage_for_cache = message.usage or {}
            if isinstance(_usage_for_cache, dict) and _usage_for_cache:
                self._turn_input_tokens = int(
                    _usage_for_cache.get("input_tokens", 0) or 0
                )
                self._turn_cache_read_tokens = int(
                    _usage_for_cache.get("cache_read_input_tokens", 0) or 0
                )
                self._turn_cache_creation_tokens = int(
                    _usage_for_cache.get("cache_creation_input_tokens", 0) or 0
                )
            else:
                # No usage dict this turn — leave fields as None so
                # log_bot_response omits the cache block entirely.
                self._turn_input_tokens = None
                self._turn_cache_read_tokens = None
                self._turn_cache_creation_tokens = None

            # Phase 3 #1B — housekeeping turns (e.g. shallow rest injection)
            # suppress both the normal text dispatch and the silent-drop
            # fallback. The model is told not to respond and we don't want
            # to leak a Discord message either way. Token usage is still
            # tracked normally below — the inject did consume tokens.
            # Flag is one-shot.
            is_housekeeping = self._next_turn_is_housekeeping
            if is_housekeeping:
                self._next_turn_is_housekeeping = False
                # Clear the text buffer so neither dispatch branch fires.
                self._turn_text_buffer.clear()
                logger.debug("[%s] housekeeping turn — suppressing dispatch", self.key)

            # Read stop_reason defensively — the Python SDK docs claim
            # this field is TypeScript-only but the dataclass actually
            # defines it as `stop_reason: str | None = None`. Using getattr
            # tolerates SDK versions where the field is missing entirely.
            # Values we care about: 'end_turn' (normal completion, model
            # chose to stop), 'refusal' (model declined), 'max_tokens'
            # (output cap hit), 'tool_use' (pause for tool call, should
            # not reach this branch). None means the SDK either didn't
            # populate it or the turn ended abnormally.
            stop_reason = getattr(message, "stop_reason", None)

            if (self._turn_text_buffer
                    and not self._turn_used_send_discord
                    and self._on_text
                    and self._chat_id):
                if self._turn_tool_called:
                    # Tools were called this turn — everything before the last
                    # TextBlock was mid-turn narration. Only the final segment
                    # is the intended response. Streaming was disabled when
                    # tools fired, so send fresh.  If streaming had already
                    # started before the first tool, the orphaned message
                    # gets cleaned up in _stream_reset_with_cleanup.
                    asyncio.create_task(self._stream_reset_with_cleanup())
                    text_to_send = self._turn_text_buffer[-1]
                    asyncio.create_task(self._fire_on_text(text_to_send))
                elif self._stream_initiated:
                    # Streaming was initiated this turn. The message may or
                    # may not be on Discord yet (first-chunk send is async).
                    # _fire_on_text_streamed awaits _stream_first_chunk_done
                    # to close the race window, then does a final edit.
                    text_to_send = "\n".join(self._turn_text_buffer)
                    asyncio.create_task(
                        self._fire_on_text_streamed(text_to_send)
                    )
                else:
                    # Text-only turn but streaming didn't activate (no
                    # on_text_edit callback, or first chunk failed). Fall
                    # back to the original single-send path.
                    text_to_send = "\n".join(self._turn_text_buffer)
                    asyncio.create_task(self._fire_on_text(text_to_send))
            elif (not self._turn_used_send_discord
                  and not self._turn_tool_called
                  and not is_housekeeping
                  and self._on_text
                  and self._chat_id):
                # ── Phantom-turn discriminator (Phase 3 phantom-fix-v2) ──
                # Three cases to distinguish, previously all collapsed into
                # "fire fallback":
                #
                # Case A: Intentional silent drop. The model received a
                #   message, ran thinking tokens, and correctly decided
                #   not to respond per the soul's silent-drop rule (e.g.
                #   cross-talk addressed to another user). Shape:
                #     - AssistantMessage arrived (any_assistant_msg_seen=True)
                #     - ContentBlock(s) inside it (any_content_block_seen=True)
                #       — typically ThinkingBlock only, no TextBlock
                #     - stop_reason == 'end_turn' (clean completion)
                #   Correct handling: SUPPRESS fallback. The silence is
                #   the response.
                #
                # Case B: True broken turn. Something structurally failed —
                #   no AssistantMessage at all, or an empty content list
                #   with no blocks, or an error stop_reason. The user is
                #   waiting and nothing's coming. Shape:
                #     - any_assistant_msg_seen=False OR
                #       any_content_block_seen=False
                #     - stop_reason None or indicates error
                #   Correct handling: FIRE fallback so user knows it broke.
                #
                # Case C: Housekeeping turn (context injection, shallow rest).
                #   Handled above by is_housekeeping check — already suppressed.
                #
                # Decision rule: if the model got as far as emitting an
                # AssistantMessage with at least one content block AND the
                # stop_reason is 'end_turn', treat it as Case A and suppress.
                # Anything else falls through to the fallback path.
                is_intentional_silent_drop = (
                    self._turn_any_assistant_msg_seen
                    and self._turn_any_content_block_seen
                    and stop_reason == "end_turn"
                )
                if is_intentional_silent_drop:
                    logger.info(
                        "[%s] intentional silent drop — model declined to respond "
                        "(thinking-only, stop_reason=end_turn) — suppressing fallback",
                        self.key,
                    )
                else:
                    logger.warning(
                        "[%s] phantom turn — no output blocks "
                        "(assistant_msg=%s, content_block=%s, stop_reason=%s) — "
                        "sending fallback",
                        self.key,
                        self._turn_any_assistant_msg_seen,
                        self._turn_any_content_block_seen,
                        stop_reason,
                    )
                    fallback_text = (
                        "(no response generated — try rephrasing or asking again)"
                    )
                    asyncio.create_task(self._fire_on_text(fallback_text))
            # Follow-up signal: if this turn used tools and we know which
            # user triggered it, record (channel_id, user_id, now) so that
            # a follow-up message from the same user within the window
            # gets a score boost at ingest time. Skipped for housekeeping,
            # restart, and any turn where sender_id is unknown.
            if (
                self._turn_tool_called
                and self._turn_sender_id is not None
                and self._chat_id is not None
            ):
                try:
                    from brendbot import discord as _bd
                    _bd.record_tool_turn(self._chat_id, self._turn_sender_id)
                except Exception as exc:
                    logger.debug(
                        "[%s] record_tool_turn failed: %s", self.key, exc
                    )
            self._turn_text_buffer.clear()
            self._turn_used_send_discord = False
            self._turn_tool_called = False
            self._turn_sender_id = None
            self._turn_bypass_pending = False
            # Reset phantom-turn discriminator flags for the next turn.
            self._turn_any_assistant_msg_seen = False
            self._turn_any_content_block_seen = False

            cost = f" (${message.total_cost_usd:.4f})" if message.total_cost_usd else ""
            usage = message.usage or {}
            if isinstance(usage, dict) and usage:
                input_tokens = usage.get("input_tokens", 0) or 0
                cache_read = usage.get("cache_read_input_tokens", 0) or 0
                cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                total_context = input_tokens + cache_read + cache_creation
                if total_context:
                    self._last_input_tokens = int(total_context)
                    if self._last_input_tokens >= _CONTEXT_REFRESH_THRESHOLD:
                        if self._context_state == "normal":
                            self._context_state = "threshold_hit"
                        logger.warning(
                            "[%s] context at %d tokens (>=%d threshold) — clean restart queued",
                            self.key, self._last_input_tokens, _CONTEXT_REFRESH_THRESHOLD,
                        )
                    elif (self._last_input_tokens >= _CONTEXT_SOFT_WARNING
                          and not self._soft_warning_sent):
                        self._soft_warning_sent = True
                        # Promote soft warning to threshold_hit so the receiver
                        # loop fires _trigger_clean_restart() at end of this
                        # turn — preempts the 400k mid-turn ambush by
                        # restarting at ~320k while we still have headroom.
                        if self._context_state == "normal":
                            self._context_state = "threshold_hit"
                        logger.info(
                            "[%s] context at %d tokens — soft warning, preemptive restart queued",
                            self.key, self._last_input_tokens,
                        )

            # ── Cognitive load update (Phase 3 #1A) ───────────────────
            # Roll per-turn counters into cumulative load. Compute current
            # load score as weighted sum and trip preemptive restart if it
            # exceeds budget — independent of token count. Catches the
            # heavy-tool-use turns that don't spike tokens but do degrade
            # the worker's effective capacity.
            self._cumulative_bash_calls += self._turn_bash_calls
            self._cumulative_other_tools += self._turn_other_tool_calls
            current_load = (
                (self._last_input_tokens / 1000.0) * _LOAD_WEIGHT_TOKENS_PER_K
                + self._cumulative_bash_calls * _LOAD_WEIGHT_BASH_CALL
                + self._cumulative_haiku_invocations * _LOAD_WEIGHT_HAIKU_INVOCATION
                + self._cumulative_other_tools * _LOAD_WEIGHT_TOOL_OTHER
            )
            self._cumulative_load = current_load
            if (current_load >= _LOAD_BUDGET_PREEMPTIVE
                    and self._context_state == "normal"):
                self._context_state = "threshold_hit"
                logger.info(
                    "[%s] load score %.1f >= budget %.1f — preemptive restart queued "
                    "(bash=%d, haiku=%d, other=%d, tokens=%d)",
                    self.key, current_load, _LOAD_BUDGET_PREEMPTIVE,
                    self._cumulative_bash_calls, self._cumulative_haiku_invocations,
                    self._cumulative_other_tools, self._last_input_tokens,
                )
            elif (current_load >= _LOAD_BUDGET_SHALLOW
                    and not self._shallow_rested
                    and self._context_state == "normal"):
                # Phase 3 #1B — fire shallow rest. Scheduled as a task so
                # it runs after the current ResultMessage finishes processing
                # and doesn't block the receive loop.
                logger.info(
                    "[%s] load score %.1f >= shallow budget %.1f — rest cycle queued",
                    self.key, current_load, _LOAD_BUDGET_SHALLOW,
                )
                asyncio.create_task(self._trigger_shallow_rest())
            elif (self._shallow_rested
                    and current_load < _LOAD_BUDGET_SHALLOW * 0.7):
                # Recovery: load dropped well below the shallow trigger.
                # Clear the rested flag so a future spike can re-fire.
                self._shallow_rested = False
            logger.info(
                "[%s] turn complete%s (context_tokens=%d, load=%.1f)",
                self.key, cost, self._last_input_tokens, current_load,
            )
            try:
                import pathlib
                status_path = pathlib.Path(self.cwd) / "context_status.txt"
                pct = round(self._last_input_tokens / 1_000_000 * 100, 1)
                status_path.write_text(f"{self._last_input_tokens} {pct}\n")
            except Exception:
                pass
            if self._react_msg_id and self._active_reactions:
                reactions_to_clear = set(self._active_reactions)
                unreact_fn = self._unreact_fn
                channel = self._react_channel
                msg_id = self._react_msg_id
                async def _do_clear(reactions=reactions_to_clear, fn=unreact_fn, ch=channel, mid=msg_id):
                    for emoji in reactions:
                        if fn and ch and mid:
                            try:
                                await fn(ch, mid, emoji)
                            except Exception as e:
                                logger.debug("Unreaction cleanup error %s: %s", emoji, e)
                asyncio.create_task(_do_clear())
            self._react_phase = 0
            self._react_msg_id = ""
            self._active_reactions = set()
            self._module_emote_count = 0

            if self._turn_log:
                last = self._turn_log[-1]
                if last.get("role") == "assistant":
                    if self._turn_modules_queried:
                        last["kb_modules"] = sorted(self._turn_modules_queried)
                    last["kb_grounded"] = self._turn_kb_query_used

    def set_reaction_target(
        self,
        react_fn: Any,
        unreact_fn: Any,
        channel_id: str,
        message_id: str,
    ) -> None:
        self._react_fn = react_fn
        self._unreact_fn = unreact_fn
        self._react_channel = channel_id
        self._react_msg_id = message_id
        self._react_phase = 0
        self._active_reactions = set()
        self._module_emote_count = 0

    async def _react(self, emoji: str) -> None:
        if self._react_fn and self._react_channel and self._react_msg_id:
            try:
                await self._react_fn(self._react_channel, self._react_msg_id, emoji)
                self._active_reactions.add(emoji)
            except Exception as e:
                logger.debug("Reaction error: %s", e)

    async def _unreact(self, emoji: str) -> None:
        if self._unreact_fn and self._react_channel and self._react_msg_id:
            try:
                await self._unreact_fn(self._react_channel, self._react_msg_id, emoji)
                self._active_reactions.discard(emoji)
            except Exception as e:
                logger.debug("Unreaction error: %s", e)

    def _is_housekeeping_tool_call(self, tool_name: str, tool_input: dict) -> bool:
        if tool_name in ("Bash", "Write", "Edit", "WebSearch", "WebFetch", "NotebookEdit"):
            return False
        path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or tool_input.get("pattern", "")
            or ""
        )
        return any(frag in path for frag in self._HOUSEKEEPING_PATH_FRAGMENTS)

    async def _advance_reaction_phase(self, tool_name: str, tool_input: dict) -> None:
        if not self._react_msg_id:
            return
        if self._is_housekeeping_tool_call(tool_name, tool_input):
            return

        self._react_phase = 1

        path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or ""
        )
        module_emote = None
        for module_name, emote in self._MODULE_EMOTES.items():
            if f"knowledge/{module_name}.json" in path:
                module_emote = emote
                break

        if module_emote:
            if module_emote in self._active_reactions:
                return
            if self._NEUTRAL_EMOTE in self._active_reactions:
                return
            if self._module_emote_count < self._MAX_MODULE_EMOTES:
                self._module_emote_count += 1
                await self._react(module_emote)
            else:
                for emote in list(self._active_reactions):
                    if emote in self._MODULE_EMOTES.values():
                        await self._unreact(emote)
                await self._react(self._NEUTRAL_EMOTE)
        else:
            if "🧠" not in self._active_reactions:
                await self._react("🧠")

    def _write_checkpoint(self) -> None:
        if not self._turn_log:
            return
        try:
            summary_path = Path(self.cwd) / "CONTEXT_SUMMARY.md"
            lines = [f"# CTX {len(self._turn_log)}t @{self._last_input_tokens}tok\n"]
            for entry in self._turn_log:
                role = entry.get("role", "unknown")
                text = entry.get("text", "")
                cap = 400 if role == "user" else 200
                truncated = text[:cap] + ("…" if len(text) > cap else "")
                gate_tag = ""
                if role == "assistant" and entry.get("kb_grounded"):
                    mods = entry.get("kb_modules", [])
                    gate_tag = f"|kb:{','.join(mods)}" if mods else "|kb:?"
                lines.append(f"{role[0]}{gate_tag}:{truncated}\n")
            summary_path.write_text("".join(lines))
            logger.info("[%s] checkpoint written (%d turns, turn=%d)",
                        self.key, len(self._turn_log), self._completed_turn_count)
        except Exception as exc:
            logger.warning("[%s] checkpoint write failed: %s", self.key, exc)

    async def _trigger_clean_restart(self) -> None:
        if self._turn_log:
            self._write_checkpoint()
            logger.info("[%s] CONTEXT_SUMMARY.md written (%d turns) — triggering clean restart",
                        self.key, len(self._turn_log))

        # Phase 3 #2A — write an episode row before respawning. Best-effort:
        # any failure is logged inside write_episode and does not block the
        # restart. The episode captures the cues (channel, domains, entities,
        # bookends) that future sessions can use for retrieval matching.
        try:
            from brendbot.episodes import write_episode
            outcome = "ok"
            if self._shallow_rest_count > 0:
                outcome = "rest_fired"
            write_episode(
                channel=self._chat_id or self.key,
                ts_start=self._session_started_at,
                turn_log=list(self._turn_log),
                domains=sorted(self._session_domains_seen),
                outcome=outcome,
            )
        except Exception as exc:
            logger.warning("[%s] episode write skipped: %s", self.key, exc)

        self._context_state = "restarting"
        if self._on_needs_restart:
            asyncio.create_task(self._on_needs_restart())

    async def _trigger_shallow_rest(self) -> None:
        """Phase 3 #1B — shallow rest cycle.

        Fires when cumulative load crosses _LOAD_BUDGET_SHALLOW but stays
        below _LOAD_BUDGET_PREEMPTIVE. Cheaper than _trigger_clean_restart:
        no subprocess respawn, no CLAUDE.md rebuild, no transcript reseed.

        What it does:
          - Resets the non-token components of cumulative load
            (Bash count, haiku count, other tool count) to zero
          - Recomputes _cumulative_load from the surviving token component
          - Injects a brief system message telling the model the equivalent
            of "take a breath" — flush mid-thread tool-use working memory
            but keep core conversational state
          - Marks _shallow_rested to prevent re-firing every turn

        What it does NOT do:
          - Reduce input tokens. The token component of load is unchanged.
            Only a real restart can free tokens.
          - Spawn a new subprocess. The model stays in place.
          - Reset the per-turn lock or in-flight state.

        Cumulative load drops by exactly the tool components, so a session
        that had load=290 (260 from tokens + 30 from tools) becomes load=260
        post-rest. The next preemptive trigger still fires when tokens grow
        enough on their own.
        """
        token_component = (self._last_input_tokens / 1000.0) * _LOAD_WEIGHT_TOKENS_PER_K
        load_before = self._cumulative_load

        self._cumulative_bash_calls = 0
        self._cumulative_haiku_invocations = 0
        self._cumulative_other_tools = 0
        self._cumulative_load = token_component
        self._shallow_rested = True
        self._shallow_rest_count += 1

        logger.info(
            "[%s] shallow rest #%d — load %.1f → %.1f (token component preserved)",
            self.key, self._shallow_rest_count, load_before, token_component,
        )

        # Inject a brief rest message. The model receives it as a normal
        # user-turn injection but it carries an explicit do-not-respond
        # marker so the result handler routes it as housekeeping.
        rest_message = (
            "<system-rest>\n"
            "Brief rest cycle. Discard mid-thread tool-use working memory "
            "from prior turns in this session — the accumulated context of "
            "what tools were just called and why is no longer needed for "
            "the next user message. Keep core conversational state and "
            "memory of who you are talking to. Do not respond to this "
            "message. The next user message will arrive normally.\n"
            "</system-rest>"
        )
        try:
            await self.inject(rest_message, housekeeping=True)
        except Exception as exc:
            logger.warning("[%s] shallow rest injection failed: %s", self.key, exc)

    def _build_options(self) -> ClaudeAgentOptions:
        import os

        if self.tier == "admin":
            tools = [
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "WebSearch", "WebFetch", "Task", "NotebookEdit",
                "CronCreate", "CronDelete", "CronList",
            ]
            perm_mode = "acceptEdits"
            turn_limit = 200
            # Admin sessions get full reasoning depth — deep tool use,
            # complex code generation, and multi-step planning are common.
            effort = "high"
        elif self.tier == "trusted":
            tools = ["Read", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
            perm_mode = "acceptEdits"
            turn_limit = 50
            # Trusted users get balanced effort — enough depth for
            # meaningful tool use, but not burning tokens on overanalysis.
            effort = "medium"
        else:
            tools = ["Read", "Bash", "Glob", "Grep"]
            perm_mode = "acceptEdits"
            turn_limit = 30
            # Default tier: optimize for speed and cost. Limited tool
            # budget anyway, so deeper reasoning yields diminishing returns.
            effort = "low"

        env: dict[str, str] = {}

        opts = ClaudeAgentOptions(
            cwd=self.cwd,
            allowed_tools=tools,
            permission_mode=perm_mode,
            setting_sources=["project"],
            model=self._model,
            fallback_model="haiku" if self._model != "haiku" else "sonnet",
            max_turns=turn_limit,
            max_buffer_size=10 * 1024 * 1024,
            thinking={"type": "adaptive"},
            effort=effort,
            env=env,
        )

        opts.can_use_tool = self._permission_check
        opts.extra_args = {"session-id": str(uuid.uuid4())}
        return opts

    async def _permission_check(
        self, tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name in ("Write", "Edit"):
            path = tool_input.get("file_path", "")
            if "CLAUDE.md" in path:
                return PermissionResultDeny(
                    message="CLAUDE.md is managed by admin only"
                )

        # Bash-specific sub-budget — enforced before the overall tool count so
        # a cascade of git/gcloud/cat calls is cut off independently of how
        # many Read/Edit/Grep calls are in flight. _turn_bash_calls is already
        # incremented in the ToolUseBlock handler; read it here pre-increment
        # because the ToolUseBlock fires before _permission_check in the SDK
        # event order — so by the time _permission_check sees this Bash call,
        # _turn_bash_calls already reflects the current call.
        if tool_name == "Bash":
            if self._turn_bash_calls >= _BASH_CALL_BUDGET:
                logger.warning(
                    "[%s] Bash budget exceeded (%d/%d) — blocking",
                    self.key, self._turn_bash_calls, _BASH_CALL_BUDGET,
                )
                return PermissionResultDeny(
                    message=(
                        f"Bash budget exceeded ({self._turn_bash_calls}/{_BASH_CALL_BUDGET} "
                        "per turn). Consolidate remaining work into a single command "
                        "or respond with what is known so far."
                    )
                )

        self._tool_call_count += 1

        # Patch 1a — agent_core.BudgetState enforcement. step_cap is
        # parallel to the legacy _tool_call_count check below (kept for
        # back-compat with operators reading existing log lines), but
        # time_cap_s is a new halting-problem defence: a turn that stays
        # under the step budget can still run wall-clock unbounded on
        # slow I/O (flaky network retries, gcloud timeouts). The cap
        # here bounds the entire turn.
        if self._turn_budget is not None:
            from brendbot.agent_core.budgets import BudgetExceeded
            # Sync step count from the in-house counter so both views agree.
            self._turn_budget.steps = self._tool_call_count
            tcap = self._turn_budget.budget.time_cap_s
            if tcap is not None and self._turn_budget.elapsed_s > tcap:
                exc = BudgetExceeded("time_s", self._turn_budget.elapsed_s, tcap)
                logger.warning("[%s] turn budget %s", self.key, exc)
                return PermissionResultDeny(
                    message=(
                        f"Turn budget exceeded on {exc.dimension}: "
                        f"used={exc.used:.1f}s, cap={exc.cap:.0f}s. "
                        "Stop tool use and respond with what is known."
                    )
                )

        # Address-level budget cap (FUSED-CORE Budget Throttle enforcement).
        # Maps to the levels defined in engagement.yaml address_levels:
        #   high     → full budget (_TOOL_CALL_BUDGET)
        #   moderate → 3 tool calls per turn
        #   low      → 0 tool calls (text-only response)
        # The model is also told this in the <message> XML, but the model
        # follows instructions inconsistently — this is the enforcement layer.
        _ADDRESS_BUDGETS = {"high": _TOOL_CALL_BUDGET, "moderate": 3, "low": 0}
        addr_cap = _ADDRESS_BUDGETS.get(self.current_address_level, _TOOL_CALL_BUDGET)
        if self._tool_call_count > addr_cap:
            logger.info(
                "[%s] Address-level budget exceeded (%d/%d, level=%s) — blocking",
                self.key, self._tool_call_count, addr_cap, self.current_address_level,
            )
            return PermissionResultDeny(
                message=(
                    f"Address level '{self.current_address_level}' caps tool calls at "
                    f"{addr_cap} per turn ({self._tool_call_count} attempted). "
                    "Stop tool use and respond with what is known."
                )
            )

        if self._tool_call_count > _TOOL_CALL_BUDGET:
            logger.warning(
                "[%s] Tool call budget exceeded (%d/%d) — blocking",
                self.key, self._tool_call_count, _TOOL_CALL_BUDGET,
            )
            return PermissionResultDeny(
                message=(
                    f"Tool call budget exceeded ({self._tool_call_count}/{_TOOL_CALL_BUDGET} "
                    "per turn). Stop tool use and respond with what is known."
                )
            )

        effective = self.current_sender_tier or self.tier

        if effective == "admin":
            return PermissionResultAllow()

        if effective in ("default", "unknown"):
            if tool_name in ("Write", "Edit", "NotebookEdit"):
                return PermissionResultDeny(
                    message=f"{tool_name} blocked for {effective} tier"
                )
            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                if "send-discord" not in cmd:
                    return PermissionResultDeny(
                        message="Bash blocked for default tier (only send-discord allowed)"
                    )
            if tool_name == "Read":
                path = tool_input.get("file_path", "")
                sensitive = [".ssh", ".env", "credentials", "secrets", "token"]
                if any(s in path for s in sensitive):
                    return PermissionResultDeny(
                        message="Sensitive file blocked"
                    )

        if effective == "trusted":
            if tool_name in ("Write", "Edit", "NotebookEdit"):
                return PermissionResultDeny(
                    message=f"{tool_name} blocked for trusted tier"
                )

        return PermissionResultAllow()

    # ------------------------------------------------------------------
    # Cron persistence helpers
    # ------------------------------------------------------------------

    def _cron_file_path(self) -> Path:
        """Return the path to this session's cron persistence file."""
        return Path(self.cwd) / CRON_FILE

    def _persist_cron(self, tool_input: dict) -> None:
        """Append or update a cron entry from a CronCreate tool call.

        Stores the full tool_input dict as-is. Earlier versions looked for
        hard-coded "schedule"/"command" keys, but the CronCreate tool
        actually uses taskId/cronExpression/fireAt/prompt/description —
        so every entry collapsed to the same empty (schedule, command)
        tuple and the dedupe filter wiped prior entries, leaving only
        one cron in the file regardless of how many were created.

        Dedupe now uses a canonical JSON serialisation of the full entry,
        so identical re-creations collapse but genuinely distinct crons
        (different taskId or fire time) each persist as their own row."""
        cron_path = self._cron_file_path()
        try:
            existing: list[dict] = []
            if cron_path.exists():
                existing = json.loads(cron_path.read_text())

            entry = dict(tool_input)
            entry_canonical = json.dumps(entry, sort_keys=True)
            existing = [
                e for e in existing
                if json.dumps(e, sort_keys=True) != entry_canonical
            ]
            existing.append(entry)
            cron_path.write_text(json.dumps(existing, indent=2))
            # Log a human-readable marker. Try the canonical CronCreate
            # fields first, then legacy names, then fall back to a tag.
            marker = (
                entry.get("taskId")
                or entry.get("fireAt")
                or entry.get("cronExpression")
                or entry.get("schedule")
                or entry.get("description")
                or "cron"
            )
            logger.info(
                "[%s] Persisted cron: %s (total=%d)",
                self.key, marker, len(existing),
            )
        except Exception as exc:
            logger.warning("[%s] Failed to persist cron: %s", self.key, exc)

    def _remove_cron(self, tool_input: dict) -> None:
        """Remove a cron entry matching a CronDelete tool call.

        Matches across all plausible identifier fields (taskId, id,
        cronExpression, fireAt, schedule, command, prompt) so removal
        works regardless of which naming convention the SDK passes."""
        cron_path = self._cron_file_path()
        if not cron_path.exists():
            return
        try:
            existing: list[dict] = json.loads(cron_path.read_text())

            # Identifier candidates from the CronDelete input. An empty
            # string never matches anything in _matches, so missing keys
            # simply don't contribute to the match predicate.
            delete_task_id = tool_input.get("taskId", "")
            delete_id = tool_input.get("id", "")
            delete_cron_expr = tool_input.get("cronExpression", "")
            delete_fire_at = tool_input.get("fireAt", "")
            delete_schedule = tool_input.get("schedule", "")
            delete_command = tool_input.get("command", "")
            delete_prompt = tool_input.get("prompt", "")

            def _matches(e: dict) -> bool:
                if delete_task_id and e.get("taskId") == delete_task_id:
                    return True
                if delete_id and e.get("id") == delete_id:
                    return True
                if delete_cron_expr and e.get("cronExpression") == delete_cron_expr:
                    return True
                if delete_fire_at and e.get("fireAt") == delete_fire_at:
                    return True
                if delete_schedule and e.get("schedule") == delete_schedule:
                    if not delete_command or e.get("command") == delete_command:
                        return True
                if delete_command and e.get("command") == delete_command:
                    return True
                if delete_prompt and e.get("prompt") == delete_prompt:
                    return True
                return False

            filtered = [e for e in existing if not _matches(e)]
            if len(filtered) < len(existing):
                cron_path.write_text(json.dumps(filtered, indent=2))
                logger.info("[%s] Removed %d cron(s)", self.key,
                            len(existing) - len(filtered))
        except Exception as exc:
            logger.warning("[%s] Failed to remove cron: %s", self.key, exc)

    @staticmethod
    def load_persisted_crons(cwd: str) -> list[dict]:
        """Load persisted crons from a session's working directory."""
        cron_path = Path(cwd) / CRON_FILE
        if not cron_path.exists():
            return []
        try:
            data = json.loads(cron_path.read_text())
            return data if isinstance(data, list) else []
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Session pool
# ---------------------------------------------------------------------------

class SessionPool:
    """Manages one Session per contact/channel."""

    def __init__(
        self,
        model: str,
        bot_name: str,
        on_text: Optional[Any] = None,
        on_text_edit: Optional[Any] = None,
        max_sessions: int = 0,
    ) -> None:
        self._model = model
        self._bot_name = bot_name
        self._on_text = on_text
        self._on_text_edit = on_text_edit
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()          # guards _sessions dict reads/writes
        self._creation_locks: dict[str, asyncio.Lock] = {}  # per-key; serialises _create()
        # LRU eviction (Stage 3). _lru_order is the recency list, most-recent
        # last. max_sessions=0 disables the cap (previous unbounded behaviour).
        # When a new session would exceed the cap, the oldest idle session
        # (not currently processing a turn) is stopped and evicted.
        self._max_sessions: int = max_sessions
        self._lru_order: list[str] = []

        # ── Static prompt-fragment cache ──────────────────────────────────
        # SOUL.md, GROUP_SOUL.md, FUSED-CORE.md, and the MANIFEST.json
        # knowledge-index block were read from disk on every _create() call.
        # That's 4 file reads per session spawn, and the manifest also
        # parsed JSON every time. Cached here at pool init; refreshed via
        # SIGHUP if the host process catches one (handler wired in main.py).
        self._cached_soul = _load_template("SOUL.md")
        self._cached_group_soul = _load_template("GROUP_SOUL.md")
        self._cached_fused_core: str = ""
        try:
            fused_core_path = PROJECT_ROOT / "FUSED-CORE.md"
            if fused_core_path.exists():
                self._cached_fused_core = fused_core_path.read_text()
        except Exception as exc:
            logger.warning("FUSED-CORE.md cache load failed: %s", exc)
        self._cached_kb_index_block: str = self._build_kb_index_block()
        self._cached_kb_index_stub: str = self._build_kb_index_stub()

    def _build_kb_index_stub(self) -> str:
        """Small pointer injected at session-creation time in place of the
        full FUSED-CORE MODULES + KNOWLEDGE QUERY TOOL block. The full
        block is deferred until the first domain-matched message on this
        channel, injected as housekeeping. Keeps initial system prompt
        lean (~2-3k tokens saved on first-response prefill for sessions
        that never touch a knowledge module, which is the majority in
        casual group chat)."""
        kb_query_path = SCRIPTS_DIR / "kb-query"
        return (
            "\n\n## KNOWLEDGE BASE\n"
            f"Domain knowledge modules are available via `{kb_query_path}` "
            f"(Bash). Full module index and query reference will be injected "
            f"when a domain keyword matches the incoming message. Until then, "
            f"answer from general knowledge — do not fabricate claims that "
            f"would require a module lookup.\n"
        )

    def _build_kb_index_block(self) -> str:
        """Build the FUSED-CORE MODULES + KNOWLEDGE QUERY TOOL block once.
        Returns empty string if MANIFEST.json missing or unreadable —
        _create() handles the empty case by skipping the append."""
        knowledge_dir = PROJECT_ROOT / "brendbot" / "knowledge"
        kb_query_path = SCRIPTS_DIR / "kb-query"
        manifest_path = knowledge_dir / "MANIFEST.json"
        if not manifest_path.exists():
            return ""
        try:
            manifest = json.loads(manifest_path.read_text())
            load_order = manifest.get("load_order", [])
            module_map = {m["id"]: m for m in manifest.get("modules", [])}
            idx_lines = []
            for module_id in load_order:
                desc = module_map.get(module_id, {}).get("desc", "")
                idx_lines.append(f"- {module_id}: {desc}")
            if not idx_lines:
                return ""
            idx = "\n".join(idx_lines)
            return (
                "\n\n## FUSED-CORE MODULES\n"
                f"{idx}\n\n"
                "## KNOWLEDGE QUERY TOOL\n"
                f"Use `{kb_query_path}` via Bash to query knowledge modules.\n"
                f"  {kb_query_path} search \"<terms>\"      — full-text search\n"
                f"  {kb_query_path} defs <MODULE>          — list definitions\n"
                f"  {kb_query_path} facts <MODULE> [topic] — list facts\n"
                f"  {kb_query_path} thms <MODULE>          — core theorems\n"
                f"  {kb_query_path} thms <MODULE> --extended — all theorems (incl. geometry etc.)\n"
                f"  {kb_query_path} def <ID>               — look up one definition\n"
                f"  {kb_query_path} topics <MODULE>        — list fact topics\n"
                f"  {kb_query_path} gates                  — governance gates\n"
                f"  {kb_query_path} imgstyle <id|label>    — image style descriptor set\n"
                f"  {kb_query_path} imgstyle list          — list all styles\n"
                f"  {kb_query_path} imgfail <class>        — remediation for render failure\n"
                f"  {kb_query_path} imgfail list           — list all failure classes\n"
                f"  {kb_query_path} imglog recent [N]      — recent render outcomes\n"
                "Use kb-query for all knowledge lookups (~200 bytes per result).\n"
                "Do not answer from training weights when a module is available.\n\n"
                "## MODULE PRIORITY\n"
                "BUILDSCI covers building-science facts (insulation, HVAC, moisture,\n"
                "enclosure, combustion). IMAGEGEN covers image-generation style and\n"
                "failure-mode data. Anything outside those two domains is answered\n"
                "from general knowledge — no module lookup needed, and no T1/T2\n"
                "provenance obligation applies."
            )
        except Exception as exc:
            logger.warning("FUSED-CORE system prompt index build failed: %s", exc)
            return ""

    # Modules with structured defs/facts/thms content in knowledge.db.
    # IMAGEGEN uses imagegen_config/prompt_styles/render_* schemas instead
    # and is not fetched via this path — it's queried on-demand via
    # kb-query imgstyle/imgfail/imglog when the model needs it.
    _GROUNDABLE_MODULES = frozenset({"BUILDSCI"})

    # Hard character cap on the injected grounded_facts block per module.
    # BUILDSCI currently runs ~6-9KB across defs+facts+thms; the cap keeps
    # unexpectedly large future modules from dominating the context window.
    _GROUNDED_FACTS_MAX_CHARS = 12000

    def _fetch_grounded_facts(self, module_id: str) -> str:
        """Read a module's defs/facts/thms from knowledge.db and render a
        compact grounded-facts block suitable for housekeeping injection.

        Returns empty string for modules not in _GROUNDABLE_MODULES, for
        any DB error, or when the module has no content. Hard-truncated at
        _GROUNDED_FACTS_MAX_CHARS so a future content explosion can't
        silently eat the context budget — the block is bounded and
        observable in the log line written by the caller."""
        if module_id not in self._GROUNDABLE_MODULES:
            return ""
        knowledge_db = PROJECT_ROOT / "brendbot" / "knowledge" / "knowledge.db"
        if not knowledge_db.exists():
            return ""
        import sqlite3
        try:
            # timeout waits for a contending writer rather than raising; the
            # pragmas match user_registry._conn / episodes._open so the DB
            # runs uniformly in WAL mode across all call sites.
            conn = sqlite3.connect(str(knowledge_db), timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            defs = conn.execute(
                "SELECT id, term, description, formal, source FROM definitions "
                "WHERE module_id = ? ORDER BY id",
                (module_id,),
            ).fetchall()
            facts = conn.execute(
                "SELECT topic, name, description, source FROM facts "
                "WHERE module_id = ? ORDER BY topic, name",
                (module_id,),
            ).fetchall()
            thms = conn.execute(
                "SELECT id, description, formal, source FROM theorems "
                "WHERE module_id = ? AND extended = 0 ORDER BY id",
                (module_id,),
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.debug("_fetch_grounded_facts(%s) DB error: %s", module_id, exc)
            return ""
        if not (defs or facts or thms):
            return ""
        parts: list[str] = [f"<grounded_facts module='{module_id}'>"]
        if defs:
            parts.append("  <definitions>")
            for did, term, desc, formal, src in defs:
                line = f"    {did}: {term}"
                if desc:
                    line += f" = {desc}"
                if formal:
                    line += f" | F: {formal}"
                if src:
                    line += f" | {src}"
                parts.append(line)
            parts.append("  </definitions>")
        if facts:
            parts.append("  <facts>")
            for topic, name, desc, src in facts:
                line = f"    {topic}.{name} = {desc}"
                if src:
                    line += f" | {src}"
                parts.append(line)
            parts.append("  </facts>")
        if thms:
            parts.append("  <theorems>")
            for tid, desc, formal, src in thms:
                line = f"    {tid}"
                if desc:
                    line += f" = {desc}"
                if formal:
                    line += f" | F: {formal}"
                if src:
                    line += f" | {src}"
                parts.append(line)
            parts.append("  </theorems>")
        parts.append("</grounded_facts>")
        block = "\n".join(parts)
        if len(block) > self._GROUNDED_FACTS_MAX_CHARS:
            block = (
                block[: self._GROUNDED_FACTS_MAX_CHARS]
                + "\n[truncated — full module via kb-query defs/facts/thms]"
            )
        return block

    def refresh_cache(self) -> None:
        """Re-read all cached prompt fragments and engagement config. Wire
        to SIGHUP in main.py so live edits to SOUL.md / FUSED-CORE.md /
        MANIFEST.json / engagement.yaml take effect without a full process
        restart."""
        self._cached_soul = _load_template("SOUL.md")
        self._cached_group_soul = _load_template("GROUP_SOUL.md")
        try:
            fused_core_path = PROJECT_ROOT / "FUSED-CORE.md"
            self._cached_fused_core = (
                fused_core_path.read_text() if fused_core_path.exists() else ""
            )
        except Exception as exc:
            logger.warning("FUSED-CORE.md refresh failed: %s", exc)
        self._cached_kb_index_block = self._build_kb_index_block()
        self._cached_kb_index_stub = self._build_kb_index_stub()
        # Hot-reload engagement.yaml — scoring deltas, thresholds, noise
        # tokens, domain keywords, classifier prompts, content gate config.
        try:
            from brendbot.discord import refresh_engagement_config
            refresh_engagement_config()
        except Exception as exc:
            logger.warning("engagement.yaml refresh failed: %s", exc)
        logger.info("SessionPool cache refreshed")

    def _bump_lru(self, key: str) -> None:
        """Move `key` to the most-recently-used position. Caller must hold
        self._lock (or be in a create path where the new key isn't yet
        visible to other coroutines)."""
        try:
            self._lru_order.remove(key)
        except ValueError:
            pass
        self._lru_order.append(key)

    async def _evict_if_over_cap(self, protect_key: str) -> None:
        """If _sessions count exceeds max_sessions, evict the oldest idle
        session. `protect_key` is the key currently being created — never
        evicted even if it sits at the head of the LRU list.

        An 'idle' session is one with an empty queue and no current turn
        lock held. If every session is busy, no eviction happens and the
        cap is soft — we log a warning and proceed. This keeps the hot
        path from blocking on arbitrary turn latency."""
        if self._max_sessions <= 0:
            return
        excess = len(self._sessions) - self._max_sessions
        if excess <= 0:
            return
        evicted = 0
        for candidate in list(self._lru_order):
            if evicted >= excess:
                break
            if candidate == protect_key:
                continue
            sess = self._sessions.get(candidate)
            if sess is None:
                try:
                    self._lru_order.remove(candidate)
                except ValueError:
                    pass
                continue
            # Idle check: queue empty and turn lock not held.
            if not sess._queue.empty():
                continue
            if sess._turn_lock.locked():
                continue
            logger.info(
                "SessionPool LRU eviction: stopping idle session %s "
                "(cap=%d, size=%d)",
                candidate, self._max_sessions, len(self._sessions),
            )
            try:
                await sess.stop()
            except Exception as exc:
                logger.warning("LRU evict: stop failed for %s: %s", candidate, exc)
            self._sessions.pop(candidate, None)
            try:
                self._lru_order.remove(candidate)
            except ValueError:
                pass
            try:
                from brendbot import discord as _bd
                _bd._admin_alert(
                    "session_evict",
                    f"ℹ️ session pool evicted idle session `{candidate}` "
                    f"(cap={self._max_sessions}).",
                )
            except Exception:
                pass
            evicted += 1
        if evicted < excess:
            logger.warning(
                "SessionPool at %d/%d sessions — all candidates busy, "
                "cap temporarily exceeded",
                len(self._sessions), self._max_sessions,
            )

    async def route_message(
        self,
        platform: str,
        sender_id: str,
        chat_id: str,
        text: str,
        tier: str,
        is_group: bool = False,
        message_id: str = "",
        reply_to_id: str = "",
        reply_to_text: str = "",
        reply_to_author: str = "",
        context_messages: list | None = None,
        is_direct_mention: bool = False,
        domain_hint: str = "",
        address_level: str = "high",
        score: float | None = None,
        haiku_invoked: bool = False,
        guild_id: str = "",
        recv_ts: float | None = None,
        engage_done_ts: float | None = None,
    ) -> None:
        key = f"{platform}:{chat_id}" if is_group else f"{platform}:{sender_id}"

        # Phase 1: check existing session under the global dict lock.
        # Dead sessions are reaped here. If none exists, acquire a per-key
        # creation lock before calling _create() so concurrent messages for
        # the same channel don't each spin up independent sessions.
        async with self._lock:
            session = self._sessions.get(key)
            if session is not None and not session.is_alive():
                logger.warning("Dead session for %s, recreating", key)
                await session.stop()
                del self._sessions[key]
                try:
                    self._lru_order.remove(key)
                except ValueError:
                    pass
                session = None
            if key not in self._creation_locks:
                self._creation_locks[key] = asyncio.Lock()
            creation_lock = self._creation_locks[key]
            if session is not None:
                self._bump_lru(key)

        is_new_session = False
        if session is None:
            # Phase 2: per-key lock serialises creation for this channel only.
            # Other channels are unaffected — no global lock held during _create().
            async with creation_lock:
                # Re-check under creation lock: another coroutine may have
                # completed _create() while we were waiting.
                async with self._lock:
                    session = self._sessions.get(key)
                if session is None or not session.is_alive():
                    session = await self._create(
                        key, tier, platform, chat_id, is_group, guild_id=guild_id,
                    )
                    async with self._lock:
                        self._sessions[key] = session
                        self._bump_lru(key)
                        await self._evict_if_over_cap(protect_key=key)
                    is_new_session = True
        # Keep the stashed guild_id fresh on pre-existing sessions too —
        # allows an existing session to pick up a guild association if it
        # was created before this plumbing existed.
        if session is not None and guild_id and not getattr(session, "_guild_id", ""):
            session._guild_id = guild_id

        context_block = ""
        if context_messages and is_new_session:
            lines = []
            for m in context_messages:
                s = escape(m.get("sender_id", ""))
                n = escape(m.get("display_name", ""))
                mid = escape(m.get("message_id", ""))
                t = escape(m.get("text", ""))
                lines.append(
                    f"  <prior_message sender='{s}' display_name='{n}' message_id='{mid}'>"
                    f"{t}</prior_message>"
                )
            context_block = "\n<channel_context>\n" + "\n".join(lines) + "\n</channel_context>\n"

        msg_id_attr = f" message_id='{message_id}'" if message_id else ""
        reply_block = ""
        if reply_to_id:
            reply_block = (
                f"\n<reply_to message_id={quoteattr(reply_to_id)} "
                f"sender={quoteattr(reply_to_author)}>"
                f"{escape(reply_to_text)}</reply_to>"
            )
        type_attr = " type='group'" if is_group else ""
        domain_attr = f" domain_hint='{escape(domain_hint)}'" if domain_hint else ""
        # address_level surfaces FUSED-CORE Budget Throttle to the model AND is
        # used by _permission_check to cap the per-turn Bash budget below.
        addr_attr = f" address_level='{escape(address_level)}'"

        # Phase 3 #2B — retrieval cue scoring at message ingest.
        # Query prior episodes for this channel matching the current
        # domain hints. If matches exist, prepend a brief <recall> block
        # to the message — bookended summaries from up to 3 prior session
        # segments. This implements the encoding-specificity principle:
        # the cue at retrieval time (current channel + matched domains)
        # surfaces memories whose encoding context matched.
        #
        # Hard-bounded to ~600 chars total to prevent recall bloat.
        # Logged for hit-rate audit.
        recall_block = ""
        try:
            from brendbot.episodes import query_episodes
            current_domains = (
                sorted(domain_hint.split(",")) if domain_hint else []
            )
            episodes = query_episodes(
                channel=chat_id,
                domains=current_domains,
                limit=3,
                # Patch 3: semantic re-rank. The raw inbound text is
                # the recall cue — passing it lets query_episodes
                # cosine-score stored episode summaries against the
                # current question instead of relying purely on the
                # lexical domain-LIKE prefilter + recency order.
                # Degrades gracefully when sentence-transformers is
                # unavailable.
                query_text=text,
            )
            if episodes:
                # Patch A: filter out episodes already injected this session.
                # Episodes are keyed by their SQLite rowid (ep["id"]). An
                # episode already in _recalled_episode_ids is already in the
                # model's context window — re-injecting it adds identical
                # text to every subsequent message with zero information gain.
                novel_episodes = [
                    ep for ep in episodes
                    if ep.get("id") not in session._recalled_episode_ids
                ]
                recall_lines = []
                used_chars = 0
                injected_ids: list[int] = []
                for ep in novel_episodes:
                    summary = (ep.get("summary") or "")[:200]
                    if not summary:
                        continue
                    line = (
                        f"  <prior_episode ts={quoteattr(ep.get('ts_end', ''))} "
                        f"domains={quoteattr(ep.get('domains', ''))} "
                        f"turns='{ep.get('turn_count', 0)}'>"
                        f"{escape(summary)}</prior_episode>"
                    )
                    if used_chars + len(line) > 600:
                        break
                    recall_lines.append(line)
                    used_chars += len(line)
                    if ep.get("id") is not None:
                        injected_ids.append(ep["id"])
                if recall_lines:
                    recall_block = (
                        "\n<recall>\n" + "\n".join(recall_lines) + "\n</recall>\n"
                    )
                    session._recalled_episode_ids.update(injected_ids)
                    logger.info(
                        "[%s] recall injected: %d novel episode(s), %d chars "
                        "(%d already known, skipped)",
                        key, len(recall_lines), used_chars,
                        len(episodes) - len(novel_episodes),
                    )
                elif episodes:
                    logger.debug(
                        "[%s] recall: all %d episode(s) already in context — skipped",
                        key, len(episodes),
                    )
        except Exception as exc:
            logger.warning("[%s] recall query skipped: %s", key, exc)

        wrapped = (
            f"{context_block}"
            f"{recall_block}"
            f"<message platform='{platform}' sender='{sender_id}' "
            f"chat='{chat_id}' tier='{tier}'{type_attr}{msg_id_attr}"
            f"{domain_attr}{addr_attr}"
            f" context_tokens='{session._last_input_tokens}'>"
            f"{reply_block}\n{text}\n</message>"
        )

        session.current_sender_tier = tier
        # Stash address level on the session so _permission_check can read it
        # when enforcing per-turn tool budgets. Reset on every new turn.
        session.current_address_level = address_level
        # Stash turn metadata for feedback logging in _fire_on_text.
        session._turn_user_message_id = message_id
        session._turn_user_text = text
        session._turn_score = score
        session._turn_domains = sorted(domain_hint.split(",")) if domain_hint else []
        # Phase 3 #2A — accumulate domains seen across the whole session segment
        # for the episode row written at restart time.
        if session._turn_domains:
            session._session_domains_seen.update(session._turn_domains)
        session.log_turn("user", text)
        session._tool_call_count = 0
        session._turn_modules_queried = set()
        session._turn_kb_query_used = False
        # Reset per-turn load counters; cumulative counters persist until restart.
        session._turn_bash_calls = 0
        session._turn_other_tool_calls = 0
        # Patch 1a — fresh BudgetState for this turn. BudgetState carries
        # typed caps from agent_core.budgets.Budget (step_cap, time_cap_s)
        # and a monotonic start timestamp; _permission_check reads from
        # it to enforce the wall-clock cap on every tool-call request.
        from brendbot.agent_core.budgets import Budget, BudgetState
        session._turn_budget = BudgetState(budget=Budget(
            step_cap=_TOOL_CALL_BUDGET,
            time_cap_s=_TURN_TIME_CAP_S,
            token_cap=None,
        ))
        session._turn_sender_id = sender_id
        # If the engagement gate fired the haiku classifier on this message,
        # account for it in the cumulative load score (Phase 3 #1A). Also
        # record per-turn for flow-class / fabrication-risk in log_bot_response.
        session._turn_haiku_invoked = bool(haiku_invoked)
        if haiku_invoked:
            session._cumulative_haiku_invocations += 1

        # Phase 2a — carry discord.py-side timing stamps onto the session.
        # These are time.monotonic() values; deltas are computed in
        # _fire_on_text / _fire_on_text_streamed. None is tolerated at every
        # stage (callers that don't instrument still work). Set before the
        # complexity preflight so an early refusal still logs timings.
        session._turn_t_received = recv_ts
        session._turn_t_engage_gate_done = engage_done_ts
        session._turn_t_content_gate_done = None
        session._turn_t_first_token = None

        # Patch 1b — agent_core.complexity pre-flight. Before the content
        # gate spends a haiku call and the session spends a full
        # generation pass, classify the inbound request by Chapter-5
        # complexity tier. Hard-reject requests that classify as RE
        # (halting-problem-adjacent) or NON_RE (beyond semi-decidable)
        # with a local refusal — the model would either loop or
        # fabricate a "proof" that isn't one, and neither is cheap.
        # PSPACE hints (long-horizon planning, game-tree search) are
        # not rejected but are logged; callers may still get a useful
        # approximation.
        try:
            from brendbot.agent_core.complexity import (
                classify as _complexity_classify,
                Route as _ComplexityRoute,
                Tier as _ComplexityTier,
            )
            classification = _complexity_classify(text)
            if classification.route == _ComplexityRoute.REJECT:
                logger.info(
                    "[%s] complexity preflight REJECT tier=%s rationale=%s",
                    session.key, classification.tier.name, classification.rationale,
                )
                refusal = (
                    f"can't take that one — the request is tier "
                    f"{classification.tier.name} "
                    f"({classification.rationale}). "
                    "beyond what can be decided in finite time; "
                    "reframe with a concrete bound and I can try."
                )
                asyncio.create_task(session._fire_on_text(refusal))
                return
            if classification.tier == _ComplexityTier.PSPACE:
                logger.info(
                    "[%s] complexity preflight PSPACE tier (bounded_search path) — %s",
                    session.key, classification.rationale,
                )
        except Exception as exc:
            # Preflight is advisory; fall through on any error to keep
            # the response path live.
            logger.debug(
                "[%s] complexity preflight error (advisory, ignoring): %s",
                session.key, exc,
            )

        # Content gate (phase 4) runs here, between turn state setup and
        # inject. Decides PASS / FLAG / REFUSE / FLOOR_HIT / BYPASS via
        # a one-shot haiku classifier spawn. Returns 'inject' if the caller
        # should proceed with normal injection, or 'handled' if the gate
        # already dispatched a response (refusal, flagged-path reroute,
        # or budget-exhausted message) via _fire_on_text.
        try:
            gate_action = await session.apply_content_gate(
                wrapped_text=wrapped,
                raw_user_text=text,
                tier=tier,
                sender_id=sender_id,
                message_id=message_id,
            )
        except Exception as exc:
            logger.error(
                "[%s] content gate raised unexpected error, failing open to inject: %s",
                session.key, exc,
            )
            gate_action = "inject"
        # Phase 2a — stamp content-gate-done after the classifier returns.
        # Stamped regardless of gate_action so FLAG/REFUSE/BYPASS turns still
        # emit a timing for the gate stage (useful when tuning gate latency
        # relative to generation latency).
        session._turn_t_content_gate_done = time.monotonic()

        if gate_action == "inject":
            # Stage 4 — expand the KB index on first domain match. The
            # stub injected at session creation points to this mechanism.
            # The full block is injected as housekeeping so the model
            # doesn't respond to it but learns the full query reference
            # before tackling the domain-matching message.
            if (
                domain_hint
                and not session._kb_index_expanded
                and self._cached_kb_index_block
            ):
                try:
                    session._next_turn_is_housekeeping = True
                    await session.inject(
                        "<kb_index_expansion>\n"
                        "The following index is now live. Use it to answer "
                        "the next message and any subsequent domain-matched "
                        "queries in this session.\n"
                        + self._cached_kb_index_block
                        + "\n</kb_index_expansion>",
                        housekeeping=True,
                    )
                    session._kb_index_expanded = True
                except Exception as exc:
                    logger.warning(
                        "[%s] KB index expansion inject failed: %s",
                        session.key, exc,
                    )
            # Premise Check enforcement — pre-fetch structured module
            # content for any matched module not already grounded in this
            # session. Converts the documented gate from "model chooses to
            # call kb-query" to "module data is already in context". The
            # [ctx] suffix (set by discord.py when the match came from
            # recent-channel-context fallback rather than the current
            # message) is stripped before lookup — the grounded_facts
            # block should fire on context matches too, since the model
            # still needs the data to reason correctly.
            if domain_hint:
                new_modules: list[str] = []
                for token in domain_hint.split(","):
                    mod = token.strip().replace("[ctx]", "")
                    if mod and mod not in session._grounded_modules:
                        new_modules.append(mod)
                for mod in new_modules:
                    block = self._fetch_grounded_facts(mod)
                    if not block:
                        # Mark as grounded anyway so we don't retry every
                        # message for modules that have no structured data
                        # (e.g. IMAGEGEN — queried on-demand via imgstyle).
                        session._grounded_modules.add(mod)
                        continue
                    try:
                        session._next_turn_is_housekeeping = True
                        await session.inject(
                            "[SYSTEM] Pre-fetched module content for Premise "
                            f"Check. Use this to answer the next message "
                            f"rather than answering from general knowledge.\n"
                            + block,
                            housekeeping=True,
                        )
                        session._grounded_modules.add(mod)
                        logger.info(
                            "[%s] grounded_facts injected: module=%s (%d chars)",
                            session.key, mod, len(block),
                        )
                    except Exception as exc:
                        logger.warning(
                            "[%s] grounded_facts inject failed for %s: %s",
                            session.key, mod, exc,
                        )
            await session.inject(wrapped)
        # else 'handled' — gate dispatched directly, no inject needed

    async def _create(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool,
        guild_id: str = "",
    ) -> Session:
        safe_id = chat_id.replace("+", "_").replace("/", "_").replace("=", "")
        prefix = "group_" if is_group else ""
        transcript_dir = TRANSCRIPTS_DIR / platform / f"{prefix}{safe_id}"
        transcript_dir.mkdir(parents=True, exist_ok=True)

        claude_md = transcript_dir / "CLAUDE.md"
        # Build send_command and companion script paths — all resolved at runtime
        # from SCRIPTS_DIR so hardcoded paths never appear in soul files.
        send_cmd = f'{SCRIPTS_DIR}/send-discord "{chat_id}" "<message>"'
        react_cmd = f'{SCRIPTS_DIR}/react-discord "{chat_id}"'
        generate_image_cmd = f'{SCRIPTS_DIR}/generate-image'
        kb_db_path = PROJECT_ROOT / "brendbot" / "knowledge" / "knowledge.db"

        template_file = "GROUP_SOUL.md" if is_group else "SOUL.md"
        # Read from SessionPool cache instead of hitting disk on every spawn.
        # Refreshable via SessionPool.refresh_cache() (SIGHUP-wired in main.py).
        template = self._cached_group_soul if is_group else self._cached_soul
        prompt = _render(template, {
            "bot_name": self._bot_name,
            "send_command": send_cmd,
            "react_command": react_cmd,
            "generate_image_command": generate_image_cmd,
            "kb_path": str(kb_db_path),
            "platform_name": "Discord",
        })

        # Append cached FUSED-CORE.md content (was a per-spawn disk read).
        if self._cached_fused_core:
            prompt += "\n\n" + self._cached_fused_core

        # Append the lightweight KB stub instead of the full index.
        # The full MODULES + KNOWLEDGE QUERY TOOL block is deferred to the
        # first domain-matched message and injected as housekeeping via
        # route_message. See Session._kb_index_expanded and the Stage 4
        # note in SessionPool._build_kb_index_stub for rationale.
        if self._cached_kb_index_stub:
            prompt += self._cached_kb_index_stub

        # Append compact user registry table so the model can resolve
        # @mention snowflakes at reasoning time without a tool call. Lists
        # the _COMPACT_TABLE_MAX most recently active server members with
        # their display names, tiers, message counts, and domain history.
        # The bot's own ID is excluded from the table.
        try:
            from brendbot.user_registry import compact_table
            from brendbot.discord import _discord_client as _dc
            _bot_id = str(_dc.user.id) if _dc and _dc.user else ""
            # Filter to this guild's users so a Wheat session doesn't see
            # Pizzacord members in its user table (multi-server isolation).
            # Empty guild_id (DM sessions or unplumbed callers) falls back
            # to the unfiltered global list.
            _user_table = compact_table(bot_id=_bot_id, guild_id=guild_id)
            if _user_table:
                prompt += (
                    "\n\n## SERVER MEMBERS\n"
                    "Known server members. Use this table to resolve @mention "
                    "snowflakes — if a mention target is not you, do not respond "
                    "unless your name also appears in the message.\n"
                    + _user_table
                )
        except Exception as exc:
            logger.debug("user_registry compact_table inject failed: %s", exc)

        claude_md.write_text(prompt)

        session_tier = "admin" if is_group else tier
        session = Session(
            key=key,
            tier=session_tier,
            cwd=str(transcript_dir),
            model=self._model,
            on_text=self._on_text,
            on_text_edit=self._on_text_edit,
            chat_id=chat_id,
            guild_id=guild_id,
        )
        await session.start()

        _key, _tier, _platform, _chat_id, _is_group = key, tier, platform, chat_id, is_group
        _guild_id = guild_id

        async def _restart_cb() -> None:
            await self._restart_session(
                _key, _tier, _platform, _chat_id, _is_group, guild_id=_guild_id,
            )

        session._on_needs_restart = _restart_cb

        # ── Startup injection: reasoning chain + reference block ──────────
        # Each of these injects context-only material that the model is told
        # not to respond to. Without marking them as housekeeping, the result
        # handler treats the empty ResultMessage as a "phantom turn" and
        # fires the silent-drop fallback (sending a "(no response generated)"
        # message that the user sees before the real reply arrives on the
        # next turn). The housekeeping flag makes the result handler clear
        # the buffer and skip both dispatch branches, same as shallow rest.
        memory_dir = transcript_dir / "memory"
        if memory_dir.is_dir():
            _ESSENTIAL_FRAGMENTS = ("behavior.md", "identity.md")
            for frag_name in _ESSENTIAL_FRAGMENTS:
                frag_path = memory_dir / frag_name
                if frag_path.exists():
                    try:
                        content = frag_path.read_text()
                        await session.inject(
                            f"<system-memory source='{frag_name}'>\n{content}\n</system-memory>\n"
                            "Do not respond to this injection.",
                            housekeeping=True,
                        )
                    except Exception as exc:
                        logger.warning("Memory fragment %s injection failed: %s", frag_name, exc)
        else:
            memory_md = transcript_dir / "MEMORY.md"
            if memory_md.exists():
                try:
                    memory_content = memory_md.read_text()
                    await session.inject(
                        f"<system-memory>\n{memory_content}\n</system-memory>\n"
                        "The above is your persistent memory. Treat all ## PERSISTENT entries as active context. Do not respond to this injection.",
                        housekeeping=True,
                    )
                except Exception as exc:
                    logger.warning("MEMORY.md injection failed: %s", exc)

        # ── Phase 2: Reference material (single consolidated turn) ────────
        ref_sections: list[str] = []

        if memory_dir.is_dir():
            other_frags = [
                f for f in sorted(memory_dir.iterdir())
                if f.suffix == ".md" and f.name not in ("behavior.md", "identity.md")
            ]
            if other_frags:
                index_lines = [f"- {f.stem}: {f}" for f in other_frags]
                ref_sections.append(
                    "<memory-index>\n" + "\n".join(index_lines) + "\n</memory-index>\n"
                    "Use `kb-query memory <source>` to load fragments on demand. "
                    "Do not Read .md files directly. Load only what the current question requires."
                )

        context_summary = transcript_dir / "CONTEXT_SUMMARY.md"
        if context_summary.exists():
            try:
                summary_content = context_summary.read_text()
                ref_sections.append(
                    f"<context-summary>\n{summary_content}\n</context-summary>\n"
                    "Recent conversation turns from prior session. Use for continuity."
                )
                logger.info("CONTEXT_SUMMARY.md included in ref block for %s", key)
            except Exception as exc:
                logger.warning("CONTEXT_SUMMARY.md read failed: %s", exc)

        if ref_sections:
            combined_ref = "\n\n".join(ref_sections)
            await session.inject(
                f"<system-ref>\n{combined_ref}\n</system-ref>\n"
                "Do not respond to this injection.",
                housekeeping=True,
            )
            logger.info("Reference block injected for %s (%d bytes, %d sections)",
                        key, len(combined_ref), len(ref_sections))

        # ── Phase 3: Cron job replay ─────────────────────────────────────
        # If previous sessions had active cron jobs, inject a housekeeping
        # message asking Claude to re-create them via CronCreate.
        persisted_crons = Session.load_persisted_crons(str(transcript_dir))
        if persisted_crons:
            cron_specs = json.dumps(persisted_crons, indent=2)
            await session.inject(
                "[SYSTEM] The following cron jobs were active before your last session reset. "
                "Please re-create them now using CronCreate for each entry:\n"
                f"```json\n{cron_specs}\n```",
                housekeeping=False,
            )
            logger.info("Cron replay injected for %s (%d cron(s))",
                        key, len(persisted_crons))

        return session

    async def _restart_session(
        self, key: str, tier: str, platform: str, chat_id: str, is_group: bool,
        guild_id: str = "",
    ) -> None:
        logger.info("Restarting session %s after context refresh", key)
        async with self._lock:
            old_session = self._sessions.pop(key, None)
        # Preserve guild_id across restarts: fall back to the dying
        # session's stashed value if no explicit guild_id was passed.
        if not guild_id and old_session is not None:
            guild_id = getattr(old_session, "_guild_id", "") or ""
        if old_session:
            try:
                await old_session.stop()
            except Exception as exc:
                logger.warning("Error stopping session %s during restart: %s", key, exc)
        # Use per-key creation lock so restart doesn't block other channels.
        creation_lock = self._creation_locks.get(key)
        if creation_lock is None:
            creation_lock = asyncio.Lock()
            self._creation_locks[key] = creation_lock
        async with creation_lock:
            try:
                new_session = await self._create(
                    key, tier, platform, chat_id, is_group, guild_id=guild_id,
                )
                async with self._lock:
                    self._sessions[key] = new_session
                    self._bump_lru(key)
                logger.info("Session %s restarted successfully", key)
            except Exception as exc:
                logger.error("Failed to recreate session %s after restart: %s", key, exc)

    async def stop_all(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.stop()
            self._sessions.clear()
        # Drain the warm classifier pool so spawned subprocesses don't leak
        pool = get_classifier_pool()
        await pool.drain()

