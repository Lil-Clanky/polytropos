import argparse
import importlib
import json
import os
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
from polytropos.freellm import BuildError, Detected, Pin, PortBusy, UpdateResult
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


def probe(argv):
    """`<name> --version`, or stack-canary's `canary run --help`, which stands in for a version flag."""
    return argv[1:] in (["--version"], ["run", "--help"])


class FakeRun:
    """Records argv. A probe prints versions[name] (the name without its folder and .exe) or raises FileNotFoundError.
    A command that starts with a prefix in `fail` exits 1; `on` maps a prefix to a side effect."""

    def __init__(self, versions=None, fail=(), on=None):
        self.versions, self.fail, self.on, self.calls = versions or {}, fail, on or {}, []

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if probe(argv):
            name = Path(argv[0]).name.removesuffix(".exe")
            if name not in self.versions:
                raise FileNotFoundError(argv[0])
            return SimpleNamespace(returncode=0, stdout=f"{name} {self.versions[name]}\n", stderr="")
        for prefix, effect in self.on.items():
            if argv[:len(prefix)] == list(prefix):
                effect(argv)
        code = 1 if any(argv[:len(p)] == list(p) for p in self.fail) else 0
        if code and kw.get("check"):
            raise subprocess.CalledProcessError(code, argv)
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    def commands(self):
        return [c for c in self.calls if not probe(c)]


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
    (tmp_home / ".hermes").mkdir()
    (tmp_home / ".hermes" / "config.yaml").write_text("model:\n  provider: polytropos-router\n")
    assert cli.handle(parse("doctor"), cli.Effects(run=FakeRun(), ask=no_questions)) == 0


def test_doctor_offers_the_router_as_default_model_after_a_clean_run(tmp_home, monkeypatch, capsys):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "custom", "default": "free-model", "base_url": "http://localhost:3001/v1"}}))
    monkeypatch.setattr(doctor, "CHECKS", (lambda ctx: [doctor.CheckResult("warn", "a warning is no failure")],))
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-test-key")
    run, asked = FakeRun(), []
    assert cli.handle(parse("doctor"), cli.Effects(run=run, ask=lambda q: asked.append(q) or "y")) == 0
    assert asked == ["Make the Polytropos router the default model (provider polytropos-router, model auto)? [y/N] "]
    assert run.commands() == [["hermes", "config", "set", "model.provider", "polytropos-router"],
                              ["hermes", "config", "set", "model.default", "auto"]]
    assert config_changes(load_state(), str(home)) == [
        {"key": "model.base_url", "old": "http://localhost:3001/v1", "new": None},
        {"key": "model.provider", "old": "custom", "new": "polytropos-router"},
        {"key": "model.default", "old": "free-model", "new": "auto"}]

    def no_terminal(question):
        raise EOFError

    run = FakeRun()
    assert cli.handle(parse("doctor"), cli.Effects(run=run, ask=no_terminal)) == 0
    assert run.commands() == []
    assert "To make the Polytropos router the default model, run: hermes polytropos setup" in capsys.readouterr().out
    monkeypatch.delenv("FREELLMAPI_API_KEY")  # without this profile's key the router could not answer
    assert cli.handle(parse("doctor"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == []
    assert "with: hermes config set FREELLMAPI_API_KEY <key>" in capsys.readouterr().out


def test_router_default_removes_model_context_length_and_uninstall_restores_it(tmp_home, monkeypatch):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "custom", "default": "free-model", "context_length": 131072}}))
    monkeypatch.setattr(doctor, "CHECKS", (lambda ctx: [doctor.CheckResult("ok", "fine")],))
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-test-key")
    run = FakeRun()
    assert cli.handle(parse("doctor"), cli.Effects(run=run, ask=lambda q: "y")) == 0
    assert run.commands()[-1] == ["hermes", "config", "unset", "model.context_length"]
    assert config_changes(load_state(), str(home))[-1] == {"key": "model.context_length", "old": 131072, "new": None}
    (home / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "polytropos-router", "default": "auto"}}))
    run = FakeRun()
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands()[0] == ["hermes", "config", "set", "model.context_length", "131072"]


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
    for kept in ({}, {"polytropos-router": meta["polytropos-router"]}):  # as in a run from a checkout
        (home / "plugins" / ".install-metadata.json").write_text(json.dumps(kept))
        assert cli.handle(parse("doctor"), cli.Effects(run=FakeRun(), ask=no_questions)) == 1
        out = capsys.readouterr().out
        assert "fail  plugin Lil-Clanky/polytropos#plugins/polytropos is not installed\n" \
               "        install Polytropos again, see Install in its README" in out
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


