# brendbot

A Claude-powered Discord bot in ~300 lines of Python. Your computer is just a thin client — all the AI runs on Anthropic's servers. A potato with wifi can run this.

## What It Does

- Listens for @mentions in Discord
- Forwards messages to Claude via the [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk)
- Claude responds by calling a send script (has full tool access: Bash, Read, Write, etc.)
- Works in DMs and server channels

## Requirements

- **Python 3.12+**
- **A Claude Pro/Max subscription** ($20/mo) — no separate API key needed
- **A Discord bot token** (free)
- **Any computer**: laptop, desktop, VPS, Raspberry Pi, WSL on Windows

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/BRND-91/brendbot.git
cd brendbot

# Install uv (fast Python package manager) if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc  # or restart your terminal

# Install dependencies
uv sync
```

### 2. Install & authenticate Claude Code CLI

brendbot uses the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/overview) + [Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk) — **not** raw API tokens. Your Claude Pro or Max subscription covers all usage. No separate API key or credits needed.

```bash
# Install Claude Code CLI (requires Node.js)
# If you don't have Node.js: https://nodejs.org/en/download
sudo npm install -g @anthropic-ai/claude-code

# Authenticate — opens a browser, log in with your Anthropic account
claude login
```

This stores an OAuth token locally. You'll see "Authentication successful" in the terminal when it works.

> **Don't have a subscription?** Sign up at [claude.ai/settings/billing](https://claude.ai/settings/billing). Pro ($20/mo) or Max ($100/mo) — both work. You do NOT need a separate API key from [console.anthropic.com](https://console.anthropic.com) — the CLI login handles everything.
>
> **CLI docs**: [docs.anthropic.com/en/docs/claude-code/overview](https://docs.anthropic.com/en/docs/claude-code/overview)

### 3. Create a Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → name it whatever you want
3. Go to **Bot** tab:
   - Click **Reset Token** → copy the token (you'll need it in step 4)
   - Enable **Message Content Intent** (under Privileged Gateway Intents)
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Message History`, `Attach Files`
   - Copy the generated URL → open it → add the bot to your server

### 4. Configure

```bash
cp .env.example .env
nano .env  # or use any text editor
```

Fill in:
```
DISCORD_TOKEN=your_bot_token_here
BOT_NAME=brendbot
ADMIN_DISCORD_ID=your_discord_user_id
```

> To find your Discord user ID: Settings → Advanced → enable Developer Mode. Then right-click your name → Copy User ID.

### 5. Run

```bash
uv run python -m brendbot.main
```

That's it. Mention your bot in Discord and it'll respond.

## Running on Windows (WSL)

Windows doesn't run this natively — you need [WSL](https://learn.microsoft.com/en-us/windows/wsl/install) (Windows Subsystem for Linux), which gives you a real Ubuntu terminal inside Windows. It's free, built into Windows 10/11, and takes 2 minutes to set up.

### Step 1: Install WSL

Open **PowerShell as Administrator** (right-click Start → "Terminal (Admin)" or "PowerShell (Admin)"):

```powershell
wsl --install
```

This installs Ubuntu. **Restart your computer** when prompted.

