import json

import openai
import pytest

from polytropos_router.streams import GuardedStream
from polytropos_router.toolcheck import broken_tool_calls

TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {
    "type": "object", "properties": {}, "required": ["path"]}}}]


def call(name, args):
    return {"function": {"name": name, "arguments": args}}


def test_valid_call_passes():
    assert broken_tool_calls([call("write_file", '{"path": "a"}')], TOOLS) == []


def test_unknown_tool():
    assert broken_tool_calls([call("rm", "{}")], TOOLS) == ["unknown tool rm"]


def test_bad_json():
    assert broken_tool_calls([call("write_file", "{bad"), call("write_file", "[1]")], TOOLS) == \
        ["arguments of write_file are not a JSON object"] * 2


def test_missing_required():
    assert broken_tool_calls([call("write_file", "{}")], TOOLS) == ["write_file is missing required path"]


def test_sdk_objects_accepted():
    from types import SimpleNamespace as NS
    ok = NS(function=NS(name="write_file", arguments='{"path": "a"}'))
    assert broken_tool_calls([ok], TOOLS) == []


def stream_of(fake, model, reply):
    fake.script(model, reply)
    client = openai.OpenAI(base_url=fake.base_url, api_key="x", max_retries=0)
    return lambda: client.chat.completions.create(model=model, messages=[], stream=True)


def collect(stream):
    text, calls = "", {}
    for chunk in stream:
        d = chunk.choices[0].delta
        text += d.content or ""
        for tc in d.tool_calls or []:
            calls.setdefault(tc.index, []).append(tc.function.arguments or "")
    return text, {i: "".join(v) for i, v in calls.items()}


def test_stream_text_passthrough_order(fake_openai):
    make = stream_of(fake_openai, "m", {"content": "hello world"})
    retries = []
    gs = GuardedStream(make(), tools=TOOLS, retry=lambda: retries.append(1))
    text, _ = collect(gs)
    assert text == "hello world" and not retries and not gs.retried


def test_stream_broken_tool_only_is_retried(fake_openai):
    bad = stream_of(fake_openai, "bad", {"tool_calls": [{"name": "write_file", "arguments": "{bad"}]})
    good = stream_of(fake_openai, "good", {"tool_calls": [{"name": "write_file", "arguments": '{"path": "a"}'}]})
    closed = []
    gs = GuardedStream(bad(), tools=TOOLS, retry=good, on_close=lambda: closed.append(1))
    _, calls = collect(gs)
    assert json.loads(calls[0]) == {"path": "a"} and gs.retried and closed == [1]
    gs.close()
    assert closed == [1]


def test_valid_tool_call_not_retried(fake_openai):
    ok = stream_of(fake_openai, "ok", {"tool_calls": [{"name": "write_file", "arguments": '{"path": "a"}'}]})
    gs = GuardedStream(ok(), tools=TOOLS, retry=lambda: pytest.fail("retried"))
    _, calls = collect(gs)
    assert json.loads(calls[0]) == {"path": "a"} and not gs.retried


def test_stream_text_then_broken_tool_not_retried(fake_openai):
    bad = stream_of(fake_openai, "bad", {"content": "let me", "tool_calls": [{"name": "write_file", "arguments": "{bad"}]})
    gs = GuardedStream(bad(), tools=TOOLS, retry=lambda: pytest.fail("retried"))
    text, calls = collect(gs)
    assert text == "let me" and calls[0] == "{bad" and not gs.retried


def test_stream_error_after_text_reraises(fake_openai):
    make = stream_of(fake_openai, "m", {"content": "hello world", "fail_after": 2})
    gs = GuardedStream(make(), tools=TOOLS, retry=lambda: pytest.fail("retried"))
    seen = []
    with pytest.raises(openai.APIConnectionError):
        for chunk in gs:
            seen.append(chunk.choices[0].delta.content)
    assert any(seen)


def test_stream_error_before_output_retries(fake_openai):
    first = stream_of(fake_openai, "a", {"content": "x", "fail_after": 1})
    second = stream_of(fake_openai, "b", {"content": "recovered"})
    gs = GuardedStream(first(), tools=TOOLS, retry=second)
    text, _ = collect(gs)
    assert text == "recovered" and gs.retried
