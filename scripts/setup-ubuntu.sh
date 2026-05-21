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

# ---------- helpers ----------

# ask required: prompt, varname
ask_required() {
    local prompt="$1" varname="$2" value=""
    while [ -z "$value" ]; do
        read -r -p "  ${prompt}: " value
        [ -z "$value" ] && warn "required — please enter a value"
    done
    printf -v "$varname" '%s' "$value"
}

# ask secret (no echo): prompt, varname
ask_secret() {
    local prompt="$1" varname="$2" value=""
    while [ -z "$value" ]; do
        read -r -s -p "  ${prompt}: " value; echo
        [ -z "$value" ] && warn "required — please enter a value"
    done
    printf -v "$varname" '%s' "$value"
}

# ask with default: prompt, default, varname
ask_default() {
    local prompt="$1" default="$2" varname="$3" value=""
    read -r -p "  ${prompt} ${DIM}[${default}]${RESET}: " value
    [ -z "$value" ] && value="$default"
    printf -v "$varname" '%s' "$value"
}

# ask yes/no: prompt, default_yn (Y/N), varname
ask_yn() {
    local prompt="$1" default="$2" varname="$3" value=""
    local hint
    if [ "$default" = "Y" ]; then hint="${BOLD}Y${RESET}/n"; else hint="y/${BOLD}N${RESET}"; fi
    read -r -p "  ${prompt} [${hint}]: " value
    [ -z "$value" ] && value="$default"
    case "${value,,}" in
        y|yes|true|1) printf -v "$varname" '%s' "yes" ;;
        *)            printf -v "$varname" '%s' "no" ;;
    esac
}

# validate integer
is_int() { [[ "$1" =~ ^[0-9]+$ ]]; }

# mask a secret for display
mask() {
    local s="$1" len=${#1}
    if [ "$len" -lt 12 ]; then printf '%s' '****'
    else printf '%s****%s' "${s:0:4}" "${s: -4}"; fi
}

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

# ---------- 6. .env interactive ----------
if [ -f "$PROJECT_DIR/.env" ]; then
    step ".env already exists"
    ask_yn "Regenerate .env (overwrite existing)?" "N" REGEN
    if [ "$REGEN" = "no" ]; then
        ok "keeping existing .env"
        SKIP_ENV=1
    else
        cp "$PROJECT_DIR/.env" "$PROJECT_DIR/.env.$(date +%Y%m%d-%H%M%S).bak"
        ok "backed up existing .env"
    fi
fi

if [ -z "${SKIP_ENV:-}" ]; then
    echo
    step "Configuring .env — required values"
    echo "${DIM}  TG_BOT_TOKEN: from @BotFather → /newbot (looks like 123:ABC...)${RESET}"
    while true; do
        ask_secret "Telegram bot token" TG_BOT_TOKEN
        [[ "$TG_BOT_TOKEN" == *":"* ]] && break
        warn "token should contain ':' — re-enter"
    done

    echo "${DIM}  OPENROUTER_API_KEY: from https://openrouter.ai/keys (sk-or-v1-...)${RESET}"
    while true; do
        ask_secret "OpenRouter API key" OPENROUTER_API_KEY
        [[ "$OPENROUTER_API_KEY" == sk-or-* ]] && break
        warn "key should start with 'sk-or-' — re-enter (or Ctrl+C to abort)"
    done

    echo "${DIM}  OWNER_USER_ID: your numeric Telegram user id (from @userinfobot)${RESET}"
    while true; do
        ask_required "Owner user id (integer)" OWNER_USER_ID
        is_int "$OWNER_USER_ID" && break
        warn "must be an integer"
    done

    ask_required "Your first name (used in the persona prompt)" OWNER_FIRST_NAME

    echo
    step "Optional — press Enter for defaults"

    ask_default "Reply model" "anthropic/claude-sonnet-4-6" OPENROUTER_MODEL
    ask_default "How many turns of history per reply" "12" HISTORY_TURNS
    ask_default "Auto-reply delay (s)" "30" AUTO_REPLY_DELAY_SECONDS
    ask_default "Away-mode delay (s)" "5" AWAY_REPLY_DELAY_SECONDS
    ask_default "Owner-active cooldown (s)" "600" OWNER_ACTIVE_COOLDOWN_SECONDS
    ask_yn       "Transcribe voice notes automatically?" "Y" VOICE_YN
    if [ "$VOICE_YN" = "yes" ]; then VOICE_TRANSCRIBE="true"; else VOICE_TRANSCRIBE="false"; fi
    ask_default "Whisper model" "openai/whisper-large-v3" WHISPER_MODEL
    ask_default "Extractor model (memory/summary/style)" "google/gemini-3.1-flash-lite" EXTRACTOR_MODEL

    echo
    step "Summary"
    echo "  TG_BOT_TOKEN              = $(mask "$TG_BOT_TOKEN")"
    echo "  OPENROUTER_API_KEY        = $(mask "$OPENROUTER_API_KEY")"
    echo "  OWNER_USER_ID             = $OWNER_USER_ID"
    echo "  OWNER_FIRST_NAME          = $OWNER_FIRST_NAME"
    echo "  OPENROUTER_MODEL          = $OPENROUTER_MODEL"
    echo "  HISTORY_TURNS             = $HISTORY_TURNS"
    echo "  AUTO_REPLY_DELAY_SECONDS  = $AUTO_REPLY_DELAY_SECONDS"
    echo "  AWAY_REPLY_DELAY_SECONDS  = $AWAY_REPLY_DELAY_SECONDS"
    echo "  OWNER_ACTIVE_COOLDOWN_S   = $OWNER_ACTIVE_COOLDOWN_SECONDS"
    echo "  VOICE_TRANSCRIBE          = $VOICE_TRANSCRIBE"
    echo "  WHISPER_MODEL             = $WHISPER_MODEL"
    echo "  EXTRACTOR_MODEL           = $EXTRACTOR_MODEL"
    echo
    ask_yn "Write .env with these values?" "Y" CONFIRM
    [ "$CONFIRM" = "yes" ] || die "aborted by user"

    cat > "$PROJECT_DIR/.env" <<EOF
# Generated by scripts/setup-ubuntu.sh on $(date -Iseconds)
TG_BOT_TOKEN=${TG_BOT_TOKEN}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
OPENROUTER_MODEL=${OPENROUTER_MODEL}
OWNER_USER_ID=${OWNER_USER_ID}
OWNER_FIRST_NAME=${OWNER_FIRST_NAME}
DB_PATH=./secretary.db
HISTORY_TURNS=${HISTORY_TURNS}
AUTO_REPLY_DELAY_SECONDS=${AUTO_REPLY_DELAY_SECONDS}
AWAY_REPLY_DELAY_SECONDS=${AWAY_REPLY_DELAY_SECONDS}
OWNER_ACTIVE_COOLDOWN_SECONDS=${OWNER_ACTIVE_COOLDOWN_SECONDS}
SYSTEM_PROMPT_PATH=
VOICE_TRANSCRIBE=${VOICE_TRANSCRIBE}
WHISPER_MODEL=${WHISPER_MODEL}
EXTRACTOR_MODEL=${EXTRACTOR_MODEL}
PROMPTS_DIR=./prompts
EOF
    chmod 600 "$PROJECT_DIR/.env"
    ok ".env written (mode 0600)"
fi

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
