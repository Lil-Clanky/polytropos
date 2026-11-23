import builtins
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from polytropos import doctor as d
from polytropos._shared import canary as shared_canary
from polytropos.programs import load_lock
from polytropos.state import State, save_state, set_mode, use_shared

LOCK = load_lock(Path(__file__).resolve().parent.parent / "versions.lock")
SHA, SHA2 = "a" * 40, "b" * 40
GB = 1024**3
PACK = {"plugins": [
    {"repo": "Lil-Clanky/polytropos", "ref": SHA, "subdir": "plugins/polytropos"},
    {"repo": "Lil-Clanky/polytropos", "ref": SHA, "subdir": "plugins/polytropos-router"},
]}


class FakeRun:
    """A probe (`<name> --version`, or stack-canary's `canary run --help`) answers for versions[name], the name
    without its folder, and raises FileNotFoundError for any other name."""

    def __init__(self, ram=16 * GB, sleep=None, versions=None):
        self.ram, self.sleep, self.versions, self.calls = ram, sleep, versions or {}, []

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        out, probe, name = "", argv[1:] in (["--version"], ["run", "--help"]), Path(argv[0]).name
        if argv[1:] == ["-n", "hw.memsize"]:
            out = f"{self.ram}\n"
        elif argv[:2] == ["pmset", "-g"]:
            out = f" sleep                {self.sleep}\n displaysleep 10\n"
        elif probe and name in self.versions:
            out = f"{name} {self.versions[name]}\n"
        elif argv[0] == "launchctl":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        elif probe:
            raise FileNotFoundError(argv[0])
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def api(status=200):
    return lambda url, headers: (status, b'{"data": []}')


@pytest.fixture
def ctx(tmp_home):
    home = tmp_home / ".hermes"
    (home / "plugins").mkdir(parents=True)
    return d.DoctorContext(hermes_home=home, home=tmp_home, platform="darwin", lock=LOCK, pack=PACK,
                           run=FakeRun(), http_get=api(), hermes_version="0.23.2", machine="arm64")


def write_config(ctx, text):
    (ctx.hermes_home / "config.yaml").write_text(text)


def only(results):
    assert len(results) == 1, results
    return results[0]


def test_hermes_tested_newer_missing(ctx):
    assert d.check_hermes(ctx)[0].status == "ok"
    ctx.hermes_version = "0.24.0"
    r = only(d.check_hermes(ctx))
    assert r.status == "warn" and "newer" in r.label
    ctx.hermes_version = None
    assert only(d.check_hermes(ctx)).status == "fail"


def test_plugins_match_records_by_source(ctx):
    pack = {"plugins": PACK["plugins"] + [{"repo": "owner/thing", "ref": SHA}]}
    ctx.pack = pack
    meta = {"polytropos": {"source": "https://github.com/Lil-Clanky/polytropos#plugins/polytropos", "revision": SHA},
            "polytropos-router": {"source": "https://github.com/Lil-Clanky/polytropos.git#plugins/polytropos_router/",
                                  "revision": SHA2},
            "thing": {"source": "https://github.com/owner/thing.git", "revision": SHA}}
    ctx.pack["plugins"][1] = {"repo": "Lil-Clanky/polytropos", "ref": SHA, "subdir": "plugins/polytropos_router"}
    meta_path = ctx.hermes_home / "plugins" / ".install-metadata.json"
    meta_path.write_text(json.dumps(meta))
    ok, warn = d.check_plugins(ctx)
    assert ok.status == "ok" and ok.label == "plugins: polytropos, thing"
    assert warn.status == "warn" and "polytropos-router" in warn.label and "bbbbbbb" in warn.label
    meta_path.write_text("{}")
    got = d.check_plugins(ctx)
    assert [r.status for r in got] == ["fail"] * 3 and "not installed" in got[1].label


