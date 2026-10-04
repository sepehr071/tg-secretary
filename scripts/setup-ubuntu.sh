#!/usr/bin/env bash
# Interactive one-time Ubuntu setup for tg-secretary + pm2.
# Tested on Ubuntu 22.04 / 24.04. Re-running is safe.
#
# Usage:
#   chmod +x scripts/setup-ubuntu.sh
#   ./scripts/setup-ubuntu.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# ---------- colors ----------
if [ -t 1 ]; then
    BOLD=$(tput bold); DIM=$(tput dim); RED=$(tput setaf 1); GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3); BLUE=$(tput setaf 4); RESET=$(tput sgr0)
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

step()    { echo "${BLUE}==>${RESET} ${BOLD}$*${RESET}"; }
ok()      { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
die()     { echo "${RED}✗${RESET} $*" >&2; exit 1; }

# ---------- 1. system deps ----------
step "Installing apt deps (curl, build-essential, ca-certificates)"
sudo apt-get update -qq
sudo apt-get install -y -qq curl build-essential ca-certificates

# ---------- 2. uv ----------
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    step "Installing uv (Python package manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
grep -q "/.local/bin" "$HOME/.bashrc" 2>/dev/null || \
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
ok "uv: $(uv --version)"

# ---------- 3. Python 3.13 ----------
step "Installing Python 3.13 via uv"
uv python install 3.13

# ---------- 4. project deps ----------
step "Syncing project deps"
uv sync
ok "deps installed"

# ---------- 5. Node.js + pm2 ----------
if ! command -v node >/dev/null 2>&1; then
    step "Installing Node.js 22"
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y -qq nodejs
fi
if ! command -v pm2 >/dev/null 2>&1; then
    step "Installing pm2 globally"
    sudo npm install -g pm2
fi
ok "pm2: $(pm2 --version)"

# ---------- 6. configure .env (validated wizard; re-run safe) ----------
step "Running the setup wizard"
# The wizard polls the bot for the owner check; a running copy would conflict.
pm2 stop tg-secretary >/dev/null 2>&1 || true
# An aborted wizard writes nothing: keep the old .env and still restart the bot below.
uv run python -m secretary.setup || {
    [ -f "$PROJECT_DIR/.env" ] && warn "setup aborted — keeping the existing .env" \
        || die "setup aborted and there is no .env — re-run this script"
}

# ---------- 7. logs dir + executable ----------
mkdir -p "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/run.sh"

# ---------- 8. pm2 start ----------
step "Starting bot under pm2"
pm2 delete tg-secretary >/dev/null 2>&1 || true
pm2 start ecosystem.config.cjs
pm2 save

echo
ok "tg-secretary is running"
echo
echo "  ${BOLD}Commands:${RESET}"
echo "    pm2 list                       # status"
echo "    pm2 logs tg-secretary          # tail logs"
echo "    pm2 restart tg-secretary       # restart after .env edits"
echo
echo "  ${BOLD}Autostart on reboot (one time):${RESET}"
echo "    pm2 startup systemd -u \"\$USER\" --hp \"\$HOME\""
echo "    # copy/run the sudo line it prints, then:"
echo "    pm2 save"
