import json
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from polytropos_shared import guard
from polytropos_shared.guard import Limits, Signals, Watchdog

GB = 1024**3


def fake_run(outputs: dict):
    def run(cmd, **kwargs):
        value = outputs[cmd[-1]]
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(cmd, 0, stdout=value, stderr="")

    return run


def mac_signals(pressure="1", swap="total = 2048.00M  used = 1536.25M  free = 511.75M  (encrypted)", tmp=None):
    run = fake_run({"kern.memorystatus_vm_pressure_level": pressure, "vm.swapusage": swap})
    return guard.read_signals(platform="darwin", run=run, disk_path=tmp or Path("."))


def test_macos_pressure_levels():
    assert [mac_signals(p).pressure for p in ("1", "2", "4")] == ["normal", "warning", "critical"]


def test_macos_swap_parse():
    assert mac_signals().swap_bytes == int(1536.25 * 1024**2)


def test_linux_psi_and_swap():
    files = {
        "/proc/pressure/memory": "some avg10=12.00 avg60=1.00 avg300=0.00 total=1\nfull avg10=1.00 avg60=0.00 avg300=0.00 total=1\n",
        "/proc/meminfo": "SwapTotal:       8000000 kB\nSwapFree:        7000000 kB\n",
    }
    signals = guard.read_signals(platform="linux", read_text=files.__getitem__, disk_path=Path("."))
    assert signals.pressure == "warning"
    assert signals.swap_bytes == 1000000 * 1024
    files["/proc/pressure/memory"] = "some avg10=12.00 avg60=1 avg300=0 total=1\nfull avg10=11.00 avg60=0 avg300=0 total=1\n"
    assert guard.read_signals(platform="linux", read_text=files.__getitem__, disk_path=Path(".")).pressure == "critical"
    files["/proc/pressure/memory"] = "some avg10=1.00 avg60=1 avg300=0 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=1\n"
    assert guard.read_signals(platform="linux", read_text=files.__getitem__, disk_path=Path(".")).pressure == "normal"


def test_unreadable_is_unknown(tmp_path):
    boom = fake_run({"kern.memorystatus_vm_pressure_level": OSError("x"), "vm.swapusage": OSError("x")})
    signals = guard.read_signals(platform="darwin", run=boom, disk_path=tmp_path)
    assert signals.pressure == "unknown" and signals.swap_bytes is None
    assert signals.free_disk_bytes is not None
    missing = guard.read_signals(platform="linux", read_text=lambda p: (_ for _ in ()).throw(OSError()), disk_path=tmp_path)
    assert missing.pressure == "unknown" and missing.swap_bytes is None
    gone = guard.read_signals(platform="linux", read_text=lambda p: "", disk_path=tmp_path / "nope")
    assert gone.free_disk_bytes is None


def test_judge_rules():
    ok = Signals("normal", 0, 50 * GB)
    assert guard.judge(ok).allowed
    assert "memory pressure" in guard.judge(Signals("warning", 0, 50 * GB)).reason
    assert guard.judge(Signals("critical", 0, 50 * GB)).allowed is False
    assert "swap use is 5.0 GB" in guard.judge(Signals("normal", 5 * GB, 50 * GB)).reason
    assert "free disk is 4.0 GB" in guard.judge(Signals("normal", 0, 4 * GB)).reason
    assert guard.judge(Signals("unknown", None, None)).allowed


def test_kv_cache_llama32_3b():
    info = {
        "llama.block_count": 28,
        "llama.attention.head_count_kv": 8,
        "llama.embedding_length": 3072,
        "llama.attention.head_count": 24,
    }
    assert guard.kv_cache_bytes(info, 8192) == 2 * 28 * 8 * 128 * 8192 * 2
    assert guard.kv_cache_bytes({"llama.block_count": 28}, 8192) is None


def test_fits():
    assert guard.fits(2 * 10**9, 10**9, 16 * GB, 0) is True
    assert guard.fits(2 * 10**9, 10**9, 16 * GB, 10 * GB) is False


