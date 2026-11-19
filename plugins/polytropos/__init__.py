"""Polytropos general plugin: the `hermes polytropos` command, the /polytropos chat command and the hooks."""

import sys
import types
from pathlib import Path

from . import cli, hooks, slash

try:
    from hermes_yaml import safe_load
except ImportError:  # outside Hermes
    from yaml import safe_load


def _router_folder(home: Path) -> Path | None:
    for base in (home / "plugins", home / "plugins" / "model-providers"):
        try:
            folders = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for folder in folders:
            try:
                manifest = safe_load((folder / "plugin.yaml").read_text())
            except Exception:  # no manifest or an unreadable one
                continue
            if isinstance(manifest, dict) and manifest.get("name") == "polytropos-router":
                return folder
    return None


def _stub_router() -> None:
    """Make `polytropos_router` importable for the doctor. Hermes loads the router under another
    module name, so this package only points at the router's folder and never runs its __init__."""
    if "polytropos_router" in sys.modules:
        return
    folder = _router_folder(cli.hermes_home())
    if folder is not None:
        stub = types.ModuleType("polytropos_router")
        stub.__path__ = [str(folder)]
        sys.modules["polytropos_router"] = stub


def register(ctx) -> None:
    _stub_router()
    ctx.register_cli_command("polytropos", "Polytropos doctor, installer and FreeLLMAPI manager",
                             cli.setup_parser, cli.handle)
    ctx.register_command("polytropos", slash.handle,
                         description="Show FreeLLMAPI health, the guard verdict and the last router decision")
    hooks.configure(ctx.get_config)
    ctx.register_hook("pre_llm_call", hooks.on_pre_llm_call)
    ctx.register_hook("pre_api_request", hooks.on_pre_api_request)
    ctx.register_hook("post_llm_call", hooks.on_post_llm_call)
    ctx.register_hook("on_session_end", hooks.on_session_end)