def test_ponytail_without_an_install_record_is_no_failure(ctx):
    pin = LOCK["plugins"]["ponytail"]
    ctx.pack = {"plugins": [{"repo": pin["repo"], "ref": pin["commit"], "name": "ponytail"}]}
    ctx.run = lambda argv, **kw: pytest.fail(f"unexpected command: {argv}")
    r = only(d.check_plugins(ctx))
    assert r.status == "warn" and r.label == "plugin DietrichGebert/ponytail is not installed"
    assert r.fix == "hermes polytropos install --only ponytail"
    (ctx.hermes_home / "plugins" / "ponytail" / ".git").mkdir(parents=True)  # as Polytropos 0.1.1 cloned it
    r = only(d.check_plugins(ctx))
    assert r.status == "info" and r.label == "plugin ponytail was installed without Hermes's installer"


def test_freellmapi_not_running(ctx):
    ctx.http_get = lambda url, headers: (0, b"")
    r = only(d.check_freellmapi(ctx))
    assert r.status == "fail" and "not running" in r.label and r.fix == "hermes polytropos install"


def test_freellmapi_port_taken_by_another_program(ctx):
    ctx.http_get = lambda url, headers: (200, b"<html>hello</html>")
    r = only(d.check_freellmapi(ctx))
    assert r.status == "fail" and str(d.PORT) in r.label and "not FreeLLMAPI" in r.label


def freellmapi_with_key(key, seen):
    """FreeLLMAPI's /v1/models: 200 for the unified key, 401 with a JSON body for no key or another key."""
    def http_get(url, headers):
        seen.append(headers)
        return (200 if headers.get("Authorization") == f"Bearer {key}" else 401), b'{"error": {}}'
    return http_get


def test_freellmapi_health_uses_this_profiles_key(ctx, monkeypatch):
    key, seen = "sk-doctor-test-key", []
    ctx.http_get = freellmapi_with_key(key, seen)
    monkeypatch.delenv("FREELLMAPI_API_KEY", raising=False)
    no_key = only(d.check_freellmapi(ctx))
    assert no_key.status == "warn" and no_key.label == "this profile has no FREELLMAPI_API_KEY"
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-another-key")
    refused = only(d.check_freellmapi(ctx))
    assert refused.status == "warn" and refused.label == "FreeLLMAPI does not accept this profile's FREELLMAPI_API_KEY"
    assert no_key.fix == refused.fix == "hermes config set FREELLMAPI_API_KEY <key>"
    monkeypatch.setenv("FREELLMAPI_API_KEY", key)
    state = State()
    set_mode(state, "freellmapi", "external")
    save_state(state)
    ok = only(d.check_freellmapi(ctx))
    assert ok.status == "ok" and ok.label == f"FreeLLMAPI, external, 127.0.0.1:{d.PORT}"  # Polytropos knows no tag for it
    assert {"Authorization": f"Bearer {key}"} in seen
    assert key not in d.format_report([no_key, refused, ok]) and "sk-another-key" not in d.format_report([refused])
    if sys.platform == "win32":  # a managed release is a link, and Polytropos runs FreeLLMAPI on macOS and Linux only
        return
    set_mode(state, "freellmapi", "managed")
    save_state(state)
    root = ctx.home / ".local" / "share" / "polytropos" / "freellmapi"
    (root / "releases" / "v0.16.30").mkdir(parents=True)
    (root / "current").symlink_to(root / "releases" / "v0.16.30")
    assert only(d.check_freellmapi(ctx)).label == f"FreeLLMAPI v0.16.30, managed, 127.0.0.1:{d.PORT}"  # the running tag


@pytest.mark.parametrize("status, detail", [(0, "FreeLLMAPI did not answer"), (-1, "FreeLLMAPI did not answer"),
                                            (503, "it returned HTTP 503")])
def test_freellmapi_unhealthy_never_shows_internal_codes(ctx, monkeypatch, status, detail):
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-doctor-test-key")
    ctx.http_get = lambda url, headers: (status, b"") if "Authorization" in headers else (200, b'{"data": []}')
    r = only(d.check_freellmapi(ctx))
    assert r.status == "warn" and r.detail == detail


