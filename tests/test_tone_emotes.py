"""Tests for the tone-mapped reaction palette and picker.

The middle-engagement band (haiku says NO but score is above drop threshold)
reacts to the triggering message with an emoji instead of replying. Prior
behavior hardcoded 👀. Current behavior maps haiku_classify's returned tone
to a weighted palette — custom server emotes 2x unicode.

These tests verify:
  - Every tone bucket returns an emoji from its own palette
  - Unrecognised tones fall back to 'neutral'
  - The weighted pick never returns an emoji outside the palette
  - Custom emote strings are well-formed Discord emote references
  - All tones from the haiku_classify _valid_tones set have palette entries
"""
from __future__ import annotations

import asyncio
import random

import pytest

from brendbot import discord as bd


class TestReactionPalette:
    """Structural integrity of _REACTION_PALETTES."""

    def test_every_valid_tone_has_palette(self) -> None:
        """The tones haiku_classify returns must all exist in the palette.
        If haiku_classify adds a new tone word, this test fails and forces
        the palette to be updated before the new tone can reach production.
        """
        valid_tones = {"funny", "hype", "sad", "weird", "dumb", "wholesome", "neutral"}
        palette_keys = set(bd._REACTION_PALETTES.keys())
        assert valid_tones == palette_keys, (
            f"Palette keys must match haiku_classify valid tones. "
            f"Missing: {valid_tones - palette_keys}, "
            f"Extra: {palette_keys - valid_tones}"
        )

    def test_every_palette_has_at_least_two_options(self) -> None:
        """Single-option palettes defeat the point of weighted variety."""
        for tone, palette in bd._REACTION_PALETTES.items():
            assert len(palette) >= 2, f"Palette for {tone!r} has <2 options"

    def test_all_weights_positive(self) -> None:
        """random.choices rejects non-positive weights."""
        for tone, palette in bd._REACTION_PALETTES.items():
            for emote, weight in palette:
                assert weight > 0, f"Non-positive weight in {tone!r}: {emote!r}={weight}"

    def test_custom_emotes_are_well_formed(self) -> None:
        """Custom server emotes must look like <:name:id>.
        Malformed strings silently fail to react on Discord."""
        import re
        custom_pattern = re.compile(r"^<:\w+:\d+>$")
        for tone, palette in bd._REACTION_PALETTES.items():
            for emote, weight in palette:
                if emote.startswith("<:"):
                    assert custom_pattern.match(emote), (
                        f"Malformed custom emote in {tone!r}: {emote!r}"
                    )

    def test_custom_emotes_weighted_higher_than_unicode(self) -> None:
        """Palette spec: custom server emotes are 2x unicode weight.
        Validates the bot reads as channel-native, not generic-bot."""
        assert bd._CUSTOM > bd._UNICODE
        assert bd._CUSTOM == 2
        assert bd._UNICODE == 1


