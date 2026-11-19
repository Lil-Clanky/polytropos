"""Spike provider: forwards every call to the OpenAI-compatible upstream named in
``plugins.entries.polytropos-spike.upstream`` and appends one JSON line per event to
``<HERMES_HOME>/spike.jsonl``, so the spike can see how Hermes drives a provider client."""

import json
import os
import threading
import time

import openai
from providers import register_provider
from providers.base import ProviderProfile

try:
    from ._shared import MARKER as RELATIVE_IMPORT
except ImportError as exc:  # recorded, not raised: the spike reports whether the plugin loads as a package
    RELATIVE_IMPORT = f"failed: {exc!r}"


def log(record: dict) -> None:
    from hermes_constants import get_hermes_home
    record.update(src="provider", t=round(time.time(), 3), pid=os.getpid(), thread=threading.get_ident(),
                  thread_name=threading.current_thread().name)
    with open(get_hermes_home() / "spike.jsonl", "a") as f:
        f.write(json.dumps(record, default=repr) + "\n")


def _shown(key: str, value):
    if key == "api_key":  # fake sandbox keys start with "spike-"; anything else shows its length only
        return value if str(value or "").startswith("spike-") else f"<{len(str(value or ''))} chars>"
    return value if isinstance(value, (str, int, float, bool, type(None))) else type(value).__name__


class _Probe:
    """Attribute holder that records each attribute Hermes asks for and it lacks."""

    def __init__(self, label: str, **attrs):
        self.__dict__.update(attrs, _label=label)

    def __getattr__(self, name):
        log({"event": "missing_attr", "on": self._label, "name": name})
        raise AttributeError(name)


class _Recorder:
    """Transparent proxy over a completion or stream that records the attributes Hermes reads."""

    def __init__(self, target, label: str):
        self.__dict__.update(_target=target, _label=label, _seen=set())

    @property
    def __class__(self):  # isinstance() checks see the wrapped type
        return type(self._target)

    def _note(self, name: str, found: bool = True) -> None:
        if name not in self._seen:
            self._seen.add(name)
            log({"event": "result_attr" if found else "missing_attr", "on": self._label, "name": name})

    def __getattr__(self, name):
        try:
            value = getattr(self._target, name)
        except AttributeError:
            self._note(name, found=False)
            raise
        self._note(name)
        return value

    def __iter__(self):
        self._note("__iter__")
        return iter(self._target)

    def __next__(self):
        self._note("__next__")
        return next(self._target)

    def __enter__(self):
        self._note("__enter__")
        return self

    def __exit__(self, *exc):
        self._note("__exit__")
        return self._target.__exit__(*exc)


class SpikeClient:
    """Facade in the shape of ``agent.acp_client.ACPClient`` over a real OpenAI client."""

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, *, upstream: str, api_key=None, base_url=None, **_):
        self.api_key, self.base_url, self.is_closed = api_key, base_url or "polytropos://router", False
        self._upstream = openai.OpenAI(base_url=upstream, api_key=api_key or "none", max_retries=0)
        self.chat = _Probe("chat", completions=_Probe("chat.completions", create=self._create))

    def __getattr__(self, name):
        log({"event": "missing_attr", "on": "client", "name": name})
        raise AttributeError(name)

    def close(self) -> None:
        log({"event": "close"})
        self.is_closed = True
        self._upstream.close()

    def _create(self, **kwargs):
        messages = kwargs.get("messages") or []
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        log({"event": "create", "model": kwargs.get("model"), "stream": kwargs.get("stream"),
             "kwargs": sorted(kwargs), "timeout": repr(kwargs.get("timeout")),
             "roles": [m.get("role") + ("+tool_calls" if m.get("tool_calls") else "") for m in messages],
             "tools": [t.get("function", {}).get("name") for t in kwargs.get("tools") or []],
             "last_user_content": last_user.get("content")})
        # Main-loop calls carry tools; side calls (titles, summaries) get their own script queue.
        target = "main" if kwargs.get("tools") else "side"
        result = self._upstream.chat.completions.create(**{**kwargs, "model": target})
        return _Recorder(result, "stream" if kwargs.get("stream") else "completion")


class SpikeProfile(ProviderProfile):
    def create_client(self, **client_kwargs):
        from hermes_cli.config import load_config  # lazy: provider discovery runs while hermes_cli.config imports
        entry = ((load_config().get("plugins") or {}).get("entries") or {}).get("polytropos-spike") or {}
        log({"event": "create_client", "client_kwargs": {k: _shown(k, v) for k, v in client_kwargs.items()},
             "upstream": entry.get("upstream"), "relative_import": RELATIVE_IMPORT,
             "key_in_os_environ": "POLYTROPOS_SPIKE_KEY" in os.environ,
             "other_key_in_os_environ": "POLYTROPOS_SPIKE_OTHER" in os.environ})
        if not entry.get("upstream"):
            raise RuntimeError("plugins.entries.polytropos-spike.upstream is not set")
        return SpikeClient(upstream=entry["upstream"], **client_kwargs)

    def get_model_context_length(self, model: str) -> int:
        log({"event": "get_model_context_length", "model": model})
        return 128_000


register_provider(SpikeProfile(
    name="polytropos-spike", display_name="Polytropos spike",
    description="Records how Hermes drives a provider client", base_url="polytropos://router",
    env_vars=("POLYTROPOS_SPIKE_KEY",), fallback_models=("auto",), supports_streaming=True,
    supports_health_check=False, supports_model_listing=False))
log({"event": "import", "module": __name__, "file": __file__, "relative_import": RELATIVE_IMPORT})
