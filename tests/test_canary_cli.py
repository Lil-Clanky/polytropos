import argparse
import json
import os
import sys

import pytest
import yaml

from polytropos import cli
from polytropos._shared import canary
from polytropos.freellm import UpdateResult
from polytropos.state import State, save_state, set_mode, use_shared
from polytropos_router.config import parse_config

SECRET = "sk-canary-secret-value"
ROUTER = {
    "upstreams": {
        "freellmapi": {"base_url": "http://127.0.0.1:43001/v1", "api_key_env": "FREELLMAPI_API_KEY"},
        "remote": {"base_url": "https://api.example.com/v1", "api_key_env": "REMOTE_KEY"},
        "ollama": {"base_url": "http://127.0.0.1:43002/v1", "local": True, "runtime": "ollama"},
    },
    "routes": {"simple": ["ollama/llama3.2:3b", "freellmapi/auto"],
               "complex": ["freellmapi/auto", "freellmapi/big", "remote/large"]},
    "classifier": "freellmapi/auto",
}

FAKE_CANARY = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))  # its settings: Polytropos passes it no FAKE_* variable
args = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": args, "key_set": os.environ.get("FREELLMAPI_API_KEY") == "{secret}",
                           "provider_key_seen": "OPENAI_API_KEY" in os.environ}}) + "\\n")
models = [args[i + 1] for i, a in enumerate(args) if a == "--model"]
fail = os.path.join(here, "fail")
failing = open(fail).read().split(",") if os.path.exists(fail) else []
rows = [{{"model": m, "check": c, "status": "fail" if m in failing and c == "A3" else "pass", "passed": 1,
          "attempts": 1}} for m in models for c in ("A1", "A2", "A3", "A4", "A5", "A6")]
with open(args[args.index("--summary-json") + 1], "w") as f:
    json.dump({{"endpoint": args[args.index("--openai-endpoint") + 1], "endpoint_kind": "openai", "results": rows}}, f)
"""


@pytest.fixture
def fake_canary(tmp_home, monkeypatch):
    bin_dir = tmp_home / "bin"
    bin_dir.mkdir()
    script = bin_dir / "canary"
    script.write_text(FAKE_CANARY.format(python=sys.executable, secret=SECRET))
    script.chmod(0o755)
    if sys.platform == "win32":  # no shebangs there: a .bat that run_command finds through PATHEXT starts the script
        (bin_dir / "canary.bat").write_text(f'@"{sys.executable}" "{script}" %*\n')
    log = bin_dir / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FREELLMAPI_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider-test-key")  # a provider key that must not reach it
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def write_router_config(tmp_home):
    home = tmp_home / ".hermes"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {"polytropos-router": ROUTER}}}))


def test_run_canary_saves_results_for_loopback_cloud_models(fake_canary, capsys):
    canary.save_local_results({"other/model": "fail"})
    results = cli.run_canary(parse_config(ROUTER))
    assert results == {"freellmapi/auto": "pass", "freellmapi/big": "pass"}
    assert canary.load_local_results() == {"other/model": "fail", "freellmapi/auto": "pass", "freellmapi/big": "pass"}
    [call] = fake_canary()
    argv = call["argv"]
    assert argv[:9] == ["run", "--openai-endpoint", "http://127.0.0.1:43001/v1", "--api-key-env", "FREELLMAPI_API_KEY",
                        "--model", "auto", "--model", "big"]
    assert argv[9:12] == ["--attempts", "1", "--summary-json"]
    assert call["key_set"] and not call["provider_key_seen"] and SECRET not in json.dumps(argv)
    assert "remote" in capsys.readouterr().out


def test_run_canary_only_given_models(fake_canary):
    assert cli.run_canary(parse_config(ROUTER), models=["big"]) == {"freellmapi/big": "pass"}
    assert [a for a in fake_canary()[0]["argv"] if a in ("auto", "big")] == ["big"]


def test_canary_run_command_exits_1_when_a_model_fails(tmp_home, fake_canary, monkeypatch, capsys):
    write_router_config(tmp_home)
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    assert cli.handle(parser.parse_args(["canary", "run"])) == 0
    (tmp_home / "bin" / "fail").write_text("big")
    assert cli.handle(parser.parse_args(["canary", "run"])) == 1
    out = capsys.readouterr().out
    assert "fail" in out and "freellmapi/big" in out and SECRET not in out
    assert canary.load_local_results()["freellmapi/big"] == "fail"


class CheckingFreeLLM:
    """Runs the update's check once the new release answers, as FreeLLM.update does, and rolls back on False."""

    def __init__(self):
        self.checks = []

    def current_tag(self):
        return "v0.16.35"

    def update(self, pin, *, check=None):
        self.checks.append(check())
        if self.checks[-1]:
            return UpdateResult(True, "v0.16.35", pin.tag, False, f"updated to {pin.tag}")
        return UpdateResult(False, "v0.16.35", pin.tag, True, "health check failed; restored v0.16.35")


def update_freellmapi(tmp_home, run=None):
    """`hermes polytropos update freellmapi` on a managed FreeLLMAPI, answering yes. Returns (exit, the fake)."""
    write_router_config(tmp_home)
    state = State()
    use_shared(state, "freellmapi", str(tmp_home / ".hermes"), version="v0.16.35", installed_by_us=True)
    set_mode(state, "freellmapi", "managed")
    save_state(state)
    fl = CheckingFreeLLM()
    fx = cli.Effects(ask=lambda q: "y", freellm=fl)
    if run is not None:
        fx.run = run
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    return cli.handle(parser.parse_args(["update", "freellmapi"]), fx), fl


def test_update_checks_the_first_cloud_model_with_the_canary(tmp_home, fake_canary):
    code, fl = update_freellmapi(tmp_home)
    assert code == 0 and fl.checks == [True]
    assert [a for a in fake_canary()[0]["argv"] if a in ("auto", "big")] == ["auto"]


def test_update_stands_when_stack_canary_is_missing_and_says_why(tmp_home, capsys):
    def no_canary(argv, **kw):
        raise FileNotFoundError(argv[0])

    code, fl = update_freellmapi(tmp_home, run=no_canary)
    out = capsys.readouterr().out
    assert code == 0 and fl.checks == [True] and "FreeLLMAPI: updated to" in out
    assert "stack-canary is not installed" in out and "hermes polytropos install" in out
    assert "The tool-call check was skipped" in out


def test_update_rolls_back_when_the_canary_fails(tmp_home, fake_canary, monkeypatch, capsys):
    (tmp_home / "bin" / "fail").write_text("auto")
    code, fl = update_freellmapi(tmp_home)
    assert code == 1 and fl.checks == [False] and "restored v0.16.35" in capsys.readouterr().out
