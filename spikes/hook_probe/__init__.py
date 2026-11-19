"""Spike plugin: appends the thread and kwargs of each lifecycle hook around a turn to
``<HERMES_HOME>/spike.jsonl`` and returns a marker as ``pre_llm_call`` context."""

import json
import os
import threading
import time

CONTEXT = "HOOK-PROBE-CONTEXT"
HOOKS = ("pre_llm_call", "pre_api_request", "post_llm_call", "on_session_end")


def log(record: dict) -> None:
    from hermes_constants import get_hermes_home
    record.update(src="hook", t=round(time.time(), 3), pid=os.getpid(), thread=threading.get_ident(),
                  thread_name=threading.current_thread().name)
    with open(get_hermes_home() / "spike.jsonl", "a") as f:
        f.write(json.dumps(record, default=repr) + "\n")


def _short(value):
    if isinstance(value, str):
        return value[:80]
    return value if isinstance(value, (int, float, bool, type(None))) else type(value).__name__


def _probe(hook: str):
    def callback(**kwargs):
        log({"hook": hook, "kwargs": {k: _short(v) for k, v in sorted(kwargs.items())}})
        return {"context": CONTEXT} if hook == "pre_llm_call" else None
    return callback


def register(ctx) -> None:
    log({"event": "register", "plugin_id": getattr(ctx, "plugin_id", None), "get_config_probe": ctx.get_config("probe")})
    for hook in HOOKS:
        ctx.register_hook(hook, _probe(hook))
