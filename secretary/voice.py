"""Transcribe Telegram voice notes via OpenRouter Whisper."""
import base64
import logging
import httpx
from .config import settings

log = logging.getLogger(__name__)

OPENROUTER_AUDIO_URL = "https://openrouter.ai/api/v1/audio/transcriptions"


async def transcribe(ogg_bytes: bytes) -> str | None:
    """Send OGG/OPUS bytes to OpenRouter Whisper. Return text or None on failure."""
    if not ogg_bytes:
        return None
    payload = {
        "model": settings.whisper_model,
        "input_audio": {
            "data": base64.b64encode(ogg_bytes).decode("ascii"),
            "format": "ogg",
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(OPENROUTER_AUDIO_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("text") or "").strip()
            if not text:
                log.warning("whisper returned empty text: %s", data)
                return None
            return text
    except Exception:
        log.exception("voice transcription failed")
        return None
