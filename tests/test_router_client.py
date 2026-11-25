import json
import logging
import socket
import stat
import sys
import threading
import time
import types
from types import SimpleNamespace as NS
from unittest.mock import ANY

import httpx2
import openai
import pytest

from fake_openai import FakeOpenAI
from test_guard import Fake as FakeOllama
from polytropos_router import client as client_mod
from polytropos_router import trace
from polytropos_router._shared import bridge, guard
from polytropos_router._shared.bridge import TurnInfo, remember_turn
from polytropos_router._shared.canary import Cell, Matrix
from polytropos_router.client import RouterClient, RouterError
from polytropos_router.config import RouterConfig, Slot, Upstream
from polytropos_router.decisions import DecisionCache

MODEL = "llama3.2:3b"
CLS = "router-classifier"
GB = 1024**3
NORMAL = guard.Signals("normal", 0, 100 * GB)
WARNING = guard.Signals("warning", 0, 100 * GB)
CRITICAL = guard.Signals("critical", 0, 100 * GB)
SIMPLE = {"content": "simple"}
SYS = {"role": "system", "content": "s"}
TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {
    "type": "object", "properties": {}, "required": ["path"]}}}]
CALL = {"id": "1", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "a"}'}}
TOOL_STEP = [{"role": "assistant", "content": None, "tool_calls": [CALL]},
             {"role": "tool", "tool_call_id": "1", "content": "done"}]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(client_mod, "DECISIONS", DecisionCache())
    bridge._clear()
    yield
    bridge._clear()


def _serve():
    fake = FakeOpenAI().__enter__()
    yield fake
    # shutdown() waits up to 0.5 s for the server's poll; let it finish in the background
    threading.Thread(target=fake.__exit__, args=(None, None, None), daemon=True).start()


@pytest.fixture
def local():
    yield from _serve()


@pytest.fixture
def cloud():
    yield from _serve()


def config(local, cloud, *, simple=(Slot("ollama", MODEL), Slot("freellmapi", "auto")), **kw):
    upstreams = {
        "ollama": Upstream("ollama", local.base_url, local=True, runtime="ollama", context_window=8192),
        "freellmapi": Upstream("freellmapi", cloud.base_url, api_key_env="FREELLMAPI_API_KEY"),
    }
    routes = {"simple": simple, "complex": (Slot("freellmapi", "auto"),)}
    return RouterConfig(upstreams, routes, **{"classifier": Slot("freellmapi", CLS), **kw})


def router(local, cloud, cfg=None, **kw):
    defaults = {"guard_read": lambda: NORMAL, "matrix_loader": lambda: Matrix([]), "local_results": dict,
                "runtime_version": lambda up: "0.40.16", "fit_check": lambda up, model: None,
                "env_lookup": {}.get}
    return RouterClient(cfg or config(local, cloud), **{**defaults, **kw})


def turn(text):
    return [SYS, {"role": "user", "content": text}]


def acted(fake):
    """Models of the chat requests a fake got, classifier calls left out."""
    return [r["model"] for r in fake.requests if r["_path"].endswith("/chat/completions") and r["model"] != CLS]


def classified(fake):
    return [r for r in fake.requests if r.get("model") == CLS]


def text_of(stream):
    return "".join(c.choices[0].delta.content or "" for c in stream if c.choices)


def test_simple_turn_goes_local(local, cloud):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "from local"})
    reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("hoi"))
    assert reply.choices[0].message.content == "from local"
    assert acted(local) == [MODEL] and acted(cloud) == [] and len(classified(cloud)) == 1


def test_guard_refusal_skips_local(local, cloud):
    cloud.script(CLS, SIMPLE)
    router(local, cloud, guard_read=lambda: WARNING).chat.completions.create(model="auto", messages=turn("hoi"))
    assert acted(local) == [] and acted(cloud) == ["auto"]


def test_request_too_big_for_local_window(local, cloud):
    cloud.script(CLS, SIMPLE)
    router(local, cloud).chat.completions.create(model="auto", messages=turn("x" * 40_000))
    assert acted(local) == [] and acted(cloud) == ["auto"]


def test_canary_fail_skips_local_for_tool_turns_only(local, cloud):
    cloud.script(CLS, SIMPLE, SIMPLE)
    matrix = Matrix([Cell("0.40.16", MODEL, "A6", "fail")])
    create = router(local, cloud, matrix_loader=lambda: matrix).chat.completions.create
    create(model="auto", messages=turn("write it down"), tools=TOOLS)
    assert acted(local) == [] and acted(cloud) == ["auto"]
    create(model="auto", messages=turn("hoi"))
    assert acted(local) == [MODEL]