def test_watchdog_trips_once():
    reads = iter([Signals("normal", 0, None), Signals("critical", 0, None), Signals("critical", 0, None)])
    last = Signals("critical", 0, None)
    tripped = []
    with Watchdog(interval_s=0.01, read=lambda: next(reads, last), on_trip=tripped.append) as dog:
        for _ in range(200):
            if dog.tripped:
                break
            threading.Event().wait(0.01)
        threading.Event().wait(0.05)
    assert len(tripped) == 1 and "critical" in tripped[0] and dog.tripped == tripped[0]


def test_watchdog_trips_on_swap():
    tripped = []
    with Watchdog(Limits(max_swap_bytes=GB), interval_s=0.01, read=lambda: Signals("normal", 2 * GB, None), on_trip=tripped.append):
        for _ in range(200):
            if tripped:
                break
            threading.Event().wait(0.01)
    assert tripped and "swap" in tripped[0]


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # skip the reverse DNS lookup that HTTPServer.server_bind does
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Fake:
    """Loopback Ollama stand-in. routes maps (method, path) -> (status, body)."""

    def __init__(self, routes):
        self.routes, self.posts = routes, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                if method == "POST":
                    fake.posts.append((self.path, body))
                status, payload = fake.routes.get((method, self.path), (404, {}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply("GET")

            def do_POST(self):
                self._reply("POST")

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_ollama_helpers_against_fake():
    routes = {("GET", "/api/ps"): (200, {"models": [{"name": "m", "size": 5}]}), ("POST", "/api/generate"): (200, {})}
    with Fake(routes) as fake:
        assert guard.ollama_root(fake.url + "/v1") == fake.url
        assert guard.ollama_root(fake.url) == fake.url
        assert guard.ollama_loaded(fake.url + "/v1") == [{"name": "m", "size": 5}]
        assert guard.ollama_unload(fake.url + "/v1", "m") is True
        assert fake.posts == [("/api/generate", {"model": "m", "keep_alive": 0})]
    assert guard.ollama_loaded(fake.url) == []
    assert guard.ollama_unload(fake.url, "m") is False


def test_total_ram_macos_linux():
    assert guard.total_ram_bytes(platform="darwin", run=fake_run({"hw.memsize": "17179869184\n"})) == 17179869184
    meminfo = lambda p: "MemTotal:       16000000 kB\nMemFree: 1 kB\n"  # noqa: E731
    assert guard.total_ram_bytes(platform="linux", read_text=meminfo) == 16000000 * 1024
    assert guard.total_ram_bytes(platform="darwin", run=fake_run({"hw.memsize": OSError()})) is None
    assert guard.total_ram_bytes(platform="linux", read_text=lambda p: (_ for _ in ()).throw(OSError())) is None


def test_ollama_fit_against_fake():
    info = {"llama.block_count": 28, "llama.attention.head_count_kv": 8, "llama.embedding_length": 3072, "llama.attention.head_count": 24}
    routes = {
        ("GET", "/api/ps"): (200, {"models": [{"name": "loaded:1b", "size": 10**9}]}),
        ("GET", "/api/tags"): (200, {"models": [{"name": "small:3b", "size": 2 * 10**9}, {"model": "big:70b", "size": 14 * GB}]}),
        ("POST", "/api/show"): (200, {"model_info": info}),
    }
    with Fake(routes) as fake:
        ram = 16 * GB
        assert guard.ollama_fit(fake.url, "loaded:1b", 8192, total_ram=ram) is True
        assert guard.ollama_fit(fake.url + "/v1", "small:3b", 8192, total_ram=ram) is True
        assert guard.ollama_fit(fake.url, "big:70b", 8192, total_ram=ram) is False
        assert guard.ollama_fit(fake.url, "small:3b", 8192, total_ram=None) is None
        assert guard.ollama_fit(fake.url, "unknown", 8192, total_ram=ram) is None
        routes[("POST", "/api/show")] = (500, {})
        assert guard.ollama_fit(fake.url, "small:3b", 8192, total_ram=ram) is None
