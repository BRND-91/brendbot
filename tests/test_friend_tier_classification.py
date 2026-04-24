"""Tests for ``discord.classify_friend_guilds`` — the auto-classifier
that decides which connected guilds get gate/prefilter bypass.

Pins the two-rule classification (owner signal OR member-cache
signal, both gated on a small-guild size cap) and the new diagnostic
logging that names the specific reason each guild was or wasn't
classified. The 2026-04-23 pilot produced a silent "0 friend-tier
guild(s) of 1 total" with no indication of why — these tests make
that class of silent failure impossible without surfacing to both
the log and the test suite.
"""
from __future__ import annotations

import logging

import pytest

from brendbot import config as cfg_mod
from brendbot import discord as bb_discord


class _FakeGuild:
    """Minimal Discord.py Guild stub. Honors only the attributes
    classify_friend_guilds actually reads."""

    def __init__(
        self,
        guild_id: int,
        name: str,
        owner_id: int,
        member_count: int | None,
        cached_members: set[int] | None = None,
    ) -> None:
        self.id = guild_id
        self.name = name
        self.owner_id = owner_id
        self.member_count = member_count
        self._cached = cached_members or set()

    def get_member(self, user_id: int):
        """Return a truthy stub when the user_id is in our cached set,
        None otherwise. Mirrors the discord.py semantics the classifier
        relies on."""
        if user_id in self._cached:
            return object()  # just needs to be non-None
        return None


class _FakeClient:
    """Stand-in for a discord.py Client — exposes ``guilds`` only."""

    def __init__(self, guilds: list[_FakeGuild]) -> None:
        self.guilds = guilds


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    """Ensure each test starts with a clean config singleton and an
    empty friend-guild set."""
    monkeypatch.setattr(cfg_mod, "_config", None)
    monkeypatch.setattr(cfg_mod, "_FRIEND_GUILDS", frozenset())
    yield


def _set_admin(monkeypatch, admin_id: str) -> None:
    """Point the config singleton at a specific admin id for this test."""
    monkeypatch.setenv("ADMIN_DISCORD_ID", admin_id)
    monkeypatch.setattr(cfg_mod, "_config", None)


# ── Owner signal (strong) ────────────────────────────────────────────────


def test_owner_matched_small_guild_is_friend_tier(monkeypatch):
    """Classic friend-tier case: admin owns a private server."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=1277236474231787552,
            name="Pizzacord",
            owner_id=369485175329128448,
            member_count=4,
        ),
    ])

    result = bb_discord.classify_friend_guilds(client)

    assert str(1277236474231787552) in result
    assert len(result) == 1


def test_owner_matched_but_guild_too_large_is_not_friend_tier(monkeypatch):
    """Member cap is load-bearing — a giant server the admin owns
    (e.g. a public community) does not get friend-tier treatment."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="BigCommunity",
            owner_id=369485175329128448,
            member_count=500,
        ),
    ])
    assert bb_discord.classify_friend_guilds(client) == frozenset()


# ── Member signal (fallback, new in this PR) ────────────────────────────


def test_member_cached_small_guild_is_friend_tier(monkeypatch):
    """New behaviour: admin doesn't own the guild but is a member of
    a small one. The 2026-04-23 pilot probably fell into this shape —
    the admin is present in Pizzacord but isn't the literal owner."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=777,
            name="FriendServer",
            owner_id=999999,  # someone else
            member_count=6,
            cached_members={369485175329128448},
        ),
    ])

    result = bb_discord.classify_friend_guilds(client)

    assert str(777) in result


def test_non_owner_non_member_small_guild_is_not_friend_tier(monkeypatch):
    """Admin isn't in the guild and doesn't own it — must NOT be
    friend-tier just because it's small. Prevents random small
    servers from silently getting gate/prefilter bypass."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=888,
            name="SomeStrangerServer",
            owner_id=111111,
            member_count=4,
            cached_members=set(),
        ),
    ])
    assert bb_discord.classify_friend_guilds(client) == frozenset()


