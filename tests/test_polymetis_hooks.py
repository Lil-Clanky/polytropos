import argparse
import hashlib
import io
import json
import logging
import os
import sys
import threading
from pathlib import Path

import pytest
import yaml
from polymetis_data import INDEX, TOOLS

import polymetis
from polymetis import catalog, cli, hooks, log, sessions
from polymetis.config import Settings

SECRET = "zebracornflakes"  # a word that must never reach a file


@pytest.fixture
def pm(tmp_path, monkeypatch):
    """The hooks with an editable settings dict and their files under tmp_path."""
    values: dict = {"min_score": 1.0}  # this small index scores lower than a real one
    monkeypatch.setattr(hooks, "_get_config", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(hooks, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(hooks, "_settings", Settings())
    monkeypatch.setattr(hooks, "ablation_ready", True)  # as if Hermes loaded the plugin during an ablation
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
    assert start("Merge the other pull request too") is None
    request = {"model": "m", "messages": messages("hi"), "tools": TOOLS}
    out = hooks.on_llm_request(request=request, original_request=request, session_id="s1", turn_id="t")
    assert out["source"] == "polymetis" and "skill_view" not in str(out["request"])
    hooks.on_session_end(session_id="s1", completed=True)
    rec = records(tmp_path)[-1]
    assert (rec["arm"], rec["level"], rec["level_reason"], rec["tip"]) == ("ablation", "none", "ablation", [])


def test_an_ablation_ends_with_its_settings(pm, tmp_path):
    pm["ablation.share"] = 1.0
    turn("Please merge my pull request")
    pm["ablation.until"] = "2020-01-01"  # its last day has passed
    start("Merge the other pull request too")
    request = {"model": "m", "messages": messages("hi"), "tools": TOOLS}
    assert hooks.on_llm_request(request=request, session_id="s1", turn_id="t") is None
    hooks.on_session_end(session_id="s1", completed=True)
    assert [(r["arm"], r["level"]) for r in records(tmp_path)] == [("ablation", "none"), ("normal", "lean")]


def test_an_ablation_takes_only_new_sessions(pm, tmp_path):
    pm["ablation.share"] = 1.0
    hooks.on_pre_llm_call(session_id="s1", turn_id="t", user_message="Merge it", platform="cli", model="m",
                          is_first_turn=False)  # a chat with history, first seen after a restart
    hooks.on_session_end(session_id="s1", completed=True)
    turn("Please merge my pull request", session="s2")
    assert [r["arm"] for r in records(tmp_path)] == ["normal", "ablation"]


def test_an_ablation_set_while_hermes_runs_waits_for_a_restart(pm, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(hooks, "ablation_ready", False)  # Hermes loaded the plugin before the ablation was set
    pm["ablation.share"] = 1.0
    hooks._warn_once.cache_clear()
    with caplog.at_level(logging.WARNING, logger="hermes_plugins.polymetis"):
        turn("Please merge my pull request")
    assert records(tmp_path)[0]["arm"] == "normal" and "restart Hermes" in caplog.text


def test_middleware_leaves_a_normal_session_alone(pm):
    turn("Please merge my pull request")
    assert hooks.on_llm_request(request={"messages": []}, session_id="s1") is None
    assert hooks.on_llm_request(request={"messages": []}, session_id="unknown") is None


def test_the_background_review_is_neither_counted_nor_shaped(pm, tmp_path):
    pm["apply"] = ["local_tools"]
    pm["ablation.share"] = 1.0
    turn("Please merge my pull request")
    start("Merge the other pull request too")  # the review of the last turn runs alongside this one
    review = messages("Please merge my pull request") + [{"role": "user", "content": "Review the conversation"}]
    hooks.on_pre_api_request(session_id="s1", turn_id="review", system_prompt=INDEX, request_messages=review,
                             request={"body": {"tools": TOOLS}}, tool_count=len(TOOLS))
    hooks.on_post_api_request(session_id="s1", turn_id="review", response_model="m-review",
                              response={"usage": {"prompt_tokens": 900, "total_tokens": 1000}})
    hooks.on_post_tool_call(tool_name="memory", args={}, session_id="s1", turn_id="review", status="error")
    slot = {"fits": lambda r: True}
    assert hooks.on_llm_request(request={"messages": review}, session_id="s1", turn_id="review") is None
    assert hooks.shape_local({"messages": review, "tools": TOOLS}, slot) is None
    own = messages("Merge the other pull request too")
    api(own)  # the user's own request, which is counted and shaped
    assert hooks.on_llm_request(request={"messages": own}, session_id="s1", turn_id="t") is not None
    assert hooks.shape_local({"messages": own, "tools": TOOLS}, slot) is not None
    hooks.on_session_end(session_id="s1", completed=True)
    rec = records(tmp_path)[-1]
    assert (rec["api_calls"], rec["tokens_in"], rec["used_tools"], rec["answered_by"]) == (1, 120, {}, ["m-answered"])


def test_only_index_skills_and_tool_names_reach_the_log(pm, tmp_path):
    turn("Please merge my pull request")
    start("Draw some ascii art")
    for name in (SECRET, "ascii-art"):
        hooks.on_post_tool_call(tool_name="skill_view", args={"name": name}, session_id="s1", status="ok")
    hooks.on_post_tool_call(tool_name=f"say {SECRET}", args={}, session_id="s1", status="error")
    hooks.on_session_end(session_id="s1", completed=True)
    rec = records(tmp_path)[-1]
    assert rec["used_skills"] == ["ascii-art"] and rec["used_tools"] == {"skill_view": 2, "other": 1}
    assert SECRET not in (tmp_path / "data" / log.NAME).read_text(encoding="utf-8")


def test_offered_tools_falls_back_to_hermes_count_for_a_large_request(pm, tmp_path):
    start("Please merge my pull request")
    hooks.on_pre_api_request(session_id="s1", system_prompt=INDEX, request_messages=messages("hi"),
                             request={"_truncated": True}, tool_count=42)
    hooks.on_session_end(session_id="s1", completed=True)
    assert records(tmp_path)[0]["offered_tools"] == 42


def test_sessions_saving_the_index_at_once_leave_a_whole_file(pm, tmp_path):
    skills, errors = tuple(catalog.parse_index(INDEX)), []

    def save(n):
        try:
            hooks._save_catalog("cli", skills[:n])
        except Exception as e:  # an error in a thread would otherwise only print
            errors.append(e)
    threads = [threading.Thread(target=save, args=(1 + i % len(skills),)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = json.loads((tmp_path / "data" / "index-cli.json").read_text(encoding="utf-8"))
    assert errors == [] and [r["name"] for r in rows] == [s.name for s in skills[:len(rows)]]
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["index-cli.json"]


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
    assert a == sessions.fingerprint([{"role": "developer", "content": INDEX}, {"role": "user", "content": "hi"}])
    assert a != sessions.fingerprint(messages("hi there"))
    assert a != sessions.fingerprint(messages("hi") + [{"role": "user", "content": "Review the conversation"}])
    assert sessions.fingerprint("not a list") is None and sessions.fingerprint([]) is None


class FakeCtx:
    def __init__(self, data_dir, values=None):
        self.hooks, self.middleware, self.commands, self.unload = {}, {}, {}, []
        self.state = type("State", (), {"data_dir": data_dir})()
        self.values = values or {}

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_middleware(self, kind, fn):
        self.middleware[kind] = fn

    def register_cli_command(self, name, help_text, setup, handle):
        self.commands[name] = (setup, handle)

    def on_unload(self, fn):
        self.unload.append(fn)


def test_register_wires_the_hooks_the_manifest_names(tmp_path, monkeypatch):
    for name in ("_get_config", "_data_dir", "_settings", "ablation_ready"):
        monkeypatch.setattr(hooks, name, getattr(hooks, name))  # register sets them; the test puts them back
    ctx = FakeCtx(tmp_path / "pd")
    polymetis.register(ctx)
    manifest = yaml.safe_load((Path(polymetis.__file__).parent / "plugin.yaml").read_text(encoding="utf-8"))
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert ctx.middleware == {} and hooks.ablation_ready is False  # Hermes copies every request while one is registered
    assert set(ctx.commands) == {"polymetis"} and hooks.log_path() == tmp_path / "pd" / log.NAME
    assert sys.modules["polytropos_router_hooks"].local_request["polymetis"] is hooks.shape_local
    during = FakeCtx(tmp_path / "pd", {"ablation.share": 0.25})
    polymetis.register(during)
    assert set(during.middleware) == {"llm_request"} and hooks.ablation_ready is True
    for fn in ctx.unload + during.unload:
        fn()
    assert "polymetis" not in sys.modules["polytropos_router_hooks"].local_request


def test_without_ctx_state_the_files_go_where_hermes_would_put_them():
    folder = hooks._default_data_dir()  # Hermes names it agent-plugin-<id>-<first 8 hex of sha256(id)>
    assert folder.name == "agent-plugin-polymetis-" + hashlib.sha256(b"polymetis").hexdigest()[:8]
    assert folder.parent.name == "plugin-data"


def test_cli_report(pm, tmp_path):
    turn("Please merge my pull request", used=["github-pr-workflow"])
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    out = io.StringIO()
    assert cli.handle(parser.parse_args(["report", "--days", "7"]), out=out) == 0
    text = out.getvalue()
    assert "Polymetis report, last 7 days: 1 sessions, 1 turns." in text
    assert "Most loaded skills: github-pr-workflow 1." in text
    assert text.endswith(f"Log: {tmp_path / 'data' / log.NAME}\n")