def test_rate_limit_falls_back(local, cloud, tmp_path):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 429})
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path)
    reply = rc.chat.completions.create(model="auto", messages=turn("hoi"))
    assert reply.choices[0].message.content == "ok" and acted(local) == [MODEL] and acted(cloud) == ["auto"]
    record = json.loads(path.read_text())
    assert record["fell_back_from"] == ["ollama/llama3.2:3b"] and record["slot"] == "freellmapi/auto"


def test_tool_loop_keeps_decision(local, cloud):
    # also ported: the PR's test_sticky_per_user_turn_no_reclassification
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 429})  # the first call falls back, so the cloud slot is pinned
    first = turn("plan my day")
    router(local, cloud).chat.completions.create(model="auto", messages=first, tools=TOOLS)
    router(local, cloud).chat.completions.create(model="auto", messages=first + TOOL_STEP, tools=TOOLS)
    assert len(classified(cloud)) == 1
    assert acted(local) == [MODEL] and acted(cloud) == ["auto", "auto"]
    later = first + TOOL_STEP + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "thanks"}]
    router(local, cloud).chat.completions.create(model="auto", messages=later)
    assert len(classified(cloud)) == 2  # a new user message starts a new turn


def test_tool_loop_outgrows_local_window_escalates(local, cloud):
    cloud.script(CLS, SIMPLE)
    first = turn("read the log")
    create = router(local, cloud).chat.completions.create
    create(model="auto", messages=first, tools=TOOLS)
    grown = first + [TOOL_STEP[0], {"role": "tool", "tool_call_id": "1", "content": "x" * 40_000}]
    create(model="auto", messages=grown, tools=TOOLS)
    create(model="auto", messages=grown + TOOL_STEP, tools=TOOLS)
    assert len(classified(cloud)) == 1 and acted(local) == [MODEL] and acted(cloud) == ["auto", "auto"]


def test_broken_tool_call_retried_non_stream(local, cloud):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"tool_calls": [{"name": "write_file", "arguments": "{bad"}]})
    cloud.script("auto", {"tool_calls": [{"name": "write_file", "arguments": '{"path": "a"}'}]})
    reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("save"), tools=TOOLS)
    assert json.loads(reply.choices[0].message.tool_calls[0].function.arguments) == {"path": "a"}
    assert acted(local) == [MODEL] and acted(cloud) == ["auto"]


def test_broken_tool_call_kept_when_no_other_model_answers(local, cloud):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"tool_calls": [{"name": "write_file", "arguments": "{bad"}]})
    cloud.script("auto", {"status": 503})
    reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("save"), tools=TOOLS)
    assert reply.choices[0].message.tool_calls[0].function.arguments == "{bad"


def test_all_fail_raises_last(local, cloud):
    # also ported: the PR's test_chain_exhausted_reraises
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 429})
    cloud.script("auto", {"status": 500})
    with pytest.raises(openai.InternalServerError):
        router(local, cloud).chat.completions.create(model="auto", messages=turn("hoi"))


def test_api_key_from_client_kwargs_for_freellmapi(local, cloud):
    rc = router(local, cloud, api_key="k", env_lookup={"FREELLMAPI_API_KEY": "from-env"}.get)
    rc.chat.completions.create(model="auto", messages=turn("hoi"))
    assert {r["_auth"] for r in cloud.requests} == {"Bearer k"}


def test_upstream_clients_use_env_lookup_without_retries(local, cloud):
    made = []

    def factory(**kw):
        made.append(kw)
        return openai.OpenAI(**kw)

    rc = router(local, cloud, openai_factory=factory, env_lookup={"FREELLMAPI_API_KEY": "from-env"}.get)
    assert made == [] and rc.base_url == "polytropos://router" and rc.is_closed is False
    assert not hasattr(rc, "_client")  # Hermes probes `_client` for an http client
    assert RouterClient.HERMES_SKIP_TRANSPORT_WRAP is True and RouterClient.HERMES_SKIP_ASYNC_WRAP is True
    cloud.script(CLS, SIMPLE, SIMPLE)
    rc.chat.completions.create(model="auto", messages=turn("hoi"))
    rc.chat.completions.create(model="auto", messages=turn("hoi again"))
    assert made == [{"base_url": cloud.base_url, "api_key": "from-env", "max_retries": 0},
                    {"base_url": local.base_url, "api_key": "none", "max_retries": 0}]
    assert {r["_auth"] for r in cloud.requests} == {"Bearer from-env"}


