"""The /polytropos chat command: FreeLLMAPI health, the guard verdict and the last router decision."""

import json
import os

from . import doctor
from ._shared import guard, paths
from .cli import hermes_home
from .freellm import PORT, FreeLLM


def _api_key() -> str | None:
    try:
        from hermes_cli.config import get_env_value  # reads the active profile's .env
    except ImportError:  # outside Hermes
        return os.environ.get("FREELLMAPI_API_KEY")
    return get_env_value("FREELLMAPI_API_KEY")


def _freellmapi() -> str:
    fl = FreeLLM()
    found = fl.detect()
    if not found.listening:
        return f"FreeLLMAPI: not running on 127.0.0.1:{PORT}"
    if not found.is_freellmapi:
        return f"FreeLLMAPI: port {PORT} is used by a program that is not FreeLLMAPI"
    key = _api_key()
    if fl.health(key) if key else found.healthy:
        return f"FreeLLMAPI: healthy on 127.0.0.1:{PORT}"
    return f"FreeLLMAPI: answers on port {PORT}, but /v1/models is not healthy"


def _guard() -> str:
    verdict = guard.judge(guard.read_signals())
    if verdict.allowed:
        return "Memory guard: local models may run"
    return f"Memory guard: local turns are paused because {verdict.reason}"


def _last_decision() -> str | None:
    cfg, _ = doctor._router().load_config(hermes_home())
    if not cfg.save_traces:
        return None
    try:
        with open(paths.state_dir() / "router-traces.jsonl", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))  # the last record is near the end of a file that only grows
            trace = json.loads(f.read().splitlines()[-1])
    except (OSError, IndexError, ValueError):
        trace = None
    if not isinstance(trace, dict):
        return "Last router decision: none saved yet"
    text = f"Last router decision: {trace.get('tier')} tier ({trace.get('reason')}), {trace.get('slot')}"
    if trace.get("retried"):
        text += ", retried"
    if trace.get("fell_back_from"):
        text += f", fell back from {trace['fell_back_from']}"
    return text


def handle(args: str) -> str:
    lines = [_freellmapi(), _guard()]
    try:
        last = _last_decision()
    except ImportError:  # the router plugin is not installed
        last = None
    return "\n".join(lines + ([last] if last else []))
