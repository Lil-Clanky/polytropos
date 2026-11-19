import argparse
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import polytropos
from polytropos import cli, doctor, hooks, slash
from polytropos._shared import guard, paths
from polytropos.freellm import BuildError, Detected, Pin, UpdateResult
from polytropos.programs import load_lock
from polytropos.state import State, config_changes, load_state, record_config_change, save_state, set_mode, use_shared

ROOT = Path(__file__).resolve().parent.parent
LOCK = load_lock(ROOT / "versions.lock")
R = "plugins.entries.polytropos-router."
SHA_OLD, SHA_NEW = "a" * 40, "b" * 40
GB = 1024**3


def parse(*argv):
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    return parser.parse_args(list(argv))


def no_questions(question):
    pytest.fail(f"unexpected question: {question}")


class FakeRun:
    """Records argv. `<name> --version` prints versions[name] or raises FileNotFoundError. A command
    that starts with a prefix in `fail` exits 1; `on` maps a prefix to a side effect."""

    def __init__(self, versions=None, fail=(), on=None):
        self.versions, self.fail, self.on, self.calls = versions or {}, fail, on or {}, []

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[1:] == ["--version"]:
            if argv[0] not in self.versions:
                raise FileNotFoundError(argv[0])
            return SimpleNamespace(returncode=0, stdout=f"{argv[0]} {self.versions[argv[0]]}\n", stderr="")
        for prefix, effect in self.on.items():
            if argv[:len(prefix)] == list(prefix):
                effect(argv)
        code = 1 if any(argv[:len(p)] == list(p) for p in self.fail) else 0
        if code and kw.get("check"):
            raise subprocess.CalledProcessError(code, argv)
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    def commands(self):
        return [c for c in self.calls if c[1:] != ["--version"]]


class FakeCtx:
    def __init__(self):
        self.cli, self.commands, self.hooks = [], [], []

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli.append((name, help, setup_fn, handler_fn))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append((name, handler, description))

    def register_hook(self, hook_name, callback, *, hold_stream=False):
        self.hooks.append((hook_name, callback))

    def get_config(self, key, default=None):
        return default


@pytest.fixture
def restore_hook_config(monkeypatch):
    monkeypatch.setattr(hooks, "_get_config", hooks._get_config)


@pytest.fixture
def no_router_modules():
    def router_modules():
        return [k for k in sys.modules if k == "polytropos_router" or k.startswith("polytropos_router.")]
    saved = {k: sys.modules.pop(k) for k in router_modules()}
    yield
    for k in router_modules():
        del sys.modules[k]
    sys.modules.update(saved)


def test_register_records_commands_hooks_and_config(tmp_home, restore_hook_config):
    ctx = FakeCtx()
    polytropos.register(ctx)
    assert ctx.cli == [("polytropos", "Polytropos doctor, installer and FreeLLMAPI manager",
                        cli.setup_parser, cli.handle)]
    assert [(name, handler) for name, handler, _ in ctx.commands] == [("polytropos", slash.handle)]
    assert dict(ctx.hooks) == {"pre_llm_call": hooks.on_pre_llm_call, "pre_api_request": hooks.on_pre_api_request,
                               "post_llm_call": hooks.on_post_llm_call, "on_session_end": hooks.on_session_end}
    assert hooks._get_config == ctx.get_config


@pytest.mark.parametrize("folder", ["polytropos-router", "polytropos_router", "model-providers/polytropos-router"])
def test_register_makes_router_config_importable(tmp_home, restore_hook_config, no_router_modules, folder):
    target = tmp_home / ".hermes" / "plugins" / folder
    shutil.copytree(ROOT / "plugins" / "polytropos_router", target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "__init__.py").write_text("raise RuntimeError('the router __init__ ran')\n")
    (target / "plugin.yaml").write_text("name: polytropos-router\nkind: model-provider\n")
    polytropos.register(FakeCtx())
    config = importlib.import_module("polytropos_router.config")
    assert Path(config.__file__).resolve().is_relative_to(target.resolve())


def test_register_adds_no_stub_without_a_router(tmp_home, restore_hook_config, no_router_modules):
    (tmp_home / ".hermes" / "plugins" / "other").mkdir(parents=True)
    (tmp_home / ".hermes" / "plugins" / "other" / "plugin.yaml").write_text("name: other\n")
    polytropos.register(FakeCtx())
    assert "polytropos_router" not in sys.modules


