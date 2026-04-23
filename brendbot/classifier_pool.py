"""Warm classifier pool + one-shot classifier entry points.

Previously colocated in ``session.py``; extracted as part of the Stage 4
repo cleanup. Nothing in this module depends on ``Session`` /
``SessionPool`` state — the pool is a module-level singleton and each
classifier function is self-contained.

Public surface
--------------
- ``ClassifierPool`` — pool of pre-connected one-shot haiku clients.
- ``get_classifier_pool`` / ``warm_classifier_pool`` — module-level
  singleton access and boot-time warm-up.
- ``acquire_classifier_client`` — ``async with`` context manager that
  collapses the acquire/dispose try/finally pattern used by every
  classifier entry point.
- ``haiku_classify`` — engagement-gate classifier.
- ``content_gate_classify`` — content-safety classifier (with parse-
  error retry).
- ``content_gate_cross_check_floor`` — second-pass hard-floor
  verification.

``flagged_generate`` was deleted in the 2026-04-23 strip — see the
note near the bottom of this module and CLEANUP_LOG.md. The content
gate no longer emits a FLAG outcome, so there's no caller.

Call sites inside ``session.py`` continue to reference these names via
``from brendbot.classifier_pool import …`` re-imports, so tests that
monkeypatch ``brendbot.session.content_gate_classify`` still work —
``session``'s module namespace owns the binding the gate function
resolves.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Warm classifier pool — pre-spawned ClaudeSDKClient instances
# ---------------------------------------------------------------------------
#
# Both the engagement classifier (haiku_classify) and the content-gate
# classifier (content_gate_classify) previously spawned a fresh
# ClaudeSDKClient per call. Subprocess spawn + connect is ~18s cold,
# ~7-8s warm. ClassifierPool pre-spawns N connected clients at boot and
# replenishes in the background after each acquisition, eliminating the
# cold-start penalty for classifier calls.
#
# The pool is a module-level singleton initialised by SessionPool or by
# calling warm_classifier_pool() from main.py during boot-split.
# ---------------------------------------------------------------------------

_CLASSIFIER_POOL_SIZE = 3  # target warm clients in the pool


class ClassifierPool:
    """Maintains a rotating pool of pre-connected haiku ClaudeSDKClient
    instances for one-shot classifier calls.

    Usage:
        async with acquire_classifier_client() as client:
            await client.query(prompt)
            ... read response ...

        # or, low-level:
        client = await pool.acquire()
        try:
            ...
        finally:
            await pool.dispose(client)
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
                    # discord.py imports this module via haiku_classify.
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


@asynccontextmanager
async def acquire_classifier_client(
    pool: ClassifierPool | None = None,
) -> AsyncIterator[ClaudeSDKClient]:
    """Async context manager that acquires a classifier client from the
    pool and disposes of it on exit.

    Collapses the three copies of::

        client = await pool.acquire()
        try:
            ...
        finally:
            await pool.dispose(client)

    into::

        async with acquire_classifier_client() as client:
            ...

    Parameters
    ----------
    pool:
        Optional pool override. Defaults to the module-level singleton —
        tests or alternate callers can inject their own pool here.
    """
    pool = pool or get_classifier_pool()
    client = await pool.acquire()
    try:
        yield client
    finally:
        await pool.dispose(client)


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

    try:
        async with acquire_classifier_client() as classifier_client:
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
        async with acquire_classifier_client() as classifier_client:
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
            criteria={"_parse_error": 10.0},
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

    async def _one_call() -> str:
        async with acquire_classifier_client() as classifier_client:
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


# ``flagged_generate`` was removed in the 2026-04-23 strip. The
# content-gate FLAG outcome used to reroute ambiguous-band messages to
# a soul-stripped claude-sonnet-4-6 call via this function; the
# reroute produced confident out-of-character output ("happy to oblige"
# LinkedIn voice, bold headers, life-coach framing) and was the worst-
# behaving subsystem in the 2026-04-23 pilot. Collapsing FLAG into
# PASS inside ``decide_outcome`` made this function unreachable; it's
# deleted to prevent accidental reintroduction.
