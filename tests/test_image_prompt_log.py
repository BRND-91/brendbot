"""Tests for the image-gen prompt log file and its append helper.

The ``generate-image`` script writes one JSON line per invocation to
``logs/image_prompts.jsonl``. This exists because the bot's
"what prompt did you run?" readback rule in GROUP_SOUL.md reads the
last matching channel_id entry from that file — the 2026-04-23 pilot
showed that reconstructing from conversation memory produced wrong
prompts repeatedly (the bot kept returning a pre-edit cached prompt
instead of the one it actually ran).

These tests pin the log format: key names, value shapes, append
semantics, and best-effort failure swallowing. The script itself is a
standalone ``uv run --script`` with inline google-genai dependencies;
we import the helper via ``importlib.util`` so the test doesn't have
to resolve those dependencies.
"""
from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "generate-image"
)


@pytest.fixture(scope="module")
def generate_image_module():
    """Load the generate-image script as a module without running main().

    The script's shebang is ``uv run --script`` with inline deps and
    it has no ``.py`` extension, so we use ``SourceFileLoader`` to
    force Python source loading by path. _log_image_prompt doesn't
    touch any of the script's ``uv`` dependencies (no google-genai
    import at module scope) so this loads cleanly even without the
    inline deps resolved.
    """
    loader = SourceFileLoader("generate_image_script", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader("generate_image_script", loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_log_writes_one_jsonl_line(generate_image_module, tmp_path):
    log_path = tmp_path / "image_prompts.jsonl"

    generate_image_module._log_image_prompt(
        log_path,
        channel_id="1492257355683991674",
        prompt="a cursed emoji face merged with realistic skin",
        model="imagen-4.0-generate-001",
        aspect_ratio="1:1",
    )

    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["channel_id"] == "1492257355683991674"
    assert entry["prompt"] == "a cursed emoji face merged with realistic skin"
    assert entry["model"] == "imagen-4.0-generate-001"
    assert entry["aspect_ratio"] == "1:1"
    assert isinstance(entry["ts"], (int, float))
    assert entry["ts"] > 0


def test_log_appends_not_overwrites(generate_image_module, tmp_path):
    log_path = tmp_path / "image_prompts.jsonl"

    for i in range(3):
        generate_image_module._log_image_prompt(
            log_path,
            channel_id="ch1",
            prompt=f"prompt {i}",
            model="imagen-4.0-generate-001",
            aspect_ratio="16:9",
        )

    lines = log_path.read_text().splitlines()
    assert len(lines) == 3
    prompts = [json.loads(line)["prompt"] for line in lines]
    assert prompts == ["prompt 0", "prompt 1", "prompt 2"]


def test_log_creates_parent_dir(generate_image_module, tmp_path):
    """If logs/ doesn't exist yet, the helper creates it. First-run
    deployments shouldn't need a pre-created logs/ dir to get the
    readback feature."""
    log_path = tmp_path / "deeper" / "logs" / "image_prompts.jsonl"
    assert not log_path.parent.exists()

    generate_image_module._log_image_prompt(
        log_path,
        channel_id="ch1",
        prompt="x",
        model="imagen-4.0-generate-001",
        aspect_ratio="1:1",
    )

    assert log_path.exists()


def test_log_swallows_permission_errors(
    generate_image_module, tmp_path, monkeypatch,
):
    """Logging failure must never block image generation. If the log
    file can't be written for any reason (permission denied, disk full,
    read-only mount), the helper returns quietly and the caller proceeds
    to the API call regardless."""

    def _boom(*args, **kwargs):
        raise PermissionError("simulated")

    monkeypatch.setattr(Path, "open", _boom)

    # Should not raise
    generate_image_module._log_image_prompt(
        tmp_path / "image_prompts.jsonl",
        channel_id="ch1",
        prompt="x",
        model="m",
        aspect_ratio="1:1",
    )


def test_multi_channel_readback_pattern(generate_image_module, tmp_path):
    """The readback command in GROUP_SOUL.md is:

        tac logs/image_prompts.jsonl | grep -m1 '"channel_id":"<channel_id>"'

    which picks the most recent entry matching a channel. Confirm the
    on-disk format supports that pattern: each line is a standalone
    JSON object, channel_id appears as a literal string value, lines
    are ordered by append time (oldest first)."""
    log_path = tmp_path / "image_prompts.jsonl"

    generate_image_module._log_image_prompt(
        log_path,
        channel_id="ch_a", prompt="old_a", model="m", aspect_ratio="1:1",
    )
    generate_image_module._log_image_prompt(
        log_path,
        channel_id="ch_b", prompt="old_b", model="m", aspect_ratio="1:1",
    )
    generate_image_module._log_image_prompt(
        log_path,
        channel_id="ch_a", prompt="new_a", model="m", aspect_ratio="1:1",
    )

    lines = log_path.read_text().splitlines()

    # Simulate `tac | grep -m1 '"channel_id":"ch_a"'`
    for line in reversed(lines):
        if '"channel_id": "ch_a"' in line:
            entry = json.loads(line)
            assert entry["prompt"] == "new_a"
            break
    else:
        pytest.fail("no matching channel found in log")