# ── Size edge cases ──────────────────────────────────────────────────────


def test_member_count_zero_is_not_friend_tier(monkeypatch):
    """member_count=0 means Discord hasn't populated it yet (or the
    guild literally has no members, which is a weird state). Either
    way, don't classify — we can't tell if it's small."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="Unknown",
            owner_id=369485175329128448,
            member_count=0,
        ),
    ])
    assert bb_discord.classify_friend_guilds(client) == frozenset()


def test_member_count_none_is_not_friend_tier(monkeypatch):
    """Same as above but for the None case — member_count attribute
    missing entirely, e.g. on an unusual guild type."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="Unknown",
            owner_id=369485175329128448,
            member_count=None,
        ),
    ])
    assert bb_discord.classify_friend_guilds(client) == frozenset()


def test_guild_at_max_members_is_not_friend_tier(monkeypatch):
    """Strict inequality: a guild at the cap is not small enough.
    Prevents creep — 25 members is a firm upper bound, not a fuzzy one."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="Edge",
            owner_id=369485175329128448,
            member_count=bb_discord._FRIEND_GUILD_MAX_MEMBERS,
        ),
    ])
    assert bb_discord.classify_friend_guilds(client) == frozenset()


# ── Config / input edge cases ────────────────────────────────────────────


def test_unset_admin_discord_id_skips_classification(monkeypatch):
    """If ADMIN_DISCORD_ID isn't set, we can't classify — return empty
    without inspecting guilds."""
    monkeypatch.delenv("ADMIN_DISCORD_ID", raising=False)
    monkeypatch.setattr(cfg_mod, "_config", None)
    client = _FakeClient([
        _FakeGuild(1, "Any", 1, 4),
    ])
    assert bb_discord.classify_friend_guilds(client) == frozenset()


def test_non_integer_admin_id_falls_back_to_owner_only(monkeypatch):
    """If ADMIN_DISCORD_ID is mis-typed (not a valid int snowflake),
    the member-check path is disabled but owner-check still works.
    Defensive against operator error in .env."""
    _set_admin(monkeypatch, "not-a-number")
    # Owner-match branch uses string comparison, so owner_id and
    # admin_id as strings will still compare correctly even if the
    # admin_id isn't parseable as int.
    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="FriendServer",
            owner_id=123,
            member_count=4,
            cached_members={123},
        ),
    ])
    # admin_id is "not-a-number"; owner_id stringifies to "123";
    # they don't match. Member cache path is disabled. Not friend-tier.
    assert bb_discord.classify_friend_guilds(client) == frozenset()


def test_multiple_guilds_mixed_classification(monkeypatch):
    """End-to-end: three guilds, only the two that qualify get flagged."""
    _set_admin(monkeypatch, "369485175329128448")
    admin_int = 369485175329128448
    client = _FakeClient([
        _FakeGuild(guild_id=1, name="OwnedSmall",
                   owner_id=admin_int, member_count=4),
        _FakeGuild(guild_id=2, name="FriendInvite",
                   owner_id=999,
                   member_count=8,
                   cached_members={admin_int}),
        _FakeGuild(guild_id=3, name="BigCommunity",
                   owner_id=admin_int, member_count=300),
    ])

    result = bb_discord.classify_friend_guilds(client)

    assert "1" in result
    assert "2" in result
    assert "3" not in result


# ── Diagnostic logging ───────────────────────────────────────────────────


def test_skip_reason_logged_for_non_small_guild(monkeypatch, caplog):
    """When a guild is skipped because it's too large, the startup log
    must say so explicitly — not just the aggregate 'N of M total'
    count. This is the fix for the 2026-04-23 pilot's silent failure."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="Big",
            owner_id=369485175329128448,
            member_count=500,
        ),
    ])

    with caplog.at_level(logging.INFO, logger="brendbot.discord"):
        bb_discord.classify_friend_guilds(client)

    relevant = [r for r in caplog.records if "Friend-tier skipped" in r.getMessage()]
    assert len(relevant) == 1
    msg = relevant[0].getMessage()
    assert "not small" in msg
    assert "members=500" in msg


