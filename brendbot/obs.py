"""Observability: append-only JSONL logging for runtime events.

Central module that owns every ``logs/*.jsonl`` writer the bot uses.
The single public function :func:`append_jsonl` takes a log name and a
dict, writes a single line, rotates at 10MB, and swallows I/O errors.
Callers never see an exception — observability must never block actual
behaviour.

Why this module exists (2026-04-24 structural-honesty pass)
-----------------------------------------------------------
The bot kept lying about its own past activity because there was no
external ground truth to read. The CoT-faithfulness literature is
clear that models cannot reliably introspect their own prior
reasoning, and OpenAI's September 2025 hallucination paper shows that
the training incentives actively reward confident guessing over
calibrated abstention. You don't fix that by asking the model to try
harder. You externalise state so the model is forced to read rather
than recall.

Every category of self-report the bot might make — tool calls, image
prompts, music prompts, turn events, gate outcomes, runtime errors —
now has a log file here. Soul rules (see ``GROUP_SOUL.md::SELF-REPORT
RULES``) direct the model to read the relevant log before answering
questions about past activity. The model is not trusted to narrate
itself. The log is the source of truth.

Files and their shapes
----------------------
``logs/tool_calls.jsonl``     — every Bash/Read/Write/Edit/Glob/Grep/Web* call.
``logs/image_prompts.jsonl``  — every ``scripts/generate-image`` invocation (PR #22).
``logs/music_gens.jsonl``     — every music generation invocation.
``logs/turn_events.jsonl``    — structured turn_complete records.
``logs/gate_events.jsonl``    — every content_gate outcome.
``logs/errors.jsonl``         — API errors, subprocess crashes, classifier failures.

Rotation
--------
Each log file rotates at ``_ROTATE_BYTES`` (10MB). Rotated files are
``<name>.1.jsonl``, ``<name>.2.jsonl``, ... up to ``_MAX_ROTATIONS``
(10). Oldest is dropped when the limit is exceeded. No time-based
rotation — size is the only axis — so a quiet week doesn't roll
files and a busy day doesn't overrun disk.

Failure semantics
-----------------
Every public-surface call is wrapped in a bare ``except Exception``.
A failed write is logged to the Python ``logging`` module at DEBUG
(not WARNING — we don't want observability-of-observability to be
noisy) and returns silently. The caller's control flow is never
interrupted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Repo root; logs/ is a sibling of brendbot/.
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# Rotation thresholds. 10MB per file, 10 files max → 100MB cap per
# log name. Tune by changing these; no operator-facing config needed
# because nobody should ever be manually reading 100MB of jsonl.
_ROTATE_BYTES = 10 * 1024 * 1024
_MAX_ROTATIONS = 10


def _log_path(name: str) -> Path:
    """Resolve a log name (like ``tool_calls``) to its absolute path.

    Accepts either ``tool_calls`` or ``tool_calls.jsonl``; both resolve
    to ``<repo>/logs/tool_calls.jsonl``. Parent dir creation is handled
    at write time, not here, so a test can safely call this without
    side effects."""
    if not name.endswith(".jsonl"):
        name = name + ".jsonl"
    return _LOGS_DIR / name


def _rotate_if_needed(path: Path) -> None:
    """If ``path`` is at or past ``_ROTATE_BYTES``, shift the rotation
    chain one position (``.9 → dropped, .8 → .9, ..., base → .1``).

    Best-effort. If any step fails (permission, race with another
    process, file locked on Windows) we swallow and let the next
    write go onto the too-large file; eventually a successful
    rotation will happen."""
    try:
        if not path.exists() or path.stat().st_size < _ROTATE_BYTES:
            return
        # Drop the oldest rotation if it exists.
        oldest = path.with_suffix(f".{_MAX_ROTATIONS}.jsonl")
        if oldest.exists():
            try:
                oldest.unlink()
            except Exception:
                pass
        # Shift each rotated file down by one (.N-1 → .N), highest first.
        for i in range(_MAX_ROTATIONS - 1, 0, -1):
            src = path.with_suffix(f".{i}.jsonl")
            dst = path.with_suffix(f".{i + 1}.jsonl")
            if src.exists():
                try:
                    src.rename(dst)
                except Exception:
                    pass
        # Move the base file to .1.
        try:
            path.rename(path.with_suffix(".1.jsonl"))
        except Exception:
            pass
    except Exception as exc:
        logger.debug("obs.rotate failed for %s: %s", path, exc)


def append_jsonl(name: str, entry: dict[str, Any]) -> None:
    """Append one JSON line to ``logs/<name>.jsonl``.

    Never raises. Never blocks on exceptions. The entry always gets
    a ``ts`` field (seconds since epoch, float) added if the caller
    didn't supply one; callers can override by including ``ts`` in
    the entry dict.

    The write is a single ``f.write(json.dumps(entry) + "\\n")`` call
    under an ``open(append)`` context, which on POSIX is atomic for
    buffer sizes under PIPE_BUF — i.e. a concurrent reader doing
    ``tac ... | grep`` never sees a partial line. Lines longer than
    PIPE_BUF (4KB on Linux) can in principle interleave; in practice
    no single jsonl entry here approaches that."""
    try:
        if "ts" not in entry:
            entry = {"ts": time.time(), **entry}
        path = _log_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("obs.append_jsonl failed for %s: %s", name, exc)


# ── Typed convenience wrappers ───────────────────────────────────────────
#
# These wrap ``append_jsonl`` with fixed schemas per log file so call
# sites in the instrumented modules don't re-type the field names every
# time and the schema is centralized here. Adding a field to one of
# these wrappers is the recommended way to extend a log — search is
# easier than grepping for the log name across modules.


def log_tool_call(
    *,
    session_key: str,
    turn_id: str,
    tool: str,
    input_summary: str,
    output_shape: dict[str, Any] | None = None,
) -> None:
    """One line per Bash/Read/Write/Edit/Glob/Grep/WebSearch/WebFetch/etc.

    ``input_summary`` is truncated to 200 chars by the caller; this
    function does not re-truncate. ``output_shape`` is a small dict
    (``{"bytes": N, "lines": N, "error": bool}``) — not the output
    itself, which would blow the log size. Callers that don't yet
    know the output (log-at-start pattern) may pass ``None``."""
    append_jsonl("tool_calls", {
        "session_key": session_key,
        "turn_id": turn_id,
        "tool": tool,
        "input_summary": input_summary[:200],
        "output_shape": output_shape,
    })


def log_turn_event(
    *,
    session_key: str,
    channel_id: str,
    turn_id: str,
    model: str,
    context_tokens: int,
    cost_usd: float,
    duration_ms: int,
    text_emitted: bool,
    tool_call_count: int,
    stop_reason: str | None = None,
) -> None:
    """One line per turn-complete. Matches the structured fields that
    used to live as unstructured log.info() calls in session_handler.
    Retains the human log line (for tail -f during dev) and adds this
    structured record so the bot can read its own turn history."""
    append_jsonl("turn_events", {
        "session_key": session_key,
        "channel_id": channel_id,
        "turn_id": turn_id,
        "model": model,
        "context_tokens": context_tokens,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "text_emitted": text_emitted,
        "tool_call_count": tool_call_count,
        "stop_reason": stop_reason,
    })


def log_gate_event(
    *,
    session_key: str,
    channel_id: str,
    message_id: str,
    outcome: str,
    weighted_sum: float | None,
    criteria: dict[str, float] | None,
    refusal_text: str | None,
    user_text_preview: str,
) -> None:
    """One line per content_gate outcome (PASS, REFUSE, FLOOR_HIT,
    friend-tier skip, bypass). ``refusal_text`` is None for PASS.

    This log is how the bot answers "why did you refuse" without
    guessing. The soul's SELF-REPORT RULES section directs it to grep
    this file by message_id."""
    append_jsonl("gate_events", {
        "session_key": session_key,
        "channel_id": channel_id,
        "message_id": message_id,
        "outcome": outcome,
        "weighted_sum": weighted_sum,
        "criteria": criteria,
        "refusal_text": refusal_text,
        "user_text_preview": user_text_preview[:200],
    })


def log_error(
    *,
    session_key: str,
    error_class: str,
    error_msg: str,
    context_tokens: int | None = None,
    recoverable: bool = True,
    detail: dict[str, Any] | None = None,
) -> None:
    """One line per API 529, subprocess crash, classifier parse-error,
    tool timeout, or similar runtime failure. This is the log the
    runtime_events.signal_runtime_error call reads from when surfacing
    an error to Discord, and the log the bot consults on
    'why did X fail' questions."""
    append_jsonl("errors", {
        "session_key": session_key,
        "error_class": error_class,
        "error_msg": error_msg[:500],
        "context_tokens": context_tokens,
        "recoverable": recoverable,
        "detail": detail,
    })
