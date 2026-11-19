"""Routing traces: one JSON object per line, in a file only the user can read."""

import json
import os
from pathlib import Path


def write_trace(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
