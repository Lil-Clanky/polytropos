import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from ._shared.bridge import lookup_turn, message_text
from .config import RouterConfig, Slot


@dataclass
class Decision:
    tier: str
    reason: str  # "classifier" | "default" | "classifier_error" | "hint:<platform>"
    pinned: Slot | None = None


def last_user(messages: list) -> tuple[int, str] | None:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i, message_text(messages[i].get("content"))
    return None


def is_new_turn(messages: list) -> bool:
    return bool(messages) and messages[-1].get("role") == "user"


class DecisionCache:
    def __init__(self, max_entries: int = 256):
        self._max = max_entries
        self._data: OrderedDict[str, Decision] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def key_for(self, messages: list) -> str | None:
        # every user message up to the last one, so two chats that end alike do not share a decision
        pairs = [f"{i}:{message_text(m.get('content'))}" for i, m in enumerate(messages) if m.get("role") == "user"]
        return hashlib.sha256("\0".join(pairs).encode()).hexdigest() if pairs else None

    def get(self, key: str) -> Decision | None:
        with self._lock:
            decision = self._data.get(key)
            if decision is not None:
                self._data.move_to_end(key)
            return decision

    def put(self, key: str, decision: Decision) -> None:
        with self._lock:
            self._data[key] = decision
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


def decide(cfg: RouterConfig, messages: list, *, classify_fn: Callable[[str], str | None]) -> Decision:
    found = last_user(messages)
    text = found[1] if found else ""
    info = lookup_turn(text)
    if info and info.platform in cfg.channel_hints:
        return Decision(cfg.channel_hints[info.platform], f"hint:{info.platform}")
    if cfg.classifier is None:
        return Decision(cfg.default_route, "default")
    verdict = classify_fn(text)
    if verdict in cfg.routes:
        return Decision(verdict, "classifier")
    return Decision(cfg.default_route, "classifier_error")
