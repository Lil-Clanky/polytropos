"""WhatsApp human pacing.

Incoming WhatsApp messages are held back (skipped at ``pre_gateway_dispatch``) and re-injected
into the WhatsApp adapter after a random delay (default 5-10 minutes). Everything that arrives
for the same chat while it is waiting is merged into one turn, like a person who picks up their
phone once and reads the whole chat. Slash commands (/new, /stop, ...) pass straight through.

Settings (optional, in the Hermes .env file):
  HUMAN_PACE_MIN_SECONDS   default 300
  HUMAN_PACE_MAX_SECONDS   default 600
  HUMAN_PACE_DISABLED      "true" to turn pacing off without uninstalling

Disclosure (config.yaml, plugins.entries.whatsapp-human-pace):
  disclosure       default true; the first reply in a chat each local day starts with a short note
  disclosure_text  default "Replies in this chat come from an assistant."
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes_plugins.whatsapp_human_pace")

_PENDING: dict[str, dict[str, Any]] = {}   # chat_key -> {"events": [...], "due": ts, "task": Task}
_RELEASED: set[int] = set()                 # id(event) of events we re-inject ourselves
_TASKS: set[asyncio.Task] = set()


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _state_file() -> Path:
    return _home() / "plugins" / "whatsapp-human-pace" / "pending.json"


def _settings_file() -> Path:
    return _home() / "plugins" / "whatsapp-human-pace" / "settings.json"


def _settings() -> dict[str, Any]:
    """settings.json (written by /pace) wins over the env defaults."""
    try:
        data = json.loads(_settings_file().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_settings(data: dict[str, Any]) -> None:
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _bounds() -> tuple[float, float]:
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return default
    s = _settings()
    lo = float(s.get("min_seconds", _f("HUMAN_PACE_MIN_SECONDS", 300)))
    hi = float(s.get("max_seconds", _f("HUMAN_PACE_MAX_SECONDS", 600)))
    return (max(0.0, min(lo, hi)), max(0.0, lo, hi))


def _disabled() -> bool:
    s = _settings()
    if "enabled" in s:
        return not bool(s["enabled"])
    return os.environ.get("HUMAN_PACE_DISABLED", "").lower() in {"1", "true", "yes", "on"}


def _fmt(sec: float) -> str:
    return f"{round(sec / 60, 1):g} min" if sec >= 60 else f"{round(sec):g} s"


def _status() -> str:
    if _disabled():
        return "WhatsApp pacing is off: replies are sent right away."
    lo, hi = _bounds()
    waiting = ", ".join(f"{k.split('|')[-1]} in {_fmt(max(0, v['due'] - time.time()))}" for k, v in _PENDING.items())
    extra = f"\nWaiting now: {waiting}" if waiting else ""
    return f"WhatsApp reply delay: {_fmt(lo)} to {_fmt(hi)} (random).{extra}"


_PACE_HELP = (
    "/pace            show the current reply delay\n"
    "/pace 1-3        random 1 to 3 minutes\n"
    "/pace 2          about 2 minutes (1.5-2.5)\n"
    "/pace 30s-90s    in seconds\n"
    "/pace off        reply right away\n"
    "/pace on         turn pacing back on\n"
    "/pace reset      back to 5-10 min\n"
    "/pace now        answer everything that is waiting now"
)


def _parse_amount(token: str) -> float:
    token = token.strip().lower().replace(",", ".")
    mult = 60.0
    for suffix, m in (("sec", 1.0), ("s", 1.0), ("min", 60.0), ("m", 60.0), ("u", 3600.0), ("h", 3600.0)):
        if token.endswith(suffix):
            token, mult = token[: -len(suffix)].strip(), m
            break
    return float(token) * mult


def _pace_command(raw_args: str = "", platform: Any = None, **_: Any) -> str | None:
    plat = str(getattr(platform, "value", platform) or "").lower()
    if plat == "whatsapp":
        return None  # never controllable from the WhatsApp side
    arg = (raw_args or "").strip().lower()
    s = _settings()
    if not arg or arg in {"status", "?"}:
        return _status()
    if arg in {"help", "-h"}:
        return _PACE_HELP
    if arg in {"off", "uit", "0", "direct"}:
        s["enabled"] = False
        _write_settings(s)
        _release_all_now()
        return _status()
    if arg in {"on", "aan"}:
        s["enabled"] = True
        _write_settings(s)
        return _status()
    if arg in {"reset", "default"}:
        _write_settings({"enabled": True})
        return _status()
    if arg in {"now", "nu"}:
        n = _release_all_now()
        return f"{n} waiting chat(s) will be answered now." if n else "Nothing is waiting."
    try:
        parts = [p for p in arg.replace(" tot ", "-").replace(" ", "").split("-") if p]
        if len(parts) == 1:
            mid = _parse_amount(parts[0])
            lo, hi = mid * 0.75, mid * 1.25
        elif len(parts) == 2:
            lo, hi = sorted((_parse_amount(parts[0]), _parse_amount(parts[1])))
        else:
            raise ValueError
    except ValueError:
        return "Not understood.\n" + _PACE_HELP
    s.update({"enabled": True, "min_seconds": round(lo, 1), "max_seconds": round(hi, 1)})
    _write_settings(s)
    _reschedule_pending()
    return _status()


_GATEWAY: Any = None


def _release_all_now() -> int:
    """Answer everything that is currently held, right away."""
    if _GATEWAY is None:
        return 0
    keys = list(_PENDING)
    for key in keys:
        _reschedule(key, 0.0)
    return len(keys)


def _reschedule_pending() -> None:
    """After a pace change, bring held chats in line with the new window (only ever earlier)."""
    lo, hi = _bounds()
    for key, entry in list(_PENDING.items()):
        remaining = entry["due"] - time.time()
        new = random.uniform(lo, hi)
        if new < remaining:
            _reschedule(key, new)


def _reschedule(key: str, delay: float) -> None:
    entry = _PENDING.get(key)
    if not entry or _GATEWAY is None:
        return
    entry["due"] = time.time() + delay
    loop = entry.get("loop")
    if loop is None:
        return

    def _start() -> None:
        old = entry.get("task")
        if old is not None:
            old.cancel()
        if _PENDING.get(key) is not entry:
            return
        task = loop.create_task(_release_later(_GATEWAY, key, delay))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        entry["task"] = task
    loop.call_soon_threadsafe(_start)


def _is_whatsapp(event: Any) -> bool:
    platform = getattr(getattr(event, "source", None), "platform", None)
    value = getattr(platform, "value", platform)
    return str(value or "").lower() == "whatsapp"


def _chat_key(event: Any) -> str:
    src = event.source
    return f"{getattr(src, 'profile', '') or ''}|{src.chat_id or src.user_id or ''}"


# ---- tiny persistence so a gateway restart doesn't silently eat held texts -------------------

def _save() -> None:
    try:
        data = {
            key: {"due": entry["due"], "texts": [e.text or "" for e in entry["events"] if (e.text or "").strip()]}
            for key, entry in _PENDING.items()
        }
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:  # never break dispatch over bookkeeping
        logger.debug("human-pace: could not save pending state", exc_info=True)


def _load_orphans() -> dict[str, list[str]]:
    """Texts left behind by a previous gateway process (held, never released)."""
    try:
        data = json.loads(_state_file().read_text())
        return {k: v.get("texts") or [] for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        return {}


_ORPHANS: dict[str, list[str]] | None = None


# ---- release ---------------------------------------------------------------------------------

def _merge(events: list[Any]) -> list[Any]:
    """Merge consecutive plain-text events into one; keep media events as they are."""
    out: list[Any] = []
    for ev in events:
        prev = out[-1] if out else None
        is_text = not getattr(ev, "media_urls", None)
        prev_text = prev is not None and not getattr(prev, "media_urls", None)
        if is_text and prev_text:
            joined = "\n".join(t for t in ((prev.text or "").strip(), (ev.text or "").strip()) if t)
            out[-1] = dataclasses.replace(ev, text=joined)
        else:
            out.append(ev)
    return out


async def _release_later(gateway: Any, key: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    entry = _PENDING.pop(key, None)
    _save()
    if not entry:
        return
    events = _merge(entry["events"])
    platform = events[-1].source.platform
    adapter = None
    profile = getattr(events[-1].source, "profile", None)
    if profile:
        adapter = (getattr(gateway, "_profile_adapters", None) or {}).get(profile, {}).get(platform)
    adapter = adapter or (getattr(gateway, "adapters", None) or {}).get(platform)
    if adapter is None:
        logger.warning("human-pace: no WhatsApp adapter to release %d held message(s) for %s", len(events), key)
        return
    logger.info("human-pace: releasing %d message(s) for %s after %.0fs", len(events), key, delay)
    for i, ev in enumerate(events):
        _RELEASED.add(id(ev))
        try:
            await adapter.handle_message(ev)
        except Exception:
            logger.warning("human-pace: re-inject failed", exc_info=True)
            _RELEASED.discard(id(ev))
        if i < len(events) - 1:
            await asyncio.sleep(2)


async def _pre_gateway_dispatch(event: Any = None, gateway: Any = None, **_: Any) -> Any:
    global _ORPHANS, _GATEWAY
    if gateway is not None:
        _GATEWAY = gateway
    if event is None or not _is_whatsapp(event):
        return None
    if id(event) in _RELEASED:            # our own re-injection: let it through
        _RELEASED.discard(id(event))
        return {"action": "allow"}
    if _disabled():
        return None
    if getattr(event, "internal", False):
        return None
    text = (getattr(event, "text", "") or "").lstrip()
    if text.startswith("/"):              # commands stay instant
        return None

    key = _chat_key(event)
    if _ORPHANS is None:
        _ORPHANS = _load_orphans()
    orphan_texts = _ORPHANS.pop(key, None) if key not in _PENDING else None
    if orphan_texts:
        joined = "\n".join(orphan_texts + [event.text or ""]).strip()
        event = dataclasses.replace(event, text=joined)

    entry = _PENDING.get(key)
    if entry is None:
        lo, hi = _bounds()
        delay = random.uniform(lo, hi)
        entry = {"events": [], "due": time.time() + delay, "loop": asyncio.get_running_loop()}
        _PENDING[key] = entry
        task = asyncio.get_running_loop().create_task(_release_later(gateway, key, delay))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        entry["task"] = task
        logger.info("human-pace: holding message for %s, reply in ~%.1f min", key, delay / 60)
    entry["events"].append(event)
    _save()
    return {"action": "skip", "reason": "human-pace: held for delayed reply"}


# ---- de-AI the outgoing WhatsApp text ----------------------------------------------------------

import re as _re

_EMOJI_RE = _re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U000020E3\U0001F3FB-\U0001F3FF]+"
)
# The only ones a casual Dutch texter plausibly drops now and then.
_OK_EMOJI = {"😂", "🙈", "😅", "😘", "🥰", "😭", "🤣", "😏", "🙄", "😊", "🫶", "😴", "🥲", "🤭"}
_KEEP_EMOJI_CHANCE = 0.12
_AI_OPENERS = _re.compile(
    r"^(?:(?:goede vraag|wat leuk dat je dat vraagt)[^.!?\n]*[.!?]|(?:natuurlijk|absoluut|zeker weten|leuk)!)\s*",
    _re.I,
)


def _humanize(text: str) -> str:
    s = text.strip()
    # markdown -> plain text
    s = _re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), s)
    s = _re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = _re.sub(r"`([^`]*)`", r"\1", s)
    s = _re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=_re.M)
    s = _re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", s, flags=_re.M)
    # dashes an AI loves
    s = _re.sub(r"\s*[\u2014\u2013]\s*", ", ", s)
    # emoji: drop them, but now and then keep one plausible one at the end
    found = _EMOJI_RE.findall(s)
    s = _EMOJI_RE.sub("", s)
    keep = next((e for e in found if e.replace("\ufe0f", "") in _OK_EMOJI), None)
    # stray unicode quotes / leftover quote at the end
    s = s.replace("\u201c", "").replace("\u201d", "").replace("\u2018", "'").replace("\u2019", "'")
    s = _re.sub(r'(^"|"$)', "", s.strip())
    # canned assistant openers
    s = _AI_OPENERS.sub("", s)
    # tidy whitespace and punctuation
    s = _re.sub(r"[ \t]+([,.!?])", r"\1", s)
    s = _re.sub(r",\s*,", ",", s)
    s = _re.sub(r"!{2,}", "!", s)
    s = _re.sub(r"[ \t]{2,}", " ", s)
    s = _re.sub(r"\n{3,}", "\n\n", s)
    s = "\n".join(line.strip() for line in s.splitlines()).strip(" ,\n")
    if keep and random.random() < _KEEP_EMOJI_CHANCE:
        s = f"{s} {keep}"
    return s or text


_NOTED: dict[str, datetime.date] = {}  # session_id -> local day of its last disclosure note
_DEFAULT_NOTE = "Replies in this chat come from an assistant."


def _disclosure() -> str | None:
    """The note to show, or None when disclosure is off. Bad or missing config means the defaults."""
    try:
        from hermes_cli.config import load_config_readonly
        opts = load_config_readonly()["plugins"]["entries"]["whatsapp-human-pace"]
    except Exception:
        opts = {}
    if not isinstance(opts, dict) or opts.get("disclosure", True) is False:
        return None
    return str(opts.get("disclosure_text") or _DEFAULT_NOTE)


def _transform_llm_output(response_text: str = "", platform: Any = None, session_id: str = "", **_: Any) -> str | None:
    plat = str(getattr(platform, "value", platform) or "").lower()
    if plat != "whatsapp" or not response_text:
        return None
    out = response_text
    if _settings().get("humanize", True) is not False:
        out = _humanize(out)
    note = _disclosure()
    today = datetime.date.today()
    # Held per session (one WhatsApp chat is one session) because the hook gets no chat id.
    if note and _NOTED.get(session_id) != today:
        _NOTED[session_id] = today
        out = f"{note}\n{out}"
    return out if out != response_text else None


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_command(
        "pace", _pace_command,
        description="Set the WhatsApp reply delay (for example /pace 1-3, /pace off, /pace reset)",
        args_hint="[1-3 | 2 | 30s-90s | off | on | reset | now]",
    )
