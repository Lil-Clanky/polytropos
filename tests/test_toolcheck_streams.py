import json
import threading
from types import SimpleNamespace as NS

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


def fake_chunk(content=None, role=None, **extra):
    return NS(choices=[NS(delta=NS(content=content, role=role, tool_calls=None, **extra), finish_reason=None)])


class Failing:
    def __init__(self, chunks, error=RuntimeError("dropped")):
        self.chunks, self.error = chunks, error

    def __iter__(self):
        yield from self.chunks
        raise self.error


class Blocking:
    """Blocks until closed, then fails like a broken connection."""

    def __init__(self):
        self.started, self.closed = threading.Event(), threading.Event()

    def __iter__(self):
        self.started.set()
        self.closed.wait(5)
        raise RuntimeError("closed")

    def close(self):
        self.closed.set()


def test_role_only_then_error_retries_with_one_role_delta():
    gs = GuardedStream(Failing([fake_chunk(role="assistant", content="")]), tools=TOOLS,
                       retry=lambda: [fake_chunk(role="assistant", content=""), fake_chunk("ok")])
    chunks = list(gs)
    assert gs.retried and [c.choices[0].delta.role for c in chunks] == ["assistant", None]


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "reasoning_details"])
def test_error_after_reasoning_reraises(field):
    chunks = [fake_chunk(role="assistant", content=""), fake_chunk(**{field: "Plan A"})]
    gs = GuardedStream(Failing(chunks), tools=TOOLS, retry=lambda: pytest.fail("retried"))
    seen = []
    with pytest.raises(RuntimeError):
        for c in gs:
            seen.append(c)
    assert len(seen) == 2


def test_close_before_text_does_not_retry():
    up, order = Blocking(), []
    gs = GuardedStream(up, tools=TOOLS, retry=lambda: order.append("retry") or [], on_close=lambda: order.append("on_close"))
    errors = []

    def consume():
        try:
            list(gs)
        except RuntimeError as e:
            errors.append(e)

    t = threading.Thread(target=consume)
    t.start()
    assert up.started.wait(2)
    gs.close()
    t.join(2)
    assert not t.is_alive() and order == ["on_close"] and errors


def test_close_during_retry_closes_retry_stream():
    second = Blocking()
    gs = GuardedStream(Failing([]), tools=TOOLS, retry=lambda: second)
    t = threading.Thread(target=_drain, args=(gs,))
    t.start()
    assert second.started.wait(2)
    gs.close()
    t.join(2)
    assert not t.is_alive() and second.closed.is_set()


def _drain(gs):
    try:
        list(gs)
    except RuntimeError:
        pass
