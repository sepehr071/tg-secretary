"""The only place that shells out to pm2. One process per tenant: tgs-<id>."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import settings

RUN = subprocess.run  # tests swap this


class Pm2Error(Exception):
    pass


def name(tid: int) -> str:
    return f"tgs-{tid}"


def _pm2(*args: str) -> str:
    r = RUN(["pm2", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise Pm2Error((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def start(tid: int, cwd: Path) -> None:
    python = settings.repo_root / ".venv" / "bin" / "python"
    _pm2("start", str(python), "--name", name(tid), "--interpreter", "none", "--cwd", str(cwd),
         "--max-memory-restart", "300M", "--restart-delay", "5000", "--time",
         "--output", str(cwd / "logs" / "out.log"), "--error", str(cwd / "logs" / "err.log"),
         "--", "-m", "secretary")
    _pm2("save")


def restart(tid: int) -> None:
    _pm2("restart", name(tid))


def stop(tid: int) -> None:
    _pm2("stop", name(tid))
    _pm2("save")


def delete(tid: int) -> None:
    try:
        _pm2("delete", name(tid))
    except Pm2Error as e:
        if "not found" not in str(e).lower():
            raise
    _pm2("save")


def status() -> dict[str, dict]:
    procs = json.loads(_pm2("jlist") or "[]")
    return {p["name"]: {"status": p["pm2_env"]["status"], "restarts": p["pm2_env"]["restart_time"]}
            for p in procs if p["name"].startswith("tgs-")}
