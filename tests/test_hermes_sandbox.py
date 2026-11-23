import json
import os
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
import yaml

import hermes_sandbox

GATEWAY_ARGV = ["main.py", "gateway", "run", "--quiet", "--idle-exit"]  # as Hermes records an unmanaged daemon
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
# The sandbox drives a macOS or Linux Hermes: it finds and stops the gateway with ps and POSIX signals.
posix = pytest.mark.skipif(sys.platform == "win32", reason="the sandbox stops the gateway with ps and signals")


def test_make_home_writes_config_and_copies_plugins(tmp_path):
    plugin = tmp_path / "src" / "hook_probe"
    (plugin / "__pycache__").mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: hook-probe\n")
    config = {"model": {"provider": "p", "default": "m"}, "plugins": {"enabled": ["hook-probe"]}}

    home = hermes_sandbox.make_home(tmp_path / "box", config, [plugin])

    assert home == tmp_path / "box" / "hermes"
    assert yaml.safe_load((home / "config.yaml").read_text()) == config
    assert (home / "plugins" / "hook_probe" / "plugin.yaml").read_text() == "name: hook-probe\n"
    assert not (home / "plugins" / "hook_probe" / "__pycache__").exists()
    assert (tmp_path / "box" / "home").is_dir()


def test_make_home_twice_replaces_plugins(tmp_path):
    plugin = tmp_path / "src" / "hook_probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: hook-probe\n")
    (plugin / "old.py").write_text("")
    hermes_sandbox.make_home(tmp_path / "box", {}, [plugin])
    (plugin / "old.py").unlink()

    home = hermes_sandbox.make_home(tmp_path / "box", {}, [plugin])

    assert [p.name for p in (home / "plugins" / "hook_probe").iterdir()] == ["plugin.yaml"]