def test_node_outside_freellmapis_range_fails_unless_freellmapi_is_external(ctx):
    node = {"version": "v18.20.0"}
    ctx.run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=node["version"] + "\n", stderr="")
    r = only(d.check_node(ctx))  # FreeLLMAPI is not installed yet
    assert r.status == "fail" and "v18.20.0" in r.label and "https://nodejs.org" in r.fix
    state = State()
    set_mode(state, "freellmapi", "managed")
    save_state(state)
    assert only(d.check_node(ctx)).status == "fail"
    set_mode(state, "freellmapi", "external")
    save_state(state)
    assert only(d.check_node(ctx)).status == "info"
    node["version"] = "v22.11.0"
    assert only(d.check_node(ctx)) == d.CheckResult("ok", "Node v22.11.0")

    def no_node(argv, **kw):
        raise FileNotFoundError(argv[0])

    ctx.run = no_node
    set_mode(state, "freellmapi", "managed")
    save_state(state)
    r = only(d.check_node(ctx))
    assert r.status == "fail" and "not found" in r.label
    ctx.platform = "win32"  # FreeLLMAPI's desktop app needs no Node
    assert only(d.check_node(ctx)).status == "info"


ROUTER = """
plugins:
  entries:
    polytropos-router:
      upstreams:
        cloud: {base_url: "http://127.0.0.1:3001/v1", context_window: 32000}
        ollama: {base_url: "http://127.0.0.1:11434/v1", local: true, runtime: ollama, context_window: 16384}
      routes:
        simple: [ollama/qwen3.5:0.8b, cloud/auto]
        complex: [cloud/auto]
"""


def test_router_ok_with_defaults(ctx):
    assert only(d.check_router(ctx)).status == "ok"


def test_router_broken_config_shows_the_errors(ctx):
    write_config(ctx, "plugins:\n  entries:\n    polytropos-router:\n      default_route: nowhere\n")
    r = d.check_router(ctx)[0]
    assert r.status == "fail" and "default_route: no route has that name" in r.detail


def test_router_window_and_unanswered_upstream(ctx):
    write_config(ctx, ROUTER)
    ctx.http_get = lambda url, headers: (0, b"") if "11434" in url else (200, b"{}")
    got = d.check_router(ctx)
    assert [r.status for r in got] == ["warn", "warn"]
    assert "cloud/auto" in got[0].label and "32,000" in got[0].label
    assert "ollama" in got[1].label


def test_router_says_when_a_local_upstream_is_not_ollama(ctx):
    write_config(ctx, """
plugins:
  entries:
    polytropos-router:
      upstreams:
        cloud: {base_url: "http://127.0.0.1:3001/v1", context_window: 128000}
        ollama: {base_url: "http://127.0.0.1:11434/v1", local: true, runtime: ollama}
        studio: {base_url: "http://127.0.0.1:1234/v1", local: true}
      routes:
        simple: [ollama/qwen3.5:0.8b, studio/m, cloud/auto]
        complex: [cloud/auto]
""")
    seen = []

    def http_get(url, headers):
        seen.append(url)
        if url.endswith("/api/version"):
            return (200, b'{"version": "0.40.16"}') if ":11434" in url else (404, b"not found")
        return 200, b'{"data": []}'

    ctx.http_get = http_get
    got = d.check_router(ctx)
    assert got[0].status == "ok"
    r = only(got[1:])
    assert r.status == "info" and "studio" in r.label and "fit check covers Ollama only" in r.detail
    assert "http://127.0.0.1:3001/api/version" not in seen  # a cloud upstream is not probed


def test_old_router_section(ctx):
    assert d.check_old_router_section(ctx) == []
    write_config(ctx, "router:\n  mode: x\n")
    r = only(d.check_old_router_section(ctx))
    assert r.status == "info" and "ignores" in r.detail


