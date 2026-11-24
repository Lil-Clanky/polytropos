"""Polymetis and polytropos-router together: the router asks Polymetis for a local model's request."""

import json
import sys
import types

import pytest
from polymetis_data import INDEX, TOOLS
from test_router_client import CLS, MODEL, SIMPLE, cloud, config, fresh_state, local, router  # noqa: F401

import polymetis
from polymetis import hooks, log
from polymetis.config import Settings


@pytest.fixture
def linked(tmp_path, monkeypatch):
    """Polymetis registered with the router's hook point, as Hermes would load it."""
    monkeypatch.setitem(sys.modules, "polytropos_router_hooks", types.ModuleType("polytropos_router_hooks"))
    values = {"apply": ["local_tools"], "min_score": 1.0, "local_tools": ["terminal"]}
    monkeypatch.setattr(hooks, "_get_config", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(hooks, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(hooks, "_settings", Settings())
    hooks.STORE.clear()
    polymetis._router_hooks()["polymetis"] = hooks.shape_local
    yield values
    hooks.STORE.clear()


def hermes_turn(text, messages):
    hooks.on_pre_llm_call(session_id="s1", turn_id="t", user_message=text, platform="cli", model="router")
    hooks.on_pre_api_request(session_id="s1", platform="cli", model="router", provider="polytropos-router",
                             system_prompt=INDEX, request_messages=messages, request={"body": {"tools": TOOLS}})


def bodies(fake, model):
    return [r for r in fake.requests if r["_path"].endswith("/chat/completions") and r["model"] == model]


def test_the_local_model_gets_the_session_variant(local, cloud, linked, tmp_path):
    text = "Please merge my pull request"
    messages = [{"role": "system", "content": INDEX}, {"role": "user", "content": text}]
    hermes_turn(text, messages)
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "done"})
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path)
    rc.chat.completions.create(model="auto", messages=messages, tools=TOOLS)
    body = bodies(local, MODEL)[0]
    assert [t["function"]["name"] for t in body["tools"]] == ["terminal", "skill_view"]
    system = body["messages"][0]["content"]
    assert "github-pr-workflow" in system and "ascii-art" not in system
    assert json.loads(path.read_text())["shaped"] == "lean"
    hooks.on_session_end(session_id="s1", completed=True)
    assert log.read(tmp_path / "data" / log.NAME)[0]["local"] == "lean"


def test_without_apply_the_router_sends_the_request_unchanged(local, cloud, linked):
    linked["apply"] = []
    text = "Please merge my pull request"
    messages = [{"role": "system", "content": INDEX}, {"role": "user", "content": text}]
    hermes_turn(text, messages)
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "done"})
    router(local, cloud).chat.completions.create(model="auto", messages=messages, tools=TOOLS)
    body = bodies(local, MODEL)[0]
    assert len(body["tools"]) == len(TOOLS) and body["messages"][0]["content"] == INDEX
