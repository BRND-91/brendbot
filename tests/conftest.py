"""Shared pytest fixtures.

The brendbot package imports claude_agent_sdk and discord at module load
time. Neither is installable in a clean test environment without the full
runtime, and neither is needed for the pure-Python logic under test
(scoring, classification, feedback log writers).

This conftest installs minimal stubs into sys.modules before any test
file imports brendbot.* — pytest collects this file before any test
module, so the stubs are in place by the time individual tests run.

The autouse ``_isolate_obs_writes`` fixture redirects every
``brendbot.obs.append_jsonl`` call from the production ``logs/``
directory to a per-test tmp directory. Without it, every gate /
phantom-discriminator / pipeline test that drives a code path which
calls obs would silently corrupt the operator's production logs —
proven by the 2026-04-25 production log dump which showed 23 turn
events, 16 errors, and 27 gate events all carrying the test-fixture
session keys ``test:fake`` and ``test:ch1`` mixed into real data.
"""
from __future__ import annotations

import sys
import types

import pytest


def _install_stubs() -> None:
    for mod in (
        "claude_agent_sdk",
        "claude_agent_sdk._internal",
        "claude_agent_sdk._internal.message_parser",
        "claude_agent_sdk._internal.client",
        "discord",
        "dotenv",
        "httpx",
    ):
        sys.modules.setdefault(mod, types.ModuleType(mod))
    sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
    for cls in (
        "AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
        "PermissionResultAllow", "PermissionResultDeny", "ProcessError",
        "ResultMessage", "SystemMessage", "TextBlock", "ThinkingBlock",
        "ToolResultBlock", "ToolUseBlock", "UserMessage",
    ):
        setattr(sys.modules["claude_agent_sdk"], cls, type(cls, (), {}))
    d = sys.modules["discord"]
    d.Client = type("Client", (), {})
    d.Intents = type("Intents", (), {
        "default": staticmethod(lambda: type("I", (), {
            "message_content": False, "guilds": False, "reactions": False,
        })())
    })
    d.Message = type("Message", (), {})
    d.Attachment = type("Attachment", (), {})
    d.LoginFailure = type("LoginFailure", (Exception,), {})
    d.RawReactionActionEvent = type("RawReactionActionEvent", (), {})
    d.abc = types.ModuleType("discord.abc")
    d.abc.Messageable = type("Messageable", (), {})


_install_stubs()


@pytest.fixture(autouse=True)
def _isolate_obs_writes(tmp_path, monkeypatch):
    """Per-test redirect of ``brendbot.obs._LOGS_DIR`` to a tmp dir.

    Autouse — applies to every test in the suite. Without it, any
    test that exercises code calling ``obs.append_jsonl`` (gate
    decisions, turn events, errors, tool calls) writes to
    ``<repo>/logs/*.jsonl``, which on a deploy box is the operator's
    real observability stream.

    Confirmed-bad evidence from the 2026-04-25 production log pull:

    - ``errors.jsonl`` had 16 entries, *all* from
      ``test:fake`` / ``test:ch1`` session keys
    - ``turn_events.jsonl`` had 23 entries with
      ``channel_id="ch1"``
    - ``gate_events.jsonl`` had 27 entries with
      ``channel_id="100"``

    The fixture is a no-op when ``brendbot.obs`` isn't importable
    (e.g. very early stub-install phase), so it's safe even for
    tests that don't touch the obs module."""
    try:
        from brendbot import obs
        monkeypatch.setattr(obs, "_LOGS_DIR", tmp_path / "logs")
    except Exception:
        pass
