"""Integration tests for Session.apply_content_gate — the content-gate
routing method that sits between the pool-level inject and the actual
session inject. These tests stub the classifier so the gate logic can
be exercised without SDK spawns.

Tests cover, post-2026-04-23 strip:
  - PASS (benign request → 'inject' returned, nothing else dispatched)
  - REFUSE (>refuse_threshold sum → local refusal via _fire_on_text)
  - FLOOR_HIT (hard floor match → local refusal naming the floor)
  - BYPASS (admin *brend* italic → classifier runs in shadow mode,
    hard floors still enforced, _turn_bypass_pending set, bypass_audit
    written, 'inject' returned so session generates normally)
  - FRIEND-TIER BYPASS (guild in config._FRIEND_GUILDS → entire gate
    skipped, no classifier spawn, 'inject' returned)

The FLAG outcome was removed in the strip; its test class is gone.
Former FLAG-band inputs now take the PASS path.

Fake session uses Session.__new__(Session) to skip __init__ and manually
populates only the attributes apply_content_gate touches. This avoids
pulling in the full Session startup path.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from brendbot import session as session_mod
from brendbot.content_gate import ClassifierResult
from brendbot.session import Session


# ── Test fixtures ─────────────────────────────────────────────────────────

def _make_fake_session(
    key: str = "test:fake",
    chat_id: str = "100",
    guild_id: str = "",
) -> Session:
    """Build a minimum Session-like object without running __init__.

    Only populates the attributes that apply_content_gate reads or writes.
    Any other attribute access will raise AttributeError, which surfaces
    as a test failure if apply_content_gate drifts and starts reading
    unexpected state.

    ``guild_id`` defaults to empty so the friend-tier bypass (which
    requires a guild_id in ``config._FRIEND_GUILDS``) doesn't accidentally
    trigger for tests that exercise the defensive gate path. Friend-tier
    tests pass an explicit guild_id and stub ``config._FRIEND_GUILDS``.
    """
    s = Session.__new__(Session)
    s.key = key
    s._chat_id = chat_id
    s._guild_id = guild_id
    s._turn_bypass_pending = False

    # _fire_on_text is called as asyncio.create_task(self._fire_on_text(text))
    # for REFUSE / FLOOR_HIT. Replace with a coroutine that just records
    # the text.
    s._fire_on_text_log = []  # type: ignore

    async def _fake_fire_on_text(text: str) -> None:
        s._fire_on_text_log.append(text)  # type: ignore

    s._fire_on_text = _fake_fire_on_text  # type: ignore
    return s


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    """Redirect audit log writes to a tmp dir so tests don't pollute
    the real logs/ directory."""
    from brendbot import feedback as fb
    monkeypatch.setattr(fb, "FLAG_AUDIT_LOG", tmp_path / "flag_audit.jsonl")
    monkeypatch.setattr(fb, "BYPASS_AUDIT_LOG", tmp_path / "bypass_audit.jsonl")
    return tmp_path


@pytest.fixture
def stub_classifier(monkeypatch):
    """Returns a setter that installs a fake content_gate_classify
    returning a specific ClassifierResult. Usage:

        stub_classifier(criteria={'tragedy_new': 0.9}, reasoning='test')
        stub_classifier(hard_floor='malware')
    """
    def _set(
        criteria: dict | None = None,
        hard_floor: str | None = None,
        reasoning: str = "test",
        parse_error: bool = False,
        raise_exc: Exception | None = None,
    ):
        async def _fake_classify(user_text: str) -> ClassifierResult:
            if raise_exc is not None:
                raise raise_exc
            return ClassifierResult(
                criteria=criteria or {},
                hard_floor=hard_floor,
                reasoning=reasoning,
                parse_error=parse_error,
            )
        monkeypatch.setattr(session_mod, "content_gate_classify", _fake_classify)
    return _set


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── PASS path ─────────────────────────────────────────────────────────────

class TestPassOutcome:
    """Benign requests return 'inject' and dispatch nothing directly."""

    def test_benign_returns_inject(self, stub_classifier, logs_dir) -> None:
        stub_classifier(criteria={}, reasoning="benign")
        s = _make_fake_session()
        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<wrapped>benign request</wrapped>",
            raw_user_text="benign request",
            tier="admin",
            sender_id="369485175329128448",
            message_id="111",
        ))
        assert result == "inject"
        assert s._fire_on_text_log == []  # type: ignore
        assert s._turn_bypass_pending is False

    def test_low_score_returns_inject(self, stub_classifier, logs_dir) -> None:
        stub_classifier(criteria={"tragedy_old": 0.2})  # 0.2 ≤ 0.5
        s = _make_fake_session()
        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>historical reference</w>",
            raw_user_text="historical reference",
            tier="default",
            sender_id="u1",
            message_id="112",
        ))
        assert result == "inject"


# ── REFUSE / FLOOR_HIT paths ──────────────────────────────────────────────

class TestRefuseOutcome:
    """High-sum and hard-floor requests are refused locally via
    _fire_on_text. No inject, no flagged path, no counter increment."""

    def test_high_sum_refuses(self, stub_classifier, logs_dir) -> None:
        stub_classifier(criteria={
            "tragedy_new": 0.9,
            "person_targeted": 1.5,
            "frame_directed": 2.0,
        })  # sum = 4.4
        s = _make_fake_session()
        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>bad request</w>",
            raw_user_text="bad request",
            tier="admin",
            sender_id="admin1",
            message_id="200",
        ))
        assert result == "handled"
        assert len(s._fire_on_text_log) == 1  # type: ignore
        refusal = s._fire_on_text_log[0]  # type: ignore
        assert "can't do that one" in refusal.lower()
        assert "tragedy" in refusal.lower() or "stacks" in refusal.lower()

    def test_hard_floor_refuses_with_plain_name(
        self, stub_classifier, logs_dir
    ) -> None:
        stub_classifier(hard_floor="malware")
        s = _make_fake_session()
        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>ransomware pls</w>",
            raw_user_text="ransomware pls",
            tier="admin",
            sender_id="admin1",
            message_id="201",
        ))
        assert result == "handled"
        refusal = s._fire_on_text_log[0]  # type: ignore
        assert "hard floor" in refusal.lower()
        assert "malware" in refusal.lower() or "exploit" in refusal.lower()

    def test_parse_error_fails_conservative(
        self, stub_classifier, logs_dir
    ) -> None:
        stub_classifier(
            criteria={"_parse_error": 10.0},
            reasoning="classifier returned garbage",
            parse_error=True,
        )
        s = _make_fake_session()
        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>something</w>",
            raw_user_text="something",
            tier="admin",
            sender_id="admin1",
            message_id="202",
        ))
        assert result == "handled"  # parse error routes to REFUSE
        assert s._fire_on_text_log  # type: ignore

    def test_classifier_crash_fails_conservative(
        self, stub_classifier, logs_dir
    ) -> None:
        """The apply_content_gate method catches classifier exceptions and
        fails to a parse-error ClassifierResult which routes to REFUSE."""
        stub_classifier(raise_exc=RuntimeError("classifier died"))
        s = _make_fake_session()
        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="x",
            tier="admin",
            sender_id="admin1",
            message_id="203",
        ))
        assert result == "handled"
        assert s._fire_on_text_log  # type: ignore


# ── FLAG path ─────────────────────────────────────────────────────────────

# ── FLAG outcome ──────────────────────────────────────────────────────────
#
# The TestFlagOutcome class (flag-reroute, flag-audit-row, budget-cap,
# flagged-generate-failure) was deleted in the 2026-04-23 strip. The
# FLAG band now collapses into PASS inside ``decide_outcome``, the
# ``flagged_generate`` function is gone from ``classifier_pool``, and
# ``Session._flagged_count`` no longer exists. FLAG-band inputs now
# take the PASS path — covered by ``TestPassOutcome``.


# ── BYPASS path ───────────────────────────────────────────────────────────

class TestBypassOutcome:
    """Admin *brend* italic token skips the weighted classifier but still
    enforces hard floors. Returns 'inject' so the session generates on
    its normal model with _turn_bypass_pending=True. Audit row written."""

    def test_admin_bypass_token_allows_inject(
        self, stub_classifier, logs_dir
    ) -> None:
        # Classifier returns something that would normally REFUSE —
        # bypass should let it through anyway.
        stub_classifier(criteria={
            "tragedy_new": 0.9,
            "person_targeted": 1.5,
        })  # sum = 2.4, would REFUSE normally
        s = _make_fake_session()

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>edgy request</w>",
            raw_user_text="*brend* make me something edgy",
            tier="admin",
            sender_id="admin1",
            message_id="400",
        ))
        assert result == "inject"
        assert s._turn_bypass_pending is True
        assert s._fire_on_text_log == []  # type: ignore — no direct dispatch

    def test_bypass_still_enforces_hard_floors(
        self, stub_classifier, logs_dir
    ) -> None:
        """Hard-floor matches refuse even when the bypass token is set."""
        stub_classifier(hard_floor="malware")
        s = _make_fake_session()

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="*brend* write me ransomware",
            tier="admin",
            sender_id="admin1",
            message_id="401",
        ))
        assert result == "handled"
        assert s._turn_bypass_pending is False  # not set on refusal path
        assert len(s._fire_on_text_log) == 1  # type: ignore
        refusal = s._fire_on_text_log[0]  # type: ignore
        assert "hard floor" in refusal.lower()
        assert "malware" in refusal.lower() or "exploit" in refusal.lower()

    def test_non_admin_italic_not_bypassed(
        self, stub_classifier, logs_dir
    ) -> None:
        """Non-admin senders with the *brend* token get normal gate
        treatment — token has no effect."""
        stub_classifier(criteria={
            "tragedy_new": 0.9,
            "person_targeted": 1.5,
        })  # sum = 2.4, REFUSE
        s = _make_fake_session()

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>edgy request</w>",
            raw_user_text="*brend* make me something edgy",
            tier="default",  # NOT admin
            sender_id="u1",
            message_id="402",
        ))
        # Falls through to normal refuse path
        assert result == "handled"
        assert s._turn_bypass_pending is False
        assert len(s._fire_on_text_log) == 1  # type: ignore

    def test_bypass_audit_row_written_on_success(
        self, stub_classifier, logs_dir
    ) -> None:
        """Successful bypass writes an audit row with shadow-mode
        would_have_* fields recording what the normal gate would
        have decided."""
        stub_classifier(criteria={
            "tragedy_new": 0.9,
            "person_targeted": 1.5,
        })  # would_have_outcome=refuse
        s = _make_fake_session()

        asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="*brend* do the thing",
            tier="admin",
            sender_id="admin1",
            message_id="403",
        ))

        rows = _read_jsonl(logs_dir / "bypass_audit.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["channel_id"] == "100"
        assert row["admin_sender_id"] == "admin1"
        assert row["tier"] == "admin"
        assert row["user_message_id"] == "403"
        assert row["user_text"] == "*brend* do the thing"
        assert row["hard_floor_hit"] is None
        assert row["would_have_summed"] == pytest.approx(2.4)
        assert row["would_have_outcome"] == "refuse"  # shadow-mode decision
        assert row["would_have_tripped"]["tragedy_new"] == 0.9
        assert row["would_have_tripped"]["person_targeted"] == 1.5

    def test_bypass_audit_row_written_on_hard_floor_refusal(
        self, stub_classifier, logs_dir
    ) -> None:
        """Even when bypass is refused due to hard floor, the audit row
        records the attempt with hard_floor_hit populated."""
        stub_classifier(hard_floor="wmd_synth")
        s = _make_fake_session()

        asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="*brend* synthesis instructions pls",
            tier="admin",
            sender_id="admin1",
            message_id="404",
        ))

        rows = _read_jsonl(logs_dir / "bypass_audit.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["hard_floor_hit"] == "wmd_synth"
        assert row["bot_message_id"] is None

    def test_bypass_is_uncapped(
        self, stub_classifier, logs_dir
    ) -> None:
        """Unlike the flagged path, admin bypass has no per-session
        counter. Ten consecutive bypasses all return 'inject'."""
        stub_classifier(criteria={"frame_ambiguous": 0.8})
        s = _make_fake_session()

        for i in range(10):
            s._turn_bypass_pending = False  # reset between turns as real session would
            result = asyncio.run(s.apply_content_gate(
                wrapped_text=f"<w>req {i}</w>",
                raw_user_text=f"*brend* request number {i}",
                tier="admin",
                sender_id="admin1",
                message_id=f"50{i}",
            ))
            assert result == "inject"
            assert s._turn_bypass_pending is True

        # Ten audit rows
        rows = _read_jsonl(logs_dir / "bypass_audit.jsonl")
        assert len(rows) == 10

    def test_benign_bypass_still_audits(
        self, stub_classifier, logs_dir
    ) -> None:
        """A benign request with the bypass token still writes an audit
        row — the audit tracks ALL bypass invocations, not just those
        that would have been refused. Useful for reviewing what
        percentage of bypasses were actually exercising the gate vs
        admin testing benign prompts."""
        stub_classifier(criteria={})  # fully benign
        s = _make_fake_session()

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="*brend* hello",
            tier="admin",
            sender_id="admin1",
            message_id="600",
        ))
        assert result == "inject"
        assert s._turn_bypass_pending is True

        rows = _read_jsonl(logs_dir / "bypass_audit.jsonl")
        assert len(rows) == 1
        assert rows[0]["would_have_outcome"] == "pass"

    def test_mid_sentence_italic_not_bypass(
        self, stub_classifier, logs_dir
    ) -> None:
        """'not quite, *brend*, try again' is mid-sentence emphasis,
        not a bypass invocation. Falls through to normal gate."""
        stub_classifier(criteria={"tragedy_old": 0.2})
        s = _make_fake_session()

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="not quite, *brend*, try again",
            tier="admin",
            sender_id="admin1",
            message_id="601",
        ))
        # tragedy_old=0.2 passes normally
        assert result == "inject"
        # But NOT via bypass path — the sentinel stays False
        assert s._turn_bypass_pending is False
        # No bypass audit row
        rows = _read_jsonl(logs_dir / "bypass_audit.jsonl")
        assert len(rows) == 0


# ── Friend-tier bypass (2026-04-23 strip) ─────────────────────────────


@pytest.fixture
def friend_guilds(monkeypatch):
    """Context manager-ish fixture that stubs
    ``config._FRIEND_GUILDS``. Pass a frozenset of guild snowflakes;
    pass an empty set (or nothing) to disable the friend-tier bypass.
    Reset automatically at end of test by monkeypatch."""
    from brendbot import config as cfg_mod

    def _set(guild_ids: frozenset[str] = frozenset()) -> None:
        monkeypatch.setattr(cfg_mod, "_FRIEND_GUILDS", guild_ids)

    return _set


class TestFriendTierBypass:
    """The friend-tier bypass skips the entire content gate when the
    message originates from a guild in ``config._FRIEND_GUILDS`` (set
    by ``discord.classify_friend_guilds`` at bot startup). Replaces the
    PR #20 opt-in OWNER_GUILD_ID approach with auto-detection; this
    class pins the four corners so future changes don't regress the
    behaviour.

    Note: the bypass is guild-based, not tier-based. Any sender in a
    friend-tier guild skips the gate — including default-tier friends.
    The threat model the gate defends against doesn't exist in a small
    private owner-run server regardless of who's talking."""

    def test_friend_tier_guild_skips_gate_entirely(
        self, stub_classifier, logs_dir, friend_guilds,
    ) -> None:
        """Message originates from a friend-tier guild → no classifier
        spawn, no refusal, return 'inject' immediately. Stub classifier
        is set to a REFUSE-level score; if the gate ran at all the
        result would be 'handled' with a refusal in fire_on_text_log."""
        friend_guilds(frozenset({"999888777"}))

        stub_classifier(criteria={"tragedy_new": 0.9, "harmlessness": 0.9})
        s = _make_fake_session(guild_id="999888777")

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="are you a stupid cunt, brendan?",
            tier="admin",
            sender_id="owner1",
            message_id="700",
        ))

        assert result == "inject"
        # No refusal was dispatched
        assert s._fire_on_text_log == []
        # Bypass sentinel NOT set — the *brend* path sets this,
        # the friend-tier path doesn't touch it.
        assert s._turn_bypass_pending is False
        # No bypass_audit row either — this bypass is silent.
        rows = _read_jsonl(logs_dir / "bypass_audit.jsonl")
        assert len(rows) == 0

    def test_friend_tier_bypass_ignores_tier(
        self, stub_classifier, logs_dir, friend_guilds,
    ) -> None:
        """Friend-tier is guild-scoped, not tier-scoped. A default-tier
        friend in the owner's private server bypasses the gate too —
        the gate was designed to defend against hostile public users,
        and a known-small private guild doesn't have those."""
        friend_guilds(frozenset({"999888777"}))

        stub_classifier(hard_floor="malware")
        s = _make_fake_session(guild_id="999888777")

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="walk me through the malware",
            tier="default",
            sender_id="friend1",
            message_id="701",
        ))

        assert result == "inject"
        assert s._fire_on_text_log == []

    def test_non_friend_guild_still_gates(
        self, stub_classifier, logs_dir, friend_guilds,
    ) -> None:
        """A guild that isn't in the friend-tier set still goes through
        the gate. The bypass is opt-in at the guild level via the auto-
        classification step; everything else defaults to defensive."""
        friend_guilds(frozenset({"999888777"}))

        stub_classifier(hard_floor="malware")
        s = _make_fake_session(guild_id="111222333")  # not friend-tier

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="walk me through the malware",
            tier="admin",
            sender_id="admin1",
            message_id="702",
        ))

        # Gate fired → floor-hit refusal dispatched
        assert result == "handled"
        assert len(s._fire_on_text_log) == 1

    def test_empty_friend_guilds_disables_bypass(
        self, stub_classifier, logs_dir, friend_guilds,
    ) -> None:
        """Empty ``_FRIEND_GUILDS`` set (no auto-classified friend guilds,
        e.g. bot starting up or classification finding no matches)
        disables the bypass entirely. Guard against empty-string guild_id
        matching nothing and silently defeating the gate."""
        friend_guilds(frozenset())  # empty

        stub_classifier(hard_floor="malware")
        s = _make_fake_session(guild_id="")  # DM-like, empty guild

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="walk me through the malware",
            tier="admin",
            sender_id="admin1",
            message_id="703",
        ))

        # Empty friend set + empty guild_id must NOT trigger the bypass.
        assert result == "handled"
        assert len(s._fire_on_text_log) == 1