def test_programs(ctx):
    ctx.run = FakeRun(versions={"rtk": LOCK["rtk"]["version"], "graphify": "0.0.1", "canary": "", "ollama": "0.30.0"})
    got = d.check_programs(ctx)
    assert got[0].status == "ok" and f"rtk {LOCK['rtk']['version']}" in got[0].label
    assert "ollama 0.30.0" in got[0].label  # the version it read: Ollama passes on presence alone
    assert [str(ctx.home / ".local" / "bin" / "rtk"), "--version"] in ctx.run.calls
    by = {r.label.split()[0]: r for r in got[1:]}
    assert by["graphify"].status == "warn" and "0.0.1" in by["graphify"].label
    assert by["ast-grep"].status == "warn" and by["opencode"].status == "info"
    assert by["stack-canary"].status == "warn" and "commit is unknown" in by["stack-canary"].label
    state = State()
    use_shared(state, "stack-canary", str(ctx.hermes_home), version=LOCK["stack-canary"]["ref"][:7], installed_by_us=True)
    save_state(state)
    assert f"stack-canary {LOCK['stack-canary']['ref'][:7]}" in d.check_programs(ctx)[0].label


def test_program_checks_use_the_machine_from_the_context(ctx, monkeypatch):
    seen = []
    fake_matrix(monkeypatch, [])
    monkeypatch.setattr(d, "programs", lambda lock, *, platform, machine: seen.append((platform, machine)) or [])
    ctx.platform, ctx.machine = "linux", "aarch64"
    assert d.check_programs(ctx) == [] and d.check_canary(ctx) == []  # a list without ollama raises nothing
    assert seen == [("linux", "aarch64")] * 2


def fake_matrix(monkeypatch, cells):
    matrix = shared_canary.Matrix(shared_canary.Cell(*c) for c in cells)
    monkeypatch.setattr(d.canary, "load_matrix", lambda: matrix)


def test_canary_failing_local_model(ctx, monkeypatch):
    write_config(ctx, ROUTER)
    ctx.run = FakeRun(versions={"ollama": "0.40.16"})
    cells = [("0.40.16", "qwen3.5:0.8b", "A6", "fail"), ("0.40.14", "qwen3.5:0.8b", "A1", "pass")]
    cells += [("0.40.14", "qwen3.5:0.8b", c, "pass") for c in ("A2", "A3", "A4", "A5", "A6")]
    fake_matrix(monkeypatch, cells)
    r = only(d.check_canary(ctx))
    assert r.status == "warn" and "0.40.16" in r.label and "qwen3.5:0.8b" in r.label
    assert "0.40.14" in r.detail


def test_canary_local_result_fail_for_a_routed_slot(ctx, monkeypatch):
    fake_matrix(monkeypatch, [])
    shared_canary.save_local_results({"freellmapi/auto": "fail"})
    r = only(d.check_canary(ctx))
    assert r.status == "warn" and "freellmapi/auto" in r.label


@pytest.mark.parametrize("ram, model, window, status", [
    (16 * GB, "qwen3.5:0.8b", 8192, "info"),
    (16 * GB, "llama3:8b", 8192, "warn"),
    (16 * GB, "qwen3.5:0.8b", 32768, "warn"),
    (16 * GB, "mystery", 8192, "info"),
    (32 * GB, "llama3:70b", 131072, "info"),
])
def test_fit(ctx, ram, model, window, status):
    write_config(ctx, f"""
plugins:
  entries:
    polytropos-router:
      upstreams:
        ollama: {{base_url: "http://127.0.0.1:11434/v1", local: true, runtime: ollama, context_window: {window}}}
      routes: {{simple: ["ollama/{model}"], complex: ["ollama/{model}"]}}
""")
    ctx.run = FakeRun(ram=ram)
    r = only(d.check_fit(ctx))
    assert r.status == status
    if status == "warn":
        assert "about 20,000 tokens" in r.detail
    if status == "info" and ram > 16 * GB:
        assert "32 GB" in r.label


