"""Directories used by Polytropos, and the port of FreeLLMAPI. Each call reads the environment."""

import json
import os
import sys
from pathlib import Path

FREELLMAPI_DESKTOP_PORT = 31415  # FreeLLMAPI's desktop app, desktop/src/main.ts DEFAULT_PORT
_CONFIG = "FreeLLMAPI/config.json"  # the desktop app's settings under %APPDATA%, desktop/src/config.ts


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


def default_hermes_home() -> Path:
    """Hermes's own default (hermes_constants.py): %LOCALAPPDATA%\\hermes on Windows, ~/.hermes elsewhere."""
    return _local_appdata() / "hermes" if sys.platform == "win32" else Path.home() / ".hermes"


def hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"]) if os.environ.get("HERMES_HOME") else default_hermes_home()


def _saved_port(config: Path) -> int | None:
    try:
        port = json.loads(config.read_text(encoding="utf-8"))["port"]
    except (KeyError, TypeError, ValueError, OSError):  # no file yet, or not the shape config.ts writes
        return None
    return port if type(port) is int and 0 < port < 65536 else None


def freellmapi_port() -> int:
    """3001, where Polytropos runs FreeLLMAPI. On Windows the desktop app's port: the app moves to the next free port
    when its port is taken and saves the one it got as "port" in %APPDATA%\\FreeLLMAPI\\config.json (config.ts)."""
    if sys.platform != "win32":
        return 3001
    # The Store (MSIX) app, identityName TashAhmed.FreeLLMAPI in desktop/electron-builder.yml, reads and writes its own
    # copy of %APPDATA% in its package folder. That copy comes first, so a plain file that an earlier .exe install left
    # does not win; of several packages the newest copy counts.
    try:
        stored = sorted(_local_appdata().glob(f"Packages/TashAhmed.FreeLLMAPI_*/LocalCache/Roaming/{_CONFIG}"),
                        key=lambda config: config.stat().st_mtime, reverse=True)
        plain = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / _CONFIG
    except (OSError, RuntimeError):  # an unreadable folder, or no home: this runs when the plugins load
        return FREELLMAPI_DESKTOP_PORT
    return next((port for config in [*stored, plain] if (port := _saved_port(config))), FREELLMAPI_DESKTOP_PORT)
