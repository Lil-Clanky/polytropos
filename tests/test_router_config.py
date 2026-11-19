import pytest

from polytropos_router.config import (
    DEFAULT_CONFIG, ConfigError, Slot, cloud_slots_below_floor, load_config,
    parse_config, parse_slot, reported_context,
)
from polytropos_router.tokens import estimate_tokens

SPEC = {
    "upstreams": {
        "ollama": {"base_url": "http://127.0.0.1:11434/v1", "local": True, "runtime": "ollama"},
        "freellmapi": {"base_url": "http://127.0.0.1:3001/v1", "api_key_env": "FREELLMAPI_API_KEY"},
    },
    "routes": {
        "simple": ["ollama/llama3.2:3b", "freellmapi/auto"],
        "complex": ["freellmapi/auto"],
    },
    "classifier": "freellmapi/auto",
}


def _write(tmp_path, text):
    (tmp_path / "config.yaml").write_text(text)


def test_defaults_when_section_missing(tmp_path):
    assert load_config(tmp_path) == (DEFAULT_CONFIG, [])
    _write(tmp_path, "model: x\n")
    assert load_config(tmp_path) == (DEFAULT_CONFIG, [])


def test_parse_spec_example():
    cfg = parse_config(SPEC)
    assert cfg.routes["simple"] == (Slot("ollama", "llama3.2:3b"), Slot("freellmapi", "auto"))
    assert cfg.upstreams["ollama"].local is True
    assert cfg.classifier == Slot("freellmapi", "auto")


def test_load_config_reads_section(tmp_path):
    _write(tmp_path, "plugins:\n  entries:\n    polytropos-router:\n      default_route: simple\n"
                     "      upstreams:\n        a: {base_url: 'http://x/v1'}\n      routes:\n"
                     "        simple: [a/m]\n        complex: [a/m]\n")
    cfg, errors = load_config(tmp_path)
    assert errors == [] and cfg.default_route == "simple" and cfg.classifier is None


def test_slot_with_slash_in_model():
    assert parse_slot("lmstudio/qwen/qwen3-4b") == Slot("lmstudio", "qwen/qwen3-4b")


def test_unknown_upstream_is_error():
    raw = {**SPEC, "routes": {"simple": ["nope/x"], "complex": ["freellmapi/auto"]}}
    with pytest.raises(ConfigError, match=r"routes\.simple\[0\]"):
        parse_config(raw)


def test_slot_without_slash_is_error():
    with pytest.raises(ConfigError, match=r"routes\.simple\[0\]"):
        parse_config({**SPEC, "routes": {"simple": ["auto"], "complex": ["freellmapi/auto"]}})
    with pytest.raises(ValueError):
        parse_slot("auto")


def test_negative_window_is_error():
    raw = {**SPEC, "upstreams": {"freellmapi": {"base_url": "http://x/v1", "context_window": -1}}}
    with pytest.raises(ConfigError, match=r"upstreams\.freellmapi\.context_window"):
        parse_config(raw)


def test_load_config_falls_back_with_errors(tmp_path):
    _write(tmp_path, "plugins:\n  entries:\n    polytropos-router:\n      routes:\n        simple: [nope/x]\n")
    cfg, errors = load_config(tmp_path)
    assert cfg == DEFAULT_CONFIG and len(errors) == 1


def test_reported_context_floor():
    def make(window):
        return parse_config({
            "upstreams": {"c": {"base_url": "http://x/v1", "context_window": window}},
            "routes": {"simple": ["c/m"], "complex": ["c/m"]},
        })
    small = make(32000)
    assert reported_context(small) == 64_000
    assert cloud_slots_below_floor(small) == [Slot("c", "m")]
    raw = {
        "upstreams": {"a": {"base_url": "http://x/v1", "context_window": 200000},
                      "b": {"base_url": "http://y/v1", "context_window": 128000},
                      "l": {"base_url": "http://z/v1", "local": True}},
        "routes": {"simple": ["l/m", "a/m"], "complex": ["b/m"]},
    }
    cfg = parse_config(raw)
    assert reported_context(cfg) == 128000 and cloud_slots_below_floor(cfg) == []


def test_estimate_tokens_list_content():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello there"},
                                              {"type": "image_url", "image_url": {"url": "x"}}]}]
    assert estimate_tokens(messages) > 0
    assert estimate_tokens(messages, [{"type": "function"}]) > estimate_tokens(messages)