def test_stream_result_has_no_choices(local, cloud):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "streamed reply"})
    stream = router(local, cloud).chat.completions.create(
        model="auto", messages=turn("hoi"), stream=True, stream_options={"include_usage": True})
    assert not hasattr(stream, "choices")
    assert text_of(stream) == "streamed reply"
    assert [r["stream_options"] for r in local.requests] == [{"include_usage": True}]


def test_traces_off_by_default(local, cloud, tmp_path):
    rc = router(local, cloud, trace_path=tmp_path / "traces.jsonl")
    rc.chat.completions.create(model="auto", messages=turn("hoi"))
    list(rc.chat.completions.create(model="auto", messages=turn("hoi"), stream=True))
    assert list(tmp_path.iterdir()) == []


def test_trace_file_mode(local, cloud, tmp_path):
    # also ported: the record shape from the PR's test_save_router_turn_writes_record
    cloud.script(CLS, SIMPLE)
    path = tmp_path / "state" / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path)
    rc.chat.completions.create(model="auto", messages=turn("private words"))
    list(rc.chat.completions.create(model="auto", messages=turn("private words") + TOOL_STEP, stream=True))
    assert sys.platform == "win32" or stat.S_IMODE(path.stat().st_mode) == 0o600  # Windows keeps no mode bits
    expected = {"time": ANY, "tier": "simple", "reason": "classifier", "slot": "ollama/llama3.2:3b",
                "retried": False, "fell_back_from": [], "skipped": [], "tokens_est": ANY, "duration_ms": ANY}
    assert [json.loads(line) for line in path.read_text().splitlines()] == [expected, expected]
    assert "private" not in path.read_text()


def test_watchdog_trip_aborts_local_and_falls_back(local, cloud, monkeypatch):
    monkeypatch.setattr(client_mod, "WATCHDOG_INTERVAL_S", 0.05)
    cloud.script(CLS, SIMPLE, SIMPLE)
    local.script(MODEL, {"content": "too slow", "delay": 3}, {"content": "back"})
    calm, made = [], []

    def factory(**kw):
        made.append(kw["base_url"])
        return openai.OpenAI(**kw)

    rc = router(local, cloud, openai_factory=factory,
                guard_read=lambda: NORMAL if calm or not acted(local) else CRITICAL)
    started = time.monotonic()
    reply = rc.chat.completions.create(model="auto", messages=turn("hoi"))
    assert reply.choices[0].message.content == "ok" and time.monotonic() - started < 2
    unloads = [(r["model"], r["keep_alive"]) for r in local.requests if r["_path"] == "/api/generate"]
    assert unloads == [(MODEL, 0)]
    calm.append(True)
    assert rc.chat.completions.create(model="auto", messages=turn("again")).choices[0].message.content == "back"
    assert made.count(local.base_url) == 2  # the tripped client was dropped


def test_watchdog_trip_during_a_local_stream(local, cloud, monkeypatch):
    monkeypatch.setattr(client_mod, "WATCHDOG_INTERVAL_S", 0.05)
    cloud.script(CLS, SIMPLE, SIMPLE)
    cloud.script("auto", {"content": "from cloud"})
    # the fake waits a second before the headers and before each text chunk
    local.script(MODEL, {"content": "too slow", "delay": 1}, {"content": "never read", "delay": 1})
    hot = []
    rc = router(local, cloud, guard_read=lambda: CRITICAL if hot else NORMAL)

    def unloads():
        return [(r["model"], r["keep_alive"]) for r in local.requests if r["_path"] == "/api/generate"]

    # text has reached the caller: the trip stops the stream, and the error reaches the caller without a retry
    stream = rc.chat.completions.create(model="auto", messages=turn("one"), stream=True)
    seen = []
    with pytest.raises(openai.APIConnectionError):
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                seen.append(chunk.choices[0].delta.content)
                hot.append(True)
    assert seen == ["too"] and not stream.retried and acted(cloud) == []
    assert unloads() == [(MODEL, 0)]

    # no text yet: the trip stops the stream and the router retries on the next model
    hot.clear()
    stream = rc.chat.completions.create(model="auto", messages=turn("two"), stream=True)
    hot.append(True)  # the stream has started; its first text chunk is a second away
    assert text_of(stream) == "from cloud" and stream.retried
    assert acted(cloud) == ["auto"] and unloads() == [(MODEL, 0), (MODEL, 0)]


def test_fit_refusal_skips_local(local, cloud):
    cloud.script(CLS, SIMPLE, SIMPLE)
    verdicts, seen = [False, None], []
    rc = router(local, cloud, fit_check=lambda up, model: seen.append((up.name, model)) or verdicts.pop(0))
    rc.chat.completions.create(model="auto", messages=turn("one"))
    assert acted(local) == [] and acted(cloud) == ["auto"]
    rc.chat.completions.create(model="auto", messages=turn("two"))
    assert acted(local) == [MODEL] and seen == [("ollama", MODEL)] * 2