def test_adopt_reads_the_checkout_from_from_path(tmp_home, capsys):
    checkout = tmp_home / "checkout"
    args = parse("adopt", "--from", str(checkout))
    assert args.from_path == checkout and not hasattr(args, "source")  # Hermes's own --source has that destination
    adopted = []
    assert cli.handle(args, cli.Effects(freellm=SimpleNamespace(adopt=lambda src, pin: adopted.append(src)))) == 0
    assert adopted == [checkout] and load_state().shared["freellmapi"]["mode"] == "managed"
    assert f"with the data from {checkout}." in capsys.readouterr().out


@pytest.mark.parametrize("platform, hint", [
    ("darwin", ["launchctl list | grep -i freellm", "launchctl bootout gui/$(id -u)/<label>",
                "launchctl disable gui/$(id -u)/<label>"]),
    ("linux", ["systemctl --user disable --now <unit>"]),
])
def test_adopt_says_how_to_stop_the_old_freellmapi_and_stops_nothing(tmp_home, monkeypatch, capsys, platform, hint):
    def busy(src, pin):
        raise PortBusy("port 3001 still answers; stop the old FreeLLMAPI first")

    monkeypatch.setattr(cli.sys, "platform", platform)
    run = FakeRun()
    fx = cli.Effects(run=run, ask=no_questions, freellm=SimpleNamespace(adopt=busy))
    assert cli.handle(parse("adopt", "--from", str(tmp_home / "checkout")), fx) == 1
    out = capsys.readouterr().out
    assert all(line in out for line in hint) and "terminal or a dashboard" in out
    assert run.calls == [] and load_state().shared == {}


def test_a_failed_integration_step_says_the_program_was_not_set_up_for_this_profile(tmp_home, capsys):
    rtk = cli._programs(LOCK)[0].version_cmd[0]  # ~/.local/bin/rtk, rtk.exe on Windows
    run = FakeRun(versions={"rtk": LOCK["rtk"]["version"]}, fail=[(rtk, "init")])
    assert cli.handle(parse("install", "--only", "rtk"), cli.Effects(run=run, ask=no_questions)) == 1
    out = capsys.readouterr().out
    assert "rtk was not set up for this profile" in out and "was not installed" not in out


def test_canary_check_says_when_the_router_plugin_cannot_be_loaded(tmp_home, monkeypatch, capsys):
    def no_router():
        raise ImportError("no polytropos_router")

    monkeypatch.setattr(doctor, "_router", no_router)
    assert cli._canary_check(cli.Effects(run=FakeRun()))() is True  # the update stands
    out = capsys.readouterr().out
    assert "the router plugin could not be loaded" in out and "no cloud model" not in out


# --- update ---

def new_lock_text():
    text = (ROOT / "versions.lock").read_text()
    new = text.replace(f'tag = "{LOCK["freellmapi"]["tag"]}"', 'tag = "v0.16.40"').replace(
        f'ref = "{LOCK["stack-canary"]["ref"]}"', f'ref = "{"d" * 40}"')
    assert new.count("v0.16.40") == 1 and "d" * 40 in new
    return new


class FakeFreeLLM:
    def __init__(self):
        self.updates = []

    def current_tag(self):
        return "v0.16.36"

    def update(self, pin, *, check=None):
        self.updates.append((pin, check))
        return UpdateResult(True, "v0.16.36", pin.tag, False, f"updated to {pin.tag}")


def default_integrations(home):
    """What rtk, graphify and ast-grep add to the default profile, so install and update find nothing missing."""
    for folder in ("plugins/rtk", "skills/graphify", "skills/software-development/ast-grep"):
        (home / folder).mkdir(parents=True, exist_ok=True)


def managed_state(home):
    state = State()
    use_shared(state, "freellmapi", str(home), version="v0.16.36", installed_by_us=True)
    set_mode(state, "freellmapi", "managed")
    save_state(state)


