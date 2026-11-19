"""Canary verdicts: which model and runtime combinations passed the tool-call checks."""

import json
import os
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .paths import cache_dir, data_dir

MATRIX_URL = "https://raw.githubusercontent.com/Lil-Clanky/stack-canary/matrix/matrix.json"
TOOL_CHECKS = ("A1", "A2", "A3", "A4", "A5", "A6")


@dataclass(frozen=True)
class Cell:
    runtime_version: str
    model: str
    check: str
    status: str  # pass | fail | error | n/a


class Matrix:
    def __init__(self, cells: Iterable[Cell]):
        self._status = {(c.runtime_version, c.model, c.check): c.status for c in cells}

    def verdict(self, runtime_version: str, model: str, checks: Iterable[str] = TOOL_CHECKS) -> str:
        found = [self._status.get((runtime_version, model, c)) for c in checks]
        if "fail" in found:
            return "fail"
        return "pass" if found and all(s == "pass" for s in found) else "unknown"

    def passing_versions(self, model: str) -> list[str]:
        versions = {v for v, m, _ in self._status if m == model}
        good = [v for v in versions if self.verdict(v, model) == "pass"]
        # shortcut: non-numeric version parts (such as "0.41.0-rc1") sort as 0, fine while the matrix holds release versions
        return sorted(good, key=lambda v: [int(p) if p.isdigit() else 0 for p in v.split(".")], reverse=True)


def parse_matrix(data: dict) -> Matrix:
    if not isinstance(data, dict):
        raise ValueError("matrix is not a JSON object")
    if data.get("schema") != 1:
        raise ValueError(f"unsupported matrix schema: {data.get('schema')!r}")
    return Matrix(Cell(c["ollama"], c["model"], c["check"], c["status"]) for c in data.get("cells", []))


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def load_matrix(*, cache_path: Path | None = None, fetch: Callable[[str], bytes] | None = None,
                now: float | None = None, max_age_s: int = 86400) -> Matrix:
    cache_path = cache_path or cache_dir() / "canary-matrix.json"
    fetch = fetch or _fetch
    now = time.time() if now is None else now
    cached = None
    try:
        cached = parse_matrix(json.loads(cache_path.read_text()))
        if now - cache_path.stat().st_mtime < max_age_s:
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        raw = fetch(MATRIX_URL)
        matrix = parse_matrix(json.loads(raw))
        _write_atomic(cache_path, raw.decode())
        return matrix
    except (OSError, ValueError, KeyError, TypeError):
        return cached or Matrix([])


def local_results_path() -> Path:
    return data_dir() / "canary" / "openai-results.json"


def load_local_results() -> dict[str, str]:
    try:
        data = json.loads(local_results_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_local_results(results: dict[str, str]) -> None:
    _write_atomic(local_results_path(), json.dumps(results, indent=2, sort_keys=True))
