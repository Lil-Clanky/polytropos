"""Hand turn details from the plugin hooks to the router provider.

Both plugins carry their own copy of this module, so the store lives in sys.modules.
"""

import sys
import threading
import time
import types
from dataclasses import dataclass

MAX_ENTRIES = 512
MAX_AGE = 3600.0


@dataclass(frozen=True)
class TurnInfo:
    session_id: str
    platform: str
    user_text: str
    created: float


def _store() -> types.ModuleType:
    store = sys.modules.setdefault("polytropos_bridge_store", types.ModuleType("polytropos_bridge_store"))
    if not hasattr(store, "lock"):
        store.__dict__.setdefault("lock", threading.Lock())
        store.__dict__.setdefault("entries", [])
    return store


_store()  # create the shared store at import time, before any threads use it


def _clear() -> None:
    store = _store()
    with store.lock:
        store.entries.clear()


def message_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(
        part.get("text") or ""
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def remember_turn(info: TurnInfo) -> None:
    store = _store()
    with store.lock:
        store.entries.append(info)
        del store.entries[:-MAX_ENTRIES]


def lookup_turn(last_user_text: str, *, now: float | None = None) -> TurnInfo | None:
    now = time.time() if now is None else now
    store = _store()
    with store.lock:
        candidates = [
            e for e in store.entries
            if now - e.created <= MAX_AGE
            and last_user_text.startswith(e.user_text)
            and (e.user_text or not last_user_text)
        ]
    return max(candidates, key=lambda e: e.created, default=None)
