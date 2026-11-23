"""Hermes hooks: remember turns for the router, hold the machine awake, pause local turns under pressure."""

import ipaddress
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ._shared import bridge, guard
from .awake import AwakeHolder
from .cli import hermes_home

try:
    from hermes_yaml import safe_load
except ImportError:  # outside Hermes
    from yaml import safe_load

log = logging.getLogger(__name__)

BLOCK_TEXT = ("Polytropos paused this turn because {reason}. A local model can freeze this machine "
              "at this level. Switch to a cloud model with /model, or wait and try again.")
ROUTER = "polytropos-router"
ROUTER_PORT = 3001
MAX_RECORDS = 256


def _default_config(key: str, default: Any) -> Any:
    return default


_get_config: Callable[[str, Any], Any] = _default_config
_holder = AwakeHolder()
_records: "OrderedDict[str, tuple[str, str, Any]]" = OrderedDict()
_records_lock = threading.Lock()


def configure(get_config: Callable[[str, Any], Any]) -> None:
    global _get_config
    _get_config = get_config


def _enabled(key: str) -> bool:
    return bool(_get_config(key, True))


def _is_local(base_url) -> bool:
    try:
        url = urlparse(str(base_url))
        host = url.hostname
        port = url.port or {"http": 80, "https": 443}.get(url.scheme)
        if not host or port == ROUTER_PORT:
            return False
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _config_provider(model) -> tuple[Any, Any]:
    try:
        section = safe_load((hermes_home() / "config.yaml").read_text()).get("model")
        if isinstance(section, dict) and section.get("default") == model:
            return section.get("provider"), section.get("base_url")
    except Exception as exc:  # missing file, bad yaml or a non-mapping document: provider unknown
        log.debug("config.yaml not readable: %s", type(exc).__name__)
    return None, None


def _session_is_local(session_id: str, model) -> bool:
    with _records_lock:
        record = _records.get(session_id)
    provider, base_url = (record[:2] if record and record[2] == model else _config_provider(model))
    return provider != ROUTER and _is_local(base_url)


def on_pre_llm_call(session_id, user_message, conversation_history, is_first_turn, model, platform, **kwargs):
    try:
        bridge.remember_turn(bridge.TurnInfo(session_id, platform, bridge.message_text(user_message), time.time()))
        if _enabled("guard_hook") and _session_is_local(session_id, model):
            verdict = guard.judge(guard.read_signals())
            if not verdict.allowed:
                return {"action": "block", "message": BLOCK_TEXT.format(reason=verdict.reason)}
        if _enabled("awake"):
            _holder.acquire(session_id)
    except Exception as exc:
        log.warning("pre_llm_call hook failed: %s", type(exc).__name__)
    return None


def on_pre_api_request(session_id, provider, base_url, **kwargs) -> None:
    with _records_lock:
        _records[session_id] = (provider, base_url, kwargs.get("model"))
        _records.move_to_end(session_id)
        while len(_records) > MAX_RECORDS:
            _records.popitem(last=False)


def on_post_llm_call(session_id, **kwargs) -> None:
    _holder.release(session_id)


def on_session_end(session_id, **kwargs) -> None:
    _holder.release(session_id)
