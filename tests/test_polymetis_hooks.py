import argparse
import io
import logging
import os
import sys
from pathlib import Path

import pytest
import yaml
from polymetis_data import INDEX, TOOLS

import polymetis
from polymetis import cli, hooks, log, sessions
from polymetis.config import Settings

SECRET = "zebracornflakes"  # a word that must never reach a file


@pytest.fixture
def pm(tmp_path, monkeypatch):
    """The hooks with an editable settings dict and their files under tmp_path."""
    values: dict = {"min_score": 1.0}  # this small index scores lower than a real one
    monkeypatch.setattr(hooks, "_get_config", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(hooks, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(hooks, "_settings", Settings())
    hooks.STORE.clear()
    yield values
    hooks.STORE.clear()


def records(tmp_path):
    return log.read(tmp_path / "data" / log.NAME)


def start(text, *, session="s1", platform="cli"):
    return hooks.on_pre_llm_call(session_id=session, turn_id="t", user_message=text, platform=platform, model="m",
                                 conversation_history=[], is_first_turn=True)


def messages(text, system=INDEX):
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


def api(msgs, *, session="s1", platform="cli", tools=TOOLS):
    hooks.on_pre_api_request(session_id=session, platform=platform, model="m", provider="p",
                             system_prompt=msgs[0]["content"], request_messages=msgs,
                             request={"method": "POST", "body": {"model": "m", "tools": tools}}, tool_count=len(tools))
    hooks.on_post_api_request(session_id=session, response_model="m-answered",
                              response={"usage": {"prompt_tokens": 120, "total_tokens": 150}})


def turn(text, *, session="s1", platform="cli", system=INDEX, first=None, used=()):
    """One whole turn as Hermes runs it; returns what pre_llm_call gave back."""
    out = start(text, session=session, platform=platform)
    sent = text + ("\n\n" + out["context"] if out else "")
    api(messages(first or sent, system), session=session, platform=platform)
    for name in used:
        hooks.on_post_tool_call(tool_name="skill_view", args={"name": name}, session_id=session, status="ok")
    hooks.on_session_end(session_id=session, completed=True, interrupted=False)
    return out


def test_shadow_mode_changes_nothing_and_logs_the_decision(pm, tmp_path):
    turn(f"Please merge my pull request {SECRET}", used=["github-pr-workflow"])
    assert start("merge my pull request", session="s2") is None  # an index is known now, still no tip
    hooks.on_session_end(session_id="s2", completed=True)
    rec = records(tmp_path)[0]
    assert rec["level"] == "lean" and rec["level_reason"] == "match" and rec["tip"] == ["github-pr-workflow"]
    assert rec["tip_given"] is False and rec["apply"] == [] and rec["used_skills"] == ["github-pr-workflow"]
    assert rec["offered_skills"] == 5 and rec["offered_tools"] == len(TOOLS) and rec["answered_by"] == ["m-answered"]
    assert rec["tokens_in"] == 120 and rec["tokens_out"] == 30 and rec["outcome"] == "completed"
    assert rec["session"] != "s1" and len(rec["session"]) == 12


def test_files_hold_no_message_text_and_only_the_user_can_read_them(pm, tmp_path):
    turn(f"Please merge my pull request {SECRET}")
    turn(f"{SECRET} again, merge it")
    files = list((tmp_path / "data").iterdir())
    assert {f.name for f in files} == {log.NAME, "index-cli.json"}
    for f in files:
        assert SECRET not in f.read_text(encoding="utf-8")
        if os.name != "nt":
            assert f.stat().st_mode & 0o777 == 0o600


def test_log_off_writes_no_file(pm, tmp_path):
    pm["log"] = False
    turn("Please merge my pull request")
    assert not (tmp_path / "data").exists()


def test_tips_go_out_only_when_applied(pm, tmp_path):
    pm["apply"] = ["tips"]
    assert turn("Please merge my pull request") is None  # no index known yet on the first turn
    out = turn("Merge the other pull request too")
    assert out == {"context": "<skill-hint>Relevant skill: github-pr-workflow. Load one with skill_view if you need "
                              "it.</skill-hint>"}
    assert [r["tip_given"] for r in records(tmp_path)] == [False, True]


def test_a_new_process_reads_the_last_index_from_disk(pm, tmp_path):
    pm["apply"] = ["tips"]
    turn("Please merge my pull request")
    hooks.STORE.clear()  # as after a restart
    assert start("merge this pull request", session="s9") is not None


def test_the_level_is_sticky_and_upgrades_once(pm, tmp_path):
    turn("Hello, how are you?", first="Hello, how are you?")
    turn("Draw some ascii art for me", first="Hello, how are you?")
    turn("Thanks, that is all", first="Hello, how are you?")
    levels = [(r["level"], r["level_reason"]) for r in records(tmp_path)]
    assert levels == [("none", "no_match"), ("lean", "upgrade"), ("lean", "upgrade")]
    assert records(tmp_path)[2]["chosen"] == ["ascii-art"]


def test_a_platform_level_wins(pm, tmp_path):
    pm["platform_levels"] = {"whatsapp": "full"}
    turn("Hello", platform="whatsapp")
    assert records(tmp_path)[0]["level"] == "full"


def test_corrections_and_repeats_are_counted(pm, tmp_path):
    turn("merge the open pull request now")
    turn("No, merge the open pull request now", first="merge the open pull request now")
    first, second = records(tmp_path)
    assert first["correction"] is False and second["correction"] is True and second["repeat"] is True


def test_the_ablation_arm_gets_no_skills(pm, tmp_path):
    pm["apply"] = ["tips"]
    pm["ablation.share"] = 1.0
    turn("Please merge my pull request")
    assert turn("Merge the other pull request too") is None
    rec = records(tmp_path)[-1]
    assert (rec["arm"], rec["level"], rec["level_reason"], rec["tip"]) == ("ablation", "none", "ablation", [])
    request = {"model": "m", "messages": messages("hi"), "tools": TOOLS}
    out = hooks.on_llm_request(request=request, original_request=request, session_id="s1")
    assert out["source"] == "polymetis" and "skill_view" not in str(out["request"])


def test_middleware_leaves_a_normal_session_alone(pm):
    turn("Please merge my pull request")
    assert hooks.on_llm_request(request={"messages": []}, session_id="s1") is None
    assert hooks.on_llm_request(request={"messages": []}, session_id="unknown") is None


def router_request(text):
    return {"model": "router", "messages": messages(text), "tools": TOOLS, "stream": True}


def test_local_requests_get_the_session_variant(pm, tmp_path):
    pm["apply"] = ["local_tools"]
    pm["local_tools"] = ["terminal", "read_file"]
    turn("Please merge my pull request")  # the first turn decides the level
    start("Merge the other one too")
    msgs = messages("Please merge my pull request")
    api(msgs)
    out = hooks.shape_local(router_request("Please merge my pull request"), {"window": 8192, "fits": lambda r: True})
    assert out["variant"] == "lean"
    assert [t["function"]["name"] for t in out["request"]["tools"]] == ["terminal", "read_file", "skill_view"]
    small = hooks.shape_local(router_request("Please merge my pull request"), {"fits": lambda r: False})
    assert small["variant"] == "bare" and "tools" not in small["request"]
    hooks.on_session_end(session_id="s1", completed=True)
    assert records(tmp_path)[-1]["local"] == "bare"


def test_local_requests_are_left_alone_without_apply_or_a_known_session(pm):
    turn("Please merge my pull request")
    slot = {"fits": lambda r: True}
    assert hooks.shape_local(router_request("Please merge my pull request"), slot) is None
    pm["apply"] = ["local_tools"]
    start("next")
    assert hooks.shape_local(router_request("A conversation Polymetis never saw"), slot) is None
    assert hooks.shape_local(router_request("Please merge my pull request"), {"window": 8192}) is None


def test_a_full_session_that_fits_is_left_alone(pm):
    pm["apply"] = ["local_tools"]
    pm["platform_levels"] = {"cli": "full"}
    turn("Hello")
    assert hooks.shape_local(router_request("Hello"), {"fits": lambda r: True}) is None


def test_a_failing_hook_never_raises_and_never_logs_text(pm, monkeypatch, caplog):
    def boom(*a, **kw):
        raise ValueError(SECRET)
    turn("Please merge my pull request")  # the index is known from here on
    monkeypatch.setattr(hooks, "rank", boom)
    hooks._warn_once.cache_clear()
    with caplog.at_level(logging.WARNING, logger="hermes_plugins.polymetis"):
        assert start("Please merge my pull request", session="s3") is None
    assert "on_pre_llm_call failed: ValueError" in caplog.text and SECRET not in caplog.text


def test_hooks_ignore_turns_they_did_not_see(pm):
    assert hooks.on_pre_api_request(session_id="nobody", system_prompt=INDEX) is None
    assert hooks.on_post_tool_call(tool_name="terminal", session_id="nobody") is None
    assert hooks.on_session_end(session_id="nobody", completed=True) is None
    assert hooks.on_pre_llm_call(session_id="", user_message="hi") is None


def test_fingerprint_matches_the_same_conversation_only():
    a = sessions.fingerprint(messages("hi") + [{"role": "assistant", "content": "hello"}])
    assert a == sessions.fingerprint(messages("hi"))
    assert a != sessions.fingerprint(messages("hi there"))
    assert sessions.fingerprint("not a list") is None and sessions.fingerprint([]) is None


class FakeCtx:
    def __init__(self, data_dir):
        self.hooks, self.middleware, self.commands, self.unload = {}, {}, {}, []
        self.state = type("State", (), {"data_dir": data_dir})()

    def get_config(self, key, default=None):
        return default

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_middleware(self, kind, fn):
        self.middleware[kind] = fn

    def register_cli_command(self, name, help_text, setup, handle):
        self.commands[name] = (setup, handle)

    def on_unload(self, fn):
        self.unload.append(fn)


def test_register_wires_the_hooks_the_manifest_names(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_get_config", hooks._get_config)
    monkeypatch.setattr(hooks, "_data_dir", hooks._data_dir)
    ctx = FakeCtx(tmp_path / "pd")
    polymetis.register(ctx)
    manifest = yaml.safe_load((Path(polymetis.__file__).parent / "plugin.yaml").read_text(encoding="utf-8"))
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert set(ctx.middleware) == {"llm_request"} and set(ctx.commands) == {"polymetis"}
    assert hooks.log_path() == tmp_path / "pd" / log.NAME
    assert sys.modules["polytropos_router_hooks"].local_request["polymetis"] is hooks.shape_local
    for fn in ctx.unload:
        fn()
    assert "polymetis" not in sys.modules["polytropos_router_hooks"].local_request


def test_cli_report(pm, tmp_path):
    turn("Please merge my pull request", used=["github-pr-workflow"])
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    out = io.StringIO()
    assert cli.handle(parser.parse_args(["report", "--days", "7"]), out=out) == 0
    text = out.getvalue()
    assert "Polymetis report, last 7 days: 1 sessions, 1 turns." in text
    assert "Most loaded skills: github-pr-workflow 1." in text
