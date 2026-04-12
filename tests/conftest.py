"""Shared pytest fixtures.

The brendbot package imports claude_agent_sdk and discord at module load
time. Neither is installable in a clean test environment without the full
runtime, and neither is needed for the pure-Python logic under test
(scoring, classification, feedback log writers).

This conftest installs minimal stubs into sys.modules before any test
file imports brendbot.* — pytest collects this file before any test
module, so the stubs are in place by the time individual tests run.
"""
from __future__ import annotations

import sys
import types


def _install_stubs() -> None:
    for mod in (
        "claude_agent_sdk",
        "claude_agent_sdk._internal",
        "claude_agent_sdk._internal.message_parser",
        "claude_agent_sdk._internal.client",
        "discord",
        "dotenv",
        "anthropic",
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
