"""Entrypoint: `uv run python -m webtest`."""
from __future__ import annotations

import os
import sys

import uvicorn

# CLAUDE.md foot-gun: Windows stdout is cp1252 by default; Persian/Arabic in
# replies + exception traces crash the logger. Force utf-8 before uvicorn boots.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001 — best-effort, falls back to env var
    pass


def main() -> None:
    uvicorn.run(
        "webtest.app:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