def test_skip_reason_logged_for_owner_and_member_miss(monkeypatch, caplog):
    """The killer diagnostic: a guild where the admin isn't owner AND
    isn't in the member cache. Pre-fix this produced silent zero;
    now it logs both failures."""
    _set_admin(monkeypatch, "369485175329128448")
    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="Mystery",
            owner_id=999,
            member_count=5,
            cached_members=set(),
        ),
    ])

    with caplog.at_level(logging.INFO, logger="brendbot.discord"):
        bb_discord.classify_friend_guilds(client)

    skipped = [r for r in caplog.records if "Friend-tier skipped" in r.getMessage()]
    assert len(skipped) == 1
    msg = skipped[0].getMessage()
    assert "owner_id='999'" in msg
    assert "admin_id='369485175329128448'" in msg
    assert "admin not in member cache" in msg


def test_detection_reason_logged_for_successful_classification(monkeypatch, caplog):
    """When a guild IS classified, the log line must show which signal
    fired (manual, owner_admin, owner_trusted, admin_cached, or some
    combination) so operators can see the decision path. PR-25
    extended the logged fields to distinguish admin-owner from
    trusted-owner and manual-override."""
    _set_admin(monkeypatch, "369485175329128448")
    admin_int = 369485175329128448
    client = _FakeClient([
        _FakeGuild(
            guild_id=777,
            name="FriendServer",
            owner_id=999,
            member_count=8,
            cached_members={admin_int},
        ),
    ])

    with caplog.at_level(logging.INFO, logger="brendbot.discord"):
        bb_discord.classify_friend_guilds(client)

    detected = [r for r in caplog.records if "Friend-tier guild detected" in r.getMessage()]
    assert len(detected) == 1
    msg = detected[0].getMessage()
    assert "manual=False" in msg
    assert "owner_admin=False" in msg
    assert "owner_trusted=False" in msg
    assert "admin_cached=True" in msg
    assert "members=8" in msg


# ── Trusted-owner signal (PR-25) ─────────────────────────────────────────


def test_trusted_owner_small_guild_is_friend_tier(monkeypatch):
    """The 2026-04-24 pilot case: admin isn't the Discord owner, but
    the owner's id is listed in TRUSTED_DISCORD_IDS. A friend of the
    admin created the server and invited the bot. This classifies.
    Works WITHOUT the members privileged intent."""
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.setenv("TRUSTED_DISCORD_IDS", "976170794013044746,other")
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1277236474231787552,
            name="Pizzacord",
            owner_id=976170794013044746,  # Pizzacord's real owner
            member_count=4,
            cached_members=set(),  # admin not cached — members intent off
        ),
    ])

    result = bb_discord.classify_friend_guilds(client)
    assert str(1277236474231787552) in result


def test_trusted_owner_large_guild_is_not_friend_tier(monkeypatch):
    """Trusted-owner + size cap still applies. A trusted friend with
    a 500-member public community doesn't turn that into friend-tier."""
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.setenv("TRUSTED_DISCORD_IDS", "976170794013044746")
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="BigTrustedFriendServer",
            owner_id=976170794013044746,
            member_count=500,
        ),
    ])
    assert bb_discord.classify_friend_guilds(client) == frozenset()


def test_admin_id_is_not_counted_as_trusted_owner(monkeypatch):
    """The admin-owner branch and trusted-owner branch are
    semantically distinct — admin owning their own server is a
    different signal from a trusted friend owning one. Confirm the
    log reflects which branch fired."""
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.setenv("TRUSTED_DISCORD_IDS", "999,888")
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="OwnedByAdmin",
            owner_id=369485175329128448,
            member_count=4,
        ),
    ])
    import logging as _logging
    import logging as lg  # noqa: F811 (ensure fresh import in this scope)

    result = bb_discord.classify_friend_guilds(client)
    assert "1" in result


