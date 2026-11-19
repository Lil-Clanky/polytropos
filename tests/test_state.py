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
