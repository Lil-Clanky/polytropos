"""Directories used by Polytropos. Each call reads the environment."""

import os
import sys
from pathlib import Path


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