def test_canary_filter_never_empties_chain(local, cloud):
    cloud.script(CLS, {"content": "complex"})
    rc = router(local, cloud, local_results=lambda: {"freellmapi/auto": "fail"})
    rc.chat.completions.create(model="auto", messages=turn("refactor my app"), tools=TOOLS)
    assert acted(cloud) == ["auto"]


def test_canary_failing_slot_is_the_last_resort(local, cloud):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 429}, {"content": "local again"})
    create = router(local, cloud, local_results=lambda: {"freellmapi/auto": "fail"}).chat.completions.create
    reply = create(model="auto", messages=turn("write it down"), tools=TOOLS)
    assert reply.choices[0].message.content == "ok" and acted(local) == [MODEL] and acted(cloud) == ["auto"]
    create(model="auto", messages=turn("write it down") + TOOL_STEP, tools=TOOLS)
    assert acted(local) == [MODEL, MODEL]  # pinned, it still waits behind the slot that passes


def test_matrix_loads_once_per_ttl(local, cloud, monkeypatch):
    cloud.script(CLS, SIMPLE, SIMPLE, SIMPLE)
    loads = []

    def loader():
        loads.append(1)
        return Matrix([])

    def create(text):  # a new client for every call, as Hermes does
        router(local, cloud, matrix_loader=loader).chat.completions.create(
            model="auto", messages=turn(text), tools=TOOLS)

    create("one")
    create("two")
    assert len(loads) == 1
    monkeypatch.setattr(client_mod, "MATRIX_TTL_S", 0)
    create("three")
    assert len(loads) == 2


def test_stream_error_after_text_reaches_caller(local, cloud):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "hello world", "fail_after": 2})
    stream = router(local, cloud).chat.completions.create(model="auto", messages=turn("hoi"), stream=True)
    seen = []
    with pytest.raises(openai.APIConnectionError):
        for chunk in stream:
            seen.append(chunk.choices[0].delta.content)
    assert "".join(filter(None, seen)) and acted(cloud) == []


def test_stream_error_before_output_moves_to_next_model(local, cloud, tmp_path):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "lost", "fail_after": 1})
    cloud.script("auto", {"content": "recovered"})
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path)
    stream = rc.chat.completions.create(model="auto", messages=turn("hoi"), stream=True)
    assert text_of(stream) == "recovered" and stream.retried
    record = json.loads(path.read_text())
    assert (record["slot"], record["retried"], record["fell_back_from"]) == \
        ("freellmapi/auto", True, ["ollama/llama3.2:3b"])
    list(rc.chat.completions.create(model="auto", messages=turn("hoi") + TOOL_STEP, stream=True))
    assert acted(local) == [MODEL] and acted(cloud) == ["auto", "auto"]  # the turn stays on the model that answered


@pytest.fixture
def watchdog_log(monkeypatch):
    log = []

    class Recording(guard.Watchdog):
        def __enter__(self):
            log.append("on")
            return super().__enter__()

        def __exit__(self, *exc):
            log.append("off")
            super().__exit__(*exc)

    monkeypatch.setattr(guard, "Watchdog", Recording)
    return log


def test_stream_watchdog_runs_until_close(local, cloud, watchdog_log):
    cloud.script(CLS, SIMPLE, SIMPLE, SIMPLE)
    local.script(MODEL, {"content": "hi"}, {"status": 429}, {"content": "never read"})
    create = router(local, cloud).chat.completions.create
    stream = create(model="auto", messages=turn("one"), stream=True)
    assert watchdog_log == ["on"]
    assert text_of(stream) == "hi" and watchdog_log == ["on", "off"]
    watchdog_log.clear()
    stream = create(model="auto", messages=turn("two"), stream=True)  # local answers 429
    assert watchdog_log == ["on", "off"] and text_of(stream) == "ok"
    watchdog_log.clear()
    create(model="auto", messages=turn("three"), stream=True).close()  # closed without reading
    assert watchdog_log == ["on", "off"]


def test_stream_watchdog_stops_when_the_retry_starts(local, cloud, watchdog_log):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "lost", "fail_after": 1})
    local.script("qwen3:4b", {"content": "second"})
    cfg = config(local, cloud, simple=(Slot("ollama", MODEL), Slot("ollama", "qwen3:4b")))
    stream = router(local, cloud, cfg).chat.completions.create(model="auto", messages=turn("hoi"), stream=True)
    assert text_of(stream) == "second" and watchdog_log == ["on", "off", "on", "off"]


