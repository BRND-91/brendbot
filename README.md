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
git clone https://github.com/sammcgrail/brendbot.git
cd brendbot

# Install uv (fast Python package manager) if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc  # or restart your terminal

# Install dependencies
uv sync
```

### 2. Authenticate with Claude

```bash
# This opens a browser — log in with your Anthropic account
claude login
```

> This uses OAuth. Your Claude Pro/Max subscription covers all API usage. No API key or credit card beyond your subscription.

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

If you're on Windows, use WSL (Windows Subsystem for Linux):

```powershell
# In PowerShell (admin):
wsl --install

# Restart your computer, then open Ubuntu from Start menu
```

Then follow the Quick Start steps above inside your Ubuntu terminal. Everything works the same.

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

# Clone, install, configure (same as Quick Start)
git clone https://github.com/sammcgrail/brendbot.git
cd brendbot
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv sync
claude login
cp .env.example .env
nano .env

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

## How It Works

```
Discord message → brendbot listener → Claude Agent SDK → Claude Code subprocess
                                                              ↓
                                                    Claude calls tools:
                                                    - Bash (run commands)
                                                    - Read/Write/Edit files
                                                    - Web search/fetch
                                                    - send-discord script
                                                              ↓
                                                    Message posted to Discord
```

The Claude Agent SDK spawns a Claude Code subprocess that has native tool access. Your bot is just the thin glue between Discord and Claude. All the AI inference happens on Anthropic's servers — your machine just needs to keep a WebSocket connection open.

## Customizing Your Bot

Edit `SOUL.md` to change your bot's personality and instructions:

```markdown
You are brendbot — a helpful Discord bot.

Rules:
- To reply, run: {{ send_command }}
- Be helpful and concise.
- You have access to Bash, so you can run commands if asked.
```

The `{{ send_command }}` placeholder gets replaced with the actual send script path automatically.

## Project Structure

```
brendbot/
├── brendbot/
│   ├── main.py        # Entry point — starts Discord listener + session pool
│   ├── config.py      # Loads .env, defines tiers
│   ├── discord.py     # Discord.py bot client
│   └── session.py     # Claude Agent SDK session wrapper
├── scripts/
│   └── send-discord   # Standalone message sender (called by Claude via Bash)
├── SOUL.md            # Bot personality template (DMs)
├── GROUP_SOUL.md      # Bot personality template (group channels)
├── .env.example       # Config template
├── setup.sh           # One-command setup script
└── pyproject.toml     # Python dependencies
```

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

## License

MIT
