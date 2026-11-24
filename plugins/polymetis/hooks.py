"""The Hermes hooks: follow each turn, decide, log, and apply what ``apply`` allows.

Every hook catches its own errors: a failing Polymetis must never stop a turn.
"""

import dataclasses
import functools
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import decide, log, shape
from .catalog import Skill, parse_index, text_of
from .config import Settings, parse_settings
from .search import rank
from .sessions import Session, Store, Turn

logger = logging.getLogger("hermes_plugins.polymetis")
STORE = Store()
FOLDER = "agent-plugin-polymetis-1717164d"  # the name Hermes gives this plugin's data folder


def _default_data_dir() -> Path:
    """The folder where Hermes puts this plugin's data, for a Hermes whose plugins have no ``ctx.state``."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:  # outside Hermes
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "plugin-data" / FOLDER
    return Path(get_hermes_home()) / "plugin-data" / FOLDER


_get_config: Callable[[str, Any], Any] = lambda key, default=None: default  # noqa: E731
_data_dir: Callable[[], Path] = _default_data_dir
_settings = Settings()
ablation_ready = False  # register() sets it when it added the llm_request middleware that an ablation needs
_files_lock = threading.Lock()
_saved: dict[Path, tuple] = {}  # the index last written to each file
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def configure(get_config: Callable[[str, Any], Any], data_dir: Callable[[], Path] | None = None) -> None:
    global _get_config, _data_dir
    _get_config = get_config
    if data_dir is not None:
        _data_dir = data_dir


def settings() -> Settings:
    return _settings


def log_path() -> Path:
    return _data_dir() / log.NAME


def _refresh_settings() -> Settings:
    global _settings
    _settings, problems = parse_settings(_get_config)
    for problem in problems:
        _warn_once(problem)
    return _settings


@functools.lru_cache(maxsize=64)
def _warn_once(message: str) -> None:
    logger.warning("polymetis: %s", message)


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # never the message: it may quote request content
            _warn_once(f"{fn.__name__} failed: {type(e).__name__}")
            return None
    return wrapper


def _catalog_file(platform: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in platform or "default")[:64]
    return _data_dir() / f"index-{safe}.json"


def _catalog(session: Session) -> tuple | None:
    """The session's own index, else the last one seen on its platform, also by an earlier process, so
    that the first turn of a session can get a tip before Hermes sent its first request."""
    if session.catalog is not None:
        return session.catalog
    if session.platform not in STORE.catalogs:
        try:
            rows = json.loads(_catalog_file(session.platform).read_text(encoding="utf-8"))
            STORE.catalogs[session.platform] = tuple(Skill(**row) for row in rows)
        except (OSError, ValueError, TypeError):
            return None
    return STORE.catalogs.get(session.platform)


def _save_catalog(platform: str, skills: tuple) -> None:
    with STORE.lock:
        STORE.catalogs[platform] = skills
    if not _settings.log:  # the same switch as the log: Polymetis writes no file with it off
        return
    path = _catalog_file(platform)
    with _files_lock:  # the sessions of one platform can send their index at the same time
        if _saved.get(path) == skills:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # the gateway and a CLI may share the folder
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([dataclasses.asdict(s) for s in skills], handle)
            os.replace(tmp, path)  # Windows refuses while another process reads the file; a later request tries again
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        _saved[path] = skills


def _turn(session: Session | None, turn_id: Any) -> Turn | None:
    """The session's turn in progress when a hook belongs to it. Hermes's background review shares the
    session id but runs turns of its own, after the user's turn and sometimes alongside the next one."""
    turn = session.current if session else None
    if turn is None or (turn_id and turn.turn_id and turn_id != turn.turn_id):
        return None
    return turn


def _count(items: list) -> int:
    """The length of a list in Hermes's hook payload. For a large request Hermes keeps the first 50 items
    of a list and adds ``{"_truncated_items": n}`` for the rest."""
    last = items[-1] if items else None
    left_out = last.get("_truncated_items") if isinstance(last, dict) and len(last) == 1 else None
    return len(items) - 1 + left_out if isinstance(left_out, int) else len(items)


