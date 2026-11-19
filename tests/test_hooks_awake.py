import threading
import time

import pytest

from polytropos import awake, hooks
from polytropos._shared import bridge, guard
from polytropos._shared.guard import Signals

WARN = Signals("warning", 0, 50 * 1024**3)
NORMAL = Signals("normal", 0, 50 * 1024**3)


class FakeProc:
    def __init__(self, argv):
        self.argv, self.ended, self.polled = argv, False, None

    def poll(self):
        return self.polled

    def terminate(self):
        self.ended = True


class FakePopen:
    def __init__(self, exc=None):
        self.procs, self.exc = [], exc

    def __call__(self, argv, **kw):
        if self.exc:
            raise self.exc
        self.procs.append(FakeProc(list(argv)))
        return self.procs[-1]


# --- AwakeHolder ---

def test_two_threads_share_one_hold():
    popen = FakePopen()
    holder = awake.AwakeHolder(platform="darwin", popen=popen)
    barrier = threading.Barrier(2)
    holder.acquire("main")  # keeps the count above zero while the threads churn

    def run(key):
        barrier.wait()
        for _ in range(50):
            holder.acquire(key)
            holder.release(key)
            holder.acquire(key)
        barrier.wait()

    threads = [threading.Thread(target=run, args=(k,)) for k in ("a", "b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert holder.active == 3 and len(popen.procs) == 1 and not popen.procs[0].ended
    holder.release("a")
    holder.release("b")
    assert not popen.procs[0].ended
    holder.release("main")
    assert popen.procs[0].ended and holder.active == 0


class SlowPopen(FakePopen):
    def __call__(self, argv, **kw):
        time.sleep(0.001)  # widens the check-then-start window so a missing lock shows
        return super().__call__(argv, **kw)


def test_first_acquires_race_to_one_process():
    for _ in range(50):
        popen = SlowPopen()
        holder = awake.AwakeHolder(platform="darwin", popen=popen)
        barrier = threading.Barrier(2)

        def run(key):
            barrier.wait()
            holder.acquire(key)

        threads = [threading.Thread(target=run, args=(k,)) for k in ("a", "b")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(popen.procs) == 1 and holder.active == 2


def test_double_acquire_single_release_and_stray_release():
    popen = FakePopen()
    holder = awake.AwakeHolder(platform="darwin", popen=popen)
    holder.acquire("a")
    holder.acquire("a")
    assert holder.active == 1
    holder.release("a")
    assert popen.procs[0].ended
    holder.release("a")
    holder.release("never")
    assert holder.active == 0 and len(popen.procs) == 1


def test_commands_and_max_hold():
    mac, lin, other = FakePopen(), FakePopen(), FakePopen()
    awake.AwakeHolder(platform="darwin", popen=mac, max_hold_s=60).acquire("a")
    awake.AwakeHolder(platform="linux", popen=lin).acquire("a")
    awake.AwakeHolder(platform="win32", popen=other).acquire("a")
    assert mac.procs[0].argv == ["caffeinate", "-i", "-t", "60"]
    assert lin.procs[0].argv == ["systemd-inhibit", "--what=idle:sleep", "--who=polytropos",
                                 "--why=Hermes turn", "sleep", "7200"]
    assert other.procs == []


def test_missing_program_holds_nothing_and_logs_once(caplog):
    holder = awake.AwakeHolder(platform="linux", popen=FakePopen(OSError("secret path")))
    with caplog.at_level("DEBUG"):
        holder.acquire("a")
        holder.acquire("b")
    assert holder.active == 0
    assert len([r for r in caplog.records if r.levelname == "DEBUG"]) == 1
    assert "secret" not in caplog.text


def test_expired_process_is_restarted():
    popen = FakePopen()
    holder = awake.AwakeHolder(platform="darwin", popen=popen)
    holder.acquire("a")
    popen.procs[0].polled = 0
    holder.acquire("b")
    assert len(popen.procs) == 2


# --- hooks ---

@pytest.fixture
def env(tmp_home, monkeypatch):
    popen = FakePopen()
    monkeypatch.setattr(hooks, "_holder", awake.AwakeHolder(platform="darwin", popen=popen))
    monkeypatch.setattr(hooks, "_get_config", lambda key, default: default)
    monkeypatch.setattr(guard, "read_signals", lambda: WARN)
    bridge._clear()
    hooks._records.clear()
    return popen


def config(tmp_home, provider, base_url, default="llama3.2:3b"):
    home = tmp_home / ".hermes"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(
        f"model:\n  default: {default}\n  provider: {provider}\n  base_url: {base_url}\n")


def turn(model="llama3.2:3b", session="s1", text="hi"):
    return hooks.on_pre_llm_call(session, text, [], True, model, "cli")


LLAMA = "http://127.0.0.1:8080/v1"
ROUTER_URL = "http://127.0.0.1:3001/v1"


def test_blocks_local_provider_under_warning(env, tmp_home):
    config(tmp_home, "llama-cpp", LLAMA)
    out = turn()
    assert out["action"] == "block"
    assert out["message"] == hooks.BLOCK_TEXT.format(reason="memory pressure is warning")
    assert env.procs == []  # a blocked turn takes no hold


def test_router_is_not_blocked_and_takes_hold(env, tmp_home):
    config(tmp_home, "polytropos-router", ROUTER_URL)
    assert turn() is None
    assert len(env.procs) == 1


def test_normal_pressure_not_blocked(env, tmp_home, monkeypatch):
    monkeypatch.setattr(guard, "read_signals", lambda: NORMAL)
    config(tmp_home, "llama-cpp", LLAMA)
    assert turn() is None


def test_record_overrides_config(env, tmp_home):
    config(tmp_home, "polytropos-router", ROUTER_URL)
    hooks.on_pre_api_request("s1", "ollama", "http://localhost:11434/v1", model="llama3.2:3b")
    assert turn()["action"] == "block"


def test_router_record_overrides_local_config(env, tmp_home):
    config(tmp_home, "llama-cpp", LLAMA)
    hooks.on_pre_api_request("s1", "polytropos-router", ROUTER_URL, model="llama3.2:3b")
    assert turn() is None


def test_record_for_other_model_is_ignored(env, tmp_home):
    config(tmp_home, "llama-cpp", LLAMA)
    hooks.on_pre_api_request("s1", "polytropos-router", ROUTER_URL, model="other")
    assert turn()["action"] == "block"


def test_other_model_than_config_default_is_unknown(env, tmp_home):
    config(tmp_home, "llama-cpp", LLAMA)
    assert turn(model="gpt-cloud") is None
    assert len(env.procs) == 1


def test_missing_config_is_unknown(env):
    assert turn() is None


def test_non_loopback_and_default_ports():
    assert not hooks._is_local("https://api.example.com/v1")
    assert not hooks._is_local("http://127.0.0.1:3001/v1")
    assert not hooks._is_local("http://localhost:3001")
    assert not hooks._is_local("http://127.0.0.1:99999/v1")
    assert not hooks._is_local(None)
    assert hooks._is_local("http://localhost:11434")
    assert hooks._is_local("http://127.5.5.5:8080")
    assert hooks._is_local("http://[::1]:8080/v1")
    assert hooks._is_local("http://127.0.0.1/v1")  # port 80


def test_records_keep_newest_256(env):
    for i in range(300):
        hooks.on_pre_api_request(f"s{i}", "p", "u", model="m")
    assert len(hooks._records) == 256
    assert "s0" not in hooks._records and "s299" in hooks._records


def test_switches(env, tmp_home, monkeypatch):
    config(tmp_home, "llama-cpp", LLAMA)
    monkeypatch.setattr(hooks, "_get_config", lambda key, default: key != "guard_hook")
    assert turn() is None and len(env.procs) == 1
    monkeypatch.setattr(hooks, "_get_config", lambda key, default: key != "awake")
    assert turn(session="s2")["action"] == "block"
    config(tmp_home, "polytropos-router", ROUTER_URL)
    assert turn(session="s3") is None and len(env.procs) == 1


def test_awake_off_takes_no_hold(env, tmp_home, monkeypatch):
    config(tmp_home, "polytropos-router", ROUTER_URL)
    monkeypatch.setattr(hooks, "_get_config", lambda key, default: key != "awake")
    assert turn() is None
    assert env.procs == [] and hooks._holder.active == 0


def test_switches_default_on_before_configure(monkeypatch):
    monkeypatch.setattr(hooks, "_get_config", hooks._default_config)
    assert hooks._enabled("awake") and hooks._enabled("guard_hook")
    seen = []
    hooks.configure(lambda key, default: seen.append((key, default)) or False)
    assert not hooks._enabled("awake") and seen == [("awake", True)]


def test_raising_guard_returns_none(env, tmp_home, monkeypatch, caplog):
    config(tmp_home, "llama-cpp", LLAMA)

    def boom():
        raise RuntimeError("secret")

    monkeypatch.setattr(guard, "read_signals", boom)
    assert turn() is None
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text


def test_bridge_has_turn_and_list_content(env, tmp_home):
    config(tmp_home, "polytropos-router", ROUTER_URL)
    hooks.on_pre_llm_call("s1", [{"type": "text", "text": "hello"}], [], True, "m", "cli")
    assert bridge.lookup_turn("hello").session_id == "s1"


def test_release_on_post_call_and_session_end(env, tmp_home):
    config(tmp_home, "polytropos-router", ROUTER_URL)
    turn()
    hooks.on_post_llm_call("s1")
    assert env.procs[0].ended
    hooks.on_session_end("s1")  # second release does nothing
    turn()
    hooks.on_session_end("s1")
    assert env.procs[1].ended
