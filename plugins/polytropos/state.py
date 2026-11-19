"""Record of what Polytropos installed and changed, kept in state.json."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ._shared import paths


@dataclass
class State:
    version: int = 1
    profiles: dict[str, dict] = field(default_factory=dict)
    shared: dict[str, dict] = field(default_factory=dict)


def _path(path: Path | None) -> Path:
    return path or paths.state_dir() / "state.json"


def load_state(path: Path | None = None) -> State:
    path = _path(path)
    try:
        data = json.loads(path.read_text())
        return State(version=data["version"], profiles=dict(data["profiles"]), shared=dict(data["shared"]))
    except FileNotFoundError:
        return State()
    except (ValueError, KeyError, TypeError, OSError):
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return State()


def save_state(state: State, path: Path | None = None) -> None:
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state.")  # mkstemp creates mode 0o600
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(state), handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def use_shared(state: State, part: str, profile: str, *, version: str, installed_by_us: bool) -> None:
    entry = state.shared.setdefault(part, {"installed_by_us": installed_by_us, "profiles": []})
    entry["version"] = version
    if profile not in entry["profiles"]:
        entry["profiles"].append(profile)


def release_shared(state: State, part: str, profile: str) -> bool:
    """Drop the profile's use of the part. True when no profile uses it any more."""
    entry = state.shared.get(part)
    if entry is None:
        return True
    if profile in entry["profiles"]:
        entry["profiles"].remove(profile)
    return not entry["profiles"]


def record_config_change(state: State, profile: str, key: str, old: Any, new: Any) -> None:
    changes = state.profiles.setdefault(profile, {}).setdefault("config_changes", [])
    for change in changes:
        if change["key"] == key:
            change["new"] = new  # the first old value stays
            return
    changes.append({"key": key, "old": old, "new": new})


def config_changes(state: State, profile: str) -> list[dict]:
    return list(state.profiles.get(profile, {}).get("config_changes", []))


def set_mode(state: State, part: str, mode: str) -> None:
    """Mode of freellmapi: managed or external."""
    if mode not in ("managed", "external"):
        raise ValueError(f"unknown mode: {mode}")
    state.shared.setdefault(part, {"installed_by_us": False, "profiles": []})["mode"] = mode
