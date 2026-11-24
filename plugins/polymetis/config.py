"""Polymetis settings, read from plugins.entries.polymetis.settings in the profile's config.yaml."""

import datetime
from dataclasses import dataclass, field
from typing import Any, Callable

LEVELS = ("none", "lean", "full")
FEATURES = ("tips", "local_tools")
DEFAULT_LOCAL_TOOLS = ("clarify", "memory", "read_file", "search_files", "terminal", "web_search", "write_file")


@dataclass(frozen=True)
class Settings:
    apply: frozenset[str] = frozenset()  # empty: shadow mode, decide and log only
    log: bool = True
    min_score: float = 4.0
    max_tips: int = 2
    pin: tuple[str, ...] = ()
    block: frozenset[str] = frozenset()
    platform_levels: dict[str, str] = field(default_factory=dict)
    local_tools: tuple[str, ...] = DEFAULT_LOCAL_TOOLS
    ablation_share: float = 0.0
    ablation_until: datetime.date | None = None

    def ablation_active(self, today: datetime.date | None = None) -> bool:
        today = today or datetime.date.today()
        return self.ablation_share > 0 and (self.ablation_until is None or today <= self.ablation_until)


class _Bad(ValueError):
    pass


def _names(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _Bad(f"{key}: expected a list of names")
    return tuple(dict.fromkeys(value))


def _number(value: Any, key: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise _Bad(f"{key}: expected a number from {low:g} to {high:g}")
    return float(value)


def _apply(value: Any) -> frozenset[str]:
    names = _names(value, "apply")
    if not set(names) <= set(FEATURES):
        raise _Bad(f"apply: expected names from {', '.join(FEATURES)}")
    return frozenset(names)


def _levels(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and v in LEVELS for k, v in value.items()):
        raise _Bad(f"platform_levels: expected platform names mapped to {', '.join(LEVELS)}")
    return dict(value)


def _date(value: Any) -> datetime.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.date):  # YAML reads an unquoted 2026-12-01 as a date
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise _Bad("ablation.until: expected a date such as 2026-12-01") from None


def _flag(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise _Bad(f"{key}: expected true or false")
    return value


_PARSERS: dict[str, Callable[[Any], Any]] = {
    "apply": _apply,
    "log": lambda v: _flag(v, "log"),
    "min_score": lambda v: _number(v, "min_score", 0, 1000),
    "max_tips": lambda v: int(_number(v, "max_tips", 0, 5)),
    "pin": lambda v: _names(v, "pin"),
    "block": lambda v: frozenset(_names(v, "block")),
    "platform_levels": _levels,
    "local_tools": lambda v: _names(v, "local_tools"),
    "ablation.share": lambda v: _number(v, "ablation.share", 0, 1),
    "ablation.until": _date,
}
_FIELDS = {"ablation.share": "ablation_share", "ablation.until": "ablation_until"}


def parse_settings(get: Callable[[str, Any], Any]) -> tuple[Settings, list[str]]:
    """Read every key through ``get(key, default)``. A bad value keeps that key's default and adds a
    problem that names the key, never the value, which may hold a secret."""
    values, problems = {}, []
    for key, parse in _PARSERS.items():
        try:
            raw = get(key, None)
        except Exception:  # Hermes rejected the key or could not read config.yaml
            raw = None
        if raw is None:
            continue
        try:
            values[_FIELDS.get(key, key)] = parse(raw)
        except _Bad as e:
            problems.append(str(e))
    return Settings(**values), problems
