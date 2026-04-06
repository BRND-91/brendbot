#!/usr/bin/env bash
set -euo pipefail

# brendbot setup script
# Usage: ./setup.sh [--deps | --systemd | --status]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------------------------------------------------------------------------
# Install dependencies
# ---------------------------------------------------------------------------
install_deps() {
    info "Checking dependencies..."

    # Check for uv
    if ! command -v uv &>/dev/null; then
        info "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        ok "uv installed"
    else
        ok "uv found: $(uv --version)"
    fi

    # Check for claude
    if ! command -v claude &>/dev/null; then
        warn "Claude CLI not found. Installing..."
        if command -v npm &>/dev/null; then
            npm install -g @anthropic-ai/claude-code
        else
            err "npm not found. Install Node.js first:"
            echo "  curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -"
            echo "  sudo apt install -y nodejs"
            echo "  npm install -g @anthropic-ai/claude-code"
            exit 1
        fi
    else
        ok "Claude CLI found"
    fi

    # Check claude auth
    if ! claude --version &>/dev/null 2>&1; then
        warn "Claude CLI may not be authenticated. Run: claude login"
    fi

    # Install Python deps
    info "Installing Python dependencies..."
    uv sync
    ok "Dependencies installed"

    # Make send-discord executable
    chmod +x scripts/send-discord
    ok "Scripts ready"
}

# ---------------------------------------------------------------------------
# Configure .env
# ---------------------------------------------------------------------------
configure() {
    if [ -f .env ]; then
        ok ".env file exists"
        return
    fi

    info "Setting up configuration..."
    cp .env.example .env

    echo ""
    echo -e "${YELLOW}Let's configure your bot:${NC}"
    echo ""

    read -rp "Discord bot token: " token
    read -rp "Your Discord user ID: " admin_id
    read -rp "Bot name (default: brendbot): " bot_name
    bot_name=${bot_name:-brendbot}

    sed -i "s/your_bot_token_here/$token/" .env
    sed -i "s/your_user_id_here/$admin_id/" .env
    sed -i "s/BOT_NAME=brendbot/BOT_NAME=$bot_name/" .env

    ok "Configuration saved to .env"
}

# ---------------------------------------------------------------------------
# Set up systemd service
# ---------------------------------------------------------------------------
setup_systemd() {
    info "Setting up systemd service..."

    mkdir -p ~/.config/systemd/user

    cat > ~/.config/systemd/user/brendbot.service << EOF
[Unit]
Description=brendbot Discord Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$(which uv) run python -m brendbot.main
Restart=always
RestartSec=5
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable brendbot
    systemctl --user start brendbot

    ok "brendbot service installed and started"
    info "View logs: journalctl --user -u brendbot -f"
    info "Stop: systemctl --user stop brendbot"
    info "Restart: systemctl --user restart brendbot"
}

# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------
check_status() {
    echo -e "${BLUE}brendbot status:${NC}"
    echo ""

    # Check .env
    if [ -f .env ]; then
        ok ".env configured"
    else
        err ".env missing — run ./setup.sh first"
    fi

    # Check deps
    if command -v uv &>/dev/null; then
        ok "uv: $(uv --version)"
    else
        err "uv not installed"
    fi

    if command -v claude &>/dev/null; then
        ok "Claude CLI installed"
    else
        err "Claude CLI not installed"
    fi

    # Check systemd
    if systemctl --user is-active brendbot &>/dev/null 2>&1; then
        ok "systemd service: running"
    elif systemctl --user is-enabled brendbot &>/dev/null 2>&1; then
        warn "systemd service: enabled but not running"
    else
        info "systemd service: not set up (run ./setup.sh --systemd)"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "${1:-}" in
    --deps)
        install_deps
        ;;
    --systemd)
        setup_systemd
        ;;
    --status)
        check_status
        ;;
    --help|-h)
        echo "Usage: ./setup.sh [--deps | --systemd | --status]"
        echo ""
        echo "  (no args)   Full setup: install deps, configure, run"
        echo "  --deps      Install dependencies only"
        echo "  --systemd   Set up systemd service (for VPS)"
        echo "  --status    Check installation status"
        ;;
    *)
        echo -e "${GREEN}"
        echo "  _                        _ _           _   "
        echo " | |__  _ __ ___ _ __   __| | |__   ___ | |_ "
        echo " | '_ \| '__/ _ \ '_ \ / _\` | '_ \ / _ \| __|"
        echo " | |_) | | |  __/ | | | (_| | |_) | (_) | |_ "
        echo " |_.__/|_|  \___|_| |_|\__,_|_.__/ \___/ \__|"
        echo -e "${NC}"
        echo ""
        install_deps
        configure
        echo ""
        ok "Setup complete!"
        echo ""
        echo -e "  ${GREEN}Run your bot:${NC}  uv run python -m brendbot.main"
        echo -e "  ${GREEN}Run as service:${NC} ./setup.sh --systemd"
        echo ""
        ;;
esac