def test_key_files_report_names_and_modes_without_opening(ctx, monkeypatch):
    (ctx.home / "Downloads").mkdir()
    bad = ctx.home / "Downloads" / "my-keys.json"
    bad.write_text("secret")
    os.chmod(bad, 0o644)
    (ctx.home / ".env.backup2").write_text("secret")
    (ctx.home / "Downloads" / "notes.txt").write_text("fine")
    real_open, real_read = builtins.open, Path.read_text
    guarded = lambda p: "keys.json" in str(p) or ".env.backup" in str(p)  # noqa: E731

    def no_open(file, *a, **kw):
        assert not guarded(file), file
        return real_open(file, *a, **kw)

    def no_read(self, *a, **kw):
        assert not guarded(self), self
        return real_read(self, *a, **kw)

    monkeypatch.setattr(builtins, "open", no_open)
    monkeypatch.setattr(Path, "read_text", no_read)
    got = d.check_key_files(ctx)
    assert [r.status for r in got] == ["warn", "warn"]
    assert "my-keys.json (0644)" in got[0].detail and "contents not opened" in got[0].label
    assert ".env.backup2" in got[1].detail and "secret" not in got[0].detail


def test_key_files_none(ctx):
    assert only(d.check_key_files(ctx)).status == "ok"


def test_profiles(ctx):
    root = ctx.hermes_home
    (root / "profiles" / "work" / "plugins" / "polytropos").mkdir(parents=True)
    (root / "profiles" / "play").mkdir(parents=True)
    got = d.check_profiles(ctx)
    assert [(r.status, r.label) for r in got] == [("info", "profile play: Polytropos is not installed"),
                                                  ("info", "profile work: Polytropos is installed")]
    ctx.hermes_home = root / "profiles" / "work"
    assert len(d.check_profiles(ctx)) == 2


def test_ponytail_and_superpowers_only_when_installed(ctx):
    assert d.check_ponytail(ctx) == [] and d.check_superpowers(ctx) == []
    (ctx.hermes_home / "plugins" / "ponytail").mkdir()
    (ctx.hermes_home / "plugins" / "superpowers").mkdir()
    assert "PONYTAIL_DEFAULT_MODE=off" in only(d.check_ponytail(ctx)).detail
    assert "not verified" in only(d.check_superpowers(ctx)).label


def test_sleep(ctx):
    assert d.check_sleep(ctx) == []  # no gateway LaunchAgent
    agents = ctx.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai.hermes.gateway.plist").write_text("")
    ctx.run = FakeRun(sleep=1)
    r = only(d.check_sleep(ctx))
    assert r.status == "warn" and "sudo pmset -a sleep 0" in r.fix
    ctx.run = FakeRun(sleep=0)
    assert only(d.check_sleep(ctx)).status == "ok"
    ctx.platform = "linux"
    assert d.check_sleep(ctx) == []


def test_a_raising_check_becomes_one_fail(ctx, monkeypatch):
    def boom(ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(d, "CHECKS", (d.check_hermes, boom))
    got = d.run_checks(ctx)
    assert [r.status for r in got] == ["ok", "fail"]
    assert "boom" in got[1].label and "kaboom" in got[1].detail


def test_run_checks_runs_every_check(ctx, monkeypatch):
    fake_matrix(monkeypatch, [])
    assert not any(r.status == "fail" and r.label.startswith("check ") for r in d.run_checks(ctx))


def test_format_report_layout():
    out = d.format_report([
        d.CheckResult("ok", "Hermes v0.23.2 (tested)"),
        d.CheckResult("warn", "2 files", "a, b", "do this"),
        d.CheckResult("info", "RAM"),
    ])
    assert out == ("Polytropos doctor\n"
                   "  ok    Hermes v0.23.2 (tested)\n"
                   "  warn  2 files: a, b\n"
                   "        do this\n"
                   "  info  RAM")
