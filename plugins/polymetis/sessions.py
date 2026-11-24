"""What Polymetis remembers per session, in memory only, for the sessions of this process."""

import hashlib
import json
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from .catalog import Skill, text_of

MAX_SESSIONS = 256
MAX_FINGERPRINTS = 1024


@dataclass
class Turn:
    turn_id: str
    number: int
    started: float = field(default_factory=time.monotonic)
    candidates: list[tuple[str, float]] = field(default_factory=list)
    tip: list[str] = field(default_factory=list)
    tip_given: bool = False
    offered_skills: int | None = None
    offered_tools: int | None = None
    answered_by: list[str] = field(default_factory=list)
    used_skills: list[str] = field(default_factory=list)
    used_tools: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    api_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    local: str | None = None
    correction: bool = False
    repeat: bool = False


@dataclass
class Session:
    key: str
    platform: str = ""
    provider: str = ""
    model: str = ""
    arm: str = "normal"
    level: str | None = None  # None until a skill index was seen
    level_reason: str = ""
    upgraded: bool = False
    chosen: tuple[str, ...] = ()
    catalog: tuple[Skill, ...] | None = None
    turns: int = 0
    current: Turn | None = None
    last_text: str | None = None  # the previous turn's text, for the repeat signal; never written anywhere
    text: str = ""  # this turn's text, kept until the turn ends


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._fingerprints: OrderedDict[str, str] = OrderedDict()
        self.catalogs: dict[str, tuple[Skill, ...]] = {}  # the last index seen per platform

    def get(self, session_id: str) -> Session | None:
        with self.lock:
            session = self._sessions.get(session_id)
            if session is not None:
                self._sessions.move_to_end(session_id)
            return session

    def add(self, session_id: str, session: Session) -> Session:
        with self.lock:
            self._sessions[session_id] = session
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
            return session

    def remember_request(self, messages, session_id: str) -> None:
        key = fingerprint(messages)
        if key is None:
            return
        with self.lock:
            self._fingerprints[key] = session_id
            self._fingerprints.move_to_end(key)
            while len(self._fingerprints) > MAX_FINGERPRINTS:
                self._fingerprints.popitem(last=False)

    def session_of(self, messages) -> tuple[str, Session] | None:
        key = fingerprint(messages)
        with self.lock:
            session_id = self._fingerprints.get(key) if key else None
            session = self._sessions.get(session_id) if session_id else None
            return (session_id, session) if session else None

    def clear(self) -> None:
        with self.lock:
            self._sessions.clear()
            self._fingerprints.clear()
            self.catalogs.clear()


def fingerprint(messages) -> str | None:
    """A key for the requests of one turn: the system prompt, the first user message and the last one. A
    request seen in ``pre_api_request`` and the same request in the router's hands get the same key. Hermes's
    background review asks its own question at the end of the same conversation, so it gets another key."""
    if not isinstance(messages, list):
        return None
    system = next((m for m in messages if isinstance(m, dict) and m.get("role") in ("system", "developer")), None)
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    if system is None and not users:
        return None
    parts = [text_of(system.get("content")) if system else ""]
    parts += [text_of(m.get("content")) for m in users[:1] + users[-1:]]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()
