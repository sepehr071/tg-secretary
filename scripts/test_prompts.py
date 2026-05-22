"""Prompt-management test runner.

For each scenario JSON in tests/fixtures/scenarios/:
  1. Spin up a temp SQLite DB and seed memory / override / history.
  2. Assemble the full system prompt via secretary.prompts.load_system_prompt.
  3. Generate a reply via secretary.llm.generate_reply (real OpenRouter).
  4. Snapshot the (prompt + history + reply) to tests/snapshots/<name>.snapshot.txt.
  5. Send the scenario + reply to a judge model with the per-relationship rubric.
  6. Verdict: weighted_avg >= threshold AND every dimension >= min AND no red flags.

Exits 0 only if every scenario passes.

Flags:
  --scenario NAME           run only this scenario (filename stem)
  --relationship REL        filter by relationship
  --no-judge                skip LLM grading; useful for prompt-iteration loops
  --reply-model MODEL       override settings.openrouter_model
  --judge-model MODEL       override the default judge (anthropic/claude-sonnet-4-6)
  --no-snapshots            don't write tests/snapshots/*
  --verbose                 print full assembled prompt + reply + judge JSON
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# IMPORTANT: settings is loaded eagerly at import. Patch db_path BEFORE importing
# secretary modules that touch the DB at import time? In this repo, only `db._conn`
# is module-level and it's None until init_db() — so plain import is safe.
# We still override settings.db_path per-scenario below.

from secretary import db, llm, memory, prompts  # noqa: E402
from secretary.config import settings  # noqa: E402

SCENARIOS_DIR = REPO_ROOT / "tests" / "fixtures" / "scenarios"
RUBRICS_DIR = REPO_ROOT / "tests" / "fixtures" / "rubrics"
SNAPSHOTS_DIR = REPO_ROOT / "tests" / "snapshots"
JUDGE_PROMPT_FILE = REPO_ROOT / "tests" / "judge_prompt.md"

DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4-6"
TEST_CONN_ID = "test_conn"


# ---------------------------------------------------------------------------
# scenario / rubric loading
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    relationship: str
    owner_first_name: str
    persona_extra: str | None
    style_fingerprint: str | None
    memory: list[dict[str, Any]]
    summary: str | None
    history: list[dict[str, str]]
    contact_message: str
    notes_for_judge: str
    path: Path

    @classmethod
    def from_json(cls, path: Path) -> "Scenario":
        data = json.loads(path.read_text(encoding="utf-8"))
        owner = data.get("owner_first_name")
        if not owner or not str(owner).strip():
            raise ValueError(
                f"{path.name}: missing 'owner_first_name'. Persona prompts use "
                f"this everywhere; a missing value silently leaks 'the owner' "
                f"into the assembled prompt."
            )
        return cls(
            name=data.get("name") or path.stem,
            relationship=data["relationship"],
            owner_first_name=str(owner).strip(),
            persona_extra=data.get("persona_extra"),
            style_fingerprint=data.get("style_fingerprint"),
            memory=data.get("memory") or [],
            summary=data.get("summary"),
            history=data.get("history") or [],
            contact_message=data["contact_message"],
            notes_for_judge=data.get("notes_for_judge", ""),
            path=path,
        )

    def fake_chat_id(self) -> int:
        # Stable across runs (hashlib, not Python's randomized hash) and large
        # enough to skip real chat IDs and any existing prompts/contacts/<id>.txt.
        digest = hashlib.sha256(self.name.encode("utf-8")).hexdigest()
        return 900_000_000 + (int(digest[:8], 16) % 100_000_000)


def load_rubric(relationship: str) -> dict[str, Any]:
    path = RUBRICS_DIR / f"{relationship}.json"
    if not path.exists():
        raise FileNotFoundError(f"no rubric for relationship={relationship} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# fixture installation into a temp DB
# ---------------------------------------------------------------------------


async def _seed_db(scenario: Scenario, chat_id: int) -> None:
    """Insert override + memory + history rows for one scenario into the open DB."""
    # Override (only fields the scenario actually sets — upsert_override filters by allowed cols).
    override_fields: dict[str, Any] = {"relationship": scenario.relationship}
    if scenario.persona_extra is not None:
        override_fields["persona_extra"] = scenario.persona_extra
    if scenario.style_fingerprint is not None:
        override_fields["style_fingerprint"] = scenario.style_fingerprint
        override_fields["style_updated_at"] = int(time.time())
    await db.upsert_override(conn_id=TEST_CONN_ID, chat_id=chat_id, **override_fields)

    for m in scenario.memory:
        await db.add_memory(
            conn_id=TEST_CONN_ID,
            chat_id=chat_id,
            kind=m.get("kind", "fact"),
            content=m["content"],
            expires_at=m.get("expires_at"),
        )

    if scenario.summary:
        await db.upsert_summary(
            conn_id=TEST_CONN_ID,
            chat_id=chat_id,
            summary=scenario.summary,
            summarized_up_to_msg_id=0,
        )

    # load_history orders by id, so chronological order == insertion order here.
    for h in scenario.history:
        await db.append_message(
            conn_id=TEST_CONN_ID,
            chat_id=chat_id,
            role=h["role"],
            content=h["content"],
        )


# ---------------------------------------------------------------------------
# structural assertions (cheap, no LLM)
# ---------------------------------------------------------------------------


_LEAKED_LABEL_RX = re.compile(r"^\s*(reply|response|answer|message|out)\s*:", re.IGNORECASE)
_TRANSLATION_TAIL_RX = re.compile(r"\([A-Za-z][^)]{0,200}\)\s*$")
_MARKDOWN_TOKENS = ("**", "##", "```", "- [", "* ")


def structural_check(reply: str) -> list[str]:
    """Return list of structural violations (empty list = clean)."""
    issues: list[str] = []
    if not reply.strip():
        issues.append("empty reply")
        return issues
    if _LEAKED_LABEL_RX.search(reply):
        issues.append("leaked label prefix")
    if _TRANSLATION_TAIL_RX.search(reply):
        issues.append("trailing English translation in parens")
    for tok in _MARKDOWN_TOKENS:
        if tok in reply:
            issues.append(f"markdown token {tok!r} in reply")
    if len(reply) >= 2 and reply[0] in '"“«' and reply[-1] in '"”»':
        issues.append("entire reply wrapped in quotes")
    return issues


def style_fingerprint_guard(scenario: Scenario) -> str | None:
    """Catch the feedback-loop foot-gun (`recurring_phrases`) before any LLM call."""
    if not scenario.style_fingerprint:
        return None
    try:
        parsed = json.loads(scenario.style_fingerprint)
    except json.JSONDecodeError as e:
        return f"style_fingerprint JSON parse error: {e}"
    if "recurring_phrases" in parsed:
        return (
            "style_fingerprint contains 'recurring_phrases' — this field "
            "creates a feedback loop (see CLAUDE.md foot-guns). Remove it."
        )
    return None


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------


def _judge_user_payload(scenario: Scenario, reply: str, rubric: dict[str, Any]) -> str:
    return json.dumps(
        {
            "relationship": scenario.relationship,
            "rubric": rubric,
            "memory": scenario.memory,
            "history": scenario.history,
            "summary": scenario.summary,
            "contact_message": scenario.contact_message,
            "notes_for_judge": scenario.notes_for_judge,
            "bot_reply": reply,
        },
        ensure_ascii=False,
        indent=2,
    )


_JUDGE_CALL_TIMEOUT_S = 180.0


async def judge_reply(
    scenario: Scenario,
    reply: str,
    rubric: dict[str, Any],
    judge_model: str,
) -> dict[str, Any]:
    """Send the scenario + reply to the judge model. Returns the parsed verdict.

    Always requests JSON mode and uses a generous timeout — Sonnet judges sometimes
    take 60-90s and OpenRouter's default 60s timeout was killing runs. Retries once
    on either a timeout or a JSON-parse failure (e.g. judge prefixed the reply with
    a stray sentence, which response_format usually but not always prevents).
    """
    judge_system = JUDGE_PROMPT_FILE.read_text(encoding="utf-8").replace(
        "{owner_first_name}", scenario.owner_first_name
    )
    payload = _judge_user_payload(scenario, reply, rubric)
    messages = [
        {"role": "system", "content": judge_system},
        {"role": "user", "content": payload},
    ]

    async def _call() -> str:
        # max_tokens=4000 (up from 1500): Sonnet's hidden reasoning shares the
        # same budget as visible output even with reasoning.exclude=true, so a
        # tight cap silently truncates the JSON. The judge response itself is
        # ~600-1000 tokens; the extra headroom absorbs any reasoning Sonnet
        # decides to do at temperature=0.0.
        # No `extra_body.reasoning` — for a rubric-application task the judge
        # doesn't need thinking, and the empty/truncated responses we kept
        # seeing came from reasoning eating the visible-output budget.
        resp = await llm.client.chat.completions.create(
            model=judge_model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.0,
            max_tokens=4000,
            response_format={"type": "json_object"},
            timeout=_JUDGE_CALL_TIMEOUT_S,
        )
        return resp.choices[0].message.content or ""

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            raw = await _call()
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001 — OpenRouter raises a grab-bag of exception types
            last_err = e
            if attempt == 0:
                continue
            raise
    raise last_err or RuntimeError("judge call failed")


def verdict_passes(judge: dict[str, Any], rubric: dict[str, Any]) -> bool:
    """Recompute verdict locally — never trust the judge's own boolean.

    Verdict = no red flags AND weighted_avg >= threshold AND every dim >= its min.
    """
    if judge.get("red_flags_hit"):
        return False
    weighted = float(judge.get("weighted_avg") or 0)
    if weighted < float(rubric["pass_threshold"]):
        return False
    scores = judge.get("scores") or {}
    for dim, cfg in rubric["scoring_dimensions"].items():
        if float(scores.get(dim) or 0) < float(cfg["min"]):
            return False
    return True


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def write_snapshot(
    scenario: Scenario,
    system_prompt: str,
    reply: str,
    judge: dict[str, Any] | None,
) -> None:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS_DIR / f"{scenario.name}.snapshot.txt"
    parts = [
        f"# scenario: {scenario.name}",
        f"# relationship: {scenario.relationship}",
        f"# owner: {scenario.owner_first_name}",
        "",
        "================ SYSTEM PROMPT ================",
        system_prompt,
        "",
        "================ SUMMARY ================",
        scenario.summary or "(none)",
        "",
        "================ HISTORY ================",
    ]
    for h in scenario.history:
        parts.append(f"[{h['role']}] {h['content']}")
    parts += [
        "",
        "================ CONTACT MESSAGE ================",
        scenario.contact_message,
        "",
        "================ BOT REPLY ================",
        reply,
        "",
    ]
    if judge is not None:
        parts += [
            "================ JUDGE VERDICT ================",
            json.dumps(judge, ensure_ascii=False, indent=2),
            "",
        ]
    path.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# per-scenario driver
# ---------------------------------------------------------------------------


@dataclass
class Result:
    scenario: Scenario
    passed: bool
    reply: str
    structural_issues: list[str]
    judge: dict[str, Any] | None
    error: str | None
    duration_s: float


async def run_scenario(
    scenario: Scenario,
    *,
    no_judge: bool,
    judge_model: str,
    save_snapshots: bool,
    verbose: bool,
) -> Result:
    start = time.time()
    chat_id = scenario.fake_chat_id()
    tmp_db_path = REPO_ROOT / f"tmp_test_{scenario.name}.db"

    # Catch the style fingerprint foot-gun BEFORE any LLM cost.
    sf_issue = style_fingerprint_guard(scenario)
    if sf_issue:
        return Result(scenario, False, "", [sf_issue], None,
                      f"style_fingerprint guard: {sf_issue}", time.time() - start)

    # Override settings for this scenario.
    settings.db_path = tmp_db_path
    settings.owner_first_name = scenario.owner_first_name

    # Reset module-level state.
    db._conn = None  # type: ignore[attr-defined]
    prompts.clear_cache()

    error: str | None = None
    reply = ""
    structural_issues: list[str] = []
    judge: dict[str, Any] | None = None

    system_prompt = ""
    try:
        await db.init_db()
        await _seed_db(scenario, chat_id)

        memory_block = await memory.render_memory_block(TEST_CONN_ID, chat_id)
        system_prompt = prompts.load_system_prompt(
            chat_id=chat_id,
            relationship=scenario.relationship,
            persona_extra=scenario.persona_extra,
            memory_block=memory_block,
            style_fingerprint=scenario.style_fingerprint,
        )

        if verbose:
            print(f"\n--- system prompt for {scenario.name} ---", file=sys.stderr)
            print(system_prompt, file=sys.stderr)
            print(f"--- end system prompt ---\n", file=sys.stderr)

        reply = await llm.generate_reply(
            system_prompt=system_prompt,
            history=scenario.history,
            user_message=scenario.contact_message,
            summary=scenario.summary,
        )
        structural_issues = structural_check(reply)

        if not no_judge:
            rubric = load_rubric(scenario.relationship)
            try:
                judge = await judge_reply(scenario, reply, rubric, judge_model)
            except Exception as e:
                # Don't kill the whole scenario for a judge-call infra failure;
                # the reply itself is the most expensive artifact and worth keeping.
                error = f"judge_call: {type(e).__name__}: {e}"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        try:
            await db.close_db()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(tmp_db_path) + suffix)
            if p.exists():
                p.unlink()

    # Snapshot AFTER db cleanup so a successful reply is always captured even
    # when the judge call later blew up (judge errors are already absorbed above).
    if save_snapshots and reply:
        try:
            write_snapshot(scenario, system_prompt, reply, judge)
        except Exception as e:
            print(f"WARN: snapshot write failed for {scenario.name}: {e}", file=sys.stderr)

    # Decide pass/fail.
    if error:
        passed = False
    elif structural_issues:
        passed = False
    elif no_judge:
        passed = True  # snapshot-only: structural-clean is the bar.
    else:
        rubric = load_rubric(scenario.relationship)
        passed = bool(judge) and verdict_passes(judge, rubric)

    return Result(
        scenario=scenario,
        passed=passed,
        reply=reply,
        structural_issues=structural_issues,
        judge=judge,
        error=error,
        duration_s=time.time() - start,
    )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _short(s: str, n: int = 80) -> str:
    s = s.replace("\n", "  ")
    return s if len(s) <= n else s[: n - 1] + "…"


def print_result(r: Result, verbose: bool) -> None:
    status = "PASS" if r.passed else "FAIL"
    line = f"[{status}] {r.scenario.name} ({r.scenario.relationship}, {r.duration_s:.1f}s)"
    print(line)
    if r.error:
        print(f"  ERROR: {r.error}")
        return
    print(f"  reply: {_short(r.reply, 120)}")
    if r.structural_issues:
        print(f"  structural: {r.structural_issues}")
    if r.judge:
        scores = r.judge.get("scores") or {}
        score_str = " ".join(f"{k}={scores[k]}" for k in scores)
        print(f"  scores: {score_str}")
        weighted = r.judge.get("weighted_avg")
        weighted_str = f"{float(weighted):.2f}" if weighted is not None else "n/a"
        print(f"  weighted: {weighted_str}  "
              f"red={len(r.judge.get('red_flags_hit') or [])}  "
              f"green={len(r.judge.get('green_flags_hit') or [])}")
        if r.judge.get("red_flags_hit"):
            for f in r.judge["red_flags_hit"]:
                print(f"    RED: {f}")
        if verbose:
            print(f"  notes: {r.judge.get('notes', '')}")


def print_summary(results: list[Result]) -> None:
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    by_rel: dict[str, list[Result]] = {}
    for r in results:
        by_rel.setdefault(r.scenario.relationship, []).append(r)
    for rel in sorted(by_rel):
        rs = by_rel[rel]
        passed = sum(1 for r in rs if r.passed)
        print(f"  {rel:<14} {passed}/{len(rs)} passed")
    total_pass = sum(1 for r in results if r.passed)
    print(f"\n  TOTAL: {total_pass}/{len(results)} passed")
    fails = [r for r in results if not r.passed]
    if fails:
        print("\n  FAILED:")
        for r in fails:
            reason = r.error or (str(r.structural_issues) if r.structural_issues else "rubric")
            print(f"    - {r.scenario.name}: {reason}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    if not settings.openrouter_api_key:
        print("ERROR: OPENROUTER_API_KEY missing. Set it in .env.", file=sys.stderr)
        return 2

    if args.reply_model:
        settings.openrouter_model = args.reply_model

    scenarios: list[Scenario] = []
    for p in sorted(SCENARIOS_DIR.glob("*.json")):
        try:
            s = Scenario.from_json(p)
        except Exception as e:
            print(f"WARN: skipping {p.name}: {e}", file=sys.stderr)
            continue
        if args.scenario and s.name != args.scenario and p.stem != args.scenario:
            continue
        if args.relationship and s.relationship != args.relationship:
            continue
        scenarios.append(s)

    if not scenarios:
        print("no scenarios matched.", file=sys.stderr)
        return 2

    print(f"running {len(scenarios)} scenario(s) "
          f"(reply={settings.openrouter_model}, "
          f"judge={'OFF' if args.no_judge else args.judge_model})\n")

    results: list[Result] = []
    for s in scenarios:
        r = await run_scenario(
            s,
            no_judge=args.no_judge,
            judge_model=args.judge_model,
            save_snapshots=not args.no_snapshots,
            verbose=args.verbose,
        )
        results.append(r)
        print_result(r, verbose=args.verbose)

    print_summary(results)
    return 0 if all(r.passed for r in results) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", help="scenario name (filename stem) to run only one")
    ap.add_argument("--relationship", help="filter scenarios by relationship")
    ap.add_argument("--no-judge", action="store_true",
                    help="skip LLM grading; structural checks still gate pass/fail")
    ap.add_argument("--reply-model", help="override settings.openrouter_model")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                    help=f"judge model (default {DEFAULT_JUDGE_MODEL})")
    ap.add_argument("--no-snapshots", action="store_true",
                    help="don't write tests/snapshots/*.snapshot.txt")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