def test_update_moves_ponytail_programs_and_freellmapi_to_the_installed_pins(tmp_home, monkeypatch, capsys):
    installed_lock(monkeypatch, tmp_home, new_lock_text())  # the pins of the installed plugin, nothing from GitHub
    home = tmp_home / ".hermes"
    managed_state(home)
    run = FakeRun(versions={"rtk": LOCK["rtk"]["version"], "graphify": LOCK["graphify"]["version"],
                            "ast-grep": LOCK["ast-grep"]["version"]})
    fl, asked = FakeFreeLLM(), []
    fx = cli.Effects(run=run, ask=lambda q: asked.append(q) or "y", freellm=fl)
    assert cli.handle(parse("update"), fx) == 0
    assert asked[0] == "Update ponytail, the programs and FreeLLMAPI to the versions Polytropos pins? [y/N] "
    cmds = run.commands()
    ponytail = LOCK["plugins"]["ponytail"]
    assert ["hermes", "plugins", "install", ponytail["repo"], "--ref", ponytail["commit"], "--enable"] in cmds
    assert ["uv", "tool", "install", f"git+{LOCK['stack-canary']['repo']}@{'d' * 40}"] in cmds
    assert not [c for c in cmds if c[:2] == ["hermes", "plugins"] and c[3] != ponytail["repo"]]  # no own plugin
    assert fl.updates[0][0] == Pin("v0.16.40", LOCK["freellmapi"]["commit"])
    assert cli.PLUGIN_UPDATES in capsys.readouterr().out


def test_update_never_adds_an_integration_the_profile_lacks(tmp_home):
    home = tmp_home / ".hermes"
    (home / "skills" / "software-development" / "ast-grep").mkdir(parents=True)  # the user removed the other two
    state = State()
    use_shared(state, "stack-canary", str(home), version=LOCK["stack-canary"]["ref"][:7], installed_by_us=True)
    save_state(state)
    versions = {"rtk": LOCK["rtk"]["version"], "graphify": "0.0.1", "ast-grep": LOCK["ast-grep"]["version"],
                "canary": ""}
    run = FakeRun(versions=versions)
    assert cli.handle(parse("update", "programs"), cli.Effects(run=run, ask=lambda q: "y")) == 0
    assert run.commands() == [["uv", "tool", "install", f"graphifyy=={LOCK['graphify']['version']}"]]
    run = FakeRun(versions=versions)  # install adds them
    assert cli.handle(parse("install", "--only", "rtk", "graphify"), cli.Effects(run=run, ask=no_questions)) == 0
    assert [c[1:] for c in run.commands()] == [["init", "--agent", "hermes"],
                                               ["tool", "install", f"graphifyy=={LOCK['graphify']['version']}"],
                                               ["install", "--platform", "hermes"]]


def test_update_asks_before_it_installs_a_missing_ponytail(tmp_home):
    home = tmp_home / ".hermes"
    own = "https://github.com/Lil-Clanky/polytropos.git#plugins/"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / ".install-metadata.json").write_text(json.dumps({
        "polytropos": {"source": own + "polytropos", "revision": SHA_NEW},
        "polytropos-router": {"source": own + "polytropos_router", "revision": SHA_NEW}}))
    answers = {"Update ponytail": "y", "ponytail is not installed": "n"}
    asked = []

    def ask(question):
        asked.append(question)
        return next(a for start, a in answers.items() if question.startswith(start))

    run = FakeRun()
    fx = cli.Effects(run=run, ask=ask)
    cli.handle(parse("update", "all"), fx)
    assert "ponytail is not installed. Install it from its pinned commit? [y/N] " in asked
    assert not [c for c in run.commands() if c[:3] == ["hermes", "plugins", "install"]]


@pytest.mark.parametrize("revision, hint",
                         [(None, False), (SHA_OLD, True), (LOCK["plugins"]["ponytail"]["commit"], False)])
