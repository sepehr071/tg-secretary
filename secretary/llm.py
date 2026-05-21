import logging
from typing import Any

from openai import AsyncOpenAI

from .config import settings

log = logging.getLogger(__name__)

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
    resp = await client.chat.completions.create(
        model=settings.openrouter_model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.6,
        max_tokens=600,
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        # one retry with slightly lower temperature
        resp = await client.chat.completions.create(
            model=settings.openrouter_model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.3,
            max_tokens=600,
        )
        text = (resp.choices[0].message.content or "").strip()
    return text