def test_stream_retry_checks_guard_again(local, cloud):
    simple = (Slot("ollama", MODEL), Slot("ollama", "qwen3:4b"), Slot("freellmapi", "auto"))
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "lost", "fail_after": 1})
    cloud.script("auto", {"content": "from cloud"})
    rc = router(local, cloud, config(local, cloud, simple=simple),
                guard_read=lambda: WARNING if acted(local) else NORMAL)
    stream = rc.chat.completions.create(model="auto", messages=turn("hoi"), stream=True)
    assert text_of(stream) == "from cloud"
    assert acted(local) == [MODEL] and acted(cloud) == ["auto"]  # qwen3:4b was refused by fresh signals


def test_multimodal_user_content(local, cloud):
    cloud.script(CLS, SIMPLE)
    parts = [{"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "data:,"}}]
    messages = [SYS, {"role": "user", "content": parts}]
    create = router(local, cloud).chat.completions.create
    create(model="auto", messages=messages)
    create(model="auto", messages=messages + TOOL_STEP)
    assert [r["messages"][1]["content"] for r in classified(cloud)] == ["what is this"]
    assert acted(local) == [MODEL, MODEL]


def test_concurrent_sessions_keep_their_decisions(local, cloud):
    n = 8
    cloud.script(CLS, *[SIMPLE] * n)
    errors = []

    def session(i):
        try:
            first = turn(f"session {i}")
            router(local, cloud).chat.completions.create(model="auto", messages=first)
            router(local, cloud).chat.completions.create(model="auto", messages=first + TOOL_STEP)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=session, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors and len(classified(cloud)) == n and acted(local) == [MODEL] * (2 * n)
    assert len(client_mod.DECISIONS) == n


@pytest.mark.parametrize("mounted", [False, True], ids=["default", "proxy-mount"])
def test_close_aborts_in_flight_requests(local, cloud, mounted):
    def factory(**kw):  # a proxy from the environment or the system settings puts the transport on a mount
        http = openai.DefaultHttpxClient(mounts={"http://": httpx2.HTTPTransport()}) if mounted else None
        return openai.OpenAI(**kw, http_client=http)

    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "slow", "delay": 3})
    rc = router(local, cloud, openai_factory=factory)
    errors = []

    def work():
        try:
            rc.chat.completions.create(model="auto", messages=turn("hoi"))
        except Exception as e:
            errors.append(e)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    deadline = time.monotonic() + 2
    while not acted(local) and time.monotonic() < deadline:
        time.sleep(0.01)
    rc.close()
    worker.join(2)
    assert not worker.is_alive() and isinstance(errors[0], openai.APIConnectionError)
    assert acted(cloud) == [] and rc.is_closed
    rc.close()  # Hermes closes twice
    with pytest.raises(RouterError):
        rc.chat.completions.create(model="auto", messages=turn("hoi"))
    assert acted(local) + acted(cloud) == [MODEL] and len(classified(cloud)) == 1


def test_close_without_httpx_internals_warns_once(local, cloud, caplog):
    client_mod._warn_abort_lost.cache_clear()
    answer = NS(choices=[NS(message=NS(content="simple", tool_calls=None))])
    rc = router(local, cloud, openai_factory=lambda **kw: NS(chat=NS(completions=NS(create=lambda **kw: answer))))
    rc.chat.completions.create(model="auto", messages=turn("hoi"))  # one upstream client per upstream
    with caplog.at_level(logging.WARNING):
        rc.close()
    assert [r.levelname for r in caplog.records] == ["WARNING"] and "hoi" not in caplog.text


def test_abort_reaches_the_socket_under_a_proxy_connection(caplog):
    client_mod._warn_abort_lost.cache_clear()
    ours, theirs = socket.socketpair()
    with ours, theirs:
        # httpcore's proxy shape: ForwardHTTPConnection -> HTTPConnection -> HTTP11Connection
        proxied = NS(_connection=NS(_connection=NS(_network_stream=NS(get_extra_info={"socket": ours}.get))))
        connecting = NS(_connection=None)
        proxy_pool = NS(_connections=[connecting, proxied])  # a mounted proxy pool can be the transport itself
        http = NS(_transport=NS(_pool=NS(_connections=[])), _mounts={"all://localhost": None, "http://": proxy_pool})
        with caplog.at_level(logging.WARNING):
            client_mod._abort(NS(_client=http))
        theirs.setblocking(False)
        assert theirs.recv(1) == b""  # shut down: the peer reads the end of the stream
    assert [r.levelname for r in caplog.records] == ["WARNING"]  # the connecting one could not be reached


