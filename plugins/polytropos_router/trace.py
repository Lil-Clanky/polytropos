"""Routing traces: one JSON object per line, in a file only the user can read."""

import contextlib
import json
import os
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024


def write_trace(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))  # keep one older file
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
