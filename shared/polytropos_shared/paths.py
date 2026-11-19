"""Directories used by Polytropos. Each call reads the environment."""

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return (Path(os.environ[var]) if os.environ.get(var) else Path.home() / default) / "polytropos"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"]) if os.environ.get("HERMES_HOME") else Path.home() / ".hermes"
