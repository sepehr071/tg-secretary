#!/usr/bin/env bash
# One-time Ubuntu setup for tg-secretary + pm2.
# Tested on Ubuntu 22.04 / 24.04.
#
# Usage:
#   chmod +x scripts/setup-ubuntu.sh
#   ./scripts/setup-ubuntu.sh
#
# Re-running is safe — every step is idempotent.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> project: $PROJECT_DIR"

# ----- 1. system deps -----
echo "==> apt: curl, build-essential, ca-certificates"
sudo apt-get update -qq
sudo apt-get install -y -qq curl build-essential ca-certificates

# ----- 2. uv (Python package manager) -----
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "==> installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# Make uv permanent for this user
if ! grep -q "/.local/bin" "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

# ----- 3. Python 3.13 -----
echo "==> uv: install Python 3.13"
uv python install 3.13

# ----- 4. project deps -----
echo "==> uv sync"
uv sync

# ----- 5. Node.js + pm2 -----
if ! command -v node >/dev/null 2>&1; then
    echo "==> installing Node.js 22"
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y -qq nodejs
fi

if ! command -v pm2 >/dev/null 2>&1; then
    echo "==> installing pm2 globally"
    sudo npm install -g pm2
fi

# ----- 6. .env check -----
if [ ! -f "$PROJECT_DIR/.env" ]; then
    if [ -f "$PROJECT_DIR/.env.example" ]; then
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
        echo "==> created .env from .env.example — EDIT IT before continuing"
        echo "    required keys: TG_BOT_TOKEN, OPENROUTER_API_KEY, OWNER_USER_ID, OWNER_FIRST_NAME"
        exit 1
    fi
    echo ".env not found and no .env.example to copy. Aborting." >&2
    exit 1
fi

# ----- 7. logs dir + executable -----
mkdir -p "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/run.sh"

# ----- 8. pm2 start -----
echo "==> pm2 start"
pm2 delete tg-secretary >/dev/null 2>&1 || true
pm2 start ecosystem.config.cjs
pm2 save

echo
echo "==> tg-secretary is running under pm2."
echo "    pm2 list                       # processes"
echo "    pm2 logs tg-secretary          # tail logs"
echo "    pm2 restart tg-secretary       # restart"
echo
echo "==> To autostart on reboot, run this ONCE and follow the printed sudo line:"
echo "    pm2 startup systemd -u \"\$USER\" --hp \"\$HOME\""
echo "    (then run the 'sudo env ...' command it prints, then 'pm2 save')"
