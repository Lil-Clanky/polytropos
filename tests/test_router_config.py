import pytest
from polytropos_router import config as config_mod
import yaml

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


SPEC_YAML = """
upstreams:
  freellmapi: {base_url: "http://127.0.0.1:3001/v1", api_key_env: FREELLMAPI_API_KEY}
  ollama: {base_url: "http://127.0.0.1:11434/v1", local: true, runtime: ollama, context_window: 8192}
default_route: complex
classifier:
  model: freellmapi/auto
  max_tokens: 16
routes:
  simple: [ollama/llama3.2:3b, freellmapi/auto]
  complex: [freellmapi/auto]
channel_hints:
  whatsapp: simple
save_traces: false
"""


def test_spec_yaml_verbatim():
    cfg = parse_config(yaml.safe_load(SPEC_YAML))
    assert cfg.classifier == Slot("freellmapi", "auto") and cfg.classifier_max_tokens == 16
    assert cfg.upstreams["ollama"].context_window == 8192 and cfg.channel_hints == {"whatsapp": "simple"}


def test_classifier_mapping_options_and_bad_shapes():
    cfg = parse_config({**SPEC, "classifier": {"model": "freellmapi/auto", "max_tokens": 8, "timeout_s": 2}})
    assert (cfg.classifier_max_tokens, cfg.classifier_timeout_s) == (8, 2.0)
    with pytest.raises(ConfigError, match="classifier") as e:
        parse_config({**SPEC, "classifier": ["secret-value"]})
    assert "secret-value" not in str(e.value)


@pytest.mark.parametrize("section", [
    {"default_route": "complex"}, {"channel_hints": {"whatsapp": "simple"}}, {"save_traces": True}, {},
])
def test_partial_sections_fill_from_defaults(tmp_path, section):
    _write(tmp_path, yaml.safe_dump({"plugins": {"entries": {"polytropos-router": section}}}))
    cfg, errors = load_config(tmp_path)
    assert errors == [] and cfg.routes == DEFAULT_CONFIG.routes and cfg.classifier == DEFAULT_CONFIG.classifier


def test_empty_section_is_defaults(tmp_path):
    _write(tmp_path, "plugins:\n  entries:\n    polytropos-router:\n")
    assert load_config(tmp_path) == (DEFAULT_CONFIG, [])


@pytest.mark.parametrize("section", [{"default_route": ["complex"]}, {"channel_hints": {"whatsapp": ["simple"]}}])
def test_unhashable_values_do_not_raise(tmp_path, section):
    _write(tmp_path, yaml.safe_dump({"plugins": {"entries": {"polytropos-router": section}}}))
    cfg, errors = load_config(tmp_path)
    assert cfg == DEFAULT_CONFIG and len(errors) == 1


def test_yaml_error_does_not_echo_secret(tmp_path):
    _write(tmp_path, "a: 1\nb: 2\napi_key: sk-live-SECRET123: oops\n")
    cfg, errors = load_config(tmp_path)
    assert cfg == DEFAULT_CONFIG and len(errors) == 1
    assert "SECRET123" not in errors[0] and "line 3" in errors[0]


def test_loader_error_text_never_printed(tmp_path, monkeypatch):
    class Boom(Exception):
        problem = 'found duplicate key "api_key" with value "sk-live-NEW222"'
        problem_mark = type("M", (), {"line": 3, "column": 4})()

    def loader(_text):
        raise Boom()

    monkeypatch.setattr(config_mod, "safe_load", loader)
    _write(tmp_path, "x: 1\n")
    cfg, errors = load_config(tmp_path)
    assert cfg == DEFAULT_CONFIG and errors == ["config.yaml: Boom at line 4, column 5"]


def test_upstreams_without_routes_is_error(tmp_path):
    raw = {"upstreams": {"ollama": {"base_url": "http://127.0.0.1:11434/v1", "local": True}}}
    with pytest.raises(ConfigError, match="routes: required when upstreams is set"):
        parse_config(raw)
    _write(tmp_path, yaml.safe_dump({"plugins": {"entries": {"polytropos-router": raw}}}))
    cfg, errors = load_config(tmp_path)
    assert cfg == DEFAULT_CONFIG and len(errors) == 1


def test_default_routes_checked_against_upstreams():
    raw = {"upstreams": {"ollama": {"base_url": "http://x/v1"}}, "routes": {"simple": ["freellmapi/auto"]}}
    with pytest.raises(ConfigError, match=r"routes\.simple\[0\]"):
        parse_config(raw)


@pytest.mark.parametrize("entry", [{"model": "ollama/x", "api_key": "sk-SECRET"}, ["sk-SECRET"], 5])
def test_route_entry_not_string_does_not_echo(entry):
    raw = {**SPEC, "routes": {"simple": [entry], "complex": ["freellmapi/auto"]}}
    with pytest.raises(ConfigError, match=r"routes\.simple\[0\]: expected a string") as e:
        parse_config(raw)
    assert "SECRET" not in str(e.value)


@pytest.mark.parametrize("off", [None, False, ""])
def test_classifier_can_be_turned_off(off):
    assert parse_config({**SPEC, "classifier": off}).classifier is None
    assert parse_config({"classifier": off}).classifier is None
    assert parse_config({}).classifier == DEFAULT_CONFIG.classifier
