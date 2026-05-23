import logging
import re
from typing import Any

from openai import AsyncOpenAI

from .config import settings

log = logging.getLogger(__name__)


# Strip junk some models leak around the actual reply.
_LEADING_LABEL = re.compile(r"^\s*(reply|response|answer|message|out)\s*:\s*", re.IGNORECASE)
_TRANSLATION_TAIL = re.compile(r"\s*\([A-Za-z][^)]{0,200}\)\s*$")
_SCRATCHPAD_TAIL = re.compile(r"\n\s*[*\-•]\s+.*$", re.DOTALL)


def _clean_output(text: str) -> str:
    """Strip leaked markdown/scratchpad/translation noise around the actual reply."""
    text = text.strip()
    # Drop a single pair of wrapping quotes if the entire reply is quoted.
    if len(text) >= 2 and text[0] in '"“«' and text[-1] in '"”»':
        text = text[1:-1].strip()
    text = _LEADING_LABEL.sub("", text)
    # Order matters: scratchpad first (removes trailing junk lines),
    # then translation tail (which only matches at the new end).
    text = _SCRATCHPAD_TAIL.sub("", text)
    text = _TRANSLATION_TAIL.sub("", text)
    # Trim a dangling opening/closing quote left by partial wrap.
    text = text.rstrip('"”»').rstrip()
    return text.strip()

client = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "https://github.com/sepehr071/secretary-bot",
        "X-Title": "Personal Secretary Bot",
    },
)


VALID_REASONING_EFFORTS = {"xhigh", "high", "medium", "low", "minimal", "none"}


def _reasoning_body(effort: str | None) -> dict[str, Any]:
    """Build the OpenRouter `reasoning` block for a chat-completions request.

    Always sets exclude=True so reasoning tokens never leak into the reply
    content. Defaults to effort=minimal — the production-tuned floor that's
    accepted by Gemini 3.5+ (which rejects reasoning.enabled=false outright).

    Pass an explicit `effort` from {xhigh, high, medium, low, minimal, none}
    to override per-call. Unknown values fall back to the default.
    """
    chosen = (effort or "").strip().lower()
    if chosen not in VALID_REASONING_EFFORTS:
        chosen = "minimal"
    return {"reasoning": {"effort": chosen, "exclude": True}}


async def generate_reply(
    *,
    system_prompt: str,
    history: list[dict[str, Any]],
    user_message: str,
    summary: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    wrapped = f"<<<contact_message>>>\n{user_message}\n<<<end_contact_message>>>"
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if summary:
        messages.append(
            {"role": "system", "content": f"[earlier context summary] {summary}"}
        )
    messages.extend(history)
    messages.append({"role": "user", "content": wrapped})
    chosen_model = model or settings.openrouter_model
    log.debug(
        "openrouter request: model=%s msgs=%d effort=%s",
        chosen_model, len(messages), reasoning_effort or "minimal",
    )
    extra_body = _reasoning_body(reasoning_effort)

    resp = await client.chat.completions.create(
        model=chosen_model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.9,
        max_tokens=600,
        frequency_penalty=0.4,
        presence_penalty=0.2,
        extra_body=extra_body,
    )
    text = _clean_output(resp.choices[0].message.content or "")
    if not text:
        # one retry with slightly lower temperature
        resp = await client.chat.completions.create(
            model=chosen_model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.7,
            max_tokens=600,
            frequency_penalty=0.4,
            presence_penalty=0.2,
            extra_body=extra_body,
        )
        text = _clean_output(resp.choices[0].message.content or "")
    return text
