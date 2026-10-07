"""Round-trip all user-facing website text through one editable JSON file.

  uv run python scripts/site_text.py export [site_text.json]   # write the JSON
  uv run python scripts/site_text.py apply  [site_text.json]   # write edited values back into the code

Covers the hosted site (hosting/templates, hosting/static/app.js, hosting/app.py) and the owner
dashboard (secretary/dashboard: fa.py, templates, app.js, pages.py, contacts.py, web.py).
Prompts are left out on purpose (prompts/, secretary/prompts.py), and so are the bot's Telegram
messages (commands.py, handlers.py, hosting/bot.py).

JSON shape: {"<file>": {"<current text>": "<text you want>"}}. For secretary/dashboard/fa.py the
key is the English source string and the value its Persian translation.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    *sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "hosting" / "templates").glob("*.html")),
    "hosting/static/app.js",
    "hosting/app.py",
    "secretary/dashboard/fa.py",
    *sorted(str(p.relative_to(ROOT)).replace("\\", "/")
            for p in (ROOT / "secretary" / "dashboard" / "templates").glob("*.html")),
    "secretary/dashboard/static/app.js",
    "secretary/dashboard/pages.py",
    "secretary/dashboard/contacts.py",
    "secretary/dashboard/web.py",
]
FA_FILE = "secretary/dashboard/fa.py"
PERSIAN = re.compile("[\u0621-\u064a\u067e-\u06d3]")  # letters only: a lone "\u06f0" digit is code
ZWNJ = "\u200c"
# HTML: tags and Jinja blocks are code; the text between them and quoted strings inside them are text.
HTML_CODE = re.compile(r"<[^>]*>|\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
QUOTED = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')


def _html_items(src: str):
    """(start, end, text) for every Persian run in a template, positions into src."""
    pos = 0
    for m in [*HTML_CODE.finditer(src), None]:
        end = m.start() if m else len(src)
        chunk = src[pos:end]
        if PERSIAN.search(chunk):
            lead = len(chunk) - len(chunk.lstrip())
            body = chunk.strip()
            yield pos + lead, pos + lead + len(body), " ".join(body.replace("&zwnj;", ZWNJ).split()), "html"
        if m:
            # Inside {{ }}/{% %} a string is autoescaped, so "&zwnj;" there would print literally.
            mode = "html" if m.group(0).startswith("<") else "jinja"
            for q in QUOTED.finditer(m.group(0)):
                g = 1 if q.group(1) is not None else 2
                if PERSIAN.search(q.group(g)):
                    s = m.start() + q.start(g)
                    yield s, s + len(q.group(g)), q.group(g).replace("&zwnj;", ZWNJ).replace("\\u200c", ZWNJ), mode
            pos = m.end()


def _js_items(src: str):
    for q in re.finditer(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\]|\\.)*)`', src):
        g = next(i for i in (1, 2, 3) if q.group(i) is not None)
        if PERSIAN.search(q.group(g)):
            yield q.start(g), q.end(g), q.group(g).replace("\\u200c", ZWNJ), "js"


def _offsets(src: str) -> list[int]:
    """Char offset of each line start (ast gives line + UTF-8 byte column)."""
    out, n = [0, 0], 0
    for line in src.splitlines(keepends=True):
        n += len(line)
        out.append(n)
    return out


def _char(src: str, starts: list[int], line: int, col_bytes: int) -> int:
    base = starts[line]
    return base + len(src[base:].encode("utf-8")[:col_bytes].decode("utf-8"))


def _py_items(src: str, fa: bool):
    """(start, end, key, value) for Persian str constants; fa.py is keyed by the English dict key."""
    tree = ast.parse(src)
    starts = _offsets(src)
    span = lambda n: (_char(src, starts, n.lineno, n.col_offset), _char(src, starts, n.end_lineno, n.end_col_offset))
    if fa:
        d = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign))
        assert isinstance(d, ast.Dict)
        for k, v in zip(d.keys, d.values):
            assert isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            yield *span(v), k.value, v.value, "py"
        return
    fstring_parts = {id(c) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for c in n.values}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in fstring_parts
                and PERSIAN.search(n.value) and n.value.strip()):
            yield *span(n), n.value, n.value, "py"


def items(rel: str):
    with open(ROOT / rel, encoding="utf-8", newline="") as f:  # keep CRLF files CRLF
        src = f.read()
    if rel.endswith(".py"):
        return src, list(_py_items(src, rel == FA_FILE))
    gen = _html_items(src) if rel.endswith(".html") else _js_items(src)
    return src, [(s, e, t, t, mode) for s, e, t, mode in gen]


def _encode(mode: str, text: str) -> str:
    if mode == "py":
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace(ZWNJ, "\\u200c") + '"'
    if mode == "html":
        return text.replace(ZWNJ, "&zwnj;")
    if mode == "js":
        return text.replace(ZWNJ, "\\u200c")
    return text  # jinja string literal: the plain character


def export(path: Path) -> None:
    data: dict = {"_readme": (
        "Edit only the values (right side), never the keys. Each key is the text as it is now; "
        "for secretary/dashboard/fa.py the key is the English source and the value the Persian shown "
        "to users. Keep {placeholders} and %s as they are. Type the half-space (ZWNJ) normally.")}
    for rel in FILES:
        _, found = items(rel)
        if found:
            data[rel] = {k: v for _, _, k, v, _ in found}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path} ({sum(len(v) for k, v in data.items() if k != '_readme')} strings)")


def apply(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for rel, edits in data.items():
        if rel == "_readme" or rel not in FILES:
            continue
        src, found = items(rel)
        n = 0
        for s, e, key, value, mode in sorted(found, reverse=True):  # back to front keeps offsets valid
            new = edits.get(key)
            if isinstance(new, str) and new != value:
                src = src[:s] + _encode(mode, new) + src[e:]
                n += 1
        if n:
            with open(ROOT / rel, "w", encoding="utf-8", newline="") as f:
                f.write(src)
        changed += n
    print(f"applied {changed} changes")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "site_text.json"
    {"export": export, "apply": apply}.get(cmd, lambda _: sys.exit(__doc__))(target)
