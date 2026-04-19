"""Lightweight cache for haiku classifier results.

Caches engagement and content-gate classifier results keyed on the full
classifier prompt string. Two-tier lookup:

1. Exact-match: SHA-256 hash of the prompt → cached result. Catches
   repeated identical messages (common in group chat spam, greetings).

2. Semantic-match (Patch 4): shared-singleton sentence-transformer
   embedding of the *variable* part of the prompt (caller-supplied
   `semantic_key`). On exact-hash miss, linear-scan the stored entries,
   compute cosine similarity against the query embedding, and return
   the best match if it clears _SEMANTIC_THRESHOLD.

Cache is in-memory, bounded by a ring buffer with LRU eviction. Thread-
safe via asyncio (single-threaded event loop — no locks needed for
dict access).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from brendbot import embedding_model

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SIZE = 500
_DEFAULT_TTL_SECONDS = 300  # 5 minutes — matches Anthropic's prompt cache TTL

# Cosine threshold for the semantic tier. Restrictive on purpose:
# engagement and content-gate classifier answers are binary-ish
# (YES/NO + tone; TRIGGERED criteria list), so a false-positive cache
# hit can cross-contaminate the result between genuinely different
# user intents. 0.92 admits close paraphrases and near-identical
# messages while rejecting topical similarity.
_SEMANTIC_THRESHOLD = 0.92


@dataclass
class CacheEntry:
    result: Any
    created_at: float = field(default_factory=time.monotonic)
    # Pre-computed normalized float32 vector of the caller-supplied
    # semantic_key. None when the caller didn't provide a semantic key
    # or when the embedding model wasn't available at put() time.
    semantic_vec: Any = None


class ClassifierCache:
    """LRU cache for classifier results keyed on prompt hash.

    Usage:
        cache = ClassifierCache()
        hit = cache.get(prompt_string, semantic_key=user_text)
        if hit is not None:
            return hit
        result = await expensive_classify(...)
        cache.put(prompt_string, result, semantic_key=user_text)

    `semantic_key` is optional. When supplied AND the embedding model
    is available, the cache additionally supports semantic (cosine)
    matching against prior entries on exact-hash miss. Callers should
    pass the *variable* portion of the prompt (the user's message),
    not the full prompt — the large static rules prefix dominates
    full-prompt embeddings and produces false-positive matches.
    """

    def __init__(
        self,
        max_size: int = _DEFAULT_MAX_SIZE,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        semantic_threshold: float = _SEMANTIC_THRESHOLD,
    ) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._semantic_threshold = semantic_threshold
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._semantic_hits = 0

    @staticmethod
    def _hash(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def _is_fresh(self, entry: CacheEntry) -> bool:
        return (time.monotonic() - entry.created_at) <= self._ttl

    def get(self, prompt: str, semantic_key: str = "") -> Any | None:
        """Return cached result if prompt matches and entry is fresh.

        Exact-match tier: SHA-256 hash on full prompt.
        Semantic tier (optional): when `semantic_key` is non-empty AND
        the embedding model is available, linear-scan stored entries
        and return the best cosine match above threshold.
        """
        key = self._hash(prompt)
        entry = self._store.get(key)
        if entry is not None:
            if not self._is_fresh(entry):
                del self._store[key]
            else:
                self._store.move_to_end(key)
                self._hits += 1
                return entry.result

        # Exact-match miss. Try semantic tier if caller provided a key.
        if semantic_key and embedding_model.is_available():
            query_vec = embedding_model.embed_vector(semantic_key)
            if query_vec is not None:
                best_score = -1.0
                best_entry: tuple[str, CacheEntry] | None = None
                # Drop stale entries we encounter. This piggybacks eviction
                # on lookups instead of running a separate sweep.
                for k, e in list(self._store.items()):
                    if not self._is_fresh(e):
                        del self._store[k]
                        continue
                    if e.semantic_vec is None:
                        continue
                    try:
                        import numpy as np  # type: ignore

                        score = float(np.dot(e.semantic_vec, query_vec))
                    except Exception:
                        continue
                    if score > best_score:
                        best_score = score
                        best_entry = (k, e)
                if best_entry is not None and best_score >= self._semantic_threshold:
                    k, e = best_entry
                    self._store.move_to_end(k)
                    self._semantic_hits += 1
                    self._hits += 1
                    logger.debug(
                        "classifier_cache semantic hit (score=%.3f)",
                        best_score,
                    )
                    return e.result

        self._misses += 1
        return None

    def put(self, prompt: str, result: Any, semantic_key: str = "") -> None:
        """Store a result. Evicts oldest entry if at capacity.

        When `semantic_key` is provided AND the embedding model is
        available, the entry carries a precomputed normalized float32
        vector so future get() calls can semantic-match against it."""
        key = self._hash(prompt)
        semantic_vec = None
        if semantic_key and embedding_model.is_available():
            semantic_vec = embedding_model.embed_vector(semantic_key)

        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = CacheEntry(
                result=result, semantic_vec=semantic_vec,
            )
            return
        if len(self._store) >= self._max_size:
            self._store.popitem(last=False)  # evict oldest
        self._store[key] = CacheEntry(
            result=result, semantic_vec=semantic_vec,
        )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "semantic_hits": self._semantic_hits,
            "size": len(self._store),
        }

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0
        self._semantic_hits = 0


# Module-level singletons — one per classifier type.
_engage_cache: ClassifierCache | None = None
_content_cache: ClassifierCache | None = None


def get_engage_cache() -> ClassifierCache:
    global _engage_cache
    if _engage_cache is None:
        _engage_cache = ClassifierCache()
    return _engage_cache


def get_content_cache() -> ClassifierCache:
    global _content_cache
    if _content_cache is None:
        _content_cache = ClassifierCache()
    return _content_cache
