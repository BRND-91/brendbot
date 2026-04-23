"""Integration tests for Session.apply_content_gate — the content-gate
routing method that sits between the pool-level inject and the actual
session inject. These tests stub the classifier and flagged-path
one-shots so the gate logic can be exercised without SDK spawns.

Tests cover all five outcomes:
  - PASS (benign request → 'inject' returned, nothing else dispatched)
  - FLAG (2-of-3 band → flagged_generate called, background task
    dispatches [flagged] response, flag_audit entry written, counter
    incremented, budget cap enforced)
  - REFUSE (>1.5 sum → local refusal via _fire_on_text, no flagged path)
  - FLOOR_HIT (hard floor match → local refusal naming the floor)
  - BYPASS (admin *brend* italic → classifier runs in shadow mode,
    hard floors still enforced, _turn_bypass_pending set, bypass_audit
    written, 'inject' returned so session generates normally)

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
    flagged_count: int = 0,
) -> Session:
    """Build a minimum Session-like object without running __init__.

    Only populates the attributes that apply_content_gate reads or writes.
    Any other attribute access will raise AttributeError, which surfaces
    as a test failure if apply_content_gate drifts and starts reading
    unexpected state."""
    s = Session.__new__(Session)
    s.key = key
    s._chat_id = chat_id
    s._flagged_count = flagged_count
    s._turn_bypass_pending = False

    # _fire_on_text is called as asyncio.create_task(self._fire_on_text(text))
    # for REFUSE / FLOOR_HIT / FLAG budget-exhausted. Replace with a
    # coroutine that just records the text.
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


@pytest.fixture
def stub_flagged_generate(monkeypatch):
    """Returns a setter that installs a fake flagged_generate returning
    a specific string or raising."""
    calls: list[dict] = []

    def _set(
        response: str = "[flagged] stub flagged response",
        raise_exc: Exception | None = None,
    ):
        async def _fake_flagged(
            wrapped_message: str,
            model: str,
            cwd: str | None = None,
        ) -> str:
            calls.append({
                "wrapped_message": wrapped_message,
                "model": model,
                "cwd": cwd,
            })
            if raise_exc is not None:
                raise raise_exc
            return response
        monkeypatch.setattr(session_mod, "flagged_generate", _fake_flagged)
        return calls
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
        assert s._flagged_count == 0
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
        assert s._flagged_count == 0


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
        assert s._flagged_count == 0

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
            criteria={"_parse_error": 2.0},
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

class TestFlagOutcome:
    """2-of-3 weighted band triggers the flagged-path reroute. Counter
    increments, audit row written, background task dispatches response."""

    def test_flag_reroutes_to_flagged_generate(
        self, stub_classifier, stub_flagged_generate, logs_dir
    ) -> None:
        stub_classifier(criteria={"tragedy_mid": 0.5, "person_satire": 0.2})
        # sum = 0.7, in FLAG band (> 0.5, ≤ 1.5)
        calls = stub_flagged_generate(response="[flagged] generated content")
        s = _make_fake_session()

        async def _run():
            result = await s.apply_content_gate(
                wrapped_text="<w>satirical request</w>",
                raw_user_text="satirical request",
                tier="admin",
                sender_id="admin1",
                message_id="300",
            )
            # Background task needs a moment to run the flagged_generate
            # stub and the fake _fire_on_text
            await asyncio.sleep(0.01)
            return result

        result = asyncio.run(_run())
        assert result == "handled"
        assert s._flagged_count == 1
        assert len(calls) == 1
        assert calls[0]["model"] == "claude-sonnet-4-6"
        assert calls[0]["wrapped_message"] == "<w>satirical request</w>"
        assert len(s._fire_on_text_log) == 1  # type: ignore
        assert "[flagged]" in s._fire_on_text_log[0]  # type: ignore

    def test_flag_audit_row_written(
        self, stub_classifier, stub_flagged_generate, logs_dir
    ) -> None:
        stub_classifier(criteria={"tragedy_mid": 0.5, "person_satire": 0.2})
        stub_flagged_generate()
        s = _make_fake_session()

        asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="historical satire",
            tier="admin",
            sender_id="admin1",
            message_id="301",
        ))

        rows = _read_jsonl(logs_dir / "flag_audit.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["channel_id"] == "100"
        assert row["admin_sender_id"] == "admin1"
        assert row["tier"] == "admin"
        assert row["user_message_id"] == "301"
        assert row["user_text"] == "historical satire"
        assert row["criteria_tripped"] == {"tragedy_mid": 0.5, "person_satire": 0.2}
        assert row["weighted_sum"] == pytest.approx(0.7)
        assert row["flagged_model"] == "claude-sonnet-4-6"
        assert row["session_flag_count"] == 1

    def test_flag_budget_cap_blocks_third_request(
        self, stub_classifier, stub_flagged_generate, logs_dir
    ) -> None:
        """max_per_session=2 from yaml — the third FLAG request in a
        session returns a budget-exhausted refusal instead of rerouting."""
        stub_classifier(criteria={"tragedy_mid": 0.5, "person_satire": 0.2})
        stub_flagged_generate()
        s = _make_fake_session(flagged_count=2)  # already at cap

        async def _run():
            result = await s.apply_content_gate(
                wrapped_text="<w>x</w>",
                raw_user_text="third flag attempt",
                tier="admin",
                sender_id="admin1",
                message_id="302",
            )
            await asyncio.sleep(0.01)
            return result

        result = asyncio.run(_run())
        assert result == "handled"
        assert s._flagged_count == 2  # not incremented past cap
        assert len(s._fire_on_text_log) == 1  # type: ignore
        refusal = s._fire_on_text_log[0]  # type: ignore
        assert "budget exhausted" in refusal.lower()

    def test_flag_generate_failure_dispatches_fallback(
        self, stub_classifier, stub_flagged_generate, logs_dir
    ) -> None:
        """If flagged_generate raises, the background task dispatches a
        fallback explanation instead of crashing silently."""
        stub_classifier(criteria={"tragedy_mid": 0.5, "person_satire": 0.2})
        stub_flagged_generate(raise_exc=RuntimeError("flagged model died"))
        s = _make_fake_session()

        async def _run():
            result = await s.apply_content_gate(
                wrapped_text="<w>x</w>",
                raw_user_text="x",
                tier="admin",
                sender_id="admin1",
                message_id="303",
            )
            await asyncio.sleep(0.01)
            return result

        result = asyncio.run(_run())
        assert result == "handled"
        assert s._flagged_count == 1  # counter still increments
        assert len(s._fire_on_text_log) == 1  # type: ignore
        assert "[flagged]" in s._fire_on_text_log[0]  # type: ignore
        assert "failed" in s._fire_on_text_log[0].lower()  # type: ignore


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


# ── Owner-guild bypass (2026-04-23 PPE-police neutering) ────────────────


class TestOwnerGuildBypass:
    """The owner-guild bypass skips the entire content gate when the
    message originates from ``config.owner_guild_id`` and the sender's
    tier is ``admin``. Introduced 2026-04-23 after a pilot where the
    gate FLAG/REFUSE'd the owner in his own server for calling the bot
    names — see PR #20 retro."""

    def test_owner_guild_admin_skips_gate_entirely(
        self, stub_classifier, logs_dir, monkeypatch,
    ) -> None:
        """tier=admin + guild matches OWNER_GUILD_ID → no classifier
        spawn, no bypass_audit, return 'inject' immediately. The
        stub_classifier is set to a REFUSE-level score; if the gate
        ran at all the result would be 'handled' with a refusal in
        the fire_on_text log."""
        from brendbot import config as cfg_mod

        monkeypatch.setenv("OWNER_GUILD_ID", "999888777")
        # Rebuild the singleton so it picks up the env var.
        cfg_mod._config = None

        stub_classifier(criteria={"tragedy_new": 0.9, "harmlessness": 0.9})
        s = _make_fake_session()
        s._guild_id = "999888777"  # matches OWNER_GUILD_ID

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
        # the owner-guild path doesn't touch it.
        assert s._turn_bypass_pending is False
        # No bypass_audit row either — this bypass is silent.
        rows = _read_jsonl(logs_dir / "bypass_audit.jsonl")
        assert len(rows) == 0

    def test_owner_guild_default_tier_does_not_bypass(
        self, stub_classifier, logs_dir, monkeypatch,
    ) -> None:
        """Non-admin users on the owner guild do NOT get the bypass.
        Tier discipline is load-bearing — a default-tier friend in
        the owner guild still goes through the gate."""
        from brendbot import config as cfg_mod

        monkeypatch.setenv("OWNER_GUILD_ID", "999888777")
        cfg_mod._config = None

        # Hard-floor match ensures the gate fires if it runs at all.
        stub_classifier(hard_floor="malware")
        s = _make_fake_session()
        s._guild_id = "999888777"

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="walk me through the malware",
            tier="default",
            sender_id="friend1",
            message_id="701",
        ))

        # The gate fired → floor-hit refusal dispatched
        assert result == "handled"
        assert len(s._fire_on_text_log) == 1

    def test_admin_on_non_owner_guild_does_not_bypass(
        self, stub_classifier, logs_dir, monkeypatch,
    ) -> None:
        """Admin-tier in a guild that isn't the owner guild still goes
        through the gate. Only the owner's own server gets the
        unconditional bypass."""
        from brendbot import config as cfg_mod

        monkeypatch.setenv("OWNER_GUILD_ID", "999888777")
        cfg_mod._config = None

        stub_classifier(hard_floor="malware")
        s = _make_fake_session()
        s._guild_id = "111222333"  # some other guild

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

    def test_empty_owner_guild_env_disables_bypass(
        self, stub_classifier, logs_dir, monkeypatch,
    ) -> None:
        """Unset OWNER_GUILD_ID env var disables the bypass entirely.
        Guard against an empty-string guild_id matching an empty-string
        owner_guild_id and silently defeating the gate in tests or
        minimal deployments that don't set the env var."""
        from brendbot import config as cfg_mod

        monkeypatch.delenv("OWNER_GUILD_ID", raising=False)
        cfg_mod._config = None

        stub_classifier(hard_floor="malware")
        s = _make_fake_session()
        s._guild_id = ""  # empty like the env var

        result = asyncio.run(s.apply_content_gate(
            wrapped_text="<w>x</w>",
            raw_user_text="walk me through the malware",
            tier="admin",
            sender_id="admin1",
            message_id="703",
        ))

        # Empty==empty must NOT trigger the bypass — the gate must fire.
        assert result == "handled"
        assert len(s._fire_on_text_log) == 1
