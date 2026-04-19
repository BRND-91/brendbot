"""Message signatures for Phase 5 semantic-cache keying.

A stable, deterministic hash over a normalized message plus a digest of
its recent context. Same (text, context) pair → same signature, across
process restarts and across sessions. Phase 5 will wire this into a
response cache so recurring prompts hit a stored answer instead of
paying another full generation.

Not yet used at runtime in Phase 2b. Sitting ready for Phase 5 and
tested in isolation so the contract (pure, stable, no I/O) is pinned
before anything starts depending on the bytes.

Design notes:

- Uses SHA-256 truncated to 32 hex chars (128 bits). That's vanishingly
  low collision risk for a session-scope cache (a channel seeing 10^6
  messages would have ~10^-26 pairwise collision probability) while
  keeping keys compact enough to read in a log line.

- Normalization collapses whitespace and lowercases. This is
  deliberately simple — Phase 5 will layer a more aggressive normalizer
  (punctuation stripping, stopword removal, etc.) on top if the cache
  hit rate proves disappointing. Starting simple keeps the risk of
  false matches low: "yes" and "YES" are the same query but "yes i
  agree" and "yes, i agree" should almost certainly cache-hit too.

- Context digest is a 16-char prefix of the SHA-256 over the joined
  last 5 context texts. Shorter than the message sig because context
  is noisier and a strict match there would cut the hit rate too
  hard; a 64-bit context space is plenty to avoid same-message /
  different-conversation collisions in any realistic channel volume.

- No I/O, no logging, no config reads. Safe to call from any thread,
  sync or async.
"""
from __future__ import annotations

import hashlib

# Field separator used when joining message text with the context digest
# before hashing. ASCII 0x1F (unit separator) — won't appear in normal
# user text, so it can't be ambiguously injected by a crafted message.
_SEP = "\x1f"

# Entry separator when joining multiple context entries into one blob.
# ASCII 0x1E (record separator) — same non-overlap guarantee as above.
_ENTRY_SEP = "\x1e"

# How many recent context entries feed into the context signature.
# Matches the window size the haiku classifier sees (discord.py takes
# context[-5:] before passing to haiku_classify), so a cache hit in
# Phase 5 will see the same conversational window the classifier did.
_CONTEXT_WINDOW = 5

# Hex-prefix lengths. Kept as named constants so Phase 5 can tune.
_MESSAGE_HASH_LEN = 32   # 128-bit
_CONTEXT_HASH_LEN = 16   # 64-bit


def _normalize(text: str) -> str:
    """Collapse whitespace and lowercase. Empty string in → empty out."""
    if not text:
        return ""
    return " ".join(text.lower().split())


def context_signature(recent_context: list[dict] | None) -> str:
    """SHA-256 prefix over the last N context entries.

    Accepts the same list-of-dicts shape discord.py already uses:
    entries with a "text" key plus whatever other fields (sender_id,
    timestamp, etc.) — only "text" is consumed here. Empty / None
    context → empty string (not a zero hash — the empty string is a
    meaningful "no context" signal).
    """
    if not recent_context:
        return ""
    parts = []
    for entry in recent_context[-_CONTEXT_WINDOW:]:
        if not isinstance(entry, dict):
            continue
        normalized = _normalize(entry.get("text", "") or "")
        # Skip entries that normalize to empty — they carry no
        # conversational signal and including them would produce a
        # different cache key than a None/[] context that means the
        # same thing ("no meaningful context").
        if not normalized:
            continue
        parts.append(normalized)
    if not parts:
        return ""
    joined = _ENTRY_SEP.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_CONTEXT_HASH_LEN]


def message_signature(
    text: str,
    recent_context: list[dict] | None = None,
) -> str:
    """Stable SHA-256 prefix over normalized message text + context digest.

    Returns a 32-char hex string (128-bit). Same inputs → same output
    across processes, machines, Python versions.

    Designed for Phase 5 cache keying. Phase 2b adds the function so
    the contract can be tested independently of any cache implementation.
    """
    norm = _normalize(text)
    ctx = context_signature(recent_context)
    payload = f"{norm}{_SEP}{ctx}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_MESSAGE_HASH_LEN]
