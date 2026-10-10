"""Claude (Anthropic API) calls with per-call usage accounting.

Used when ANTHROPIC_API_KEY is set. Haiku 5.5 rejects sampling parameters, so variety
comes from the prompt; thinking is adaptive and kept at low effort.
"""
import logging
from typing import Any

from anthropic import AsyncAnthropic

from . import db
from .config import settings

log = logging.getLogger(__name__)

# USD per token, prompts up to 100K tokens (ours are far below). ponytail: one model, one
# row; add rows if ANTHROPIC_MODEL ever points elsewhere.
PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.10e-6, "output": 0.50e-6, "cache_read": 0.01e-6, "cache_write": 0.125e-6},
}

_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=120, max_retries=2)
    return _client


def cost_usd(model: str, usage: Any) -> float:
    p = PRICES.get(model) or next(iter(PRICES.values()))
    return (
        (usage.input_tokens or 0) * p["input"]
        + (usage.output_tokens or 0) * p["output"]
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p["cache_read"]
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p["cache_write"]
    )


async def complete(*, system: str | list[dict[str, Any]], messages: list[dict[str, Any]],
                   max_tokens: int = 2048, effort: str = "low") -> str:
    """One Claude turn. Records tokens + USD into llm_usage; returns the text ("" on refusal)."""
    first_user = next((i for i, m in enumerate(messages) if m["role"] == "user"), None)
    messages = messages[first_user:] if first_user is not None else []  # the API needs a user turn first
    model = settings.anthropic_model
    resp = await client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,  # type: ignore[arg-type]
        output_config={"effort": effort},
    )
    try:
        await db.add_usage(
            model=model,
            input_tokens=resp.usage.input_tokens or 0,
            output_tokens=resp.usage.output_tokens or 0,
            cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            cost_usd=cost_usd(model, resp.usage),
        )
    except Exception:  # noqa: BLE001 — accounting must never eat a reply
        log.exception("usage row not recorded")
    if resp.stop_reason == "refusal":
        log.warning("claude refused (%s)", getattr(resp.stop_details, "category", None))
        return ""
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def strip_fences(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()
