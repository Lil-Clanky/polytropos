import threading
from typing import Callable, Iterable

from .toolcheck import broken_tool_calls, _get


def _delta(chunk):
    choices = _get(chunk, "choices") or []
    return _get(choices[0], "delta") if choices else None


class GuardedStream:
    """Pass text through, hold tool calls until the end, and retry once when they are broken."""

    def __init__(self, upstream: Iterable, *, tools: list[dict] | None,
                 retry: Callable[[], Iterable] | None, on_close: Callable[[], None] | None = None):
        self._upstream = upstream
        self._tools = tools
        self._retry = retry
        self._on_close = on_close
        self._closed = threading.Lock()
        self._done = False
        self.retried = False

    def close(self) -> None:
        with self._closed:
            if self._done:
                return
            self._done = True
        try:
            if hasattr(self._upstream, "close"):
                self._upstream.close()
        finally:
            if self._on_close:
                self._on_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        try:
            yield from self._run()
        finally:
            self.close()

    def _retry_chunks(self):
        self.retried = True
        inner = GuardedStream(self._retry(), tools=self._tools, retry=None)
        with inner:
            yield from inner

    def _run(self):
        held, calls, text_sent = [], {}, False
        try:
            for chunk in self._upstream:
                delta = _delta(chunk)
                parts = _get(delta, "tool_calls") if delta is not None else None
                if parts:
                    for part in parts:
                        slot = calls.setdefault(_get(part, "index") or 0, {"function": {"name": "", "arguments": ""}})
                        fn = _get(part, "function")
                        slot["function"]["name"] += _get(fn, "name") or ""
                        slot["function"]["arguments"] += _get(fn, "arguments") or ""
                if calls:
                    held.append(chunk)
                    continue
                if delta is not None and _get(delta, "content"):
                    text_sent = True
                yield chunk
        except Exception:
            if text_sent or self._retry is None:
                raise
            yield from self._retry_chunks()
            return
        if (not text_sent and self._retry is not None and calls
                and broken_tool_calls([calls[i] for i in sorted(calls)], self._tools)):
            yield from self._retry_chunks()
            return
        yield from held
