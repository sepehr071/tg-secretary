#!/usr/bin/env bash
# Production entry point. Used by pm2 (see ecosystem.config.cjs).
# Idempotent: safe to run repeatedly.

set -euo pipefail

cd "$(dirname "$0")"

# uv lives in ~/.local/bin after the curl installer
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 127
fi

# Sync deps from lockfile. --frozen fails if lock drifted; fall back to normal sync.
uv sync --frozen >/dev/null 2>&1 || uv sync >/dev/null

# Exec replaces this shell so pm2 tracks the python process directly.
exec uv run --no-sync python -m secretary
