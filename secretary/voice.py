"""Transcribe Telegram voice notes via OpenRouter Whisper."""
import base64
import logging
import httpx
from .config import settings

log = logging.getLogger(__name__)

OPENROUTER_AUDIO_URL = "https://openrouter.ai/api/v1/audio/transcriptions"

# Telegram voice .oga can be a few hundred KB; cap the OpenRouter payload at
# something generous but bounded so we don't ship 10MB blobs to the API.
MAX_BYTES = 10 * 1024 * 1024  # 10 MB


async def transcribe(ogg_bytes: bytes) -> str | None:
    """Send OGG/OPUS bytes to OpenRouter Whisper. Return text or None on failure."""
    n = len(ogg_bytes or b"")
    log.info("whisper: entered transcribe (bytes=%d, model=%s)", n, settings.whisper_model)
    if n == 0:
        log.warning("whisper: empty audio buffer, skipping")
        return None
    if n > MAX_BYTES:
        log.warning("whisper: audio too large (%d bytes > %d), skipping", n, MAX_BYTES)
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
            log.info("whisper: POST %s", OPENROUTER_AUDIO_URL)
            resp = await client.post(OPENROUTER_AUDIO_URL, json=payload, headers=headers)
            log.info("whisper: response status=%d", resp.status_code)
            if resp.status_code >= 400:
                body = resp.text[:500] if resp.text else "<empty>"
                log.error("whisper: HTTP %d body=%s", resp.status_code, body)
                return None
            data = resp.json()
            text = (data.get("text") or "").strip()
            if not text:
                log.warning("whisper: empty text in response: %s", data)
                return None
            log.info("whisper: ok, %d chars transcribed", len(text))
            return text
    except Exception:
        log.exception("whisper: voice transcription failed")
        return None
