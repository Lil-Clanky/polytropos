import importlib.util
import sys
import threading
from pathlib import Path

import pytest

from polytropos_shared import bridge, paths
from polytropos_shared.bridge import TurnInfo, lookup_turn, message_text, remember_turn


@pytest.fixture(autouse=True)
def clean_store():
    bridge._clear()
    yield
    bridge._clear()


def test_paths_follow_xdg(tmp_home, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/x")
    monkeypatch.setenv("XDG_STATE_HOME", "/y")
    monkeypatch.setenv("XDG_CACHE_HOME", "/z")
    assert paths.data_dir() == Path("/x/polytropos")
    assert paths.state_dir() == Path("/y/polytropos")
    assert paths.cache_dir() == Path("/z/polytropos")
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.data_dir() == Path.home() / ".local/share/polytropos"
    assert paths.state_dir() == Path.home() / ".local/state/polytropos"
    assert paths.cache_dir() == Path.home() / ".cache/polytropos"
    monkeypatch.setattr(paths.sys, "platform", "win32")  # one folder under %LOCALAPPDATA%, like Hermes's own
    local = tmp_home / "AppData" / "Local"
    assert paths.data_dir() == paths.state_dir() == paths.cache_dir() == local / "polytropos"
    monkeypatch.delenv("LOCALAPPDATA")
    assert paths.data_dir() == Path.home() / "AppData" / "Local" / "polytropos"


def test_hermes_home_env_and_default(tmp_home, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/h")
    assert paths.hermes_home() == Path("/h")
    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.hermes_home() == Path.home() / ".hermes"
    monkeypatch.setattr(paths.sys, "platform", "win32")  # hermes_constants.py: %LOCALAPPDATA%\hermes
    assert paths.hermes_home() == tmp_home / "AppData" / "Local" / "hermes"


def test_freellmapi_port_is_the_one_the_desktop_app_saved_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.freellmapi_port() == 3001  # the service Polytropos runs
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    assert paths.freellmapi_port() == 31415
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths.freellmapi_port() == 31415  # the app has not run yet
    config = tmp_path / "FreeLLMAPI" / "config.json"
    config.parent.mkdir()
    config.write_text('{"theme": "dark", "port": 31416}', encoding="utf-8")  # as desktop/src/config.ts writes it
    assert paths.freellmapi_port() == 31416
    for other in ('{"port": "31416"}', '{"port": 0}', '{"theme": "dark"}', "[]", "{"):
        config.write_text(other, encoding="utf-8")
        assert paths.freellmapi_port() == 31415


def test_message_text_handles_parts():
    parts = [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}}, {"type": "text", "text": "b"}]
    assert message_text(parts) == "a\nb"
    assert message_text(None) == ""
    assert message_text("plain") == "plain"


def test_lookup_prefix_match():
    remember_turn(TurnInfo("s1", "whatsapp", "hello", 1000.0))
    assert lookup_turn("hello\n\n[extra context]", now=1001.0).platform == "whatsapp"
    assert lookup_turn("other", now=1001.0) is None


def test_empty_entry_matches_only_empty_lookup():
    remember_turn(TurnInfo("s1", "cli", "", 1000.0))
    assert lookup_turn("hello", now=1001.0) is None
    assert lookup_turn("", now=1001.0).session_id == "s1"


def test_lookup_returns_newest():
    remember_turn(TurnInfo("old", "cli", "hello", 1000.0))
    remember_turn(TurnInfo("new", "cli", "hello", 1001.0))
    assert lookup_turn("hello", now=1002.0).session_id == "new"


def test_lookup_expires():
    remember_turn(TurnInfo("s1", "cli", "hello", 1000.0))
    assert lookup_turn("hello", now=1000.0 + 3600) is not None
    assert lookup_turn("hello", now=1000.0 + 3601) is None


def test_store_shared_between_module_copies():
    def load(name):
        spec = importlib.util.spec_from_file_location(name, bridge.__file__)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    a, b = load("bridge_copy_a"), load("bridge_copy_b")
    try:
        a.remember_turn(a.TurnInfo("s1", "cli", "hi", 1.0))
        assert b.lookup_turn("hi", now=2.0).session_id == "s1"
    finally:
        sys.modules.pop("bridge_copy_a")
        sys.modules.pop("bridge_copy_b")


def test_concurrent_remember():
    errors = []

    def work(n):
        try:
            for i in range(200):
                remember_turn(TurnInfo(f"s{n}", "cli", f"t{n}-{i}", float(i)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(bridge._store().entries) == 512