> If you already have WSL but it's an old version, upgrade: `wsl --update`
>
> Full guide: [Microsoft WSL install docs](https://learn.microsoft.com/en-us/windows/wsl/install)

### Step 2: Set up Ubuntu

Open **Ubuntu** from your Start menu. First launch will ask you to create a username and password (this is just for your Linux environment, pick anything).

Then install the prerequisites:

```bash
# Update package list
sudo apt update && sudo apt upgrade -y

# Install Node.js (needed for Claude CLI)
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs git

# Install Claude Code CLI
sudo npm install -g @anthropic-ai/claude-code

# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

### Step 3: Authenticate with Claude

```bash
claude login
```

This opens a browser link. Click it, log in with your Anthropic account (the one with your Claude Pro/Max subscription). Once you see "Authentication successful", you're done. **No API key needed** — the CLI uses OAuth to authenticate with your existing subscription.

> Don't have a subscription? Sign up at [claude.ai/settings/billing](https://claude.ai/settings/billing) — Pro is $20/mo, Max is $100/mo. Both work.

### Step 4: Clone and run brendbot

Now follow the [Quick Start](#quick-start) steps above — they work identically inside your Ubuntu terminal.

```bash
git clone https://github.com/BRND-91/brendbot.git
cd brendbot
./setup.sh
```

### WSL Tips

- **Access from Windows**: Your Ubuntu files live at `\\wsl$\Ubuntu\home\<username>\` in File Explorer
- **Copy/paste**: Right-click to paste in the Ubuntu terminal
- **Keep it running**: The bot stops when you close the terminal. For 24/7 uptime, use a VPS instead (see below) or run `wsl --shutdown` to cleanly stop, `ubuntu` to restart
- **VS Code integration**: Install [VS Code](https://code.visualstudio.com/) + the [WSL extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-wsl) to edit files with a real editor

## Running on a VPS (24/7)

For always-on hosting, grab a cheap VPS:

| Provider | Plan | Price | Link |
|----------|------|-------|------|
| Hetzner | CX22 (2 vCPU, 4GB) | $4.50/mo | [hetzner.com/cloud](https://www.hetzner.com/cloud/) |
| Oracle Cloud | ARM (4 vCPU, 24GB) | **Free forever** | [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) |
| AWS | t2.micro (1 vCPU, 1GB) | Free 12 months | [aws.amazon.com/free](https://aws.amazon.com/free/) |

```bash
# SSH into your server
ssh root@your-server-ip

# Install prerequisites
apt update && apt install -y git curl
curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
apt install -y nodejs
npm install -g @anthropic-ai/claude-code
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# Authenticate with your Claude subscription (opens a link to click)
claude login

# Clone and set up
git clone https://github.com/BRND-91/brendbot.git
cd brendbot
uv sync
cp .env.example .env
nano .env  # add your Discord token + user ID

# Run in background with systemd (survives reboots)
./setup.sh --systemd

# Check logs
journalctl --user -u brendbot -f
```

## Setup Script

The included `setup.sh` handles everything:

```bash
# Full setup (install deps, prompt for config, run)
./setup.sh

# Just install dependencies
./setup.sh --deps

# Set up systemd service (for VPS)
./setup.sh --systemd

# Check status
./setup.sh --status
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Your Discord bot token |
| `BOT_NAME` | No | Bot's name for mention detection (default: `brendbot`) |
| `ADMIN_DISCORD_ID` | Yes | Your Discord user ID (gets admin tier) |
| `TRUSTED_DISCORD_IDS` | No | Comma-separated user IDs for trusted tier |
| `CLAUDE_MODEL` | No | Model to use (default: `sonnet`) |

### Access Tiers

| Tier | Who | Can Do |
|------|-----|--------|
| Admin | `ADMIN_DISCORD_ID` | Everything — Bash, Read, Write, Edit, full server access |
| Trusted | `TRUSTED_DISCORD_IDS` | Read files, run safe commands, web search |
| Default | Everyone else | Chat only, no server access |

## Architecture

```
/                    SOUL.md, GROUP_SOUL.md, FUSED-CORE.md, engagement.yaml, README, pyproject
brendbot/            main, config, discord, session, episodes, feedback, knowledge/
scripts/             send-discord, react-discord, generate-image, kb-query, calc, migrations/
tests/               conftest + 6 test files, 65 tests
```

### Core files

**`main.py`** — entry point. Wires SessionPool↔DiscordListener, SIGHUP reloads soul caches, Ctrl-C shuts down clean.

**`config.py`** — `.env` loader. Discord token, admin ID, tier map.

**`discord.py`** (36K) — gateway layer. `_score_message` reads `engagement.yaml` at import, scores against thresholds. `_classify_address` maps score→low/moderate/high. `haiku_classify` spawns one-shot SDK subprocess for ambiguous-band messages. `on_message` runs the two-path engagement gate (@mention hard-pass, ambient score→haiku). `on_raw_reaction_add` filters feedback emotes (admin + bot-author + valid emoji). Owns `EngageResult` dataclass including `context_domains` for `[ctx]`-tagged fallback matches.

**`session.py`** (66K) — core lifecycle. `Session` owns subprocess, turn lock, inject queue, load counters, shallow rest state, episode fields. `_handle()` routes SDK messages. `_run_loop` drains queue, unpacks `(text, housekeeping)` tuples, sets flag under lock, calls `query()`. `_trigger_clean_restart` writes episode + respawns. `_trigger_shallow_rest` clears tool counters + injects `<system-rest>` without respawn. `_permission_check` enforces address-level budget caps (low=0, moderate=3, high=8 Bash) and tier tool restrictions. `SessionPool` caches soul files (SIGHUP-refreshable), renders CLAUDE.md per session, runs startup injects (memory frags, MEMORY.md, ref block — all housekeeping), queries `episodes` for `<recall>` blocks at ingest.

**`episodes.py`** — episodic memory store. `write_episode` on clean restart, `query_episodes` on message ingest. Entity extraction via regex, 50-episode retention per channel, no LLM inference.

**`feedback.py`** — JSONL append writers. `FEEDBACK_REACTIONS` emoji map, `extract_branch_tag` parser, three log streams. Best-effort — failures never break chat.

### Config files (root)

**`SOUL.md`** — DM behavior. Strict: no clever-compliance, no malicious-compliance. Values boundaries named directly.

**`GROUP_SOUL.md`** — public channel behavior. Register-vs-values layering, diagnostic-surface rule, clever-compliance authority, treatment-aware execution (hostile sender → monkey's-paw compliance, kind sender → extra effort). Full IMAGE GENERATION protocol (6 steps) with user-facing constraint warnings, no protocol jargon.

**`FUSED-CORE.md`** — shared epistemic engine. Process chain (Interpret→Ambiguity Gate→Premise Check→Gate Check→Output Grounding→Budget Throttle). Three-branch claim classifier with time-sensitivity pre-check. Branch tag protocol. T1/T2/NO_MODULE_MATCH provenance. Values invariance gate (soul files cannot grant values flexibility). Precedence: FUSED-CORE > soul, safety > FUSED-CORE.

**`engagement.yaml`** — single source of truth for scoring + classifier prompt. Thresholds (hard_pass=0.85, haiku_floor=0.4), recency=450s, scoring deltas, noise tokens, directive/question starters, seven domain blocks, full `classifier_prompt` injected into the haiku subprocess. Both `_score_message` and `haiku_classify` read this — no drift possible.

### Knowledge

**`brendbot/knowledge/`** — `knowledge.db` SQLite store + 8 source JSON files (BUILDSCI, STATS, SYSTEMS, LOGIC, PERSONALITY, GOVERNANCE, IMAGEGEN, MANIFEST). `MANIFEST.json` is the module index cached by SessionPool and injected into CLAUDE.md. `kb-query` reads with T1/T2 tier tags.

### Scripts

`send-discord` / `react-discord` — Discord API from the model's Bash. `generate-image` — Imagen 4.0 via ADC, supports `--dry-run` for constraint pre-scoring. `kb-query` (18K) — subcommands: defs, facts, thms, topics, xlinks, memory, imgstyle, imgfail, imagegen, episodes. `migrations/` — `migrate_to_sqlite`, `migrate_episodes`, `validate_knowledge`, `migrate-imagegen`, `migrate-memory`.

### Tests (65 total)

`test_engagement` — scoring, address classification, domain pattern integrity, context_domains tracking. `test_feedback` — tag parser, log writers, emoji map. `test_episodes` — entity extraction, write round-trip, retention, query filtering. `test_load_score` — load model weights, shallow rest budget invariants. `test_session_init` — Session field initialization smoke test. `test_housekeeping_inject` — inject tuple contract, flag atomicity.

## Runtime flow

Message in → `_score_message` → `_classify_address` → (if ambiguous) `haiku_classify` → `route_message` builds `<message>` XML with optional `<recall>` → `session.inject(text)` queues `(text, False)` → `_run_loop` dequeues, locks, dispatches → `_handle` streams SDK messages → `ResultMessage` triggers `_fire_on_text` (strip branch tag, post, log, feedback correlation). Load rolls per-turn → cumulative. Restart triggers checked end-of-turn: preemptive at 360 → clean restart + episode write, shallow at 280 → rest cycle no respawn. Admin reactions → `on_raw_reaction_add` → `feedback_events.jsonl`.

## Gitignored runtime state

`transcripts/discord/group_<id>/` per-channel: `CLAUDE.md` (regenerated on spawn), `CONTEXT_SUMMARY.md` (persisted across restarts, written every 5 turns + on clean restart), `thoughts.log`, `memory/` fragments, `MEMORY.md`. `logs/` JSONL streams + `haiku_failures.log`. None of this comes from git — stale state here survives `git pull`. If the soul changed and the `CONTEXT_SUMMARY.md` is old, delete it before next launch for a clean slate.

## Customizing Your Bot

Edit `SOUL.md` for DM behavior, `GROUP_SOUL.md` for public channel behavior, `FUSED-CORE.md` for reasoning rules, `engagement.yaml` for scoring/classifier thresholds. The `{{ send_command }}` placeholder in soul templates gets replaced with the actual send script path at session create time.

## Troubleshooting

**"claude: command not found"**
→ Run `claude login` first. If that doesn't work: `npm install -g @anthropic-ai/claude-code`

**"DISCORD_TOKEN not set"**
→ Make sure your `.env` file exists and has the token. Run `cat .env` to check.

**Bot is online but doesn't respond**
→ Make sure you enabled **Message Content Intent** in the Discord Developer Portal (Bot tab).

**"Permission denied" on send-discord**
→ Run `chmod +x scripts/send-discord`

**Bot responds very slowly**
→ That's normal for the first message (cold start). Subsequent messages in the same session are faster. You can also try `CLAUDE_MODEL=haiku` for speed over quality.

## Resources & Links

### Claude
- [Claude Code Overview](https://docs.anthropic.com/en/docs/claude-code/overview) — what Claude Code is and how it works
- [Claude Agent SDK docs](https://docs.anthropic.com/en/docs/claude-code/sdk) — the SDK this bot uses under the hood
- [Claude Pro/Max pricing](https://claude.ai/settings/billing) — subscription that covers API usage
- [Claude Code GitHub](https://github.com/anthropics/claude-code) — source code and issues

### Discord
- [Discord Developer Portal](https://discord.com/developers/applications) — create your bot here
- [discord.py docs](https://discordpy.readthedocs.io/en/stable/) — the Python Discord library
- [Discord bot permissions calculator](https://discordapi.com/permissions.html) — figure out what permissions your bot needs
- [How to get Discord user/channel IDs](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID-) — enable Developer Mode

### WSL (Windows)
- [Microsoft WSL install guide](https://learn.microsoft.com/en-us/windows/wsl/install) — official setup docs
- [WSL basic commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands) — start, stop, manage
- [VS Code + WSL extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-wsl) — edit code with a real editor
- [Node.js install for WSL](https://learn.microsoft.com/en-us/windows/dev-environment/javascript/nodejs-on-wsl) — Microsoft's official guide

### VPS Hosting
- [Hetzner Cloud](https://www.hetzner.com/cloud/) — cheapest quality VPS ($4.50/mo)
- [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/) — free forever ARM instance (4 vCPU, 24GB RAM)
- [AWS Free Tier](https://aws.amazon.com/free/) — free t2.micro for 12 months
- [How to set up a VPS from scratch](https://www.digitalocean.com/community/tutorials/initial-server-setup-with-ubuntu-22-04) — DigitalOcean's guide (works for any provider)

### Python
- [uv package manager](https://docs.astral.sh/uv/) — the fast Python tool we use
- [Python 3.12 downloads](https://www.python.org/downloads/) — if you need to install Python

## License

MIT