# --- doctor ---

def test_doctor_prints_report_and_exits_1_on_a_failed_check(tmp_home, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "CHECKS", (lambda ctx: [doctor.CheckResult("fail", "broken part")],))
    assert cli.handle(parse("doctor"), cli.Effects(run=FakeRun(), ask=no_questions)) == 1
    out = capsys.readouterr().out
    assert out.startswith("Polytropos doctor") and "broken part" in out
    monkeypatch.setattr(doctor, "CHECKS", (lambda ctx: [doctor.CheckResult("ok", "fine part")],))
    assert cli.handle(parse("doctor"), cli.Effects(run=FakeRun(), ask=no_questions)) == 0


def test_doctor_checks_plugins_in_an_installed_home_without_a_pack_file(tmp_home, monkeypatch, capsys):
    home = tmp_home / ".hermes"
    plugin = home / "plugins" / "polytropos"
    plugin.mkdir(parents=True)
    shutil.copy(ROOT / "versions.lock", plugin)
    monkeypatch.setattr(cli, "_HERE", plugin / "cli.py")  # installed: no checkout and no hermes-pack.yaml around
    (home / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "polytropos-router"}}))
    ponytail, superpowers = LOCK["plugins"]["ponytail"], LOCK["plugins"]["superpowers"]
    meta = {"polytropos": {"source": "https://github.com/Lil-Clanky/polytropos.git#plugins/polytropos",
                           "revision": SHA_NEW},
            "polytropos-router": {"source": "https://github.com/Lil-Clanky/polytropos.git#plugins/polytropos_router",
                                  "revision": SHA_OLD},
            "ponytail": {"source": ponytail["repo"], "revision": ponytail["commit"]},
            "superpowers": {"source": superpowers["repo"] + ".git", "revision": superpowers["commit"]}}
    (home / "plugins" / ".install-metadata.json").write_text(json.dumps(meta))
    monkeypatch.setattr(doctor, "CHECKS", (doctor.check_plugins,))
    assert cli.handle(parse("doctor"), cli.Effects(run=FakeRun(), ask=no_questions)) == 0
    out = capsys.readouterr().out
    assert "ok    plugins: polytropos, ponytail, superpowers" in out
    assert "warn  plugin polytropos-router is at aaaaaaa, the pack pins bbbbbbb" in out
    assert "pack file" not in out
    (home / "plugins" / ".install-metadata.json").write_text("{}")  # as in a run from a checkout
    assert cli.handle(parse("doctor"), cli.Effects(run=FakeRun(), ask=no_questions)) == 1
    out = capsys.readouterr().out
    assert "fail  plugin Lil-Clanky/polytropos#plugins/polytropos is not installed\n        hermes polytropos update" in out
    assert "fail  plugin Lil-Clanky/polytropos#plugins/polytropos_router is not installed" in out


# --- install ---

def test_install_refuses_when_another_program_holds_port_3001(tmp_home, capsys):
    run = FakeRun()
    fx = cli.Effects(run=run, http_get=lambda url, headers: (200, b"<html>not an API</html>"), ask=no_questions)
    assert cli.handle(parse("install"), fx) == 1
    assert "3001" in capsys.readouterr().out
    assert run.commands() == []
    assert load_state().shared == {}


def test_install_only_qmd_installs_only_qmd(tmp_home):
    run = FakeRun()
    fx = cli.Effects(run=run, http_get=lambda url, headers: pytest.fail("FreeLLMAPI probed"), ask=no_questions)
    assert cli.handle(parse("install", "--only", "qmd"), fx) == 0
    qmd = LOCK["optional"]["qmd"]
    assert run.commands() == [["npm", "install", "-g", f"{qmd['package']}@{qmd['version']}"],
                              ["hermes", "skills", "install", "official/research/qmd"]]
    entry = load_state().shared["qmd"]
    assert entry["installed_by_us"] is True and entry["profiles"] == [str(tmp_home / ".hermes")]


