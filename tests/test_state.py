import json
import stat

from polytropos.state import (
    State, config_changes, load_state, record_config_change, release_shared, save_state, set_mode, use_shared,
)


def test_roundtrip_and_mode(tmp_home):
    state = State()
    use_shared(state, "freellmapi", "default", version="v0.16.9", installed_by_us=True)
    set_mode(state, "freellmapi", "managed")
    record_config_change(state, "default", "model.default", "a", "b")
    save_state(state)
    path = tmp_home / ".local/state/polytropos/state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_state() == state


def test_shared_part_used_by_two_profiles(tmp_home):
    state = State()
    use_shared(state, "freellmapi", "one", version="v1", installed_by_us=True)
    use_shared(state, "freellmapi", "two", version="v1", installed_by_us=False)
    assert release_shared(state, "freellmapi", "one") is False
    assert release_shared(state, "freellmapi", "two") is True


def test_installed_by_us_keeps_first_value(tmp_home):
    state = State()
    use_shared(state, "freellmapi", "one", version="v1", installed_by_us=True)
    use_shared(state, "freellmapi", "two", version="v1", installed_by_us=False)
    assert state.shared["freellmapi"]["installed_by_us"] is True


def test_key_changed_twice_keeps_original_old(tmp_home):
    state = State()
    record_config_change(state, "p", "k", "orig", "mid")
    record_config_change(state, "p", "k", "mid", "last")
    assert config_changes(state, "p") == [{"key": "k", "old": "orig", "new": "last"}]
    assert config_changes(state, "other") == []


def test_missing_file_gives_default(tmp_home):
    assert load_state() == State()


def test_corrupt_file_is_renamed(tmp_home):
    path = tmp_home / ".local/state/polytropos/state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{nope")
    assert load_state() == State()
    assert not path.exists()
    assert path.with_name("state.json.corrupt").read_text() == "{nope"


def test_a_model_record_of_0_1_1_is_split_and_keeps_no_api_key(tmp_home):
    path = tmp_home / ".local/state/polytropos/state.json"
    path.parent.mkdir(parents=True)
    old = {"provider": "openrouter", "default": "some/model", "api_key": "sk-user-test-key", "key_env": "MY_KEY"}
    new = {"provider": "custom", "default": "free-model", "api_key": "${FREELLMAPI_API_KEY}",
           "default_headers": {"User-Agent": "hermes-agent"}}
    path.write_text(json.dumps({"version": 1, "shared": {}, "profiles": {"p": {"config_changes": [
        {"key": "model", "old": old, "new": new}, {"key": "model.provider", "old": "custom", "new": "auto"}]}}}))
    expected = [{"key": "model.default", "old": "some/model", "new": "free-model"},
                {"key": "model.default_headers", "old": None, "new": {"User-Agent": "hermes-agent"}},
                {"key": "model.key_env", "old": "MY_KEY", "new": None},
                {"key": "model.provider", "old": "openrouter", "new": "auto"},
                {"key": "model.api_key", "old": None, "new": "${FREELLMAPI_API_KEY}", "old_dropped": True}]
    assert config_changes(load_state(), "p") == expected
    assert "sk-user-test-key" not in path.read_text()  # saved at once, so no command leaves the key on disk
    assert config_changes(load_state(), "p") == expected


def test_a_string_model_record_of_0_1_1_becomes_model_default(tmp_home):
    path = tmp_home / ".local/state/polytropos/state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "shared": {}, "profiles": {"p": {"config_changes": [
        {"key": "model", "old": "openrouter/some-model", "new": {"default": "free-model", "api_key": "sk-literal"}}]}}}))
    assert config_changes(load_state(), "p") == [
        {"key": "model.default", "old": "openrouter/some-model", "new": "free-model"}]
    assert "sk-literal" not in path.read_text()
