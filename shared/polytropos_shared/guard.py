"""Memory guard: host signals, a verdict, Ollama helpers and a watchdog."""

import functools
import http.client
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GB = 1024**3
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Signals:
    pressure: str  # "normal" | "warning" | "critical" | "unknown"
    swap_bytes: int | None
    free_disk_bytes: int | None


@dataclass(frozen=True)
class Limits:
    max_swap_bytes: int = 4 * 1024**3
    min_free_disk_bytes: int = 5 * 1024**3
    # Untuned starting value for Linux PSI.
    psi_some_avg10: float = 10.0


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""


def _read_file(path: str) -> str:
    return Path(path).read_text()


def _sysctl(run, key: str) -> str | None:
    try:
        done = run(["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True, timeout=2, check=True)
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _mac_pressure(run) -> str:
    try:
        level = int(_sysctl(run, "kern.memorystatus_vm_pressure_level") or "")
    except ValueError:
        return "unknown"
    # same threshold as stack-canary: 2 and 3 are warning, 4 and above critical
    return "normal" if level <= 1 else "warning" if level < 4 else "critical"


def _mac_swap(run) -> int | None:
    match = re.search(r"\bused\s*=\s*(\d+(?:\.\d+)?)\s*([BKMGT])", _sysctl(run, "vm.swapusage") or "")
    return int(float(match[1]) * 1024 ** "BKMGT".index(match[2])) if match else None


def _linux_pressure(read_text, limits: Limits) -> str:
    try:
        text = read_text("/proc/pressure/memory")
    except OSError:
        return "unknown"
    avg = {m[1]: float(m[2]) for m in re.finditer(r"^(some|full) avg10=([\d.]+)", text, re.M)}
    if not avg:
        return "unknown"
    if avg.get("full", 0.0) >= limits.psi_some_avg10:
        return "critical"
    return "warning" if avg.get("some", 0.0) >= limits.psi_some_avg10 else "normal"


def _meminfo_kb(read_text, key: str) -> int | None:
    try:
        match = re.search(rf"^{key}:\s+(\d+)\s*kB", read_text("/proc/meminfo"), re.M)
    except OSError:
        return None
    return int(match[1]) if match else None


def _linux_swap(read_text) -> int | None:
    total, free = _meminfo_kb(read_text, "SwapTotal"), _meminfo_kb(read_text, "SwapFree")
    return (total - free) * 1024 if total is not None and free is not None else None


def _free_disk(disk_path: Path | None) -> int | None:
    if disk_path is None:
        models = Path.home() / ".ollama" / "models"
        disk_path = models if models.exists() else Path.home()
    try:
        return shutil.disk_usage(disk_path).free
    except OSError:
        return None


def read_signals(*, platform: str = sys.platform, run=subprocess.run, read_text=_read_file,
                 disk_path: Path | None = None, limits: Limits = Limits()) -> Signals:
    if platform == "darwin":
        pressure, swap = _mac_pressure(run), _mac_swap(run)
    elif platform.startswith("linux"):
        pressure, swap = _linux_pressure(read_text, limits), _linux_swap(read_text)
    else:
        pressure, swap = "unknown", None
    return Signals(pressure, swap, _free_disk(disk_path))


def _gb(value: int) -> str:
    return f"{value / GB:.1f} GB"


def _problem(signals: Signals, limits: Limits, pressure_bad: tuple[str, ...]) -> str:
    if signals.pressure in pressure_bad:
        return f"memory pressure is {signals.pressure}"
    if signals.swap_bytes is not None and signals.swap_bytes > limits.max_swap_bytes:
        return f"swap use is {_gb(signals.swap_bytes)}"
    if signals.free_disk_bytes is not None and signals.free_disk_bytes < limits.min_free_disk_bytes:
        return f"free disk is {_gb(signals.free_disk_bytes)}"
    return ""


def judge(signals: Signals, limits: Limits = Limits()) -> Verdict:
    reason = _problem(signals, limits, ("warning", "critical"))
    return Verdict(not reason, reason)


def ollama_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _call(url: str, body: dict | None, timeout: float):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


_LOOKUP_ERRORS = (OSError, ValueError, AttributeError, KeyError, TypeError, http.client.HTTPException)


def _models(payload: dict) -> list[dict]:
    return [m for m in payload.get("models") or [] if isinstance(m, dict)]


def _size(model: dict) -> int | None:
    size = model.get("size")
    return size if isinstance(size, int) and not isinstance(size, bool) else None


def ollama_loaded(base_url: str, *, timeout: float = 2.0) -> list[dict]:
    try:
        return _models(_call(f"{ollama_root(base_url)}/api/ps", None, timeout))
    except _LOOKUP_ERRORS:
        return []


def ollama_unload(base_url: str, model: str, *, timeout: float = 5.0) -> bool:
    try:
        _call(f"{ollama_root(base_url)}/api/generate", {"model": model, "keep_alive": 0}, timeout)
    except _LOOKUP_ERRORS:
        return False
    return True


def kv_cache_bytes(model_info: dict, context: int) -> int | None:
    def find(suffix: str):
        return next((v for k, v in model_info.items() if k.endswith(suffix) and isinstance(v, int) and v > 0), None)

    layers, kv_heads = find(".block_count"), find(".attention.head_count_kv")
    embedding, heads = find(".embedding_length"), find(".attention.head_count")
    if None in (layers, kv_heads, embedding, heads):
        return None
    return 2 * layers * kv_heads * (embedding // heads) * context * 2


def fits(model_bytes: int, kv_bytes: int, total_ram_bytes: int, loaded_bytes: int, reserve_bytes: int = 4 * 1024**3) -> bool:
    # shortcut: rough estimate, the watchdog is the safety net; replace when coffee-vram or measurements say so
    return model_bytes + kv_bytes + loaded_bytes + reserve_bytes <= total_ram_bytes


def total_ram_bytes(*, platform: str = sys.platform, run=subprocess.run, read_text=_read_file) -> int | None:
    if platform == "darwin":
        try:
            return int(_sysctl(run, "hw.memsize") or "")
        except ValueError:
            return None
    kb = _meminfo_kb(read_text, "MemTotal")
    return kb * 1024 if kb is not None else None


def _tagged(name) -> str | None:
    """Ollama reads a name without a tag as <name>:latest."""
    if not isinstance(name, str):
        return None
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _listed(model: str, models: list[dict]) -> dict | None:
    return next((m for m in models if _tagged(model) in (_tagged(m.get("name")), _tagged(m.get("model")))), None)


@functools.cache  # once per process and model, the fit check runs on every request
def _warn_not_pulled(model: str) -> None:
    logger.warning("polytropos: the Ollama model %s is not pulled; the fit check refuses it", model)


def ollama_fit(base_url: str, model: str, context: int, *, total_ram: int | None, timeout: float = 2.0) -> bool | None:
    if total_ram is None:
        return None
    root = ollama_root(base_url)
    try:
        loaded = _models(_call(f"{root}/api/ps", None, timeout))
        if _listed(model, loaded):
            return True
        listed = _listed(model, _models(_call(f"{root}/api/tags", None, timeout)))
        if listed is None:
            _warn_not_pulled(model)
            return False
        size = _size(listed)
        info = _call(f"{root}/api/show", {"model": model}, timeout).get("model_info", {})
        kv = kv_cache_bytes(info, context)
        if size is None or kv is None:
            return None
        return fits(size, kv, total_ram, sum(_size(m) or 0 for m in loaded))
    except _LOOKUP_ERRORS:
        return None


class Watchdog:
    """Polls signals on a daemon thread and calls on_trip(reason) once on critical pressure or swap over the limit."""

    def __init__(self, limits: Limits = Limits(), *, interval_s: float = 2.0,
                 read: Callable[[], Signals] = read_signals, on_trip: Callable[[str], None]):
        self.limits, self.interval_s, self._read, self._on_trip = limits, interval_s, read, on_trip
        self.tripped: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            reason = _problem(self._read(), self.limits, ("critical",))
            if reason.startswith(("memory", "swap")):
                self.tripped = reason
                self._on_trip(reason)
                return

    def __enter__(self) -> "Watchdog":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