def test_update_and_install_leave_a_ponytail_that_is_there(tmp_home, capsys, revision, hint):
    home = tmp_home / ".hermes"
    pin = LOCK["plugins"]["ponytail"]
    (home / "plugins" / "ponytail" / ".git").mkdir(parents=True)  # without a record: the clone of Polytropos 0.1.1
    own = "https://github.com/Lil-Clanky/polytropos.git#plugins/"
    meta = {"polytropos": {"source": own + "polytropos", "revision": SHA_NEW},
            "polytropos-router": {"source": own + "polytropos_router", "revision": SHA_NEW}}
    if revision:
        meta["ponytail"] = {"source": pin["repo"], "revision": revision, "pinned": True}
    (home / "plugins" / ".install-metadata.json").write_text(json.dumps(meta))
    move = f"hermes plugins install {pin['repo']} --ref {pin['commit']} --force"
    versions = {"rtk": LOCK["rtk"]["version"], "graphify": LOCK["graphify"]["version"],
                "ast-grep": LOCK["ast-grep"]["version"], "canary": ""}
    run = FakeRun(versions=versions)
    assert cli.handle(parse("update"), cli.Effects(run=run, ask=lambda q: "y")) == 0
    assert not [c for c in run.commands() if c[:2] == ["hermes", "plugins"]]
    assert (move in capsys.readouterr().out) == hint
    run = FakeRun()
    assert cli.handle(parse("install", "--only", "ponytail"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == []
    assert (move in capsys.readouterr().out) == hint


def test_a_refused_ponytail_install_does_not_fail_install(tmp_home, capsys):
    run = FakeRun(fail=[("hermes", "plugins", "install")])
    assert cli.handle(parse("install", "--only", "ponytail"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands()[0][:3] == ["hermes", "plugins", "install"] and run.commands()[0][-1] == "--enable"
    out = capsys.readouterr().out
    assert "ponytail was not installed, because Hermes does not accept ponytail's plugin.json yet." in out
    assert "Install finished." in out


def installed_lock(monkeypatch, tmp_home, text):
    """An installed general plugin whose versions.lock holds text, as after a plugin update."""
    plugin = tmp_home / ".hermes" / "plugins" / "polytropos"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "versions.lock").write_text(text)
    monkeypatch.setattr(cli, "_HERE", plugin / "cli.py")


def test_update_programs_uses_the_installed_plugins_pins_and_fetches_nothing(tmp_home, monkeypatch):
    installed_lock(monkeypatch, tmp_home, new_lock_text())
    default_integrations(tmp_home / ".hermes")
    versions = {"rtk": LOCK["rtk"]["version"], "graphify": LOCK["graphify"]["version"],
                "ast-grep": LOCK["ast-grep"]["version"]}
    run = FakeRun(versions=versions)
    assert cli.handle(parse("update", "programs"), cli.Effects(run=run, ask=lambda q: "y")) == 0
    assert run.commands() == [["uv", "tool", "install", f"git+{LOCK['stack-canary']['repo']}@{'d' * 40}"]]
    assert load_state().shared["stack-canary"]["version"] == "d" * 7  # the commit it installed
    run = FakeRun(versions={**versions, "canary": ""})
    assert cli.handle(parse("update", "programs"), cli.Effects(run=run, ask=lambda q: "y")) == 0
    assert run.commands() == []


def test_update_freellmapi_shows_both_tags_and_the_compare_link_before_it_asks(tmp_home, monkeypatch, capsys):
    installed_lock(monkeypatch, tmp_home, new_lock_text())  # the installed plugin's pins; no release is fetched
    managed_state(tmp_home / ".hermes")
    before, fl, run, asked = load_state(), FakeFreeLLM(), FakeRun(), []
    fx = cli.Effects(run=run, ask=lambda q: asked.append(q) or "n", freellm=fl)
    assert cli.handle(parse("update", "freellmapi"), fx) == 0
    out = capsys.readouterr().out
    assert "FreeLLMAPI v0.16.36 is installed, and Polytropos pins FreeLLMAPI v0.16.40." in out
    assert "https://github.com/tashfeenahmed/freellmapi/compare/v0.16.36...v0.16.40" in out
    assert asked == ["Update FreeLLMAPI from v0.16.36 to v0.16.40? [y/N] "]
    assert fl.updates == [] and run.commands() == [] and load_state() == before
    fx.ask = lambda q: "y"
    assert cli.handle(parse("update", "freellmapi"), fx) == 0
    assert [pin for pin, _ in fl.updates] == [Pin("v0.16.40", LOCK["freellmapi"]["commit"])]
    fl.current_tag, fx.ask = lambda: "v0.16.40", no_questions
    assert cli.handle(parse("update", "freellmapi"), fx) == 0
    assert "FreeLLMAPI is already on v0.16.40." in capsys.readouterr().out and len(fl.updates) == 1


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


def test_purge_data_waits_for_the_last_profile_and_a_typed_yes(tmp_home, monkeypatch, capsys):
    first, second = tmp_home / ".hermes", tmp_home / ".hermes" / "profiles" / "work"
    state = State()
    for profile in (first, second):
        use_shared(state, "freellmapi", str(profile), version="v0.16.36", installed_by_us=True)
    set_mode(state, "freellmapi", "managed")
    save_state(state)
    root = paths.data_dir() / "freellmapi"
    (root / "data").mkdir(parents=True)
    (root / "data" / "freeapi.db").write_text("db")
    assert cli.handle(parse("uninstall", "--purge-data"), cli.Effects(run=FakeRun(), ask=no_questions)) == 0
    assert f"FreeLLMAPI is still in use or was not removed, so {root} stays." in capsys.readouterr().out
    assert (root / "data" / "freeapi.db").read_text() == "db"
    monkeypatch.setenv("HERMES_HOME", str(second))
    asked = []
    fx = cli.Effects(run=FakeRun(), ask=lambda q: asked.append(q) or "y")  # only the typed word yes deletes
    assert cli.handle(parse("uninstall", "--purge-data"), fx) == 0
    assert (root / "data" / "freeapi.db").read_text() == "db" and "Nothing was deleted." in capsys.readouterr().out
    assert asked == [f"Type yes to delete {root} with FreeLLMAPI's database, keys and backups: "]
    fx.ask = lambda q: "yes"
    assert cli.handle(parse("uninstall", "--purge-data"), fx) == 0
    assert not root.exists() and f"Deleted {root}." in capsys.readouterr().out


def test_uninstall_removes_programs_without_provider_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider-test-key")
    progs, envs = {p.name: p for p in cli._programs(LOCK)}, []
    fx = cli.Effects(run=lambda argv, **kw: envs.append((argv[:2], kw.get("env"))))
    for name in ("qmd", "graphify"):
        cli._remove_program(progs[name], fx)
    assert [argv for argv, _ in envs] == [["npm", "uninstall"], ["uv", "tool"]]
    assert all("OPENAI_API_KEY" not in env and env["PATH"] == os.environ["PATH"] for _, env in envs)


def test_uninstall_undoes_only_the_integrations_that_install_added(tmp_home):
    home = tmp_home / ".hermes"
    own_skill = home / "skills" / "software-development" / "ast-grep"  # the user's own, there before the install
    own_skill.mkdir(parents=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"mine": {"command": "my-mcp"}}}))

    def skill_installed(argv):
        (home / "skills" / Path(*argv[3].split("/")[1:])).mkdir(parents=True, exist_ok=True)

    def mcp_added(argv):
        config = yaml.safe_load((home / "config.yaml").read_text())
        config["mcp_servers"][argv[3]] = {"command": argv[5]}
        (home / "config.yaml").write_text(yaml.safe_dump(config))

    run = FakeRun(on={("hermes", "skills", "install"): skill_installed, ("hermes", "mcp", "add"): mcp_added,
                      ("graphify", "install"): lambda argv: (home / "skills" / "graphify").mkdir(parents=True)})
    assert cli.handle(parse("install", "--only", "graphify", "ast-grep", "qmd", "context7"),
                      cli.Effects(run=run, ask=no_questions)) == 0
    assert load_state().profiles[str(home)] == {
        "integrations": [
            {"ran": ["graphify", "install", "--platform", "hermes"], "added": str(home / "skills" / "graphify")},
            {"ran": ["hermes", "skills", "install", "official/research/qmd"],
             "added": str(home / "skills" / "research" / "qmd")}],
        "config_changes": [{"key": "mcp_servers.context7", "old": None, "new": {"command": "context7-mcp"}}]}
    run = FakeRun()
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert [c for c in run.commands() if c[0] == "hermes"] == [
        ["hermes", "config", "unset", "mcp_servers.context7"], ["hermes", "skills", "uninstall", "qmd", "--yes"]]
    assert not (home / "skills" / "graphify").exists() and own_skill.is_dir()
    assert str(home) not in load_state().profiles