def test_local_classifier_obeys_guard(local, cloud):
    cfg = config(local, cloud, classifier=Slot("ollama", "tiny"))
    router(local, cloud, cfg, guard_read=lambda: WARNING).chat.completions.create(model="auto", messages=turn("hoi"))
    assert local.requests == [] and acted(cloud) == ["auto"]


def test_local_classifier_runs_under_the_watchdog(local, cloud, monkeypatch):
    monkeypatch.setattr(client_mod, "WATCHDOG_INTERVAL_S", 0.05)
    local.script("tiny", {"content": "simple", "delay": 3})
    cfg = config(local, cloud, classifier=Slot("ollama", "tiny"))
    rc = router(local, cloud, cfg, guard_read=lambda: CRITICAL if local.requests else NORMAL)
    started = time.monotonic()
    rc.chat.completions.create(model="auto", messages=turn("hoi"))
    assert time.monotonic() - started < 2 and acted(cloud) == ["auto"]
    assert [(r["model"], r["keep_alive"]) for r in local.requests if r["_path"] == "/api/generate"] == [("tiny", 0)]


def test_default_runtime_version_asks_ollama():
    with FakeOllama({("GET", "/api/version"): (200, {"version": "0.40.16"})}) as fake:
        ollama = Upstream("o", fake.url + "/v1", local=True, runtime="ollama")
        assert client_mod._ollama_version(ollama) == "0.40.16"
        studio = Upstream("s", fake.url + "/v1", local=True, runtime="lmstudio")
        assert client_mod._ollama_version(studio) is None and client_mod._ollama_fit(studio, "m") is None
    assert client_mod._ollama_version(ollama) is None  # server gone


# Ported from the PR's tests/agent/test_router_loop.py (default_route "simple", as in its preset).

@pytest.mark.parametrize("reply, tier, reason", [
    (" COMPLEX.\n", "complex", "classifier"), ("simple", "simple", "classifier"),
    ("banana", "simple", "classifier_error"), ("", "simple", "classifier_error")])
def test_classifier_reply_picks_the_tier(local, cloud, reply, tier, reason):
    cloud.script(CLS, {"content": reply})
    messages = turn("hoi")
    router(local, cloud, config(local, cloud, default_route="simple")).chat.completions.create(
        model="auto", messages=messages)
    decision = client_mod.DECISIONS.get(client_mod.DECISIONS.key_for(messages))
    assert (decision.tier, decision.reason) == (tier, reason)
    assert acted(local) + acted(cloud) == [{"simple": MODEL, "complex": "auto"}[tier]]


def test_classifier_failure_uses_default_route(local, cloud):
    cloud.script(CLS, {"status": 500})
    router(local, cloud, config(local, cloud, default_route="simple")).chat.completions.create(
        model="auto", messages=turn("hoi"))
    assert acted(local) == [MODEL]


def test_classifier_off_uses_default_route(local, cloud):
    cfg = config(local, cloud, classifier=None, default_route="simple")
    router(local, cloud, cfg).chat.completions.create(model="auto", messages=turn("hoi"))
    assert classified(cloud) == [] and acted(local) == [MODEL]


def test_channel_hint_skips_classifier(local, cloud):
    remember_turn(TurnInfo("s1", "whatsapp", "hoi", time.time()))
    cfg = config(local, cloud, channel_hints={"whatsapp": "simple"})
    router(local, cloud, cfg).chat.completions.create(model="auto", messages=turn("hoi"))
    assert classified(cloud) == [] and acted(local) == [MODEL]


def test_fallback_order_and_pinned_slot(local, cloud):
    simple = (Slot("ollama", MODEL), Slot("ollama", "qwen3:4b"), Slot("freellmapi", "auto"))
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 503})
    create = router(local, cloud, config(local, cloud, simple=simple)).chat.completions.create
    create(model="auto", messages=turn("hoi"))
    create(model="auto", messages=turn("hoi") + TOOL_STEP)
    assert acted(local) == [MODEL, "qwen3:4b", "qwen3:4b"] and acted(cloud) == []


def test_kwargs_reach_the_upstream_unchanged(local, cloud):
    seen = []

    class RecordingUpstream:
        def __init__(self, **kw):
            self.chat = NS(completions=NS(create=self.create))

        def create(self, **kw):
            seen.append(kw)
            return iter([]) if kw.get("stream") else NS(choices=[NS(message=NS(content="simple", tool_calls=None))])

        def close(self):
            pass

    kwargs = dict(messages=turn("hoi"), stream=True, stream_options={"include_usage": True}, timeout=42,
                  tools=TOOLS, tool_choice="auto", extra_body={"service_tier": "flex"})
    list(router(local, cloud, openai_factory=RecordingUpstream).chat.completions.create(model="auto", **kwargs))
    classifier_call, acting_call = seen
    assert classifier_call["model"] == CLS and "stream" not in classifier_call
    assert acting_call == {**kwargs, "model": MODEL}