def _search(s: Settings, session: Session, turn: Turn) -> None:
    catalog = _catalog(session)
    if catalog is None:
        return
    turn.candidates = rank(session.text, catalog, pin=s.pin, block=s.block)
    top = max((score for _, score in turn.candidates), default=0.0)
    names = {skill.name for skill in catalog}
    if session.level is None:
        session.level, session.level_reason = decide.session_level(s, platform=session.platform, top=top)
        session.chosen = decide.chosen_for_session(s, turn.candidates, names) if session.level != "none" else ()
    elif session.level == "none" and session.level_reason == "no_match" and not session.upgraded \
            and top >= s.min_score:
        session.level, session.level_reason, session.upgraded = "lean", "upgrade", True
        session.chosen = decide.chosen_for_session(s, turn.candidates, names)
    turn.tip = decide.tip(s, turn.candidates) if session.level != "none" else []


@_safe
def on_pre_llm_call(session_id: str = "", turn_id: str = "", user_message: Any = None, platform: str = "",
                    model: str = "", is_first_turn: Any = None, **kwargs) -> dict | None:
    if not session_id:
        return None
    s = _refresh_settings()
    if s.ablation_active() and not ablation_ready:
        _warn_once("ablation: restart Hermes to start it, the plugin adds what it needs when Hermes loads it")
    with STORE.lock:
        session = STORE.get(session_id)
        if session is None:
            # only a new chat: one Polymetis first sees later, as after a restart, already used its skills
            arm = decide.arm(s, session_id) if ablation_ready and is_first_turn else "normal"
            session = STORE.add(session_id, Session(key=decide.session_key(session_id), platform=platform or "",
                                                    model=model or "", arm=arm))
            if session.arm == "ablation":  # known before any index: the middleware removes it
                session.level, session.level_reason = "none", "ablation"
        elif session.arm == "ablation" and not s.ablation_active():  # it ended: the skills come back
            session.arm, session.level, session.level_reason = "normal", None, ""
        text = text_of(user_message)
        session.turns += 1
        session.text = text
        session.current = turn = Turn(turn_id=turn_id or "", number=session.turns)
        turn.correction = session.turns > 1 and decide.is_correction(text)
        turn.repeat = decide.is_repeat(session.last_text, text)
        _search(s, session, turn)
        if "tips" in s.apply and turn.tip and session.arm != "ablation":
            turn.tip_given = True
            return {"context": decide.hint(turn.tip)}
    return None


@_safe
def on_pre_api_request(session_id: str = "", turn_id: str = "", platform: str = "", model: str = "",
                       provider: str = "", system_prompt: Any = None, request_messages: Any = None,
                       request: Any = None, tool_count: Any = None, **kwargs) -> None:
    session = STORE.get(session_id) if session_id else None
    turn = _turn(session, turn_id)
    if turn is None:  # between turns, or a request of the background review
        return
    found = parse_index(text_of(system_prompt))
    body = request.get("body") if isinstance(request, dict) else None
    tools = body.get("tools") if isinstance(body, dict) else None
    with STORE.lock:
        session.provider, session.model = provider or session.provider, model or session.model
        if found is not None:
            session.catalog = tuple(found)
        STORE.remember_request(request_messages, session_id)
        turn.api_calls += 1
        turn.offered_skills = len(found) if found is not None else 0
        # the request's own tools, after any middleware; tool_count, Hermes's count of the agent's tools,
        # for a request Hermes passes on only in short form
        turn.offered_tools = _count(tools) if isinstance(tools, list) else \
            tool_count if isinstance(tool_count, int) else None
        if session.level is None:  # no index was known when the turn began; decide with this one
            _search(_settings, session, turn)
    if found is not None:
        _save_catalog(session.platform, session.catalog)


@_safe
def on_post_api_request(session_id: str = "", turn_id: str = "", response_model: Any = None, response: Any = None,
                        **kwargs) -> None:
    turn = _turn(STORE.get(session_id) if session_id else None, turn_id)
    if turn is None:
        return
    response = response if isinstance(response, dict) else {}
    usage = response.get("usage")
    with STORE.lock:
        answered = str(response_model or response.get("model") or "")
        if answered and answered not in turn.answered_by and len(turn.answered_by) < 3:
            turn.answered_by.append(answered)
        if isinstance(usage, dict):
            prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            out = usage.get("output_tokens") or usage.get("completion_tokens")
            if out is None and usage.get("total_tokens") is not None:
                out = int(usage["total_tokens"]) - prompt
            turn.tokens_in += prompt
            turn.tokens_out += int(out or 0)