def test_uninstall_runs_rtks_own_undo_and_leaves_what_lies_outside_the_profile(tmp_home, monkeypatch, capsys):
    home = tmp_home / ".hermes" / "profiles" / "work"
    monkeypatch.setenv("HERMES_HOME", str(home))
    graphify = tmp_home / ".hermes" / "skills" / "graphify"
    graphify.mkdir(parents=True)
    rtk = [str(tmp_home / ".local" / "bin" / "rtk"), "init", "--agent", "hermes"]
    qmd = {"ran": ["hermes", "skills", "install", "official/research/qmd"],
           "added": str(home / "skills" / "research" / "qmd")}
    state = State()
    state.profiles[str(home)] = {"integrations": [
        {"ran": rtk, "added": str(home / "plugins" / "rtk")},
        {"ran": ["graphify", "install", "--platform", "hermes"], "added": str(graphify)}, qmd]}
    save_state(state)
    run = FakeRun(fail=[("hermes", "skills", "uninstall")])
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 1  # the qmd undo failed
    assert run.commands() == [[*rtk, "--uninstall"], ["hermes", "skills", "uninstall", "qmd", "--yes"]]
    out = capsys.readouterr().out
    assert graphify.is_dir() and f"added {graphify}, which is outside this profile" in out
    assert "Run this command yourself: hermes skills uninstall qmd --yes" in out
    assert load_state().profiles[str(home)] == {"integrations": [qmd]}


