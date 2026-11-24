import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from polytropos import freellm as fl
from polytropos.freellm import BuildError, FreeLLM, NodeMissing, Pin, PortBusy
from polytropos.state import State, save_state, set_mode

SHA = "a" * 40
OLD, NEW = Pin("v0.16.8", SHA), Pin("v0.16.9", SHA)
# Polytropos builds FreeLLMAPI, links its releases and runs it as a service only on macOS and Linux.
NOT_ON_WINDOWS = "Polytropos does not run FreeLLMAPI on Windows"
managed = pytest.mark.skipif(sys.platform == "win32", reason=NOT_ON_WINDOWS)


class FakeRun:
    def __init__(self, head=SHA, node="v22.1.0", fail_on=None):
        self.calls, self.head, self.node, self.fail_on = [], head, node, fail_on

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw.get("cwd")))
        out = ""
        if self.fail_on and self.fail_on in " ".join(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if argv[:2] == ["git", "clone"]:
            (Path(argv[-1]) / "server").mkdir(parents=True)
        elif argv[:2] == ["git", "rev-parse"]:
            out = self.head + "\n"
        elif argv[1:] == ["--version"]:
            out = self.node + "\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


class FakeServices:
    def __init__(self, running=False):
        self.running, self.events, self.spec = running, [], None

    def install(self, spec):
        self.spec = spec
        self.running = True
        self.events.append("install")
        return Path("unit")

    def start(self):
        self.running = True
        self.events.append("start")

    def stop(self):
        self.running = False
        self.events.append("stop")

    def is_running(self):
        return self.running

    def manual_start_hint(self):
        return "start-by-hand"


def make(tmp_home, *, status=(200, b'{"data": []}'), run=None, services=None):
    box = {"get": status}
    fl_ = FreeLLM(root=tmp_home / "fl", run=run or FakeRun(), services=services or FakeServices(),
                  http_get=lambda url, headers: box["get"], node="node")
    fl_.ready_timeout = 0
    fl_.stop_timeout = 0
    fl_.retry_delay = 0
    fl_.box = box
    svc, up = fl_.services, status

    def stop(orig=svc.stop):
        orig()
        box["get"] = (0, b"")

    def start(orig=svc.start):
        orig()
        box["get"] = up

    svc.stop, svc.start = stop, start
    return fl_


def test_detect_port_busy_with_other_program(tmp_home):
    d = make(tmp_home, status=(404, b"<html>nope</html>")).detect()
    assert (d.listening, d.is_freellmapi, d.healthy) == (True, False, False)
    d = make(tmp_home, status=(401, b'{"error": "key"}')).detect()
    assert (d.listening, d.is_freellmapi, d.healthy) == (True, True, False)
    d = make(tmp_home, status=(0, b"")).detect()
    assert (d.listening, d.is_freellmapi) == (False, False)
    d = make(tmp_home).detect()
    assert (d.is_freellmapi, d.healthy, d.managed) == (True, True, False)


def test_detect_managed_needs_state_and_running_service(tmp_home):
    state = State()
    set_mode(state, "freellmapi", "managed")
    save_state(state)
    assert make(tmp_home, services=FakeServices(running=True)).detect().managed is True
    assert make(tmp_home, services=FakeServices(running=False)).detect().managed is False


def test_install_refuses_when_port_taken_by_unmanaged(tmp_home):
    run = FakeRun()
    f = make(tmp_home, run=run)
    with pytest.raises(PortBusy):
        f.install(NEW)
    assert run.calls == [] or all(c[0][0] == "node" for c in run.calls)  # nothing was built


@managed
def test_install_writes_service_spec_then_times_out(tmp_home):
    f = make(tmp_home, status=(0, b""))
    with pytest.raises(BuildError, match="answer"):  # the service never answers

        f.install(NEW)
    spec = f.services.spec
    assert spec.program == ["node", str(f.root / "current/server/dist/index.js")]
    assert spec.env == {"PORT": "3001", "HOST": "127.0.0.1", "NODE_ENV": "production",
                        "FREEAPI_ENV_PATH": str(f.root / "data/.env"),
                        "FREEAPI_DB_PATH": str(f.root / "data/freeapi.db")}
    assert spec.workdir == f.root / "current"
    assert (f.root / "current").resolve().name == "v0.16.9"
    assert stat.S_IMODE((f.root / "data/.env").stat().st_mode) == 0o600


@managed
def test_install_succeeds_when_server_answers(tmp_home):
    f = make(tmp_home, status=(0, b""))
    orig = f.services.install

    def install(spec):
        f.box["get"] = (401, b"{}")
        return orig(spec)

    f.services.install = install
    f.install(NEW)
    assert f.services.events == ["install"]


def test_install_needs_node(tmp_home):
    f = make(tmp_home, status=(0, b""), run=FakeRun(node="v18.0.0"))
    with pytest.raises(NodeMissing):
        f.install(NEW)


def test_build_verifies_commit(tmp_home):
    f = make(tmp_home, run=FakeRun(head="b" * 40))
    with pytest.raises(BuildError, match="commit"):
        f.build(NEW)
    assert not (f.root / "releases/v0.16.9").exists()


@managed
def test_build_failed_step_removes_release(tmp_home):
    f = make(tmp_home, run=FakeRun(fail_on="npm ci"))
    with pytest.raises(BuildError, match="npm ci"):
        f.build(NEW)
    assert not (f.root / "releases/v0.16.9").exists()


@managed
def test_build_links_data(tmp_home):
    run = FakeRun()
    f = make(tmp_home, run=run)
    dest = f.build(NEW)
    link = dest / "server/data"
    assert link.is_symlink() and link.resolve() == (f.root / "data").resolve()
    argvs = [c[0] for c in run.calls]
    assert argvs[0] == ["git", "clone", "--depth", "1", "--branch", "v0.16.9", fl.REPO_URL, str(dest)]
    assert argvs[-2:] == [["npm", "ci"], ["npm", "run", "build"]]


@managed
def test_npm_gets_no_provider_keys(tmp_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider-test-key")
    run, envs = FakeRun(), {}

    def recording(argv, **kw):
        envs[" ".join(argv)] = kw.get("env")
        return run(argv, **kw)

    make(tmp_home, run=recording).build(NEW)
    for step in ("npm ci", "npm run build"):
        assert "OPENAI_API_KEY" not in envs[step] and envs[step]["HOME"] == str(tmp_home)


def test_ensure_env_once(tmp_home):
    f = make(tmp_home)
    path = f.ensure_env()
    first = path.read_text()
    assert len(first.strip().split("=", 1)[1]) == 64
    assert f.ensure_env().read_text() == first
    assert sys.platform == "win32" or stat.S_IMODE(path.stat().st_mode) == 0o600  # Windows keeps no mode bits


def test_ensure_env_reuses_key_file(tmp_home):
    f = make(tmp_home)
    (f.root / "data").mkdir(parents=True)
    (f.root / "data/.encryption-key").write_text("c" * 64 + "\n")
    assert f.ensure_env().read_text() == "ENCRYPTION_KEY=" + "c" * 64 + "\n"


def test_ensure_env_keeps_existing_key(tmp_home):
    f = make(tmp_home)
    (f.root / "data").mkdir(parents=True)
    (f.root / "data/.env").write_text("FOO=1\nENCRYPTION_KEY=x\nBAR=2\n")
    assert f.ensure_env().read_text() == "FOO=1\nENCRYPTION_KEY=x\nBAR=2\n"


def test_ensure_env_appends_to_env_without_key(tmp_home):
    f = make(tmp_home)
    (f.root / "data").mkdir(parents=True)
    (f.root / "data/.env").write_text("FOO=1")
    text = f.ensure_env().read_text()
    assert text.startswith("FOO=1\nENCRYPTION_KEY=")


def test_backup_prunes_to_five(tmp_home):
    f = make(tmp_home)
    (f.root / "data").mkdir(parents=True)
    (f.root / "data/freeapi.db").write_text("db")
    made = [f.backup(f"v{i}-to-v{i + 1}") for i in range(7)]
    left = sorted(p.name for p in (f.root / "backups").iterdir())
    assert len(left) == 5 and left == sorted(p.name for p in made[2:])
    assert (made[-1] / "freeapi.db").read_text() == "db"


def _installed(tmp_home, **kw):
    if sys.platform == "win32":
        pytest.skip(NOT_ON_WINDOWS)
    f = make(tmp_home, **kw)
    f._data_dir()
    (f.root / "data/freeapi.db").write_text("old-data")
    f.build(OLD)
    f._point("v0.16.8")
    return f


def test_update_success_switches_current(tmp_home, capsys):
    f = _installed(tmp_home)
    result = f.update(NEW, check=lambda: True)
    assert (result.ok, result.from_tag, result.to_tag, result.rolled_back) == (True, "v0.16.8", "v0.16.9", False)
    assert (f.root / "current").resolve().name == "v0.16.9"
    assert f.services.events == ["stop", "start"]
    assert any(p.name.endswith("-v0.16.8-to-v0.16.9") for p in (f.root / "backups").iterdir())


def test_update_failed_health_rolls_back(tmp_home, capsys):
    f = _installed(tmp_home)
    orig = f.services.start

    def start():
        orig()
        if (f.root / "current").resolve().name == "v0.16.9":
            (f.root / "data/freeapi.db").write_text("migrated")
            f.box["get"] = (500, b"")
        else:
            f.box["get"] = (200, b"{}")

    f.services.start = start
    result = f.update(NEW)
    assert (result.ok, result.rolled_back) == (False, True)
    assert (f.root / "current").resolve().name == "v0.16.8"
    assert (f.root / "data/freeapi.db").read_text() == "old-data"
    assert "lost" in capsys.readouterr().out


def test_update_failed_check_rolls_back(tmp_home):
    f = _installed(tmp_home)
    result = f.update(NEW, check=lambda: False)
    assert result.rolled_back and (f.root / "current").resolve().name == "v0.16.8"


def test_update_build_failure_leaves_everything(tmp_home):
    f = _installed(tmp_home)
    f.run = FakeRun(fail_on="npm run build")
    result = f.update(NEW)
    assert (result.ok, result.rolled_back) == (False, False)
    assert f.services.events == [] and (f.root / "current").resolve().name == "v0.16.8"


def test_rollback_restores_newest_backup(tmp_home, capsys):
    f = _installed(tmp_home)
    f.update(NEW, check=lambda: True)
    (f.root / "data/freeapi.db").write_text("after-update")
    result = f.rollback()
    assert (result.ok, result.from_tag, result.to_tag, result.rolled_back) == (True, "v0.16.9", "v0.16.8", True)
    assert (f.root / "data/freeapi.db").read_text() == "old-data"
    assert (f.root / "current").resolve().name == "v0.16.8"
    assert "lost" in capsys.readouterr().out


def test_rollback_without_backup(tmp_home):
    assert make(tmp_home).rollback().ok is False


@managed
def test_adopt_copies_data_and_refuses_while_running(tmp_home):
    src = tmp_home / "checkout"
    (src / "server/data").mkdir(parents=True)
    (src / "server/data/freeapi.db").write_text("db")
    (src / "server/data/.encryption-key").write_text("d" * 64)
    (src / ".env").write_text("FOO=1\n")
    (src / "server").mkdir(exist_ok=True)
    (src / "server/.env").write_text("STRAY=1\n")
    f = make(tmp_home)  # answers on the port
    with pytest.raises(PortBusy):
        f.adopt(src, NEW)
    assert not (f.root / "data/freeapi.db").exists()

    g = make(tmp_home, status=(0, b""))
    orig = g.services.install
    g.services.install = lambda spec: (g.box.update(get=(401, b"{}")), orig(spec))[1]
    g.adopt(src, NEW)
    assert (g.root / "data/freeapi.db").read_text() == "db"
    env = (g.root / "data/.env").read_text()
    assert "FOO=1" in env and "ENCRYPTION_KEY=" + "d" * 64 in env and "STRAY" not in env


@pytest.mark.parametrize("version,ok", [("v20.18.0", True), ("v24.9.1", True), ("v25.1.0", False),
                                        ("v18.0.0", False), ("v20.17.9", False), ("garbage", False)])
def test_node_range(tmp_home, version, ok):
    assert make(tmp_home, run=FakeRun(node=version)).node_ok()[0] is ok


def test_node_missing_binary(tmp_home):
    def run(argv, **kw):
        raise FileNotFoundError(argv[0])
    f = FreeLLM(root=tmp_home / "fl", run=run, services=FakeServices(), http_get=lambda u, h: (0, b""), node="node")
    assert f.node_ok()[0] is False


def test_health_with_key_needs_200(tmp_home):
    seen = []
    f = FreeLLM(root=tmp_home / "fl", run=FakeRun(), services=FakeServices(), node="node",
                http_get=lambda u, h: (seen.append(h), (401, b"{}"))[1])
    assert f.health() is True
    assert f.health("k") is False
    assert seen[-1]["Authorization"] == "Bearer k"



# --- fix round 1 ---

def _env(f, text):
    (f.root / "data").mkdir(parents=True, exist_ok=True)
    (f.root / "data/.env").write_text(text)


def test_ensure_env_replaces_placeholder_with_key_file(tmp_home):
    f = make(tmp_home)
    _env(f, "FOO=1\nENCRYPTION_KEY=your-64-char-hex-key-here\nBAR=2\n")
    (f.root / "data/.encryption-key").write_text("e" * 64)
    assert f.ensure_env().read_text() == "FOO=1\nENCRYPTION_KEY=" + "e" * 64 + "\nBAR=2\n"


@pytest.mark.parametrize("line", ["ENCRYPTION_KEY=", "ENCRYPTION_KEY=   ", 'ENCRYPTION_KEY=""'])
def test_ensure_env_replaces_empty_value(tmp_home, line):
    f = make(tmp_home)
    _env(f, f"{line}\nBAR=2\n")
    text = f.ensure_env().read_text()
    assert len(text.splitlines()[0].split("=", 1)[1]) == 64 and text.endswith("BAR=2\n")


@pytest.mark.parametrize("line", ["export ENCRYPTION_KEY=" + "a" * 64, "ENCRYPTION_KEY = " + "a" * 64,
                                  "  ENCRYPTION_KEY: " + "a" * 64])
def test_ensure_env_keeps_other_line_forms(tmp_home, line):
    f = make(tmp_home)
    _env(f, line + "\n")
    assert f.ensure_env().read_text() == line + "\n"


def test_ensure_env_last_definition_decides(tmp_home):
    f = make(tmp_home)
    _env(f, "ENCRYPTION_KEY=" + "a" * 64 + "\nENCRYPTION_KEY=your-64-char-hex-key-here\n")
    (f.root / "data/.encryption-key").write_text("e" * 64)
    assert f.ensure_env().read_text().splitlines()[-1] == "ENCRYPTION_KEY=" + "e" * 64


def test_dirs_and_env_are_private(tmp_home):
    f = _installed(tmp_home)
    f.ensure_env()
    f.backup("a-to-b")
    for d in (f.root / "data", f.root / "backups", *(f.root / "backups").iterdir()):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert not list((f.root / "data").glob(".env.*"))  # temp file was replaced


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset exists on macOS and Linux only")
def test_backup_names_use_utc(tmp_home, monkeypatch):
    import time
    from datetime import datetime, timezone
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    try:
        f = make(tmp_home)
        name = f.backup("a-to-b").name
        stamp = datetime.strptime(name.split("-", 1)[0], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
        assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 120
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_adopt_refuses_when_data_holds_files(tmp_home):
    src = tmp_home / "checkout"
    (src / "server/data").mkdir(parents=True)
    (src / "server/data/freeapi.db").write_text("adopted")
    f = make(tmp_home, status=(0, b""))
    (f.root / "data").mkdir(parents=True)
    (f.root / "data/.env").write_text("ENCRYPTION_KEY=x\n")
    (f.root / "data/freeapi.db-wal").write_text("w")
    with pytest.raises(fl.DataExists, match=r"\.env.*freeapi\.db-wal|freeapi\.db-wal.*\.env"):
        f.adopt(src, NEW)
    assert not (f.root / "data/freeapi.db").exists()
    assert f.services.events == []


def test_default_get_classifies_failures():
    import socket
    import threading

    def serve(reply):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def run():
            conn, _ = srv.accept()
            if reply is not None:
                conn.sendall(reply)
            conn.close()
            srv.close()

        threading.Thread(target=run, daemon=True).start()
        return f"http://127.0.0.1:{srv.getsockname()[1]}/v1/models"

    assert fl._default_get(serve(b"SSH-2.0-banner\r\n"), {})[0] == -1   # not HTTP
    assert fl._default_get(serve(None), {})[0] == -1                       # accepts, then closes
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{free.getsockname()[1]}/v1/models"
    free.close()
    assert fl._default_get(url, {}) == (0, b"")                            # refused


def test_detect_counts_non_http_listener_as_listening(tmp_home):
    d = make(tmp_home, status=(-1, b"")).detect()
    assert (d.listening, d.is_freellmapi) == (True, False)


def test_update_backup_failure_restarts_old_service(tmp_home):
    f = _installed(tmp_home)
    f.backup = lambda label: (_ for _ in ()).throw(OSError("disk full"))
    result = f.update(NEW)
    assert (result.ok, result.rolled_back) == (False, False) and "disk full" in result.message
    assert f.services.events == ["stop", "start"]
    assert (f.root / "current").resolve().name == "v0.16.8"


def test_update_start_raising_reverts(tmp_home, capsys):
    f = _installed(tmp_home)
    orig = f.services.start
    calls = []

    def start():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bootstrap failed")
        orig()

    f.services.start = start
    result = f.update(NEW)
    assert (result.ok, result.rolled_back) == (False, True)
    assert (f.root / "current").resolve().name == "v0.16.8"
    assert (f.root / "data/freeapi.db").read_text() == "old-data"


def test_update_check_raising_reverts(tmp_home):
    f = _installed(tmp_home)

    def boom():
        raise RuntimeError("x")

    assert f.update(NEW, check=boom).rolled_back is True


def test_update_does_not_copy_while_service_still_listens(tmp_home):
    f = _installed(tmp_home)
    f.services.stop = lambda: None            # service ignores the stop
    f.box["get"] = (200, b"{}")
    result = f.update(NEW)
    assert result.ok is False and "stop" in result.message
    assert not (f.root / "backups").exists() or not list((f.root / "backups").iterdir())
    assert (f.root / "current").resolve().name == "v0.16.8"


def test_rollback_refuses_a_stale_backup(tmp_home):
    f = _installed(tmp_home)
    f.update(NEW, check=lambda: False)        # rolled back automatically
    (f.root / "data/freeapi.db").write_text("written-since")
    result = f.rollback()
    assert result.ok is False and result.from_tag == "v0.16.8"
    assert (f.root / "data/freeapi.db").read_text() == "written-since"

    g = _installed(tmp_home / "two")
    g.update(NEW, check=lambda: True)
    assert g.rollback().ok is True
    (g.root / "data/freeapi.db").write_text("written-since")
    assert g.rollback().ok is False           # second rollback must not apply the backup again
    assert (g.root / "data/freeapi.db").read_text() == "written-since"


# --- fix round 2 ---

def _start_script(f, plan):
    """plan: list of booleans per start() call, True = raises."""
    orig, calls = f.services.start, []

    def start():
        calls.append(1)
        if plan[len(calls) - 1]:
            raise RuntimeError("Bootstrap failed: 5")
        orig()

    f.services.start = start
    return calls


def test_update_revert_start_failure_reports_restored_tag(tmp_home):
    f = _installed(tmp_home)
    _start_script(f, [True, True, True])
    result = f.update(NEW)
    assert (result.ok, result.rolled_back) == (False, True)
    assert "restored v0.16.8" in result.message and "did not start" in result.message
    assert "start-by-hand" in result.message and "run rollback" not in result.message
    assert (f.root / "data/freeapi.db").read_text() == "old-data"


def test_update_revert_retries_the_start_once(tmp_home):
    f = _installed(tmp_home)
    calls = _start_script(f, [True, True, False])
    result = f.update(NEW)
    assert (result.ok, result.rolled_back) == (False, True) and "did not start" not in result.message
    assert len(calls) == 3


def test_rollback_start_failure_is_reported_not_raised(tmp_home):
    f = _installed(tmp_home)
    f.update(NEW, check=lambda: True)
    _start_script(f, [True, True])
    result = f.rollback()
    assert (result.ok, result.rolled_back) == (False, True)
    assert "restored v0.16.8" in result.message and "start-by-hand" in result.message
    assert (f.root / "current").resolve().name == "v0.16.8"


def test_rollback_refusal_says_current(tmp_home):
    f = _installed(tmp_home)
    f.update(NEW, check=lambda: False)
    msg = f.rollback().message
    assert "is current" in msg and "is running" not in msg


@pytest.mark.parametrize("line", ["ENCRYPTION_KEY= # note", "ENCRYPTION_KEY=#x", "ENCRYPTION_KEY:" + "a" * 64])
def test_ensure_env_treats_dotenv_empty_lines_as_missing(tmp_home, line):
    f = make(tmp_home)
    _env(f, line + "\n")
    (f.root / "data/.encryption-key").write_text("e" * 64)
    assert "ENCRYPTION_KEY=" + "e" * 64 in f.ensure_env().read_text().splitlines()


def test_ensure_env_sees_key_behind_bom(tmp_home):
    f = make(tmp_home)
    (f.root / "data").mkdir(parents=True)
    (f.root / "data/.env").write_bytes(b"\xef\xbb\xbfENCRYPTION_KEY=" + b"a" * 64 + b"\nFOO=1\n")
    text = f.ensure_env().read_text()
    assert text.count("ENCRYPTION_KEY") == 1 and "a" * 64 in text


def test_adopt_with_node_missing_changes_nothing(tmp_home):
    src = tmp_home / "checkout"
    (src / "server/data").mkdir(parents=True)
    (src / "server/data/freeapi.db").write_text("db")
    f = make(tmp_home, status=(0, b""), run=FakeRun(node="v18.0.0"))
    with pytest.raises(NodeMissing):
        f.adopt(src, NEW)
    assert list((f.root / "data").iterdir()) == []


def test_default_get_keeps_status_when_error_body_is_cut_off():
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 100\r\n\r\nshort")
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    assert fl._default_get(f"http://127.0.0.1:{srv.getsockname()[1]}/v1/models", {}) == (404, b"")
