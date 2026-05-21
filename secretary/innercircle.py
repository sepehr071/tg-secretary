import re
import time

INNER_CIRCLE = {"gf", "bff", "family"}

# EN + Persian emotional lexicon
EMOTIONAL_REGEX = re.compile(
    r"\b(angry|hurt|love you|miss you|sad|cry|crying|break up|breaking up|why aren'?t you|are you mad|are you okay|are you ok|please answer|please reply)\b"
    r"|مرگ|دلم|ناراحت|دوستت|دلتنگ|بغض|گریه|عصبانی|چرا جواب|چرا نمیای|دلخور",
    re.IGNORECASE,
)


def should_escalate(
    *,
    text: str,
    relationship: str,
    last_inbound_ts: int | None,
    gate_enabled: bool,
    now_ts: int | None = None,
) -> tuple[bool, str | None]:
    """Return (escalate, reason). reason is human-readable, used in owner DM."""
    if not gate_enabled:
        return False, None
    if relationship not in INNER_CIRCLE:
        return False, None
    if EMOTIONAL_REGEX.search(text):
        return True, "emotional keyword detected"
    if len(text) > 200:
        return True, "long message (>200 chars) from inner circle"
    now = now_ts if now_ts is not None else int(time.time())
    if last_inbound_ts is None or (now - last_inbound_ts) > 24 * 3600:
        return True, "first message after 24h+ silence from inner circle"
    return False, None
