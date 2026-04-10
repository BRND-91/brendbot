# FUSED-CORE Integration — Setup Guide

## What was added

Two new files drop into `brendbot/` (the Python package dir, same level as `session.py`):

| File | Purpose |
|---|---|
| `fused_core_loader.py` | Reads the JSON knowledge base; renders it as structured markdown |
| `session.py` | Replaces the old `session.py`; calls the loader during CLAUDE.md creation |

## Step 1 — Create the knowledge directory

```bash
mkdir ~/brendbot/brendbot/knowledge
```

## Step 2 — Copy your FUSED-CORE v2 JSON files there

From Windows, copy these 7 files from `Documents\FUSED-CORE-v2\` into the WSL directory above:

```
MANIFEST.json
GOVERNANCE.json
LOGIC.json
STATS.json
SYSTEMS.json
PERSONALITY.json
BUILDSCI.json
```

One-liner from PowerShell (adjust the WSL distro name if needed):

```powershell
$src = "$env:USERPROFILE\Documents\FUSED-CORE-v2"
$dst = "\\wsl.localhost\Ubuntu\home\bmckeon91\brendbot\brendbot\knowledge"
Copy-Item "$src\*.json" $dst
```

Or from inside WSL:

```bash
cp /mnt/c/Users/bmckeon91/Documents/FUSED-CORE-v2/*.json \
   ~/brendbot/brendbot/knowledge/
```

## Step 3 — Copy the new Python files

```bash
# From wherever you saved them (adjust path as needed):
cp /mnt/c/Users/bmckeon91/Documents/brendbot/fused_core_loader.py \
   ~/brendbot/brendbot/

cp /mnt/c/Users/bmckeon91/Documents/brendbot/session.py \
   ~/brendbot/brendbot/
```

> **Note on session.py:** The new file contains a small difference from your original
> at the `ClaudeSession` import — it tries both `claude_agent_sdk` and `claude_code_sdk`.
> If your venv uses a different module name, adjust the import at the top of the file.
> Everything else (tiers, tool permissions, transcript directories) is preserved.

## Step 4 — Smoke-test the loader

```bash
cd ~/brendbot
source venv/bin/activate          # or however you activate the env
python brendbot/fused_core_loader.py brendbot/knowledge
```

You should see the full FUSED-CORE markdown block printed, ending with something like:
```
[OK] 12345 characters generated.
```

## Step 5 — Restart the bot

```bash
python brendbot/main.py
```

On the next fresh session (new channel or after clearing a transcript dir), CLAUDE.md will
contain the FUSED-CORE block appended after your SOUL.md content.

## How it works

```
SessionPool.get(chat_id)
  └─ Session._start()
       └─ Session._write_claude_md()
            ├─ renders SOUL.md / GROUP_SOUL.md template  (unchanged)
            └─ appends fused_core_loader.build_knowledge_block()
                 ├─ reads MANIFEST.json  (load order + crosslinks)
                 ├─ reads GOVERNANCE.json  (FabricationGate, dialect rules)
                 └─ reads LOGIC / STATS / SYSTEMS / PERSONALITY / BUILDSCI
```

The resulting CLAUDE.md becomes the grounding context for that session's
Claude Agent SDK instance. Existing sessions are not affected until their
transcript directory is cleared (or the bot is restarted with a fresh transcripts/).

## Tuning

Edit the caps in `fused_core_loader.py` if the block is too large or too small:

```python
_DEF_LIMIT  = 20   # definitions shown per module
_FACT_LIMIT = 10   # facts shown per module
_THM_LIMIT  = 8    # theorems shown per module
```

## Graceful degradation

If `knowledge/` is missing or any JSON file fails to parse, the loader logs a warning
and returns `""` — the bot continues normally without the knowledge block. No crash.
