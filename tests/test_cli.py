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


def probe(argv):
    """`<name> --version`, or stack-canary's `canary run --help`, which stands in for a version flag."""
    return argv[1:] in (["--version"], ["run", "--help"])


class FakeRun:
    """Records argv. A probe prints versions[name] (the name without its folder) or raises FileNotFoundError.
    A command that starts with a prefix in `fail` exits 1; `on` maps a prefix to a side effect. As in Hermes v0.23.2
    (hermes_cli/config_section_guard.py), setting the bare model key without --force exits 1 over a mapping."""

    def __init__(self, versions=None, fail=(), on=None):
        self.versions, self.fail, self.on, self.calls = versions or {}, fail, on or {}, []

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if probe(argv):
            name = Path(argv[0]).name
            if name not in self.versions:
                raise FileNotFoundError(argv[0])
            return SimpleNamespace(returncode=0, stdout=f"{name} {self.versions[name]}\n", stderr="")
        for prefix, effect in self.on.items():
            if argv[:len(prefix)] == list(prefix):
                effect(argv)
        guarded = argv[:4] == ["hermes", "config", "set", "model"] and isinstance(
            cli._read_config(Path(os.environ["HERMES_HOME"])).get("model"), dict)  # never the real ~/.hermes
        code = 1 if guarded or any(argv[:len(p)] == list(p) for p in self.fail) else 0
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
               "        hermes polytropos update" in out
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

    def current_tag(self):
        return "v0.16.36"

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


def default_integrations(home):
    """What rtk, graphify and ast-grep add to the default profile, so install and update find nothing missing."""
    for folder in ("plugins/rtk", "skills/graphify", "skills/software-development/ast-grep"):
        (home / folder).mkdir(parents=True, exist_ok=True)


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


def test_update_repairs_a_missing_pack_plugin_and_leaves_a_complete_home_alone(tmp_home, capsys):
    home = tmp_home / ".hermes"
    superpowers = LOCK["plugins"]["superpowers"]
    pack = yaml.safe_load(pack_text())
    pack["plugins"].append({"repo": "obra/superpowers", "ref": superpowers["commit"]})
    own = "https://github.com/Lil-Clanky/polytropos.git#plugins/"
    meta = {"polytropos": {"source": own + "polytropos", "revision": SHA_NEW},
            "polytropos-router": {"source": own + "polytropos_router", "revision": SHA_NEW},
            "ponytail": {"source": "https://github.com/DietrichGebert/ponytail.git", "revision": "c" * 40}}
    (home / "plugins" / "ponytail").mkdir(parents=True)
    default_integrations(home)
    meta_path = home / "plugins" / ".install-metadata.json"
    meta_path.write_text(json.dumps(meta))
    versions = {"rtk": LOCK["rtk"]["version"], "graphify": LOCK["graphify"]["version"],
                "ast-grep": LOCK["ast-grep"]["version"], "canary": ""}
    run, asked = FakeRun(versions=versions), []
    fx = cli.Effects(run=run, fetch=fetcher("v0.2.0", yaml.safe_dump(pack), new_lock_text()),
                     ask=lambda q: asked.append(q) or "y")
    assert cli.handle(parse("update"), fx) == 0
    assert "  changes: plugin obra/superpowers is not installed" in capsys.readouterr().out and len(asked) == 1
    pack_cmd = ["hermes", "plugins", "pack", "install", "--force"]
    assert [c[:5] for c in run.commands() if c[:2] == ["hermes", "plugins"] or c[0] == "git"] == [pack_cmd]

    meta["superpowers"] = {"source": "https://github.com/obra/superpowers.git", "revision": superpowers["commit"]}
    meta_path.write_text(json.dumps(meta))
    same_lock = (ROOT / "versions.lock").read_text()  # nothing new: the release pins what is installed
    state = load_state()  # stack-canary too, at the commit that install records
    use_shared(state, "stack-canary", str(home), version=LOCK["stack-canary"]["ref"][:7], installed_by_us=True)
    save_state(state)
    fx.run, fx.fetch = (run := FakeRun(versions=versions)), fetcher("v0.2.0", yaml.safe_dump(pack), same_lock)
    assert cli.handle(parse("update"), fx) == 0
    out = capsys.readouterr().out
    assert "changes:" not in out and "hermes gateway restart" not in out
    assert run.commands() == []