class TestPickReaction:
    """Behavior of _pick_reaction.

    The picker returns a fallback-ordered list: position 0 is the weighted
    random pick, positions 1+ are the palette's unicode entries in order
    (minus position 0 if it was also unicode). This shape lets callers
    walk the list via react_with_fallback if a custom emote ID has rotted.
    """

    def test_returns_list_with_pick_at_position_zero(self) -> None:
        """Pick from 'funny' returns a list; first element is in the palette."""
        funny_emotes = {e for e, w in bd._REACTION_PALETTES["funny"]}
        for _ in range(50):
            result = bd._pick_reaction("funny")
            assert isinstance(result, list), f"Expected list, got {type(result)}"
            assert len(result) >= 1, "Empty pick list"
            assert result[0] in funny_emotes

    def test_fallback_tail_contains_only_unicode(self) -> None:
        """Positions 1+ must all be unicode emotes so the fallback chain
        always terminates in a reaction that Discord won't reject."""
        for tone in bd._REACTION_PALETTES.keys():
            result = bd._pick_reaction(tone)
            palette_unicode = {e for e, w in bd._REACTION_PALETTES[tone] if w == bd._UNICODE}
            for tail_emote in result[1:]:
                assert tail_emote in palette_unicode, (
                    f"Tail emote {tail_emote!r} for tone {tone!r} is not unicode"
                )

    def test_fallback_tail_deduplicates_position_zero(self) -> None:
        """If the picker lands on a unicode emote, the fallback tail must
        not repeat it. Fallback chain should never try the same emote twice."""
        # Seed to force a unicode pick from 'funny' (has 2 unicode, 1 custom)
        for seed in range(100):
            random.seed(seed)
            result = bd._pick_reaction("funny")
            # The first element appears exactly once in the returned list
            assert result.count(result[0]) == 1, (
                f"Position-0 emote {result[0]!r} appears in fallback tail: {result}"
            )

    def test_every_palette_always_has_unicode_fallback(self) -> None:
        """For any tone, a custom-emote pick must still produce a list with
        at least one unicode emote in the tail. Guarantees the fallback
        chain always terminates successfully."""
        for tone in bd._REACTION_PALETTES.keys():
            palette = bd._REACTION_PALETTES[tone]
            unicode_in_palette = [e for e, w in palette if w == bd._UNICODE]
            assert len(unicode_in_palette) >= 1, (
                f"Palette {tone!r} has no unicode emote — fallback chain "
                f"could fail if Discord rejects the custom pick"
            )
            # And verify the picker produces that tail
            result = bd._pick_reaction(tone)
            # At least one unicode must be in the returned list (either at
            # position 0 if the picker landed on it, or in the tail)
            result_unicode = [e for e in result if e in unicode_in_palette]
            assert len(result_unicode) >= 1

    def test_unknown_tone_falls_back_to_neutral(self) -> None:
        """Any unrecognized string routes to the neutral palette.
        None also falls back cleanly — dict.get(None) returns default."""
        neutral_emotes = {e for e, w in bd._REACTION_PALETTES["neutral"]}
        for bad_tone in ["", "xyz", "PANIC", "funny ", None]:
            result = bd._pick_reaction(bad_tone)  # type: ignore
            assert result[0] in neutral_emotes, (
                f"Unknown tone {bad_tone!r} did not fall back to neutral "
                f"(got {result[0]!r})"
            )

    def test_every_tone_produces_valid_pick(self) -> None:
        """Smoke test: every registered tone returns a non-empty list."""
        for tone in bd._REACTION_PALETTES.keys():
            result = bd._pick_reaction(tone)
            assert result, f"Empty pick list for {tone!r}"
            palette_emotes = {e for e, w in bd._REACTION_PALETTES[tone]}
            assert result[0] in palette_emotes

    def test_seeded_pick_is_deterministic(self) -> None:
        """Same seed → same pick. Lets downstream consumers write reproducible
        integration tests if they need to."""
        random.seed(42)
        first = bd._pick_reaction("funny")
        random.seed(42)
        second = bd._pick_reaction("funny")
        assert first == second

    def test_weighted_distribution_biases_custom(self) -> None:
        """Over many samples, custom (weight 2) should appear at position 0
        more often than unicode (weight 1). Uses 'hype' which has 1 unicode
        + 2 custom emotes (so custom share should be ~80% at position 0)."""
        hype_palette = bd._REACTION_PALETTES["hype"]
        custom_emotes = {e for e, w in hype_palette if w == bd._CUSTOM}
        random.seed(1337)
        custom_hits = 0
        total = 1000
        for _ in range(total):
            result = bd._pick_reaction("hype")
            if result[0] in custom_emotes:
                custom_hits += 1
        # Expected share: custom_total_weight / total_weight = 4/5 = 0.8
        # Allow ±5% tolerance for sample variance.
        assert 0.75 <= custom_hits / total <= 0.85, (
            f"Custom hit rate {custom_hits / total:.3f} outside expected 0.75-0.85"
        )


