"""Record of what Polytropos installed and changed, kept in state.json."""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ._shared import paths

_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")  # a ${VAR} pointer, never a key itself


@dataclass
class State:
    version: int = 1
    profiles: dict[str, dict] = field(default_factory=dict)
    shared: dict[str, dict] = field(default_factory=dict)


def _path(path: Path | None) -> Path:
    return path or paths.state_dir() / "state.json"


def _section(value) -> dict:
    return value if isinstance(value, dict) else {} if value is None else {"default": value}


def _split_model(changes: list[dict]) -> list[dict] | None:
    """Polytropos 0.1.1 recorded the whole model section under the key model, API key included. Its keys become
    records of their own, model.provider last so that uninstall restores it first, and model.api_key keeps no value:
    only a ${VAR} pointer that setup wrote, and whether the old section had a key. None when there is no such record."""
    if not any(change["key"] == "model" for change in changes):
        return None
    split: dict[str, dict] = {}
    for change in changes:
        parts = [change]
        if change["key"] == "model":
            old, new = _section(change["old"]), _section(change["new"])
            keys = sorted((set(old) | set(new)) - {"api_key"}, key=lambda k: (k == "provider", k))
            parts = [{"key": f"model.{k}", "old": old.get(k), "new": new.get(k)} for k in keys]
            pointer = new.get("api_key") if _REFERENCE.fullmatch(str(new.get("api_key"))) else None
            if pointer or "api_key" in old:
                parts.append({"key": "model.api_key", "old": None, "new": pointer, "old_dropped": "api_key" in old})
        for part in parts:
            if part["key"] in split:
                split[part["key"]]["new"] = part["new"]  # the first old value stays
            else:
                split[part["key"]] = part
    return list(split.values())


def load_state(path: Path | None = None) -> State:
    path = _path(path)
    try:
        data = json.loads(path.read_text())
        state = State(version=data["version"], profiles=dict(data["profiles"]), shared=dict(data["shared"]))
        split = {profile: _split_model(record.get("config_changes", [])) for profile, record in state.profiles.items()}
    except FileNotFoundError:
        return State()
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return State()
    if any(changes is not None for changes in split.values()):
        for profile, changes in split.items():
            if changes is not None:
                state.profiles[profile]["config_changes"] = changes
        try:
            save_state(state, path)  # so no API key of a 0.1.1 record stays on disk
        except OSError:  # the next load tries again
            pass
    return state


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
