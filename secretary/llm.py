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


async def generate_reply(
    *,
    system_prompt: str,
    history: list[dict[str, Any]],
    user_message: str,
    summary: str | None = None,
) -> str:
    wrapped = f"<<<contact_message>>>\n{user_message}\n<<<end_contact_message>>>"
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if summary:
        messages.append(
            {"role": "system", "content": f"[earlier context summary] {summary}"}
        )
    messages.extend(history)
    messages.append({"role": "user", "content": wrapped})
    log.debug("openrouter request: model=%s msgs=%d", settings.openrouter_model, len(messages))
    # Some providers (e.g. Gemini 3.5 Flash) reject reasoning.enabled=false with
    # "Reasoning is mandatory for this endpoint." Use the lowest non-disabled
    # setting instead: effort=minimal + exclude=true.
    # - effort=minimal: providers that allow it (Gemini, OpenAI o-series) think briefly.
    # - exclude=true: hides any reasoning tokens from the returned content so the
    #   contact never sees scratchpad/thinking leakage.
    # - Claude 4.6+ ignores `effort` (adaptive thinking) but honours `exclude`.
    extra_body = {"reasoning": {"effort": "minimal", "exclude": True}}

    resp = await client.chat.completions.create(
        model=settings.openrouter_model,
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
            model=settings.openrouter_model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.7,
            max_tokens=600,
            frequency_penalty=0.4,
            presence_penalty=0.2,
            extra_body=extra_body,
        )
        text = _clean_output(resp.choices[0].message.content or "")
    return text