@_safe
def on_post_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", turn_id: str = "",
                      status: str = "", **kwargs) -> None:
    session = STORE.get(session_id) if session_id else None
    turn = _turn(session, turn_id)
    if turn is None or not isinstance(tool_name, str) or not tool_name:
        return
    with STORE.lock:
        # the model writes these names, so the log takes only a tool name's shape and the index's skill names
        turn.used_tools[tool_name if _TOOL_NAME.fullmatch(tool_name) else "other"] += 1
        if status == "error":
            turn.tool_errors += 1
        name = args.get("name") if isinstance(args, dict) else None
        known = {skill.name for skill in session.catalog or ()}
        if tool_name == "skill_view" and name in known and name not in turn.used_skills:
            turn.used_skills.append(name)


def _record(session: Session, turn: Turn, s: Settings, outcome: str) -> dict:
    return {
        "time": round(time.time(), 3), "session": session.key, "turn": turn.number,
        "platform": session.platform, "provider": session.provider, "model": session.model,
        "answered_by": turn.answered_by, "level": session.level, "level_reason": session.level_reason,
        "arm": session.arm, "apply": sorted(s.apply), "offered_skills": turn.offered_skills,
        "offered_tools": turn.offered_tools, "candidates": [[n, sc] for n, sc in turn.candidates[:5]],
        "tip": turn.tip, "tip_given": turn.tip_given, "local": turn.local, "chosen": list(session.chosen),
        "used_skills": turn.used_skills, "used_tools": dict(turn.used_tools), "tool_errors": turn.tool_errors,
        "api_calls": turn.api_calls, "tokens_in": turn.tokens_in, "tokens_out": turn.tokens_out,
        "duration_ms": int((time.monotonic() - turn.started) * 1000), "outcome": outcome,
        "correction": turn.correction, "repeat": turn.repeat,
    }


@_safe
def on_session_end(session_id: str = "", turn_id: str = "", completed: Any = None, failed: Any = None,
                   interrupted: Any = None, turn_exit_reason: Any = None, **kwargs) -> None:
    """Hermes fires this after every turn (one ``run_conversation``); the turn's record is written here."""
    session = STORE.get(session_id) if session_id else None
    with STORE.lock:
        turn = _turn(session, turn_id)
        if turn is None:  # no turn in progress, or the late end of another one
            return
        session.current = None
        session.last_text, session.text = session.text, ""
        outcome = ("interrupted" if interrupted else "failed" if failed else "completed" if completed
                   else str(turn_exit_reason or "unknown"))
        record = _record(session, turn, _settings, outcome)
    if _settings.log:
        log.write(log_path(), record)


@_safe
def on_llm_request(request: Any = None, session_id: str = "", turn_id: str = "", **kwargs) -> dict | None:
    """``llm_request`` middleware: the user's turns of a session in the ablation arm get no skills at all."""
    session = STORE.get(session_id) if session_id else None
    if session is None or session.arm != "ablation" or not isinstance(request, dict) or _turn(session, turn_id) is None:
        return None
    return {"request": shape.without_skills(request), "source": "polymetis", "reason": "ablation"}


@_safe
def shape_local(request: Any, slot: Any) -> dict | None:
    """The router's ``local_request`` hook: the session's fixed variant for a local model, one step
    smaller at a time while it does not fit the model's window."""
    s = _settings
    fits = slot.get("fits") if isinstance(slot, dict) else None
    if "local_tools" not in s.apply or not isinstance(request, dict) or not callable(fits):
        return None
    found = STORE.session_of(request.get("messages"))
    if found is None:
        return None
    _, session = found
    with STORE.lock:
        name, shaped = shape.fit(request, session.level or "full", fits, skills=session.chosen,
                                 local_tools=s.local_tools)
        if session.current is not None:
            session.current.local = name
    return None if name == "full" else {"request": shaped, "variant": name}


def catalogs() -> dict[str, list[str]]:
    """The skill names of the last index seen per platform, from the files Polymetis keeps."""
    out = {}
    for path in sorted(_data_dir().glob("index-*.json")):
        try:
            out[path.stem[len("index-"):]] = [row["name"] for row in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out