def test_update_and_install_move_a_ponytail_clone_to_the_lock_commit(tmp_home):
    home = tmp_home / ".hermes"
    folder = home / "plugins" / "ponytail"
    (folder / ".git").mkdir(parents=True)  # programs.ponytail_fallback's clone, which Hermes keeps no record of
    own = "https://github.com/Lil-Clanky/polytropos.git#plugins/"
    (home / "plugins" / ".install-metadata.json").write_text(json.dumps({
        "polytropos": {"source": own + "polytropos", "revision": SHA_NEW},
        "polytropos-router": {"source": own + "polytropos_router", "revision": SHA_NEW}}))
    commit = LOCK["plugins"]["ponytail"]["commit"]
    head = ["git", "-C", str(folder), "rev-parse", "HEAD"]  # FakeRun prints nothing, so the clone is elsewhere
    move = [head, ["git", "-C", str(folder), "fetch", "--depth", "1", "origin", commit],
            ["git", "-C", str(folder), "checkout", commit]]
    versions = {"rtk": LOCK["rtk"]["version"], "graphify": LOCK["graphify"]["version"],
                "ast-grep": LOCK["ast-grep"]["version"], "canary": ""}
    run = FakeRun(versions=versions)
    fx = cli.Effects(run=run, fetch=fetcher("v0.2.0", pack_text(), (ROOT / "versions.lock").read_text()),
                     ask=lambda q: "y")
    assert cli.handle(parse("update"), fx) == 0
    assert [c for c in run.commands() if c[0] == "git"] == [head, *move]  # the plugin check reads HEAD first
    run = FakeRun()
    assert cli.handle(parse("install", "--only", "ponytail"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == move


def installed_lock(monkeypatch, tmp_home, text):
    """An installed general plugin whose versions.lock holds text, as after a plugin update."""
    plugin = tmp_home / ".hermes" / "plugins" / "polytropos"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "versions.lock").write_text(text)
    monkeypatch.setattr(cli, "_HERE", plugin / "cli.py")


def no_fetch(url):
    pytest.fail(f"fetched {url}")


def test_update_programs_uses_the_installed_plugins_pins_and_fetches_nothing(tmp_home, monkeypatch):
    installed_lock(monkeypatch, tmp_home, new_lock_text())
    default_integrations(tmp_home / ".hermes")
    versions = {"rtk": LOCK["rtk"]["version"], "graphify": LOCK["graphify"]["version"],
                "ast-grep": LOCK["ast-grep"]["version"]}
    run = FakeRun(versions=versions)
    assert cli.handle(parse("update", "programs"), cli.Effects(run=run, fetch=no_fetch, ask=lambda q: "y")) == 0
    assert run.commands() == [["uv", "tool", "install", f"git+{LOCK['stack-canary']['repo']}@{'d' * 40}"]]
    assert load_state().shared["stack-canary"]["version"] == "d" * 7  # the commit it installed
    run = FakeRun(versions={**versions, "canary": ""})
    assert cli.handle(parse("update", "programs"), cli.Effects(run=run, fetch=no_fetch, ask=lambda q: "y")) == 0
    assert run.commands() == []


def test_update_freellmapi_shows_both_tags_and_the_compare_link_before_it_asks(tmp_home, monkeypatch, capsys):
    installed_lock(monkeypatch, tmp_home, new_lock_text())  # the installed plugin's pins; no release is fetched
    managed_state(tmp_home / ".hermes")
    before, fl, run, asked = load_state(), FakeFreeLLM(), FakeRun(), []
    fx = cli.Effects(run=run, fetch=no_fetch, ask=lambda q: asked.append(q) or "n", freellm=fl)
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
    record_config_change(state, profile, "model", USER_MODEL, FREELLM_MODEL)
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
        ["hermes", "config", "unset", R + "routes.simple"]) < cmds.index(
        ["hermes", "config", "set", "--force", "model", json.dumps(USER_MODEL)])  # the user's own block comes last
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
    record_config_change(state, str(home), "model", USER_MODEL, FREELLM_MODEL)
    record_config_change(state, str(home), "model.default", "free-model", "auto")
    save_state(state)
    run = FakeRun(fail=[("hermes", "config", "set", "--force", "model")])
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 1
    assert run.commands()[-1] == ["hermes", "config", "set", "--force", "model", json.dumps(USER_MODEL)]
    out = capsys.readouterr().out
    assert "Restoring model failed" in out and USER_MODEL["api_key"] not in out  # a model block can hold a key
    assert config_changes(load_state(), str(home)) == [{"key": "model", "old": USER_MODEL, "new": FREELLM_MODEL}]


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