def test_install_records_setup_opencode_when_it_changes_opencodes_config(tmp_home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_home / "config"))
    opencode_json = tmp_home / "config" / "opencode" / "opencode.json"
    opencode_json.parent.mkdir(parents=True)
    opencode_json.write_text('{"theme": "mine"}')  # the user's own OpenCode settings, there before the install
    os.utime(opencode_json, (1, 1))
    run = FakeRun(on={("npx",): lambda argv: opencode_json.write_text('{"theme": "mine", "provider": {}}')})
    assert cli.handle(parse("install", "--only", "opencode"), cli.Effects(run=run, ask=no_questions)) == 0
    setup = ["npx", "-y", f"freellmapi@{LOCK['freellmapi']['cli_version']}", "setup-opencode"]
    assert load_state().profiles[str(tmp_home / ".hermes")] == {
        "integrations": [{"ran": setup, "added": str(opencode_json)}]}


def test_uninstall_prints_a_note_for_a_recorded_setup_opencode_and_runs_nothing(tmp_home, capsys):
    home = tmp_home / ".hermes"
    opencode_json = tmp_home / ".config" / "opencode" / "opencode.json"
    state = State()
    state.profiles[str(home)] = {"integrations": [
        {"ran": ["npx", "-y", "freellmapi@1.0.0", "setup-opencode"], "added": str(opencode_json)}]}
    save_state(state)
    run = FakeRun()
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == []
    out = capsys.readouterr().out
    auth = tmp_home / ".local" / "share" / "opencode" / "auth.json"  # $XDG_DATA_HOME, as setup-opencode reads it
    assert f"setup-opencode added the freellmapi provider to {opencode_json}" in out
    assert f"stored FreeLLMAPI's key under freellmapi in {auth}" in out
    assert "remove that provider and that entry and change the model there by hand" in out
    assert str(home) not in load_state().profiles


def test_uninstall_exits_1_and_keeps_the_record_when_a_restore_fails(tmp_home, capsys):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "polytropos-router", "default": "auto"}}))
    state = State()
    record_config_change(state, str(home), "model.provider", "custom", "polytropos-router")
    record_config_change(state, str(home), "model.default", "free-model", "auto")
    save_state(state)
    run = FakeRun(fail=[("hermes", "config", "set", "model.provider")])
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 1
    assert run.commands() == [["hermes", "config", "set", "model.default", "free-model"],
                              ["hermes", "config", "set", "model.provider", "custom"]]
    assert "Restoring model.provider failed. Run this command yourself: hermes config set model.provider custom" \
           in capsys.readouterr().out
    assert config_changes(load_state(), str(home)) == [
        {"key": "model.provider", "old": "custom", "new": "polytropos-router"}]


def test_install_leaves_an_mcp_entry_that_was_there_before_unrecorded(tmp_home):
    home = tmp_home / ".hermes"
    home.mkdir()
    mine = {"command": "my-context7", "env": {"CONTEXT7_API_KEY": "ctx7-test-secret"}}
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"context7": mine}}))

    def mcp_added(argv):
        (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {argv[3]: {"command": argv[5]}}}))

    run = FakeRun(on={("hermes", "mcp", "add"): mcp_added})
    assert cli.handle(parse("install", "--only", "context7"), cli.Effects(run=run, ask=no_questions)) == 0
    assert "config_changes" not in load_state().profiles.get(str(home), {})
    assert not any(c[:3] == ["hermes", "mcp", "add"] for c in run.commands())  # Hermes would ask to overwrite it
    assert yaml.safe_load((home / "config.yaml").read_text())["mcp_servers"]["context7"] == mine


def test_hermes_commands_with_captured_output_get_no_terminal_input():
    seen = []

    def run(argv, **kw):
        seen.append(kw)
        return SimpleNamespace(returncode=0, stdout="Hermes Agent v0.23.2\n")

    assert cli._hermes_version(cli.Effects(run=run)) == "0.23.2"
    assert seen[0]["stdin"] is subprocess.DEVNULL  # a prompt fails at once instead of waiting unseen


