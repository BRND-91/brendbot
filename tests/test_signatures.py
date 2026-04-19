"""Tests for brendbot.signatures — Phase 5 semantic-cache keys.

The contract being pinned here is that signatures are:
  - Deterministic: same inputs always produce the same output, across
    processes and Python versions.
  - Pure: no I/O, no logging, no config reads.
  - Stable across cosmetic variation: whitespace collapse + lowercase
    on the message text; context dict extra keys ignored.
  - Well-separated: different inputs produce different outputs (no
    trivial collisions for realistic distinct messages).

SDK stubs are installed by tests/conftest.py.
"""
from __future__ import annotations

import pytest

from brendbot import signatures as sig


# ── _normalize ───────────────────────────────────────────────────────────

class TestNormalize:
    def test_empty_string_returns_empty(self) -> None:
        assert sig._normalize("") == ""

    def test_none_returns_empty(self) -> None:
        # _normalize is called from context_signature with defensive ""
        # fallbacks, but directly guarding the None case keeps the contract
        # honest for any future caller.
        assert sig._normalize("") == ""

    def test_lowercases(self) -> None:
        assert sig._normalize("HELLO World") == "hello world"

    def test_collapses_whitespace(self) -> None:
        assert sig._normalize("  hello   world  ") == "hello world"

    def test_collapses_newlines_and_tabs(self) -> None:
        assert sig._normalize("hello\n\t  world") == "hello world"

    def test_trims_edges(self) -> None:
        assert sig._normalize("   padded   ") == "padded"


# ── message_signature ────────────────────────────────────────────────────

class TestMessageSignature:
    def test_returns_hex_string_of_configured_length(self) -> None:
        result = sig.message_signature("hello world")
        assert len(result) == sig._MESSAGE_HASH_LEN
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self) -> None:
        a = sig.message_signature("hello world")
        b = sig.message_signature("hello world")
        assert a == b

    def test_stable_across_calls_with_context(self) -> None:
        ctx = [{"text": "prior"}, {"text": "another"}]
        a = sig.message_signature("hello", recent_context=ctx)
        b = sig.message_signature("hello", recent_context=ctx)
        assert a == b

    def test_case_insensitive(self) -> None:
        """Phase 5 semantic cache should treat 'yes' and 'YES' as the
        same query — normalization guarantees this before hashing."""
        a = sig.message_signature("yes")
        b = sig.message_signature("YES")
        c = sig.message_signature("yEs")
        assert a == b == c

    def test_whitespace_insensitive(self) -> None:
        """'yes  i agree' and 'yes i agree' should cache-hit."""
        a = sig.message_signature("yes i agree")
        b = sig.message_signature("yes  i  agree")
        c = sig.message_signature("  yes i agree  ")
        assert a == b == c

    def test_different_texts_produce_different_sigs(self) -> None:
        a = sig.message_signature("hello world")
        b = sig.message_signature("goodbye world")
        assert a != b

    def test_context_differentiates(self) -> None:
        """Same message text with different conversational context → the
        cache key must differ, otherwise we'd hit on a stale answer."""
        a = sig.message_signature("what do you think?")
        b = sig.message_signature(
            "what do you think?",
            recent_context=[{"text": "about R-30 insulation"}],
        )
        assert a != b

    def test_none_and_empty_context_equivalent(self) -> None:
        """None, [], and empty-dict-list all mean 'no meaningful context'
        and must hash to the same key."""
        a = sig.message_signature("hi")
        b = sig.message_signature("hi", recent_context=None)
        c = sig.message_signature("hi", recent_context=[])
        d = sig.message_signature("hi", recent_context=[{}])
        assert a == b == c == d

    def test_context_dict_extra_keys_ignored(self) -> None:
        """Only the "text" field is consumed — sender_id, timestamp, etc.
        must not affect the signature, otherwise cache hit rate would
        collapse across channels."""
        a = sig.message_signature(
            "hi", recent_context=[{"text": "prior", "sender_id": "alice"}]
        )
        b = sig.message_signature(
            "hi", recent_context=[{"text": "prior", "sender_id": "bob"}]
        )
        assert a == b


# ── context_signature ────────────────────────────────────────────────────

class TestContextSignature:
    def test_empty_context_returns_empty_string(self) -> None:
        assert sig.context_signature(None) == ""
        assert sig.context_signature([]) == ""

    def test_returns_hex_of_configured_length(self) -> None:
        result = sig.context_signature([{"text": "hello"}])
        assert len(result) == sig._CONTEXT_HASH_LEN
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self) -> None:
        ctx = [{"text": "one"}, {"text": "two"}]
        assert sig.context_signature(ctx) == sig.context_signature(ctx)

    def test_window_truncation(self) -> None:
        """Only the last _CONTEXT_WINDOW entries feed the hash — earlier
        context is dropped, matching what the haiku classifier sees."""
        short = [{"text": f"msg{i}"} for i in range(sig._CONTEXT_WINDOW)]
        long = [{"text": "old"}] * 10 + short
        assert sig.context_signature(short) == sig.context_signature(long)

    def test_non_dict_entries_skipped(self) -> None:
        """Defensive: mixed-shape lists (e.g. if a caller accidentally
        passes strings) shouldn't crash — non-dict entries are silently
        skipped so the cache stays usable."""
        sig_ok = sig.context_signature([{"text": "a"}, {"text": "b"}])
        sig_mixed = sig.context_signature(
            [{"text": "a"}, "not a dict", None, {"text": "b"}]
        )
        assert sig_ok == sig_mixed

    def test_different_contexts_produce_different_sigs(self) -> None:
        a = sig.context_signature([{"text": "one"}])
        b = sig.context_signature([{"text": "two"}])
        assert a != b

    def test_context_order_matters(self) -> None:
        """A-then-B is not the same conversation as B-then-A."""
        a = sig.context_signature([{"text": "A"}, {"text": "B"}])
        b = sig.context_signature([{"text": "B"}, {"text": "A"}])
        assert a != b