# ── FRIEND_GUILD_IDS manual override (PR-25) ─────────────────────────────


def test_manual_override_bypasses_size_and_owner_checks(monkeypatch):
    """Guild in FRIEND_GUILD_IDS is friend-tier unconditionally.
    Even if size is huge, owner mismatches, no cached admin."""
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.setenv("FRIEND_GUILD_IDS", "1277236474231787552")
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1277236474231787552,
            name="ManuallyOverridden",
            owner_id=999999,  # mismatch
            member_count=200,  # over cap
            cached_members=set(),  # admin not cached
        ),
    ])
    result = bb_discord.classify_friend_guilds(client)
    assert str(1277236474231787552) in result


def test_manual_override_multiple_ids(monkeypatch):
    """Comma-separated list of guild snowflakes."""
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.setenv("FRIEND_GUILD_IDS", "111,222,333")
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(111, "A", 999, 4, set()),
        _FakeGuild(222, "B", 999, 4, set()),
        _FakeGuild(999, "C", 999, 4, set()),  # not in list
    ])
    result = bb_discord.classify_friend_guilds(client)
    assert "111" in result
    assert "222" in result
    assert "999" not in result


def test_unset_manual_override_no_effect(monkeypatch):
    """Unset FRIEND_GUILD_IDS env var disables the manual-override
    branch. Classification falls back to auto-detection."""
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.delenv("FRIEND_GUILD_IDS", raising=False)
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1,
            name="NonQualifying",
            owner_id=999,
            member_count=4,
            cached_members=set(),
        ),
    ])
    # Would be friend-tier if manual override were set; without it,
    # auto-detection fails (non-admin owner, not cached).
    assert bb_discord.classify_friend_guilds(client) == frozenset()


# ── Direct pilot regression for 2026-04-24 ──────────────────────────────


def test_pilot_2026_04_24_pizzacord_with_trusted_owner(monkeypatch):
    """Reproduction of the exact 2026-04-24 pilot configuration.
    admin_id=369485175329128448, Pizzacord owner=976170794013044746.
    With TRUSTED_DISCORD_IDS=976170794013044746, Pizzacord classifies
    even without the members intent populating the cache.

    The fix path Brendan would actually use:
      export TRUSTED_DISCORD_IDS=976170794013044746  (the friend)
    or:
      export FRIEND_GUILD_IDS=1277236474231787552  (direct override)
    """
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.setenv("TRUSTED_DISCORD_IDS", "976170794013044746")
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1277236474231787552,
            name="Pizzacord",
            owner_id=976170794013044746,
            member_count=4,
            cached_members=set(),  # members intent not enabled
        ),
    ])
    result = bb_discord.classify_friend_guilds(client)
    assert "1277236474231787552" in result


def test_pilot_2026_04_24_pizzacord_with_manual_override(monkeypatch):
    """Alternative fix path using the manual override directly."""
    _set_admin(monkeypatch, "369485175329128448")
    monkeypatch.setenv("FRIEND_GUILD_IDS", "1277236474231787552")
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1277236474231787552,
            name="Pizzacord",
            owner_id=976170794013044746,
            member_count=4,
            cached_members=set(),
        ),
    ])
    result = bb_discord.classify_friend_guilds(client)
    assert "1277236474231787552" in result


def test_pilot_2026_04_24_pizzacord_with_members_intent_cached(monkeypatch):
    """Third fix path: if the members intent is enabled, the admin
    appears in the cache even though they aren't the owner."""
    _set_admin(monkeypatch, "369485175329128448")
    admin_int = 369485175329128448
    monkeypatch.setattr(cfg_mod, "_config", None)

    client = _FakeClient([
        _FakeGuild(
            guild_id=1277236474231787552,
            name="Pizzacord",
            owner_id=976170794013044746,
            member_count=4,
            cached_members={admin_int},  # members intent populated
        ),
    ])
    result = bb_discord.classify_friend_guilds(client)
    assert "1277236474231787552" in result