def test_install_sets_up_a_second_profile_and_uninstall_undoes_only_its_own(tmp_home, monkeypatch):
    def skill_installed(argv):
        (cli.hermes_home() / "skills" / Path(*argv[3].split("/")[1:])).mkdir(parents=True, exist_ok=True)

    def mcp_added(argv):
        config = {"mcp_servers": {argv[3]: {"command": argv[5]}}}
        (cli.hermes_home() / "config.yaml").write_text(yaml.safe_dump(config))

    on = {("hermes", "skills", "install"): skill_installed, ("hermes", "mcp", "add"): mcp_added}
    first, second = tmp_home / ".hermes", tmp_home / ".hermes" / "profiles" / "work"
    second.mkdir(parents=True)
    names = ("install", "--only", "ast-grep", "context7")
    assert cli.handle(parse(*names), cli.Effects(run=FakeRun(on=on), ask=no_questions)) == 0
    monkeypatch.setenv("HERMES_HOME", str(second))
    run = FakeRun(versions={"ast-grep": LOCK["ast-grep"]["version"],
                            "context7-mcp": LOCK["optional"]["context7"]["version"]}, on=on)  # installed now
    assert cli.handle(parse(*names), cli.Effects(run=run, ask=no_questions)) == 0
    skill = ["hermes", "skills", "install", "official/software-development/ast-grep"]
    assert run.commands() == [skill, ["hermes", "mcp", "add", "context7", "--command", "context7-mcp"]]
    state = load_state()
    assert state.profiles[str(second)] == {
        "integrations": [{"ran": skill, "added": str(second / "skills" / "software-development" / "ast-grep")}],
        "config_changes": [{"key": "mcp_servers.context7", "old": None, "new": {"command": "context7-mcp"}}]}
    assert state.shared["ast-grep"]["profiles"] == state.shared["context7"]["profiles"] == [str(first), str(second)]
    run = FakeRun()
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == [["hermes", "config", "unset", "mcp_servers.context7"],
                              ["hermes", "skills", "uninstall", "ast-grep", "--yes"]]  # the programs stay for first
    assert str(second) not in load_state().profiles and len(load_state().profiles[str(first)]["integrations"]) == 1


def missing(argv):
    raise FileNotFoundError(f"[Errno 2] No such file or directory: {argv[0]!r}")


def test_commands_that_cannot_start_are_reported_and_keep_the_state(tmp_home, monkeypatch, capsys):
    home = tmp_home / ".hermes"
    home.mkdir()
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-test-key")

    def written(argv):  # what `hermes config set` does
        router = {"routes": {"complex": json.loads(argv[4])}}
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {"polytropos-router": router}}}))

    run = FakeRun(on={("hermes", "config", "set", R + "routes.complex"): written,
                      ("hermes", "config", "set", R + "routes.simple"): missing})
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=lambda q: "n")) == 1
    out = capsys.readouterr().out
    assert "Setting plugins.entries.polytropos-router.routes.simple failed" in out and "'hermes'" in out
    assert [c["key"] for c in config_changes(load_state(), str(home))] == [R + "routes.complex"]
    run = FakeRun(on={("hermes", "config"): missing})
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 1
    assert f"Restoring {R}routes.complex failed" in capsys.readouterr().out
    assert [c["key"] for c in config_changes(load_state(), str(home))] == [R + "routes.complex"]
    fx = cli.Effects(run=FakeRun(fail=[("npm",)]), ask=no_questions)
    assert cli.handle(parse("install", "--only", "qmd"), fx) == 1
    assert "qmd was not installed: Command '['npm', 'install', '-g'" in capsys.readouterr().out


# --- setup ---

UPSTREAM = {"base_url": "http://127.0.0.1:3001/v1", "api_key_env": "FREELLMAPI_API_KEY"}
USER_MODEL = {"provider": "openrouter", "default": "some/model", "api_key": "sk-user-test-key"}
# What FreeLLMAPI's setup-hermes, which the setup of Polytropos 0.1.1 ran, left in the model block
# (~/freellmapi/cli/src/tools.ts, function hermes).
FREELLM_MODEL = {"provider": "custom", "default": "free-model", "base_url": "http://127.0.0.1:3001/v1",
                 "api_key": "${FREELLMAPI_API_KEY}", "api_mode": "chat_completions", "context_length": 131072,
                 "default_headers": {"User-Agent": "hermes-agent"}}


def test_setup_stops_without_the_key(tmp_home, monkeypatch, capsys):
    monkeypatch.delenv("FREELLMAPI_API_KEY", raising=False)
    run = FakeRun()
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=no_questions)) == 1
    assert run.commands() == []
    assert "  hermes config set FREELLMAPI_API_KEY <key>" in capsys.readouterr().out
    assert load_state().profiles == {}