@pytest.mark.parametrize("kw, text, why", [
    ({}, "x" * 40_000, "window"),
    ({"guard_read": lambda: WARNING}, "hoi", "guard"),
    ({"fit_check": lambda up, model: False}, "hoi", "fit"),
])
def test_traces_say_why_a_local_slot_was_skipped(local, cloud, tmp_path, kw, text, why):
    cloud.script(CLS, SIMPLE)
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path, **kw)
    rc.chat.completions.create(model="auto", messages=turn(text))
    record = json.loads(path.read_text())
    assert record["skipped"] == [{"slot": "ollama/llama3.2:3b", "why": why}] and record["slot"] == "freellmapi/auto"
    assert record["tokens_est"] > 0 and record["duration_ms"] >= 0 and "shaped" not in record


def test_traces_name_a_canary_demotion(local, cloud, tmp_path):
    cloud.script(CLS, SIMPLE)
    matrix = Matrix([Cell("0.40.16", MODEL, "A6", "fail")])
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path, matrix_loader=lambda: matrix)
    rc.chat.completions.create(model="auto", messages=turn("write it down"), tools=TOOLS)
    assert json.loads(path.read_text())["skipped"] == [{"slot": "ollama/llama3.2:3b", "why": "canary"}]


def test_traces_keep_one_older_file(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "MAX_BYTES", 5)
    path = tmp_path / "traces.jsonl"
    for i in range(3):
        trace.write_trace(path, {"i": i})
    assert json.loads(path.read_text()) == {"i": 2}
    assert json.loads(path.with_name("traces.jsonl.1").read_text()) == {"i": 1}


def test_a_second_writer_waits_while_the_trace_file_moves(tmp_path, monkeypatch):
    path = tmp_path / "traces.jsonl"
    trace.write_trace(path, {"pad": "x" * 100})
    monkeypatch.setattr(trace, "MAX_BYTES", 50)
    replace, second = trace.os.replace, threading.Thread(target=trace.write_trace, args=(path, {"i": 2}))

    def slow_replace(src, dst):  # a second thread writes a trace while the first one moves the file
        if second.ident is None:
            second.start()
            second.join(0.2)
        replace(src, dst)

    monkeypatch.setattr(trace.os, "replace", slow_replace)
    trace.write_trace(path, {"i": 1})
    second.join()
    assert "pad" in path.with_name("traces.jsonl.1").read_text()
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"i": 1}, {"i": 2}]


def test_a_traces_duration_counts_the_choice_of_the_tier(local, cloud, tmp_path):
    cloud.script(CLS, {**SIMPLE, "delay": 0.2})
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path)
    rc.chat.completions.create(model="auto", messages=turn("hoi"))
    assert json.loads(path.read_text())["duration_ms"] >= 200


@pytest.fixture
def hooks(monkeypatch):
    """The request hooks another plugin, such as Polymetis, would register."""
    store = types.ModuleType(client_mod.HOOKS_MODULE)
    store.local_request = {}
    monkeypatch.setitem(sys.modules, client_mod.HOOKS_MODULE, store)
    return store.local_request


def sent(fake, model):
    return [r["messages"][-1]["content"] for r in fake.requests if r["_path"].endswith("/chat/completions")
            and r["model"] == model]


def test_a_local_request_hook_can_make_a_request_fit(local, cloud, tmp_path, hooks):
    seen = []

    def shrink(request, slot):
        seen.append((slot["upstream"], slot["model"], slot["window"], slot["fits"](request)))
        return {"request": {**request, "messages": turn("hoi")}, "variant": "lean"}

    hooks["test"] = shrink
    cloud.script(CLS, SIMPLE)
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path)
    rc.chat.completions.create(model="auto", messages=turn("x" * 40_000), tools=TOOLS)
    assert seen == [("ollama", MODEL, 8192, False)]
    assert sent(local, MODEL) == ["hoi"] and acted(cloud) == []
    record = json.loads(path.read_text())
    assert (record["slot"], record["shaped"], record["skipped"]) == ("ollama/llama3.2:3b", "lean", [])


