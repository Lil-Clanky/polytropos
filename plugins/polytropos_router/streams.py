import threading
from typing import Callable, Iterable

from .toolcheck import broken_tool_calls, _get


_PAYLOAD = ("content", "reasoning_content", "reasoning", "reasoning_details")


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
                self._upstream.close()  # the active stream: the retry stream once a retry started
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
        if self._done:
            return
        self.retried = True
        inner = GuardedStream(self._retry(), tools=self._tools, retry=None)
        self._upstream = inner
        if self._done:  # closed while the retry request was being made
            inner.close()
            return
        yield from inner

    def _run(self):
        # A retry is allowed only while nothing has reached the caller. Chunks without payload
        # (the role delta) wait in `pending` and go out together with the first chunk that has any.
        pending, held, calls, released = [], [], {}, False
        try:
            for chunk in self._upstream:
                delta = _delta(chunk)
                parts = _get(delta, "tool_calls") if delta is not None else None
                for part in parts or []:
                    slot = calls.setdefault(_get(part, "index") or 0, {"function": {"name": "", "arguments": ""}})
                    fn = _get(part, "function")
                    slot["function"]["name"] += _get(fn, "name") or ""
                    slot["function"]["arguments"] += _get(fn, "arguments") or ""
                if calls:
                    held.append(chunk)
                elif delta is not None and any(_get(delta, k) for k in _PAYLOAD):
                    yield from pending
                    pending, released = [], True
                    yield chunk
                elif released:
                    yield chunk
                else:
                    pending.append(chunk)
        except Exception:
            if released or self._retry is None or self._done:
                raise
            yield from self._retry_chunks()
            return
        if (not released and self._retry is not None and calls and not self._done
                and broken_tool_calls([calls[i] for i in sorted(calls)], self._tools)):
            yield from self._retry_chunks()
            return
        yield from pending
        yield from held
