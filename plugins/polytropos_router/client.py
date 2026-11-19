"""The client the router provider hands to Hermes: one tier per turn, then the first usable model."""

import contextlib
import functools
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import openai

from . import trace
from ._shared import canary, guard
from ._shared.paths import state_dir
from .classifier import classify
from .config import RouterConfig, Slot, Upstream, window
from .decisions import Decision, DecisionCache, decide
from .streams import GuardedStream
from .tokens import estimate_tokens
from .toolcheck import broken_tool_calls

DECISIONS = DecisionCache()  # shared by every client: Hermes builds several per session
WATCHDOG_INTERVAL_S = 2.0
logger = logging.getLogger("hermes_plugins.polytropos_router")


class RouterError(RuntimeError):
    pass


class _Refused(Exception):
    """The guard refused a local slot right before its call."""


@dataclass
class _Call:
    """One create() call, as the trace records it."""
    decision: Decision
    kwargs: dict
    slot: Slot | None = None
    retried: bool = False
    fell_back_from: list[str] = field(default_factory=list)


def _label(slot: Slot) -> str:
    return f"{slot.upstream}/{slot.model}"


def _falls_back(error: Exception) -> bool:
    # APITimeoutError is an APIConnectionError
    return (isinstance(error, (openai.RateLimitError, openai.APIConnectionError))
            or isinstance(error, openai.APIStatusError) and error.status_code >= 500)


def _tool_calls(result: Any) -> list:
    return (result.choices[0].message.tool_calls or []) if result.choices else []


def _ollama_version(up: Upstream) -> str | None:
    if up.runtime != "ollama":
        return None
    try:
        return guard._call(f"{guard.ollama_root(up.base_url)}/api/version", None, 2.0).get("version")
    except guard._LOOKUP_ERRORS:
        return None


def _ollama_fit(up: Upstream, model: str) -> bool | None:
    if up.runtime != "ollama":
        return None
    return guard.ollama_fit(up.base_url, model, up.context_window, total_ram=guard.total_ram_bytes())


@functools.cache  # once per process
def _warn_abort_lost() -> None:
    logger.warning("polytropos-router: cannot abort upstream requests: the httpx internals it uses are missing")


def _abort(client: Any) -> None:
    """Abort the client's requests in flight: their calls raise APIConnectionError."""
    # shutdown(), not close(): close() leaves a read that blocks on another thread hanging, or waiting on
    # a recycled fd (both seen on macOS). A dropped client closes itself once no request holds it any
    # more (openai closes its client on garbage collection), like Hermes's retired shared clients.
    # shortcut: reads private httpx attributes and only warns if they move; replace when httpx
    # gets a public way to abort a request
    try:
        streams = [c._connection._network_stream for c in list(client._client._transport._pool._connections)
                   if c._connection is not None]  # None until it connects
    except AttributeError:
        _warn_abort_lost()
        return
    for stream in streams:
        with contextlib.suppress(AttributeError, OSError):  # no socket, or already closed
            stream.get_extra_info("socket").shutdown(socket.SHUT_RDWR)