def test_setup_writes_single_keys_and_records_no_secret(tmp_home, monkeypatch):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": USER_MODEL, "plugins": {"entries": {"polytropos-router": {"routes": {"simple": ["old/model"]}}}}}))
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-test-key")
    run, asked = FakeRun(), []
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=lambda q: asked.append(q) or "y")) == 0
    assert asked == ["Make the Polytropos router the default model (provider polytropos-router, model auto)? [y/N] "]
    assert run.commands() == [["hermes", "config", "set", key, value] for key, value in (
        (R + "routes.complex", '["freellmapi/auto"]'), (R + "routes.simple", '["freellmapi/auto"]'),
        (R + "classifier", "freellmapi/auto"), (R + "upstreams.freellmapi", json.dumps(UPSTREAM)),
        ("model.provider", "polytropos-router"), ("model.default", "auto"))]
    assert config_changes(load_state(), str(home)) == [
        {"key": R + "routes.complex", "old": None, "new": ["freellmapi/auto"]},
        {"key": R + "routes.simple", "old": ["old/model"], "new": ["freellmapi/auto"]},
        {"key": R + "classifier", "old": None, "new": "freellmapi/auto"},
        {"key": R + "upstreams.freellmapi", "old": None, "new": UPSTREAM},
        {"key": "model.provider", "old": "openrouter", "new": "polytropos-router"},
        {"key": "model.default", "old": "some/model", "new": "auto"}]
    assert USER_MODEL["api_key"] not in (paths.state_dir() / "state.json").read_text()


def test_uninstall_of_a_legacy_model_record_writes_no_secret_back(tmp_home, capsys):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"model": {
        "provider": "polytropos-router", "default": "auto", "api_key": "${FREELLMAPI_API_KEY}",
        "default_headers": FREELLM_MODEL["default_headers"]}}))
    state = State()  # as the setup of Polytropos 0.1.1 left it
    record_config_change(state, str(home), "model", {**USER_MODEL, "key_env": "MY_PROVIDER_KEY"}, FREELLM_MODEL)
    for key, old, new in (("model.base_url", FREELLM_MODEL["base_url"], None),
                          ("model.api_mode", "chat_completions", None),
                          ("model.provider", "custom", "polytropos-router"),
                          ("model.default", "free-model", "auto"), ("model.context_length", 131072, None)):
        record_config_change(state, str(home), key, old, new)
    save_state(state)
    run = FakeRun()
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == [["hermes", "config", "unset", "model.api_key"],
                              ["hermes", "config", "set", "model.provider", "openrouter"],
                              ["hermes", "config", "set", "model.key_env", "MY_PROVIDER_KEY"],
                              ["hermes", "config", "unset", "model.default_headers"],
                              ["hermes", "config", "set", "model.default", "some/model"]]
    out = capsys.readouterr().out
    assert "held an api_key, which uninstall does not write back" in out and USER_MODEL["api_key"] not in out
    assert str(home) not in load_state().profiles
    assert USER_MODEL["api_key"] not in (paths.state_dir() / "state.json").read_text()


def test_uninstall_brings_back_a_model_that_was_a_string(tmp_home, capsys):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"model": {**FREELLM_MODEL, "api_key": "sk-own-since"}}))
    state = State()
    record_config_change(state, str(home), "model", "openrouter/some-model", FREELLM_MODEL)
    save_state(state)
    run = FakeRun()
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == [["hermes", "config", "unset", "model.provider"],  # model.api_key was set since: it stays
                              ["hermes", "config", "unset", "model.default_headers"],
                              ["hermes", "config", "set", "model.default", "openrouter/some-model"],
                              ["hermes", "config", "unset", "model.context_length"],
                              ["hermes", "config", "unset", "model.base_url"],
                              ["hermes", "config", "unset", "model.api_mode"]]
    assert "api_key" not in capsys.readouterr().out


def test_setup_stops_at_a_failed_key_and_keeps_what_it_recorded(tmp_home, monkeypatch, capsys):
    home = tmp_home / ".hermes"
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-test-key")
    state = State()
    use_shared(state, "freellmapi", str(home), version="v0.16.36", installed_by_us=False)
    set_mode(state, "freellmapi", "external")
    save_state(state)
    run = FakeRun(fail=[("hermes", "config", "set", "model.provider")])
    answers = iter(["http://192.0.2.5:3001/v1/", "y"])
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=lambda q: next(answers))) == 1
    changes = config_changes(load_state(), str(home))
    assert [c["key"] for c in changes] == [R + "routes.complex", R + "routes.simple", R + "classifier",
                                           R + "upstreams.freellmapi"]
    assert changes[-1]["new"]["base_url"] == "http://192.0.2.5:3001/v1"
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


def test_uninstall_deletes_rtk_exe_on_windows(tmp_home):
    rtk = {p.name: p for p in cli.programs.programs(LOCK, platform="win32", machine="AMD64")}["rtk"]
    exe = tmp_home / ".local" / "bin" / "rtk.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    cli._remove_program(rtk, cli.Effects(run=FakeRun()))
    assert not exe.exists()
