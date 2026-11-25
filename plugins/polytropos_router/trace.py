"""Routing traces: one JSON object per line, in a file only the user can read."""

import contextlib
import json
import os
import threading
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
# shortcut: guards the threads of one process; two Hermes processes can still move the file at once, use a file
# lock if that shows up
_lock = threading.Lock()


def write_trace(path: Path, record: dict) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.stat().st_size >= MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))  # keep one older file
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
