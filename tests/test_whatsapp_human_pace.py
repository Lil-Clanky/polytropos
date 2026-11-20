import datetime
import importlib
import sys
import types

import pytest

NOTE = "Replies in this chat come from an assistant."


@pytest.fixture
def pace(tmp_home, monkeypatch):
    """Import the plugin with a stand-in for hermes_cli.config and an editable config dict."""
    cfg: dict = {}
    stub = types.ModuleType("hermes_cli.config")
    stub.load_config_readonly = lambda: cfg
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", stub)
    mod = importlib.import_module("whatsapp_human_pace")
    mod._NOTED.clear()
    mod.cfg = cfg
    return mod


def reply(mod, text="Hello there", session="s1", platform="whatsapp"):
    return mod._transform_llm_output(response_text=text, platform=platform, session_id=session)


def set_options(mod, **opts):
    mod.cfg["plugins"] = {"entries": {"whatsapp-human-pace": opts}}


def test_first_reply_of_the_day_gets_the_note(pace):
    assert reply(pace) == f"{NOTE}\nHello there"


def test_later_replies_the_same_day_get_no_note(pace):
    reply(pace)
    assert reply(pace, "Second one") is None


def test_each_chat_gets_its_own_note(pace):
    reply(pace, session="a")
    assert reply(pace, session="b").startswith(NOTE)


def test_note_returns_on_the_next_calendar_day(pace, monkeypatch):
    class Day(datetime.date):
        today_value = datetime.date(2026, 1, 1)

        @classmethod
        def today(cls):
            return cls.today_value

    monkeypatch.setattr(pace.datetime, "date", Day)
    reply(pace)
    assert reply(pace, "again") is None
    Day.today_value = datetime.date(2026, 1, 2)
    assert reply(pace, "next day").startswith(NOTE)


def test_disclosure_false_adds_no_note(pace):
    set_options(pace, disclosure=False)
    assert reply(pace) is None


def test_custom_text(pace):
    set_options(pace, disclosure_text="Bot here.")
    assert reply(pace) == "Bot here.\nHello there"


def test_other_platforms_untouched(pace):
    assert reply(pace, platform="telegram") is None


def test_note_survives_humanize_changes(pace):
    assert reply(pace, "**Hi**") == f"{NOTE}\nHi"


def test_note_added_even_when_humanize_is_off(pace):
    pace._write_settings({"humanize": False})
    assert reply(pace, "**Hi**") == f"{NOTE}\n**Hi**"


def test_broken_config_falls_back_to_defaults(pace):
    pace.cfg["plugins"] = "nonsense"
    assert reply(pace).startswith(NOTE)


def test_pending_file_is_written_whole_and_private(pace, monkeypatch):
    monkeypatch.setitem(pace._PENDING, "whatsapp|chat", {"events": [types.SimpleNamespace(text="held text")], "due": 5.0})
    pace._save()
    path = pace._state_file()
    assert pace.json.loads(path.read_text()) == {"whatsapp|chat": {"due": 5.0, "texts": ["held text"]}}
    assert path.stat().st_mode & 0o777 == 0o600  # held texts are private messages
    assert [p.name for p in path.parent.iterdir()] == ["pending.json"]  # no temp file left behind