def test_commands_that_cannot_start_are_reported_and_keep_the_state(tmp_home, capsys):
    home = tmp_home / ".hermes"
    home.mkdir()
    assert cli.handle(parse("setup"), cli.Effects(run=FakeRun(on={("npx",): missing}), ask=no_questions)) == 1
    assert "npx could not run: [Errno 2] No such file or directory: 'npx'" in capsys.readouterr().out

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
    run = FakeRun(on={("hermes", "plugins"): missing})
    fx = cli.Effects(run=run, fetch=fetcher("v0.2.0", pack_text(), new_lock_text()), ask=lambda q: "y")
    assert cli.handle(parse("update"), fx) == 1
    out = capsys.readouterr().out
    assert "No such file or directory: 'hermes'" in out and "The plugin update did not finish" in out
    fx = cli.Effects(run=FakeRun(fail=[("npm",)]), ask=no_questions)
    assert cli.handle(parse("install", "--only", "qmd"), fx) == 1
    assert "qmd was not installed: Command '['npm', 'install', '-g'" in capsys.readouterr().out


# --- setup ---

UPSTREAM = {"base_url": "http://127.0.0.1:3001/v1", "api_key_env": "FREELLMAPI_API_KEY"}
USER_MODEL = {"provider": "openrouter", "default": "some/model", "api_key": "sk-user-test-key"}
# What FreeLLMAPI's setup-hermes leaves in the model block (~/freellmapi/cli/src/tools.ts, function hermes).
FREELLM_MODEL = {"provider": "custom", "default": "free-model", "base_url": "http://127.0.0.1:3001/v1",
                 "api_key": "${FREELLMAPI_API_KEY}", "api_mode": "chat_completions", "context_length": 131072,
                 "default_headers": {"User-Agent": "hermes-agent"}}


def setup_hermes_rewrites_model(home):
    def effect(argv):
        config = yaml.safe_load((home / "config.yaml").read_text())
        config["model"] = FREELLM_MODEL
        (home / "config.yaml").write_text(yaml.safe_dump(config))
    return effect


def test_setup_records_the_users_model_before_setup_hermes_rewrites_it(tmp_home):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": USER_MODEL, "plugins": {"entries": {"polytropos-router": {"routes": {"simple": ["old/model"]}}}}}))
    run, asked = FakeRun(on={("npx",): setup_hermes_rewrites_model(home)}), []
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=lambda q: asked.append(q) or "y")) == 0
    cmds = run.commands()
    assert cmds[0] == ["npx", f"freellmapi@{LOCK['freellmapi']['cli_version']}", "setup-hermes"]
    assert [c[3:] for c in cmds[1:]] == [
        [R + "routes.complex", '["freellmapi/auto"]'], [R + "routes.simple", '["freellmapi/auto"]'],
        [R + "classifier", "freellmapi/auto"], [R + "upstreams.freellmapi", json.dumps(UPSTREAM)],
        ["model.provider", "polytropos-router"], ["model.default", "auto"]]
    assert all(c[:3] == ["hermes", "config", "set"] for c in cmds[1:])
    assert config_changes(load_state(), str(home)) == [
        {"key": "model", "old": USER_MODEL, "new": FREELLM_MODEL},
        {"key": R + "routes.complex", "old": None, "new": ["freellmapi/auto"]},
        {"key": R + "routes.simple", "old": ["old/model"], "new": ["freellmapi/auto"]},
        {"key": R + "classifier", "old": None, "new": "freellmapi/auto"},
        {"key": R + "upstreams.freellmapi", "old": None, "new": UPSTREAM},
        {"key": "model.base_url", "old": FREELLM_MODEL["base_url"], "new": None},
        {"key": "model.api_mode", "old": "chat_completions", "new": None},
        {"key": "model.provider", "old": "custom", "new": "polytropos-router"},
        {"key": "model.default", "old": "free-model", "new": "auto"}]
    assert len(asked) == 1 and "default model" in asked[0]
    run = FakeRun()  # the fake config set changed nothing, so only the model block differs from its old value
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    [restore] = run.commands()
    assert restore[:5] == ["hermes", "config", "set", "--force", "model"] and json.loads(restore[5]) == USER_MODEL


def test_uninstall_brings_back_a_model_that_was_a_string(tmp_home):
    home = tmp_home / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"model": "openrouter/some-model"}))
    run = FakeRun(on={("npx",): setup_hermes_rewrites_model(home)})
    assert cli.handle(parse("setup"), cli.Effects(run=run, ask=lambda q: "n")) == 0
    assert config_changes(load_state(), str(home))[0] == {
        "key": "model", "old": "openrouter/some-model", "new": FREELLM_MODEL}
    run = FakeRun()  # without --force, Hermes would put the string into model.default and keep FreeLLMAPI's keys
    assert cli.handle(parse("uninstall"), cli.Effects(run=run, ask=no_questions)) == 0
    assert run.commands() == [["hermes", "config", "set", "--force", "model", "openrouter/some-model"]]


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