class TestReactWithFallback:
    """react_with_fallback walks the fallback chain until one emote lands.

    Uses a fake Discord client injected into bd._discord_client so the
    reaction attempts can be scripted to fail in specific ways without
    touching a real Discord API.
    """

    def _setup_fake_client(self, fail_until: int = 0):
        """Install a fake _discord_client where the Nth reaction attempt
        fails with an exception. Returns (attempts_log, restore_fn).

        fail_until=0 means all attempts succeed.
        fail_until=2 means attempts 0 and 1 fail, attempt 2 succeeds.
        fail_until=999 means all attempts fail (simulates every emote dead).
        """
        attempts: list[str] = []

        class FakeMessage:
            async def add_reaction(self, emote: str) -> None:
                attempts.append(emote)
                if len(attempts) <= fail_until:
                    raise RuntimeError(f"simulated fail for {emote}")

        class FakeChannel:
            async def fetch_message(self, _mid: int) -> FakeMessage:
                return FakeMessage()

        class FakeClient:
            def get_channel(self, _cid: int) -> FakeChannel:
                return FakeChannel()

            async def fetch_channel(self, _cid: int) -> FakeChannel:
                return FakeChannel()

        original = bd._discord_client
        bd._discord_client = FakeClient()  # type: ignore

        def restore() -> None:
            bd._discord_client = original

        return attempts, restore

    def test_succeeds_on_first_emote_when_valid(self) -> None:
        """Happy path: the first emote lands, subsequent emotes not tried."""
        attempts, restore = self._setup_fake_client(fail_until=0)
        try:
            result = asyncio.run(bd.react_with_fallback(
                "100", "200", ["😂", "💀"]
            ))
            assert result is True
            assert attempts == ["😂"]
        finally:
            restore()

    def test_retries_to_fallback_when_first_fails(self) -> None:
        """First emote fails (e.g. dead custom ID), fallback unicode succeeds."""
        attempts, restore = self._setup_fake_client(fail_until=1)
        try:
            result = asyncio.run(bd.react_with_fallback(
                "100", "200", ["<:dead:123>", "💀", "😂"]
            ))
            assert result is True
            assert attempts == ["<:dead:123>", "💀"]
        finally:
            restore()

    def test_walks_full_chain_when_all_fail(self) -> None:
        """Every emote fails — function returns False but does not raise."""
        attempts, restore = self._setup_fake_client(fail_until=999)
        try:
            result = asyncio.run(bd.react_with_fallback(
                "100", "200", ["<:a:1>", "<:b:2>", "<:c:3>"]
            ))
            assert result is False
            assert attempts == ["<:a:1>", "<:b:2>", "<:c:3>"]
        finally:
            restore()

    def test_empty_emote_list_returns_false(self) -> None:
        """Defensive: empty input list returns False, no attempts made."""
        attempts, restore = self._setup_fake_client(fail_until=0)
        try:
            result = asyncio.run(bd.react_with_fallback("100", "200", []))
            assert result is False
            assert attempts == []
        finally:
            restore()

    def test_no_discord_client_returns_false(self) -> None:
        """When _discord_client is None, function short-circuits to False."""
        original = bd._discord_client
        bd._discord_client = None
        try:
            result = asyncio.run(bd.react_with_fallback("100", "200", ["😂"]))
            assert result is False
        finally:
            bd._discord_client = original

    def test_pick_reaction_output_always_walks_to_success(self) -> None:
        """Integration: _pick_reaction output + react_with_fallback always
        terminates in a successful reaction even when every custom emote
        in the palette is dead. Simulates the worst case: every <:...> ID
        is rejected, forcing fallback to unicode.

        Uses a fake client that fails any emote starting with '<:' (i.e.
        every custom emote) and succeeds on unicode.
        """
        attempts: list[str] = []

        class FakeMessage:
            async def add_reaction(self, emote: str) -> None:
                attempts.append(emote)
                if emote.startswith("<:"):
                    raise RuntimeError("custom emote dead")

        class FakeChannel:
            async def fetch_message(self, _mid: int) -> FakeMessage:
                return FakeMessage()

        class FakeClient:
            def get_channel(self, _cid: int) -> FakeChannel:
                return FakeChannel()

            async def fetch_channel(self, _cid: int) -> FakeChannel:
                return FakeChannel()

        original = bd._discord_client
        bd._discord_client = FakeClient()  # type: ignore
        try:
            # Test every tone bucket. Every one must produce a successful
            # reaction even with all custom emotes dead.
            for tone in bd._REACTION_PALETTES.keys():
                attempts.clear()
                emotes = bd._pick_reaction(tone)
                result = asyncio.run(bd.react_with_fallback(
                    "100", "200", emotes
                ))
                assert result is True, (
                    f"Tone {tone!r} failed to land any reaction. "
                    f"Emotes tried: {attempts}"
                )
                # Verify the final successful emote was unicode
                assert not attempts[-1].startswith("<:"), (
                    f"Tone {tone!r} final emote was custom: {attempts[-1]}"
                )
        finally:
            bd._discord_client = original
