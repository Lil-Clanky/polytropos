import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from polytropos import freellm as fl
from polytropos.freellm import BuildError, FreeLLM, NodeMissing, Pin, PortBusy
from polytropos.state import State, save_state, set_mode

SHA = "a" * 40
OLD, NEW = Pin("v0.16.8", SHA), Pin("v0.16.9", SHA)


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


def make(tmp_home, *, status=(200, b'{"data": []}'), run=None, services=None):
    box = {"get": status}
    fl_ = FreeLLM(root=tmp_home / "fl", run=run or FakeRun(), services=services or FakeServices(),
                  http_get=lambda url, headers: box["get"], node="node")
    fl_.ready_timeout = 0
    fl_.box = box
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


def test_install_writes_service_spec_then_times_out(tmp_home):
    f = make(tmp_home, status=(0, b""))
    with pytest.raises(BuildError, match="answer"):  # the service never answers

        f.install(NEW)
    spec = f.services.spec
    assert spec.program == ["node", str(f.root / "current/server/dist/index.js")]
    assert spec.env == {"PORT": "3001", "HOST": "127.0.0.1", "NODE_ENV": "production",
                        "FREEAPI_ENV_PATH": str(f.root / "data/.env")}
    assert spec.workdir == f.root / "current"
    assert (f.root / "current").resolve().name == "v0.16.9"
    assert stat.S_IMODE((f.root / "data/.env").stat().st_mode) == 0o600


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


def test_build_failed_step_removes_release(tmp_home):
    f = make(tmp_home, run=FakeRun(fail_on="npm ci"))
    with pytest.raises(BuildError, match="npm ci"):
        f.build(NEW)
    assert not (f.root / "releases/v0.16.9").exists()


def test_build_links_data_and_runs_steps(tmp_home):
    run = FakeRun()
    f = make(tmp_home, run=run)
    dest = f.build(NEW)
    link = dest / "server/data"
    assert link.is_symlink() and link.resolve() == (f.root / "data").resolve()
    argvs = [c[0] for c in run.calls]
    assert argvs[0] == ["git", "clone", "--depth", "1", "--branch", "v0.16.9", fl.REPO_URL, str(dest)]
    assert argvs[-2:] == [["npm", "ci"], ["npm", "run", "build"]]


def test_ensure_env_once(tmp_home):
    f = make(tmp_home)
    path = f.ensure_env()
    first = path.read_text()
    assert len(first.strip().split("=", 1)[1]) == 64
    assert f.ensure_env().read_text() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


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
    f = make(tmp_home, **kw)
    (f.root / "data").mkdir(parents=True)
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


def test_adopt_copies_data_and_refuses_while_running(tmp_home):
    src = tmp_home / "checkout"
    (src / "server/data").mkdir(parents=True)
    (src / "server/data/freeapi.db").write_text("db")
    (src / "server/data/.encryption-key").write_text("d" * 64)
    (src / "server/.env").write_text("FOO=1\n")
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
    assert "FOO=1" in env and "ENCRYPTION_KEY=" + "d" * 64 in env


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


def test_cli_command(tmp_home):
    lock = {"freellmapi": {"cli_version": "0.4.1"}}
    f = make(tmp_home)
    assert f.cli_command(lock) == ["npx", "freellmapi@0.4.1"]
    state = State()
    set_mode(state, "freellmapi", "managed")
    save_state(state)
    assert f.cli_command(lock) == ["node", str(f.root / "current/cli/dist/index.js")]