class FakeInstallFreeLLM:
    def __init__(self, service_file, writes_service, error):
        self.services = SimpleNamespace(path=lambda: service_file)
        self.writes_service, self.error, self.pins = writes_service, error, []

    def detect(self):
        return Detected(listening=False, is_freellmapi=False, healthy=False, managed=False)

    def install(self, pin):
        self.pins.append(pin)
        if self.writes_service:
            self.services.path().write_text("service")
        if self.error:
            raise self.error


@pytest.mark.parametrize("writes_service, error, recorded", [
    (True, None, True),
    (True, BuildError("FreeLLMAPI did not answer on port 3001 within 60 s"), True),
    (False, BuildError("`npm ci` failed"), False),
])
def test_install_records_managed_mode_once_the_service_exists(tmp_home, writes_service, error, recorded):
    fl = FakeInstallFreeLLM(tmp_home / "service.plist", writes_service, error)
    fx = cli.Effects(run=FakeRun(), ask=no_questions, freellm=fl)
    assert cli.handle(parse("install", "--only", "freellmapi"), fx) == (1 if error else 0)
    assert fl.pins == [Pin(LOCK["freellmapi"]["tag"], LOCK["freellmapi"]["commit"])]
    entry = load_state().shared.get("freellmapi")
    assert (entry is not None and entry["mode"] == "managed" and entry["installed_by_us"] is True) == recorded


def test_install_rejects_unknown_names(tmp_home, capsys):
    run = FakeRun()
    assert cli.handle(parse("install", "--only", "nope"), cli.Effects(run=run, ask=no_questions)) == 1
    assert "nope" in capsys.readouterr().out and run.calls == []


# --- update ---

def pack_text(router_ref=SHA_NEW):
    return yaml.safe_dump({"name": "polytropos", "plugins": [
        {"repo": "Lil-Clanky/polytropos", "ref": SHA_NEW, "subdir": "plugins/polytropos"},
        {"repo": "Lil-Clanky/polytropos", "ref": router_ref, "subdir": "plugins/polytropos_router"},
        {"repo": "DietrichGebert/ponytail", "ref": "c" * 40},
    ]})


def new_lock_text():
    text = (ROOT / "versions.lock").read_text()
    new = text.replace(f'tag = "{LOCK["freellmapi"]["tag"]}"', 'tag = "v0.16.40"').replace(
        f'ref = "{LOCK["stack-canary"]["ref"]}"', f'ref = "{"d" * 40}"')
    assert new.count("v0.16.40") == 1 and "d" * 40 in new
    return new


def fetcher(tag, pack, lock_text):
    urls = {cli.RELEASE_API: json.dumps({"tag_name": tag}).encode(),
            f"{cli.RAW}/{tag}/hermes-pack.yaml": pack.encode(),
            f"{cli.RAW}/{tag}/versions.lock": lock_text.encode()}
    return lambda url: urls[url]


class FakeFreeLLM:
    def __init__(self):
        self.updates = []

    def update(self, pin, *, check=None):
        self.updates.append((pin, check))
        return UpdateResult(True, "v0.16.36", pin.tag, False, f"updated to {pin.tag}")


def pack_install_writes(home, router_ref, seen):
    def effect(argv):
        seen.append(yaml.safe_load(Path(argv[-1]).read_text()))
        meta = {"polytropos": {"source": "https://github.com/Lil-Clanky/polytropos#plugins/polytropos",
                               "revision": SHA_NEW},
                "polytropos-router": {"source": "https://github.com/Lil-Clanky/polytropos#plugins/polytropos_router",
                                      "revision": router_ref}}
        (home / "plugins").mkdir(parents=True, exist_ok=True)
        (home / "plugins" / ".install-metadata.json").write_text(json.dumps(meta))
    return effect


def managed_state(home):
    state = State()
    use_shared(state, "freellmapi", str(home), version="v0.16.36", installed_by_us=True)
    set_mode(state, "freellmapi", "managed")
    save_state(state)


