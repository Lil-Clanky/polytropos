"""The decisions themselves: pure functions of the settings, the session and the turn's search."""

import hashlib
import re

from .config import Settings
from .search import words

TIP_RATIO = 0.5  # a second skill joins the tip when it scores at least this share of the best one
_CORRECTION = re.compile(
    r"^\W*(no\b|nope\b|wrong\b|incorrect\b|actually\b|that'?s not\b|that is not\b|not what i\b|i meant\b|"
    r"nee\b|neen\b|fout\b|klopt niet\b|dat klopt niet\b|niet wat ik\b|ik bedoelde\b|eigenlijk\b)", re.I)
REPEAT_OVERLAP = 0.7


def session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]


def arm(settings: Settings, session_id: str) -> str:
    """``ablation`` for the configured share of sessions while an ablation runs, else ``normal``."""
    if not settings.ablation_active():
        return "normal"
    bucket = int(hashlib.sha256(b"polymetis-ablation:" + session_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "ablation" if bucket < settings.ablation_share else "normal"


def session_level(settings: Settings, *, platform: str, top: float) -> tuple[str, str]:
    """The level a new session starts with, and why. A session in the ablation arm never gets here."""
    if platform in settings.platform_levels:
        return settings.platform_levels[platform], f"platform:{platform}"
    if top >= settings.min_score:
        return "lean", "match"
    return "none", "no_match"


def tip(settings: Settings, candidates: list[tuple[str, float]]) -> list[str]:
    """The skills worth naming for this turn: at most ``max_tips``, each above ``min_score``."""
    best = max((s for _, s in candidates), default=0.0)
    good = [n for n, s in candidates if s >= settings.min_score and s >= TIP_RATIO * best]
    return good[:settings.max_tips]


def chosen_for_session(settings: Settings, candidates: list[tuple[str, float]], catalog_names) -> tuple[str, ...]:
    """The skills a local model keeps in a ``lean`` session: the pins in the index, then the tip."""
    pins = [n for n in settings.pin if n in catalog_names]
    return tuple(dict.fromkeys(pins + tip(settings, candidates)))


def is_correction(text: str) -> bool:
    return bool(_CORRECTION.match(text or ""))


def is_repeat(previous: str | None, text: str) -> bool:
    a, b = set(words(previous or "")), set(words(text))
    return len(a) >= 3 and len(b) >= 3 and len(a & b) / len(a | b) >= REPEAT_OVERLAP


def hint(names) -> str:
    listed = ", ".join(names)
    return (f"<skill-hint>Relevant skill{'s' if len(names) > 1 else ''}: {listed}. "
            "Load one with skill_view if you need it.</skill-hint>")
