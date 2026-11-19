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
    fake_openai.script("m1", {"content": "abcdef", "fail_after": 2})
    seen = []
    with pytest.raises(openai.APIConnectionError):
        stream = _client(fake_openai).chat.completions.create(model="m1", messages=[], stream=True)
        for c in stream:
            seen.append(c.choices[0].delta.content or "")
    assert "".join(seen) == "ab"


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


def _synced(tmp_path):
    from sync_shared import sync

    shutil.copytree(ROOT / "shared", tmp_path / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("polytropos", "polytropos_router"):
        (tmp_path / "plugins" / name).mkdir(parents=True)
    sync(tmp_path)
    return tmp_path / "plugins" / "polytropos" / "_shared"


def test_sync_removes_stale_files_only_inside_shared(tmp_path):
    from sync_shared import check, sync

    shared = _synced(tmp_path)
    extra = shared / "gone.py"
    extra.write_text("")
    old_tmp = shared / ".bridge.py.abc123"
    old_tmp.write_text("")
    os.utime(old_tmp, (1, 1))
    fresh_tmp = shared / ".bridge.py.def456"  # may belong to a sync running right now
    fresh_tmp.write_text("")
    pycache = shared / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"")
    outside = [tmp_path / "plugins" / "polytropos" / "keep.py", tmp_path / "plugins" / "other" / "_shared" / "keep.py"]
    for path in outside:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    assert sorted(Path(p).name for p in check(tmp_path)) == [".bridge.py.abc123", "gone.py"]
    assert sync(tmp_path) == []
    assert not extra.exists() and not old_tmp.exists()
    assert fresh_tmp.exists() and (pycache / "x.pyc").exists()
    assert all(path.exists() for path in outside)
    assert check(tmp_path) == []


def test_synced_copy_mode_is_0644_and_repaired(tmp_path):
    from sync_shared import check, sync

    shared = _synced(tmp_path)
    copy = shared / "__init__.py"
    assert copy.stat().st_mode & 0o777 == 0o644
    copy.chmod(0o600)
    assert check(tmp_path) == [str(copy)]
    assert sync(tmp_path) == [copy]
    assert copy.stat().st_mode & 0o777 == 0o644
    assert check(tmp_path) == []


def test_tmp_home_isolates(tmp_home):
    assert Path.home() == tmp_home
    assert os.environ["HERMES_HOME"] == str(tmp_home / ".hermes")