def test_update_goes_on_when_only_ponytail_failed_in_the_pack_install(tmp_home, capsys):
    home, seen = tmp_home / ".hermes", []
    managed_state(home)
    pack_cmd = ("hermes", "plugins", "pack", "install")
    run = FakeRun(versions={"rtk": LOCK["rtk"]["version"], "graphify": LOCK["graphify"]["version"],
                            "ast-grep": LOCK["ast-grep"]["version"]},
                  fail=[pack_cmd], on={pack_cmd: pack_install_writes(home, SHA_NEW, seen)})
    fl = FakeFreeLLM()
    fx = cli.Effects(run=run, fetch=fetcher("v0.2.0", pack_text(), new_lock_text()), ask=lambda q: "y", freellm=fl)
    assert cli.handle(parse("update"), fx) == 0
    cmds = run.commands()
    assert [c[:5] for c in cmds if c[:4] == list(pack_cmd)] == [[*pack_cmd, "--force"]]
    assert seen[0]["plugins"][0]["ref"] == SHA_NEW
    ponytail = home / "plugins" / "ponytail"
    assert ["git", "clone", LOCK["plugins"]["ponytail"]["repo"], str(ponytail)] in cmds
    assert ["hermes", "plugins", "enable", "ponytail"] in cmds
    assert ["uv", "tool", "install", f"git+{LOCK['stack-canary']['repo']}@{'d' * 40}"] in cmds
    assert fl.updates[0][0] == Pin("v0.16.40", LOCK["freellmapi"]["commit"])
    assert "hermes gateway restart" in capsys.readouterr().out


def test_update_stops_when_an_own_plugin_is_at_another_revision(tmp_home, capsys):
    home, seen = tmp_home / ".hermes", []
    managed_state(home)
    pack_cmd = ("hermes", "plugins", "pack", "install")
    run = FakeRun(fail=[pack_cmd], on={pack_cmd: pack_install_writes(home, SHA_OLD, seen)})
    fl = FakeFreeLLM()
    fx = cli.Effects(run=run, fetch=fetcher("v0.2.0", pack_text(), new_lock_text()), ask=lambda q: "y", freellm=fl)
    assert cli.handle(parse("update"), fx) == 1
    assert "polytropos-router is at aaaaaaa" in capsys.readouterr().out
    assert [c for c in run.commands() if c[:4] != list(pack_cmd)] == []
    assert fl.updates == []


# --- uninstall ---

def test_uninstall_keeps_data_without_purge_data(tmp_home, capsys):
    home = tmp_home / ".hermes"
    (home / "plugins" / "polytropos").mkdir(parents=True)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "polytropos-router", "default": "auto"},
        "plugins": {"entries": {"polytropos-router": {"routes": {"simple": ["freellmapi/auto"]}}}}}))
    profile = str(home)
    state = State()
    use_shared(state, "freellmapi", profile, version="v0.16.36", installed_by_us=True)
    set_mode(state, "freellmapi", "managed")
    use_shared(state, "qmd", profile, version=LOCK["optional"]["qmd"]["version"], installed_by_us=True)
    use_shared(state, "graphify", profile, version=LOCK["graphify"]["version"], installed_by_us=False)
    record_config_change(state, profile, R + "routes.simple", None, ["freellmapi/auto"])
    record_config_change(state, profile, "model.base_url", "http://localhost:3001/v1", None)
    record_config_change(state, profile, "model.provider", "custom", "polytropos-router")
    save_state(state)
    root = paths.data_dir() / "freellmapi"
    (root / "data").mkdir(parents=True)
    (root / "data" / "freeapi.db").write_text("db")
    (root / "releases" / "v0.16.36").mkdir(parents=True)
    (root / "current").symlink_to(root / "releases" / "v0.16.36")
    run = FakeRun()
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert (root / "data" / "freeapi.db").read_text() == "db"
    assert not (root / "releases").exists() and not (root / "current").is_symlink()
    cmds = run.commands()
    assert cmds.index(["hermes", "config", "set", "model.provider", "custom"]) < cmds.index(
        ["hermes", "config", "set", "model.base_url", "http://localhost:3001/v1"]) < cmds.index(
        ["hermes", "config", "unset", R + "routes.simple"])
    assert ["npm", "uninstall", "-g", LOCK["optional"]["qmd"]["package"]] in cmds
    assert not any(c[:2] == ["uv", "tool"] for c in cmds)
    after = load_state()
    assert after.shared == {} and config_changes(after, profile) == []
    out = capsys.readouterr().out
    assert "hermes plugins remove polytropos" in out and str(root / "data") in out


# --- setup ---

UPSTREAM = {"base_url": "http://127.0.0.1:3001/v1", "api_key_env": "FREELLMAPI_API_KEY"}


