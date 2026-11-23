"""Directories used by Polytropos, and the port of FreeLLMAPI. Each call reads the environment."""

import json
import os
import sys
from pathlib import Path

FREELLMAPI_DESKTOP_PORT = 31415  # FreeLLMAPI's desktop app, desktop/src/main.ts DEFAULT_PORT


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _xdg(var: str, default: str) -> Path:
    if os.environ.get(var):
        return Path(os.environ[var]) / "polytropos"
    # Windows has no XDG folders: data, state and cache share one per-user folder there.
    return (_local_appdata() if sys.platform == "win32" else Path.home() / default) / "polytropos"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def hermes_home() -> Path:
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    # Hermes's own default (hermes_constants.py): %LOCALAPPDATA%\hermes on Windows
    return _local_appdata() / "hermes" if sys.platform == "win32" else Path.home() / ".hermes"


def freellmapi_port() -> int:
    """3001, where Polytropos runs FreeLLMAPI. On Windows the desktop app's port: the app moves to the next free port
    when its port is taken and saves the one it got as "port" in %APPDATA%\\FreeLLMAPI\\config.json (config.ts)."""
    if sys.platform != "win32":
        return 3001
    try:
        port = json.loads((Path(os.environ["APPDATA"]) / "FreeLLMAPI" / "config.json").read_text(encoding="utf-8"))
        port = port["port"]
    except (KeyError, TypeError, ValueError, OSError):  # no file yet, or not the shape config.ts writes
        return FREELLMAPI_DESKTOP_PORT
    return port if type(port) is int and 0 < port < 65536 else FREELLMAPI_DESKTOP_PORT
