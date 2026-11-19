import os
import shutil
from pathlib import Path

import openai
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _client(fake):
    return openai.OpenAI(base_url=fake.base_url, api_key="x", max_retries=0)


def test_fake_openai_non_stream_and_models(fake_openai):
    fake_openai.script("m1", {"content": "hi"})
    client = _client(fake_openai)
    out = client.chat.completions.create(model="m1", messages=[{"role": "user", "content": "yo"}])
    assert out.choices[0].message.content == "hi"
    assert "m1" in [m.id for m in client.models.list()]
    assert fake_openai.requests[-1]["model"] == "m1"


def test_fake_openai_stream_text_and_empty_queue(fake_openai):
    fake_openai.script("m1", {"content": "hello world"})
    client = _client(fake_openai)
    stream = client.chat.completions.create(model="m1", messages=[], stream=True)
    assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) == "hello world"
    out = client.chat.completions.create(model="m1", messages=[])
    assert out.choices[0].message.content == "ok"


def test_fake_openai_stream_tool_call(fake_openai):
    fake_openai.script("m1", {"tool_calls": [{"name": "write_file", "arguments": '{"path": "a"}'}]})
    stream = _client(fake_openai).chat.completions.create(model="m1", messages=[], stream=True)
    args = "".join(
        tc.function.arguments or ""
        for c in stream if c.choices
        for tc in (c.choices[0].delta.tool_calls or [])
    )
    assert args == '{"path": "a"}'


def test_fake_openai_http_error(fake_openai):
    fake_openai.script("m1", {"status": 429})
    with pytest.raises(openai.RateLimitError):
        _client(fake_openai).chat.completions.create(model="m1", messages=[])


def test_fake_openai_fail_after_drops_stream(fake_openai):
    fake_openai.script("m1", {"content": "abcdef", "fail_after": 1})
    with pytest.raises(Exception):
        stream = _client(fake_openai).chat.completions.create(model="m1", messages=[], stream=True)
        list(stream)


def test_sync_shared_check(tmp_path):
    from sync_shared import check, sync

    shutil.copytree(ROOT / "shared", tmp_path / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("polytropos", "polytropos_router"):
        (tmp_path / "plugins" / name).mkdir(parents=True)
    assert sync(tmp_path)
    assert check(tmp_path) == []
    assert sync(tmp_path) == []
    copy = tmp_path / "plugins" / "polytropos" / "_shared" / "__init__.py"
    copy.write_text("# edited\n")
    assert [Path(p).name for p in check(tmp_path)] == ["__init__.py"]


def test_tmp_home_isolates(tmp_home):
    assert Path.home() == tmp_home
    assert os.environ["HERMES_HOME"] == str(tmp_home / ".hermes")