def test_setup_records_config_changes_with_old_values(tmp_home):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "custom", "default": "free-model", "base_url": "http://localhost:3001/v1"},
        "plugins": {"entries": {"polytropos-router": {"routes": {"simple": ["old/model"]}}}}}))
    run, asked = FakeRun(), []
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=lambda q: asked.append(q) or "y")) == 0
    cmds = run.commands()
    assert cmds[0] == ["npx", f"freellmapi@{LOCK['freellmapi']['cli_version']}", "setup-hermes"]
    assert [c[3:] for c in cmds[1:]] == [
        [R + "routes.complex", '["freellmapi/auto"]'], [R + "routes.simple", '["freellmapi/auto"]'],
        [R + "classifier", "freellmapi/auto"], [R + "upstreams.freellmapi", json.dumps(UPSTREAM)],
        ["model.provider", "polytropos-router"], ["model.default", "auto"]]
    assert all(c[:3] == ["hermes", "config", "set"] for c in cmds[1:])
    assert config_changes(load_state(), str(home)) == [
        {"key": R + "routes.complex", "old": None, "new": ["freellmapi/auto"]},
        {"key": R + "routes.simple", "old": ["old/model"], "new": ["freellmapi/auto"]},
        {"key": R + "classifier", "old": None, "new": "freellmapi/auto"},
        {"key": R + "upstreams.freellmapi", "old": None, "new": UPSTREAM},
        {"key": "model.base_url", "old": "http://localhost:3001/v1", "new": None},
        {"key": "model.provider", "old": "custom", "new": "polytropos-router"},
        {"key": "model.default", "old": "free-model", "new": "auto"}]
    assert len(asked) == 1 and "default model" in asked[0]


def test_setup_stops_at_a_failed_key_and_keeps_what_it_recorded(tmp_home, capsys):
    home = tmp_home / ".hermes"
    state = State()
    use_shared(state, "freellmapi", str(home), version="v0.16.36", installed_by_us=False)
    set_mode(state, "freellmapi", "external")
    save_state(state)
    run = FakeRun(fail=[("hermes", "config", "set", "model.provider")])
    answers = iter(["http://192.168.1.5:3001/v1/", "y"])
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=lambda q: next(answers))) == 1
    assert run.commands()[0][-2:] == ["--url", "http://192.168.1.5:3001"]
    changes = config_changes(load_state(), str(home))
    assert [c["key"] for c in changes] == [R + "routes.complex", R + "routes.simple", R + "classifier",
                                           R + "upstreams.freellmapi"]
    assert changes[-1]["new"]["base_url"] == "http://192.168.1.5:3001/v1"
    assert "hermes config set model.provider polytropos-router" in capsys.readouterr().out


# --- slash command ---

class FakeSlashFreeLLM:
    keys = []

    def detect(self):
        return Detected(listening=True, is_freellmapi=True, healthy=False, managed=False)

    def health(self, api_key=None):
        FakeSlashFreeLLM.keys.append(api_key)
        return api_key == "sk-test-secret"


def test_slash_shows_health_guard_and_last_decision(tmp_home, monkeypatch):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  entries:\n    polytropos-router:\n      save_traces: true\n")
    traces = paths.state_dir() / "router-traces.jsonl"
    traces.parent.mkdir(parents=True)
    old = {"time": 1, "tier": "complex", "reason": "default", "slot": "freellmapi/auto", "retried": False,
           "fell_back_from": None}
    last = {"time": 2, "tier": "simple", "reason": "classifier", "slot": "freellmapi/auto", "retried": True,
            "fell_back_from": "local/llama3.2:3b"}
    traces.write_text(json.dumps(old) + "\n" + json.dumps(last) + "\n")
    monkeypatch.setattr(slash, "FreeLLM", FakeSlashFreeLLM)
    monkeypatch.setattr(guard, "read_signals", lambda **kw: guard.Signals("warning", 0, 100 * GB))
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-test-secret")
    out = slash.handle("")
    assert "FreeLLMAPI: healthy" in out and "memory pressure is warning" in out
    assert "simple" in out and "classifier" in out and "local/llama3.2:3b" in out
    assert FakeSlashFreeLLM.keys == ["sk-test-secret"] and "sk-test-secret" not in out
