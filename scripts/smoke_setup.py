"""Offline checks for the setup wizard helpers. No network, no .env needed.

    uv run python scripts/smoke_setup.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402

from secretary.setup import (  # noqa: E402
    TOKEN_RE, find_owner, mask, merge_env, new_code, real_value, ssh_hint,
)

# Masking never reveals the middle of a secret.
assert mask("123456:ABCDEFGHIJKLMNOP") == "1234…MNOP"
assert mask("short") == "****"

# .env.example placeholders count as unset on a re-run.
assert real_value("replace-with-your-telegram-bot-token") == ""
assert real_value("123456789") == ""
assert real_value("Alex") == ""  # OWNER_FIRST_NAME placeholder in .env.example
assert real_value(None) == ""
assert real_value(" 42 ") == "42"

# Codes: 6 chars from an unambiguous, deep-link-safe alphabet.
code = new_code()
assert len(code) == 6 and code.isalnum() and not set(code) & set("0O1IL")

# Owner match: only a private-chat "/start <code>" counts.
updates = [
    {"update_id": 1, "message": {"chat": {"type": "group"}, "from": {"id": 9}, "text": f"/start {code}"}},
    {"update_id": 2, "message": {"chat": {"type": "private"}, "from": {"id": 8}, "text": "/start WRONG1"}},
    {"update_id": 3, "message": {"chat": {"type": "private"}, "from": {"id": 7, "first_name": "Sam"},
                                 "text": f"/start {code}"}},
    {"update_id": 4, "business_message": {"text": "hi"}},
]
assert find_owner(updates, code) == {"id": 7, "first_name": "Sam"}
assert find_owner(updates[:2], code) is None

# ssh hint only over SSH, using the server-side address.
assert ssh_hint(8780, {}) is None
assert ssh_hint(8780, {"SSH_CONNECTION": "1.2.3.4 5555 10.0.0.9 22", "USER": "deploy"}) == \
    "ssh -L 8780:127.0.0.1:8780 deploy@10.0.0.9"
# A non-standard sshd port must be in the command, or the tunnel can't connect.
assert ssh_hint(8780, {"SSH_CONNECTION": "1.2.3.4 5555 89.36.137.77 7744", "USER": "ai_user"}) == \
    "ssh -L 8780:127.0.0.1:8780 -p 7744 ai_user@89.36.137.77"

# Token shape gate keeps junk (and path tricks) out of the getMe URL.
assert TOKEN_RE.match("123456:" + "A" * 35)
assert not TOKEN_RE.match("123/../x:" + "A" * 35)

# .env merge: fresh install starts from the example; a re-run backs up and keeps unknown keys.
d = Path(tempfile.mkdtemp())
example, env = d / ".env.example", d / ".env"
example.write_text("# comment\nTG_BOT_TOKEN=replace-with-x\nHISTORY_TURNS=12\n", encoding="utf-8")
assert merge_env(env, example, {"TG_BOT_TOKEN": "tok"}) is None
vals = dotenv_values(env)
assert vals["TG_BOT_TOKEN"] == "tok" and vals["HISTORY_TURNS"] == "12"
env.write_text(env.read_text(encoding="utf-8") + "CUSTOM=keep\n", encoding="utf-8")
backup = merge_env(env, example, {"OWNER_FIRST_NAME": "O'Brien"})
assert backup is not None and "CUSTOM=keep" in backup.read_text(encoding="utf-8")
vals = dotenv_values(env)
assert vals["CUSTOM"] == "keep" and vals["OWNER_FIRST_NAME"] == "O'Brien" and vals["TG_BOT_TOKEN"] == "tok"
assert "# comment" in env.read_text(encoding="utf-8")

print("smoke_setup OK")
