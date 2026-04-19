"""Lightweight cache for haiku classifier results.

Caches engagement and content-gate classifier results keyed on the full
classifier prompt string. Two-tier lookup:

1. Exact-match: SHA-256 hash of the prompt → cached result. Catches
   repeated identical messages (common in group chat spam, greetings).

2. (Future) Semantic similarity: sentence-transformer embedding + cosine
   similarity threshold. Requires `sentence-transformers` package.

Cache is in-memory, bounded by a ring buffer with LRU eviction. Thread-safe
via asyncio (single-threaded event loop — no locks needed for dict access).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SIZE = 500
_DEFAULT_TTL_SECONDS = 300  # 5 minutes — matches Anthropic's prompt cache TTL


@dataclass
class CacheEntry:
    result: Any
    created_at: float = field(default_factory=time.monotonic)


class ClassifierCache:
    """LRU cache for classifier results keyed on prompt hash.

    Usage:
        cache = ClassifierCache()
        hit = cache.get(prompt_string)
        if hit is not None:
            return hit
        result = await expensive_classify(...)
        cache.put(prompt_string, result)
    """

    def __init__(
        self,
        max_size: int = _DEFAULT_MAX_SIZE,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _hash(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def get(self, prompt: str) -> Any | None:
        """Return cached result if prompt matches and entry is fresh."""
        key = self._hash(prompt)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if (time.monotonic() - entry.created_at) > self._ttl:
            # Expired — evict and miss
            del self._store[key]
            self._misses += 1
            return None
        # Move to end for LRU ordering
        self._store.move_to_end(key)
        self._hits += 1
        return entry.result

    def put(self, prompt: str, result: Any) -> None:
        """Store a result. Evicts oldest entry if at capacity."""
        key = self._hash(prompt)
        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = CacheEntry(result=result)
            return
        if len(self._store) >= self._max_size:
            self._store.popitem(last=False)  # evict oldest
        self._store[key] = CacheEntry(result=result)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "size": len(self._store),
        }

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0


# Module-level singletons — one per classifier type.
_engage_cache: ClassifierCache | None = None
_content_cache: ClassifierCache | None = None
_combined_cache: ClassifierCache | None = None


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


def get_combined_cache() -> ClassifierCache:
    """Cache for Phase 4 combined engagement+content classifier results.

    Keyed on the combined prompt (same cache shape as the other two
    singletons). Separate from get_engage_cache / get_content_cache
    because the combined prompt is distinct and cross-pollination would
    pollute the other caches with results that mix decision types.
    If repeat-text rates across fold and non-fold paths later justify
    cross-population, it's a single-line change — not this phase.
    """
    global _combined_cache
    if _combined_cache is None:
        _combined_cache = ClassifierCache()
    return _combined_cache
