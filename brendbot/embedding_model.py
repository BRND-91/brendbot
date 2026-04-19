"""Process-wide singleton wrapper around sentence-transformers
all-MiniLM-L6-v2.

Shared between:
  - episodes.py          (cosine re-rank on episode retrieval)
  - classifier_cache.py  (semantic-match tier after SHA-256 exact miss)

Loading the 384-dim MiniLM model once per process keeps the import cost
(~100MB + a few seconds) off the hot path and off every cache lookup.

Graceful degradation:
  - If the sentence_transformers package is not installed, or numpy is
    unavailable, or the model download fails, is_available() returns
    False and embed() / embed_batch() return None / [].
  - Callers must check is_available() (or just handle the None return)
    and fall back to their pre-embedding path (lexical prefilter for
    episodes, exact-match cache only for classifier_cache).

The dependency is intentionally optional — the bot runs without
sentence-transformers. Installing it unlocks the embedding tiers on
both consumers without code changes.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Model name is documented here so callers don't hardcode it. Changing
# this requires re-embedding any stored vectors (schema rev signalled
# by different dim — 384 for MiniLM-L6, 768 for MiniLM-L12).
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_model_lock = threading.Lock()
_model = None  # type: ignore[var-annotated]
_load_attempted = False
_load_failed = False


def _try_load() -> None:
    """Load the model under the module lock. Idempotent. Records failure
    permanently so retries don't re-attempt download on every call."""
    global _model, _load_attempted, _load_failed
    with _model_lock:
        if _load_attempted:
            return
        _load_attempted = True
        try:
            # Local import keeps the dependency optional at module load.
            from sentence_transformers import SentenceTransformer  # type: ignore

            _model = SentenceTransformer(MODEL_NAME)
            logger.info("embedding_model: loaded %s", MODEL_NAME)
        except Exception as exc:
            _load_failed = True
            logger.warning(
                "embedding_model: failed to load %s (%s) — "
                "embedding-dependent paths will fall back gracefully",
                MODEL_NAME, exc,
            )


def is_available() -> bool:
    """True if embed() / embed_batch() will return usable vectors.

    First call triggers a lazy load attempt; subsequent calls are O(1).
    """
    if not _load_attempted:
        _try_load()
    return _model is not None and not _load_failed


def embed(text: str) -> "bytes | None":
    """Return a float32 embedding of `text` as raw bytes (ready for
    SQLite BLOB storage), or None if the model is unavailable or the
    call fails."""
    if not text:
        return None
    if not is_available():
        return None
    try:
        import numpy as np  # type: ignore

        vec = _model.encode(  # type: ignore[union-attr]
            text, convert_to_numpy=True, normalize_embeddings=True,
        )
        # Persist as float32 to keep blob size at ~1.5KB per episode.
        return vec.astype(np.float32).tobytes()
    except Exception as exc:
        logger.warning("embedding_model: embed() failed: %s", exc)
        return None


def embed_vector(text: str):
    """Return a float32 numpy array (normalized), or None on failure.

    Convenience for in-process consumers that want a vector directly
    without the BLOB serialize round-trip."""
    if not text:
        return None
    if not is_available():
        return None
    try:
        import numpy as np  # type: ignore

        vec = _model.encode(  # type: ignore[union-attr]
            text, convert_to_numpy=True, normalize_embeddings=True,
        )
        return vec.astype(np.float32)
    except Exception as exc:
        logger.warning("embedding_model: embed_vector() failed: %s", exc)
        return None


def cosine_similarity(a_bytes: "bytes | None", b_vec) -> float:
    """Cosine similarity between a stored BLOB (a_bytes) and an
    in-memory numpy vector (b_vec). Returns -1.0 on any failure.

    Vectors are normalized at encode time, so cosine reduces to a dot
    product. This function does the safety dance (None checks, shape
    checks, import guard) so callers can focus on threshold logic.
    """
    if a_bytes is None or b_vec is None:
        return -1.0
    try:
        import numpy as np  # type: ignore

        a = np.frombuffer(a_bytes, dtype=np.float32)
        if a.shape[0] != b_vec.shape[0]:
            return -1.0
        return float(np.dot(a, b_vec))
    except Exception as exc:
        logger.debug("embedding_model: cosine_similarity failed: %s", exc)
        return -1.0