def test_run_oneshot_isolates_the_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hermes_sandbox.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or "done")
    monkeypatch.setenv("SOME_API_KEY", "must-not-leak")
    home = hermes_sandbox.make_home(tmp_path, {}, [])

    assert hermes_sandbox.run_oneshot(home, "hi", provider="p", model="m", timeout_s=5) == "done"

    (cmd, kw), = calls
    assert cmd == [str(hermes_sandbox.HERMES_BIN), "-z", "hi", "--provider", "p", "-m", "m"]
    assert kw["env"]["HERMES_HOME"] == str(home)
    assert kw["env"]["HOME"] == str(tmp_path / "home")
    assert kw["env"]["TMPDIR"] == str(tmp_path / "tmp") and (tmp_path / "tmp").is_dir()
    assert kw["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert kw["env"]["OLLAMA_HOST"] == hermes_sandbox.CLOSED
    assert "SOME_API_KEY" not in kw["env"]
    assert kw["cwd"] == tmp_path / "work" and kw["cwd"].is_dir()
    assert kw["timeout"] == 5 and kw["stdin"] == subprocess.DEVNULL


def test_launch_snapshot_filters_labels_and_lists_agents(tmp_home, monkeypatch):
    listing = "PID\tStatus\tLabel\n12\t0\tai.hermes.gateway\n-\t0\tcom.apple.Finder\n-\t0\tio.github.lil-clanky.polytropos.x\n"
    monkeypatch.setattr(hermes_sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(
        hermes_sandbox.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr=""))
    agents = Path(tmp_home) / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.example.agent.plist").write_text("")

    assert hermes_sandbox.launch_snapshot() == {
        "ai.hermes.gateway", "io.github.lil-clanky.polytropos.x", "com.example.agent.plist"}


def _pid_file(home, pid, hermes_home, argv=GATEWAY_ARGV):
    home.mkdir(parents=True, exist_ok=True)
    (home / "gateway.pid").write_text(json.dumps(
        {"pid": pid, "kind": "hermes-gateway", "argv": argv, "hermes_home": str(hermes_home)}))


def _gone(pid):
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return not state or state.startswith("Z")


@posix
def test_stop_gateway_stops_the_daemon_of_this_home(tmp_path):
    home = tmp_path / "hermes"
    daemon = subprocess.Popen(SLEEP + GATEWAY_ARGV[1:])
    _pid_file(home, daemon.pid, home)

    assert hermes_sandbox.stop_gateway(home) is True
    assert daemon.wait(timeout=5) is not None


def test_stop_gateway_leaves_other_processes_alone(tmp_path):
    home = tmp_path / "hermes"
    other_gateway = subprocess.Popen(SLEEP + ["gateway", "run", "--external-supervisor"])
    plain = subprocess.Popen(SLEEP)
    try:
        _pid_file(home, other_gateway.pid, tmp_path / "elsewhere")
        assert hermes_sandbox.stop_gateway(home) is False
        _pid_file(home, other_gateway.pid, home)  # names this home, but the live command is another gateway's
        assert hermes_sandbox.stop_gateway(home) is False
        _pid_file(home, plain.pid, home)
        assert hermes_sandbox.stop_gateway(home) is False
        assert other_gateway.poll() is None and plain.poll() is None
    finally:
        for proc in (other_gateway, plain):
            proc.kill()
            proc.wait()
    assert hermes_sandbox.stop_gateway(tmp_path / "missing") is False


@posix
def test_stop_gateway_kills_a_daemon_that_ignores_sigterm(tmp_path):
    home = tmp_path / "hermes"
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    stubborn = subprocess.Popen([sys.executable, "-c", code, *GATEWAY_ARGV[1:]], stdout=subprocess.PIPE, text=True)
    try:
        stubborn.stdout.readline()
        _pid_file(home, stubborn.pid, home)

        assert hermes_sandbox.stop_gateway(home, timeout_s=0.5) is True
        assert stubborn.wait(timeout=5) == -signal.SIGKILL
        assert not (home / "gateway.pid").exists()
    finally:
        stubborn.kill()
        stubborn.wait()


FAKE_HERMES = """#!{python}
import json, os, subprocess, sys
home = os.environ["HERMES_HOME"]
argv = {argv!r}
daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv[1:]], start_new_session=True,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(os.path.join(home, "gateway.pid"), "w") as f:
    json.dump({{"pid": daemon.pid, "kind": "hermes-gateway", "argv": argv, "hermes_home": home}}, f)
"""


@posix
def test_run_oneshot_stops_the_daemon_before_and_after(tmp_path, monkeypatch):
    home = hermes_sandbox.make_home(tmp_path, {}, [])
    fake = tmp_path / "fake-hermes"  # like `hermes -z`, it starts a gateway daemon for the home and exits
    fake.write_text(FAKE_HERMES.format(python=sys.executable, argv=GATEWAY_ARGV))
    fake.chmod(0o755)
    monkeypatch.setattr(hermes_sandbox, "HERMES_BIN", fake)
    earlier = subprocess.Popen(SLEEP + GATEWAY_ARGV[1:])  # the daemon an earlier run left serving the home
    _pid_file(home, earlier.pid, home)
    started = None
    try:
        assert hermes_sandbox.run_oneshot(home, "hi", provider="p", model="m", timeout_s=30).returncode == 0

        started = json.loads((home / "gateway.pid").read_text())["pid"]
        assert started != earlier.pid
        assert earlier.wait(timeout=5) is not None
        assert _gone(started)
    finally:
        earlier.kill()
        earlier.wait()
        if started and not _gone(started):
            os.kill(started, signal.SIGKILL)


def _router_turn(seen: dict, *, call_fake: bool):
    """A stand-in for run_oneshot: records what the run would see and, like the router, can call the upstream."""
    def turn(home, prompt, *, provider, model, timeout_s):
        entry = yaml.safe_load((home / "config.yaml").read_text())["plugins"]["entries"]["polytropos-router"]
        if call_fake:
            url = entry["upstreams"]["fake"]["base_url"] + "/chat/completions"
            urllib.request.urlopen(urllib.request.Request(url, data=b'{"model": "model"}'), timeout=5).read()
        seen.update(home=home, provider=provider, model=model, env=(home / ".env").read_text(),
                    plugins=sorted(p.name for p in (home / "plugins").iterdir()))
        return subprocess.CompletedProcess([], 0, stdout="hi\n", stderr="")
    return turn


def test_build_root_runs_one_router_turn_against_a_fake(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(hermes_sandbox, "run_oneshot", _router_turn(seen, call_fake=True))

    assert hermes_sandbox.build_root(tmp_path / "e2e") == 0

    assert seen == {"home": tmp_path / "e2e" / "hermes", "provider": "polytropos-router", "model": "auto",
                    "env": "FREELLMAPI_API_KEY=sandbox-fake-key\n", "plugins": ["polytropos", "polytropos_router"]}
    assert "POST /v1/chat/completions model=model" in capsys.readouterr().out


def test_build_root_fails_when_no_request_reached_the_fake(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_sandbox, "run_oneshot", _router_turn({}, call_fake=False))

    assert hermes_sandbox.build_root(tmp_path / "e2e") == 1


def test_launch_snapshot_empty_off_macos(monkeypatch):
    monkeypatch.setattr(hermes_sandbox.sys, "platform", "linux")
    assert hermes_sandbox.launch_snapshot() == set()
