import os
import subprocess
import sys
from pathlib import Path

import yaml

import polytropos_router
from polytropos_router import PolytroposRouterProfile


def _profile():
    return polytropos_router.REGISTERED[-1]


def _write_config(home, section):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {"polytropos-router": section}}}))


def test_profile_fields():
    p = _profile()
    assert isinstance(p, PolytroposRouterProfile)
    assert (p.name, p.base_url, p.env_vars, p.fallback_models) == (
        "polytropos-router", "polytropos://router", ("FREELLMAPI_API_KEY",), ("auto",))
    assert p.fetch_models() == ["auto"]


def test_create_client_uses_hermes_home_config(tmp_home, fake_openai):
    _write_config(tmp_home / ".hermes", {
        "upstreams": {"cloud": {"base_url": fake_openai.base_url}},
        "routes": {"complex": ["cloud/auto"]}, "classifier": None})
    client = _profile().create_client(api_key="k")
    reply = client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "hoi"}])
    assert reply.choices[0].message.content == "ok"
    assert [r["model"] for r in fake_openai.requests if r["_path"].endswith("/chat/completions")] == ["auto"]


def test_bad_config_is_logged_and_defaults_apply(tmp_home, caplog):
    _write_config(tmp_home / ".hermes", {"routes": "nope"})
    with caplog.at_level("WARNING", logger="hermes_plugins.polytropos_router"):
        _profile().create_client(api_key="secret-key")
    assert caplog.records and "secret-key" not in caplog.text


def test_context_length_floor(tmp_home):
    assert _profile().get_model_context_length("auto") == 128_000
    _write_config(tmp_home / ".hermes", {
        "upstreams": {"cloud": {"base_url": "http://x/v1", "context_window": 32_000}},
        "routes": {"complex": ["cloud/auto"]}})
    assert _profile().get_model_context_length("auto") == 64_000


def test_import_without_hermes():
    code = "import sys; import polytropos_router as p; assert 'providers' not in sys.modules; print(len(p.REGISTERED))"
    plugins = str(Path(polytropos_router.__file__).parents[1])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": plugins, "HOME": "/nonexistent",
                              **{k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}})  # Windows
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1"
