import threading
import time

import openai

from polytropos_router.classifier import CLASSIFIER_PROMPT, classify
from polytropos_router.config import DEFAULT_CONFIG, RouterConfig, Slot
from polytropos_router.decisions import Decision, DecisionCache, decide, is_new_turn
from polytropos_shared import bridge
from polytropos_shared.bridge import TurnInfo, remember_turn

import pytest


@pytest.fixture(autouse=True)
def clean_store():
    bridge._clear()
    yield
    bridge._clear()


SYS = {"role": "system", "content": "s"}
USER = {"role": "user", "content": "hello"}
TOOL_LOOP = [SYS, USER, {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
             {"role": "tool", "tool_call_id": "1", "content": "r"}]


def cfg(**kw):
    return RouterConfig(DEFAULT_CONFIG.upstreams, {"simple": DEFAULT_CONFIG.routes["simple"],
                                                   "complex": DEFAULT_CONFIG.routes["complex"]},
                        **{"classifier": Slot("freellmapi", "auto"), **kw})


def test_turn_rule():
    assert is_new_turn([SYS, USER]) and not is_new_turn(TOOL_LOOP) and not is_new_turn([])


def test_key_stable_across_tool_loop():
    c = DecisionCache()
    assert c.key_for([SYS, USER]) == c.key_for(TOOL_LOOP)


def test_key_changes_on_next_user_message():
    c = DecisionCache()
    assert c.key_for(TOOL_LOOP) != c.key_for(TOOL_LOOP + [{"role": "user", "content": "hello"}])
    assert c.key_for([SYS]) is None


def test_key_covers_user_history():
    c = DecisionCache()
    other = [SYS, {"role": "user", "content": "different"},
             {"role": "assistant", "content": "a"}, {"role": "user", "content": "hello"}]
    same_last = [SYS, USER, {"role": "assistant", "content": "a"}, {"role": "user", "content": "hello"}]
    assert c.key_for(other) != c.key_for(same_last)
    assert c.key_for(same_last) == c.key_for(same_last + [{"role": "assistant", "content": None}, {"role": "tool", "content": "r"}])


def test_key_handles_multimodal():
    msg = {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {}}]}
    assert DecisionCache().key_for([SYS, msg]) is not None


def test_hint_wins_over_classifier():
    remember_turn(TurnInfo("s1", "whatsapp", "hello", time.time()))
    called = []
    d = decide(cfg(channel_hints={"whatsapp": "simple"}), [SYS, USER],
               classify_fn=lambda t: called.append(t) or "complex")
    assert d == Decision("simple", "hint:whatsapp") and not called


def test_classifier_result_used():
    assert decide(cfg(), [SYS, USER], classify_fn=lambda t: "simple") == Decision("simple", "classifier")


def test_classifier_none_uses_default():
    assert decide(cfg(), [SYS, USER], classify_fn=lambda t: None) == Decision("complex", "classifier_error")


def test_no_classifier_is_default():
    assert decide(cfg(classifier=None), [SYS, USER], classify_fn=lambda t: 1 / 0).reason == "default"


def test_classify_parses_reply(fake_openai):
    fake_openai.script("c", {"content": "complex."}, {"content": "simple or complex"}, {"status": 500})
    client = openai.OpenAI(base_url=fake_openai.base_url, api_key="x", max_retries=0)
    run = lambda: classify(client.chat.completions.create, Slot("u", "c"), "hi", max_tokens=16, timeout_s=5)
    assert run() == "complex"
    assert run() is None
    assert run() is None
    first = fake_openai.requests[0]
    assert first["messages"][0]["content"] == CLASSIFIER_PROMPT
    assert first["max_tokens"] == 16 and first["temperature"] == 0


def test_classify_sends_at_most_2000_characters():
    sent = []

    def create(**kw):
        sent.append(kw["messages"][1]["content"])
        raise RuntimeError

    long = "a" * 1000 + "middle" + "z" * 1000
    classify(create, Slot("u", "c"), long, max_tokens=16, timeout_s=5)
    classify(create, Slot("u", "c"), "x" * 2000, max_tokens=16, timeout_s=5)
    assert sent == ["a" * 1000 + "\n" + "z" * 1000, "x" * 2000]


def test_cache_thread_safety():
    cache = DecisionCache(max_entries=256)
    errors = []

    def work(n):
        try:
            for i in range(500):
                cache.put(f"{n}:{i}", Decision("simple", "classifier"))
                cache.get(f"{n}:{i // 2}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(cache) <= 256