def test_cloud_slots_get_the_request_as_it_came(local, cloud, hooks):
    hooks["test"] = lambda request, slot: {"request": {**request, "messages": turn("small")}, "variant": "none"}
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 429})
    router(local, cloud).chat.completions.create(model="auto", messages=turn("the original"))
    assert sent(local, MODEL) == ["small"] and sent(cloud, "auto") == ["the original"]


def test_a_failing_or_silent_hook_leaves_the_request_alone(local, cloud, hooks, caplog):
    def boom(request, slot):
        raise ValueError("secret words")

    hooks["boom"] = boom
    hooks["silent"] = lambda request, slot: None
    client_mod._warn_hook.cache_clear()
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "from local"})
    with caplog.at_level(logging.WARNING):
        reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("hoi"))
    assert reply.choices[0].message.content == "from local" and sent(local, MODEL) == ["hoi"]
    assert "local_request hook boom failed: ValueError" in caplog.text and "secret words" not in caplog.text


def test_what_a_hook_changes_in_place_stays_out_of_cloud_requests(local, cloud, hooks):
    def edit(request, slot):
        request["messages"][-1]["content"] += " EXTRA TEXT FROM HOOK"
        request["tools"].clear()

    hooks["edit"] = edit
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 429})
    router(local, cloud).chat.completions.create(model="auto", messages=turn("the original"), tools=list(TOOLS))
    assert sent(local, MODEL) == ["the original"] and sent(cloud, "auto") == ["the original"]
    assert [r["tools"] for r in cloud.requests if r.get("model") == "auto"] == [TOOLS]


@pytest.mark.parametrize("change, error", [
    (lambda request: {**request, "messages": ["not a message"]}, "AttributeError"),
    (lambda request: {k: v for k, v in request.items() if k != "messages"}, "KeyError"),
    (lambda request: {**request, "messages": None}, "TypeError"),
])
def test_a_hook_whose_request_the_router_cannot_read_is_skipped(local, cloud, hooks, caplog, change, error):
    hooks["bad"] = lambda request, slot: {"request": change(request)}
    client_mod._warn_hook.cache_clear()
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "from local"})
    with caplog.at_level(logging.WARNING):
        reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("hoi"))
    assert reply.choices[0].message.content == "from local" and sent(local, MODEL) == ["hoi"]
    assert f"local_request hook bad failed: {error}" in caplog.text


@pytest.mark.parametrize("stream, change", [
    (True, lambda request: {k: v for k, v in request.items() if k != "stream"}),
    (False, lambda request: {**request, "stream": True}),
])
def test_the_router_streams_as_the_caller_asked_whatever_a_hook_returns(local, cloud, hooks, stream, change):
    hooks["flip"] = lambda request, slot: {"request": change(request), "variant": "lean"}
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"content": "from local"})
    reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("hoi"), stream=stream)
    assert (text_of(reply) if stream else reply.choices[0].message.content) == "from local"


def test_traces_take_only_a_short_name_as_the_variant(local, cloud, tmp_path, hooks):
    hooks["test"] = lambda request, slot: {"request": request, "variant": "private user words"}
    cloud.script(CLS, SIMPLE)
    path = tmp_path / "traces.jsonl"
    rc = router(local, cloud, config(local, cloud, save_traces=True), trace_path=path)
    rc.chat.completions.create(model="auto", messages=turn("hoi"))
    assert json.loads(path.read_text())["shaped"] == "test"


def test_a_changed_request_that_the_local_model_rejects_goes_to_the_next_model(local, cloud, hooks):
    hooks["test"] = lambda request, slot: {"request": {**request, "messages": turn("small")}, "variant": "lean"}
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 400})
    cloud.script("auto", {"content": "from cloud"})
    reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("the original"))
    assert reply.choices[0].message.content == "from cloud" and sent(cloud, "auto") == ["the original"]


def test_a_changed_request_that_the_sdk_refuses_goes_to_the_next_model(local, cloud, hooks):
    hooks["test"] = lambda request, slot: {"request": {**request, "not_a_parameter": 1}, "variant": "lean"}
    cloud.script(CLS, SIMPLE)
    cloud.script("auto", {"content": "from cloud"})
    reply = router(local, cloud).chat.completions.create(model="auto", messages=turn("the original"))
    assert reply.choices[0].message.content == "from cloud" and sent(cloud, "auto") == ["the original"]


def test_a_request_as_it_came_that_the_local_model_rejects_goes_to_hermes(local, cloud):
    cloud.script(CLS, SIMPLE)
    local.script(MODEL, {"status": 400})
    with pytest.raises(openai.BadRequestError):
        router(local, cloud).chat.completions.create(model="auto", messages=turn("hoi"))
    assert acted(cloud) == []