class RouterClient:
    """OpenAI-shaped client. Upstream clients are made on first use, one per upstream."""

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True
    base_url = "polytropos://router"

    def __init__(self, cfg: RouterConfig, *, api_key: str | None = None,
                 openai_factory: Callable[..., Any] = openai.OpenAI,
                 guard_read: Callable[[], guard.Signals] = guard.read_signals,
                 matrix_loader: Callable[[], canary.Matrix] = canary.load_matrix,
                 local_results: Callable[[], dict[str, str]] = canary.load_local_results,
                 runtime_version: Callable[[Upstream], str | None] = _ollama_version,
                 fit_check: Callable[[Upstream, str], bool | None] = _ollama_fit,
                 env_lookup: Callable[[str], str | None] = os.environ.get,
                 trace_path: Path | None = None):
        # No I/O before create: Hermes builds this client about four times per turn.
        self.cfg, self.api_key = cfg, api_key
        self._factory, self._guard_read, self._env_lookup = openai_factory, guard_read, env_lookup
        self._matrix_loader, self._local_results = matrix_loader, local_results
        self._runtime_version, self._fit_check, self._trace_path = runtime_version, fit_check, trace_path
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.is_closed = False

    def close(self) -> None:
        with self._lock:
            clients, self._clients = list(self._clients.values()), {}
            self.is_closed = True
        for client in clients:
            _abort(client)  # each closes itself once no request holds it

    def _client(self, name: str) -> Any:
        with self._lock:
            if self.is_closed:
                raise RouterError("polytropos-router: the client is closed")
            if name not in self._clients:
                up = self.cfg.upstreams[name]
                if up.api_key_env == "FREELLMAPI_API_KEY" and self.api_key:
                    key = self.api_key
                else:
                    key = (up.api_key_env and self._env_lookup(up.api_key_env)) or "none"
                self._clients[name] = self._factory(base_url=up.base_url, api_key=key, max_retries=0)
            return self._clients[name]

    def _create(self, **kwargs: Any) -> Any:
        call = _Call(self._decision(kwargs.get("messages") or []), kwargs)
        slots = self._candidates(call)
        i, result, watchdog = self._first(call, slots)
        slot, rest = slots[i], slots[i + 1:]
        if kwargs.get("stream"):
            return self._stream(call, slot, result, watchdog, rest)
        if rest and broken_tool_calls(_tool_calls(result), kwargs.get("tools")):
            call.retried = True
            call.fell_back_from.append(_label(slot))
            try:
                j, second, _ = self._first(call, rest)
                slot, result = rest[j], second
            except (openai.OpenAIError, RouterError):
                pass  # no other model answered: keep the first answer
        call.slot = call.decision.pinned = slot
        self._trace(call)
        return result

    def _stream(self, call: _Call, slot: Slot, result: Any, watchdog: contextlib.ExitStack,
                rest: list[Slot]) -> GuardedStream:
        def retry():
            call.retried = True
            call.fell_back_from.append(_label(call.slot))
            i, stream, retry_watchdog = self._first(call, rest)
            call.slot = call.decision.pinned = rest[i]
            # its own close stops its watchdog, also when the outer stream was closed during the retry
            return GuardedStream(stream, tools=None, retry=None, on_close=retry_watchdog.close)

        def on_close():
            watchdog.close()
            self._trace(call)

        call.slot = call.decision.pinned = slot
        return GuardedStream(result, tools=call.kwargs.get("tools"), retry=retry if rest else None,
                             on_close=on_close)

    def _decision(self, messages: list) -> Decision:
        key = DECISIONS.key_for(messages)
        decision = DECISIONS.get(key) if key else None
        if decision is None:
            decision = decide(self.cfg, messages, classify_fn=self._classify)
            if key:
                DECISIONS.put(key, decision)
        return decision

    def _classify(self, text: str) -> str | None:
        slot = self.cfg.classifier
        if slot is None or not self._usable(slot, 0):  # a local classifier passes the guard and fit checks too
            return None
        create = lambda **kw: self._start(kw, slot)[0]  # noqa: E731  (a local one also runs under the watchdog)
        return classify(create, slot, text, max_tokens=self.cfg.classifier_max_tokens,
                        timeout_s=self.cfg.classifier_timeout_s)

    def _candidates(self, call: _Call) -> list[Slot]:
        cfg, decision, tools = self.cfg, call.decision, call.kwargs.get("tools")
        chain = cfg.routes.get(decision.tier) or cfg.routes[cfg.default_route]
        if decision.tier == "simple":
            chain += cfg.routes.get("complex", ())
        need = estimate_tokens(call.kwargs.get("messages") or [], tools) + (call.kwargs.get("max_tokens") or 1024)
        hard = [s for s in dict.fromkeys(chain) if self._usable(s, need)]
        matrix, results = functools.cache(self._matrix_loader), functools.cache(self._local_results)
        slots = [s for s in hard if not tools or self._canary_ok(s, matrix, results)] or hard
        if decision.pinned in slots:  # only among the slots that pass, so a demoted one stays last
            slots.remove(decision.pinned)
            slots.insert(0, decision.pinned)
        return slots + [s for s in hard if s not in slots]  # a failing canary demotes a slot to the last resort

    def _usable(self, slot: Slot, need: int) -> bool:
        """The hard filters, for local slots: memory guard, window and fit."""
        up = self.cfg.upstreams[slot.upstream]
        return not up.local or (need <= window(self.cfg, slot) and guard.judge(self._guard_read()).allowed
                                and self._fit_check(up, slot.model) is not False)

    def _canary_ok(self, slot: Slot, matrix: Callable[[], canary.Matrix],
                   results: Callable[[], dict[str, str]]) -> bool:
        up = self.cfg.upstreams[slot.upstream]
        if not up.local:
            return results().get(_label(slot)) != "fail"
        version = self._runtime_version(up)
        return version is None or matrix().verdict(version, slot.model) != "fail"

    def _first(self, call: _Call, slots: list[Slot]) -> tuple[int, Any, contextlib.ExitStack | None]:
        """Start the request on the first slot that takes it, or raise the last error."""
        last: Exception | None = None
        for i, slot in enumerate(slots):
            try:
                return (i, *self._start(call.kwargs, slot))
            except _Refused:
                continue
            except Exception as error:
                if self.is_closed or not _falls_back(error):
                    raise
                last = error
                call.fell_back_from.append(_label(slot))
        raise last or RouterError("polytropos-router: no model in the route can take this request")

    def _start(self, kwargs: dict, slot: Slot) -> tuple[Any, contextlib.ExitStack | None]:
        """Send the request to one slot. A stream gets back the stack that stops its watchdog."""
        up = self.cfg.upstreams[slot.upstream]
        if up.local and not guard.judge(self._guard_read()).allowed:  # fresh signals right before a local call
            raise _Refused
        client = self._client(slot.upstream)
        with contextlib.ExitStack() as stack:
            if up.local:
                stack.enter_context(guard.Watchdog(interval_s=WATCHDOG_INTERVAL_S, read=self._guard_read,
                                                   on_trip=lambda reason: self._trip(slot, client)))
            result = client.chat.completions.create(**{**kwargs, "model": slot.model})
            return result, (stack.pop_all() if kwargs.get("stream") else None)

    def _trip(self, slot: Slot, client: Any) -> None:
        with self._lock:
            if self._clients.get(slot.upstream) is client:
                del self._clients[slot.upstream]
        _abort(client)
        up = self.cfg.upstreams[slot.upstream]
        if up.runtime == "ollama":
            guard.ollama_unload(up.base_url, slot.model)

    def _trace(self, call: _Call) -> None:
        if not self.cfg.save_traces:
            return
        record = {"time": time.time(), "tier": call.decision.tier, "reason": call.decision.reason,
                  "slot": _label(call.slot), "retried": call.retried, "fell_back_from": call.fell_back_from}
        with contextlib.suppress(OSError):  # a trace that cannot be written must not fail the turn
            trace.write_trace(self._trace_path or state_dir() / "router-traces.jsonl", record)
