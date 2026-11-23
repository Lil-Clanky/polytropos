"""`hermes polytropos <command>`: doctor, setup, install, adopt, update, rollback, canary, status, uninstall."""

import argparse
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

from . import doctor, programs
from ._shared import canary, paths
from .freellm import PORT, REPO_URL, FreeLLM, Pin, PortBusy, _default_get
from .services import ServiceManager
from .state import (State, config_changes, load_state, record_config_change, release_shared, save_state, set_mode,
                    use_shared)

try:
    from hermes_yaml import safe_load
except ImportError:  # outside Hermes
    from yaml import safe_load

if TYPE_CHECKING:  # the router package is importable only after register() runs
    from polytropos_router.config import RouterConfig

OWN_REPO = "Lil-Clanky/polytropos"
ROUTER_KEY = "plugins.entries.polytropos-router."
# None removes the key: Hermes prefers a configured model.context_length over the router's own window.
ROUTER_DEFAULT = [("model.provider", "polytropos-router"), ("model.default", "auto"), ("model.context_length", None)]
# The non-secret model keys. Uninstall restores them in reverse, so model.provider comes first: setting it drops
# model.base_url and model.api_mode.
MODEL_KEYS = ("model.base_url", "model.api_mode", "model.context_length", "model.default", "model.provider")
PACK_PLUGINS = ("polytropos-router", "ponytail", "superpowers", "polytropos")
PONYTAIL_QUESTION = "ponytail is not installed. Install it from its pinned commit?"
PLUGIN_UPDATES = ("Hermes updates the Polytropos plugins themselves. After an install from the plugin catalog, run "
                  "hermes plugins update. After a pack install, run hermes plugins pack install --force with the "
                  "hermes-pack.yaml of the newer release.")
_HERE = Path(__file__).resolve()


def hermes_home() -> Path:
    """The active profile's home inside Hermes; $HERMES_HOME or ~/.hermes outside it."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:  # outside Hermes
        return paths.hermes_home()
    return Path(get_hermes_home())


@dataclass
class Effects:
    """Every external effect of a command, so tests can replace them."""

    run: Callable = subprocess.run
    ask: Callable[[str], str] = input
    http_get: Callable[[str, dict], tuple[int, bytes]] = _default_get
    download: Callable[[str, Path], None] | None = None
    freellm: FreeLLM | None = None

    def fl(self) -> FreeLLM:
        if self.freellm is None:
            self.freellm = FreeLLM(run=self.run, http_get=self.http_get, services=ServiceManager(run=self.run))
        return self.freellm


def _answer(fx: Effects, prompt: str) -> str:
    try:
        return fx.ask(prompt).strip()
    except EOFError:
        return ""


def _yes(fx: Effects, question: str) -> bool | None:
    """True on y or yes. None when there is no terminal to ask, which callers may take as no."""
    try:
        return fx.ask(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return None


def _ask_router_default(fx: Effects) -> bool | None:
    return _yes(fx, "Make the Polytropos router the default model (provider polytropos-router, model auto)?")


def _repo_file(name: str) -> Path | None:
    return next((d / name for d in (_HERE.parent, _HERE.parents[2]) if (d / name).is_file()), None)


def _lock() -> dict | None:
    path = _repo_file("versions.lock")
    if path is None:
        print("versions.lock was not found next to the plugin or in a Polytropos checkout.")
        return None
    return programs.load_lock(path)


def _pin(lock: dict) -> Pin:
    return Pin(lock["freellmapi"]["tag"], lock["freellmapi"]["commit"])


def _programs(lock: dict) -> list[programs.Program]:
    return programs.programs(lock, platform=sys.platform, machine=platform.machine())


def _read_config(home: Path) -> dict:
    try:
        data = safe_load((home / "config.yaml").read_text())
    except Exception:  # a missing or unreadable file holds no values
        return {}
    return data if isinstance(data, dict) else {}


def _get(config: dict, key: str):
    node = config
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _value(value) -> str:
    """The text that `hermes config set` parses back into value."""
    return value if isinstance(value, str) else json.dumps(value)


def _run(fx: Effects, command: list[str]) -> bool:
    """Run a command and say whether it worked. A command that cannot start says why."""
    try:
        return fx.run(command, check=False).returncode == 0
    except OSError as err:
        print(f"{command[0]} could not run: {err}")
        return False


# --- doctor ---

def _hermes_version(fx: Effects) -> str | None:
    try:  # no input, so a question from Hermes fails at once instead of waiting unseen
        out = fx.run(["hermes", "--version"], capture_output=True, text=True, timeout=30, check=False,
                     stdin=subprocess.DEVNULL).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+\.\d+\.\d+", out)
    return match.group(0) if match else None


def _expected_plugins(home: Path, lock: dict) -> dict:
    """The lock's plugins, and both own plugins at the commit of the general plugin's install record. Without that
    record (a run from a checkout) their ref is empty and the check reports both as not installed."""
    try:
        own = doctor.install_records(home).get((OWN_REPO, "plugins/polytropos"))
    except (OSError, ValueError):  # the plugin check reports it
        own = None
    revision = str(own[1].get("revision") or "") if own else ""
    return {"plugins": [{"repo": OWN_REPO, "ref": revision, "subdir": f"plugins/{name}"}
                        for name in ("polytropos", "polytropos_router")]
            + [{"repo": entry["repo"], "ref": entry["commit"], "name": name}
               for name, entry in lock.get("plugins", {}).items()]}


def _doctor(args, fx: Effects) -> int:
    lock = _lock()
    if lock is None:
        return 1
    home = hermes_home()
    ctx = doctor.DoctorContext(home, Path.home(), sys.platform, lock, _expected_plugins(home, lock), fx.run,
                               fx.http_get, _hermes_version(fx))
    results = doctor.run_checks(ctx)
    print(doctor.format_report(results))
    if any(r.status == "fail" for r in results):
        return 1
    if _get(_read_config(home), "model.provider") == "polytropos-router":
        return 0
    answer = _ask_router_default(fx) if doctor.api_key() else None  # without the key the router cannot answer
    if answer is None:
        print("To make the Polytropos router the default model, run: hermes polytropos setup")
    elif answer:
        if not _write_config(home, load_state(), ROUTER_DEFAULT, fx):
            return 1
        print("The Polytropos router is now the default model.")
    return 0


# --- setup ---

def _setup(args, fx: Effects) -> int:
    if not doctor.api_key():
        print("This profile has no FREELLMAPI_API_KEY. Copy the unified API key from FreeLLMAPI's dashboard and store "
              "it with this command, then run hermes polytropos setup again:")
        print("  hermes config set FREELLMAPI_API_KEY <key>")
        return 1
    home, state = hermes_home(), load_state()
    url = f"http://127.0.0.1:{PORT}"
    if state.shared.get("freellmapi", {}).get("mode") == "external":
        url = re.sub(r"/v1$", "", (_answer(fx, f"FreeLLMAPI URL [{url}]: ") or url).rstrip("/"))
    # In this order every step leaves a router config that parses.
    changes = [(ROUTER_KEY + "routes.complex", ["freellmapi/auto"]),
               (ROUTER_KEY + "routes.simple", ["freellmapi/auto"]),
               (ROUTER_KEY + "classifier", "freellmapi/auto"),
               (ROUTER_KEY + "upstreams.freellmapi", {"base_url": f"{url}/v1", "api_key_env": "FREELLMAPI_API_KEY"})]
    if _ask_router_default(fx):
        changes += ROUTER_DEFAULT
    if not _write_config(home, state, changes, fx):
        return 1
    print("The router settings are written.")
    return 0


def _write_config(home: Path, state: State, changes: list[tuple[str, object]], fx: Effects) -> bool:
    """Set each key through `hermes config set`, or remove it for None, and record its old value for uninstall.
    Stops at a failed key."""
    current, profile = _read_config(home), str(home)
    for key, new in changes:
        old = _get(current, key)
        if old == new:
            continue
        command = ["hermes", "config", "unset", key] if new is None else ["hermes", "config", "set", key, _value(new)]
        if not _run(fx, command):
            print(f"{'Removing' if new is None else 'Setting'} {key} failed. Run this command yourself, then run "
                  "hermes polytropos setup again:")
            print(f"  {shlex.join(command)}")
            return False
        if key == "model.provider":  # Hermes drops the old provider's route keys; recorded first, restored last
            for route_key in ("model.base_url", "model.api_mode"):
                if _get(current, route_key) is not None:
                    record_config_change(state, profile, route_key, _get(current, route_key), None)
        record_config_change(state, profile, key, old, new)
        save_state(state)
    return True


# --- install and adopt ---

def _own_freellmapi(state: State, profile: str, tag: str) -> None:
    use_shared(state, "freellmapi", profile, version=tag, installed_by_us=True)
    state.shared["freellmapi"]["installed_by_us"] = True  # an earlier external record must not hide it from uninstall
    set_mode(state, "freellmapi", "managed")


def _added_by(command: list[str], home: Path) -> Path | str | None:
    """What an integration command adds or changes: a folder or file, or the config key of an MCP server."""
    if command[:3] == ["hermes", "skills", "install"]:  # official/<category>/<name> goes to skills/<category>/<name>
        return home / "skills" / Path(*command[3].split("/")[1:])
    if command[:3] == ["hermes", "mcp", "add"]:
        return f"mcp_servers.{command[3]}"
    if command[1:] == ["init", "--agent", "hermes"]:  # rtk's Hermes plugin
        return home / "plugins" / "rtk"
    if command == ["graphify", "install", "--platform", "hermes"]:  # graphify ignores HERMES_HOME
        return Path.home() / ".hermes" / "skills" / "graphify"
    if command[-1:] == ["setup-opencode"]:  # FreeLLMAPI's CLI merges its provider into OpenCode's config (tools.ts)
        config_home = os.environ.get("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config"
        return Path(config_home) / "opencode" / "opencode.json"
    return None


def _snapshot(target: Path | str, home: Path):
    """A config value, a file's modification time or whether a folder exists, to compare around an install."""
    if isinstance(target, str):
        return _get(_read_config(home), target)
    return target.stat().st_mtime_ns if target.is_file() else target.exists()


def _record_integrations(state: State, profile: str, changed: list[tuple[list[str], Path | str, object]]) -> None:
    for command, target, old in changed:
        if isinstance(target, str):  # an MCP server entry, undone with the other config changes
            if old is None:  # an entry that was there before stays the user's, and its value (keys too) unrecorded
                record_config_change(state, profile, target, None, _snapshot(target, Path(profile)))
            continue
        items = state.profiles.setdefault(profile, {}).setdefault("integrations", [])
        if {"ran": command, "added": str(target)} not in items:
            items.append({"ran": command, "added": str(target)})
    save_state(state)


def _install_program(p: programs.Program, state: State, profile: str, fx: Effects, *,
                     add_integrations: bool = True) -> bool:
    home = Path(profile)
    # Only what this install changes is recorded, so uninstall leaves the user's own setup alone.
    touched = [(command, target, _snapshot(target, home)) for command in p.install_cmds
               if (target := _added_by(command, home)) is not None]
    existing = [c for c, t, old in touched if isinstance(t, str) and old is not None]  # Hermes would ask to overwrite
    # The program serves the whole machine and its integrations belong to each profile, so a profile that lacks one
    # gets it. graphify's skill goes to ~/.hermes whatever the profile, so another profile does not run it again.
    lacking = [c for c, t, old in touched
               if (old is None or old is False) and (isinstance(t, str) or t.is_relative_to(home))]
    # Update never adds an integration the profile lacks, so one the user removed stays removed.
    skipped = existing + ([] if add_integrations else [c for c, t, old in touched if old is None or old is False])
    step = "installed"
    try:
        before = programs.installed_version(p, fx.run)
        if programs.needs_install(p, fx.run):
            programs.install(replace(p, install_cmds=[c for c in p.install_cmds if c not in skipped]), fx.run,
                             download=fx.download)
            print(f"{p.name} {p.version} is installed.")
        elif lacking and add_integrations:
            step = "set up for this profile"
            programs.install(replace(p, asset_url="", install_cmds=lacking), fx.run)
            print(f"{p.name} is set up for this profile.")
    except (subprocess.CalledProcessError, OSError, ValueError) as err:
        print(f"{p.name} was not {step}: {err}")
        return False
    finally:  # also after a failed step, for what the earlier steps changed
        _record_integrations(state, profile, [(c, t, old) for c, t, old in touched if _snapshot(t, home) != old])
    use_shared(state, p.name, profile, version=p.version, installed_by_us=before is None)
    save_state(state)
    return True


def _ponytail(home: Path, lock: dict, fx: Effects) -> bool:
    try:
        programs.ponytail_fallback(home, lock, fx.run)
    except (subprocess.CalledProcessError, OSError) as err:
        print(f"ponytail was not installed: {err}")
        return False
    print("ponytail is installed.")
    return True


def _move_ponytail(home: Path, lock: dict, fx: Effects) -> bool:
    """Move a clone that programs.ponytail_fallback made, which Hermes keeps no record of, to the lock's commit."""
    pin, folder = lock["plugins"]["ponytail"], home / "plugins" / "ponytail"
    try:
        recorded = doctor._repo_subdir(pin["repo"]) in doctor.install_records(home)
    except (OSError, ValueError):  # unreadable records: the doctor reports them, and the clone stays as it is
        return True
    if recorded or not (folder / ".git").exists() or doctor._git_head(fx.run, folder) == pin["commit"]:
        return True
    try:
        for step in (["fetch", "--depth", "1", "origin", pin["commit"]], ["checkout", pin["commit"]]):
            fx.run(["git", "-C", str(folder), *step], check=True)
    except (subprocess.CalledProcessError, OSError) as err:
        print(f"ponytail was not moved to {pin['commit'][:7]}: {err}")
        return False
    print(f"ponytail is now at {pin['commit'][:7]}.")
    return True


def _install(args, fx: Effects) -> int:
    lock = _lock()
    if lock is None:
        return 1
    home, state = hermes_home(), load_state()
    profile, progs, only = str(home), _programs(lock), set(args.only or ())
    known = ["freellmapi", "ponytail"] + [p.name for p in progs]
    if only - set(known):
        print(f"Unknown name: {', '.join(sorted(only - set(known)))}. Known names: {', '.join(known)}.")
        return 1
    failed = 0
    if not only or "freellmapi" in only:
        fl, pin = fx.fl(), _pin(lock)
        found = fl.detect()
        if found.listening and not found.is_freellmapi:
            print(f"Port {PORT} is used by a program that is not FreeLLMAPI, so FreeLLMAPI cannot run there. "
                  f"Stop that program or free port {PORT}, then run hermes polytropos install again.")
            return 1
        if found.managed:
            use_shared(state, "freellmapi", profile, version=fl.current_tag() or pin.tag, installed_by_us=True)
            print("FreeLLMAPI already runs as the Polytropos service.")
        elif found.is_freellmapi:
            use_shared(state, "freellmapi", profile, version="", installed_by_us=False)
            set_mode(state, "freellmapi", "external")
            print(f"A FreeLLMAPI already answers on port {PORT}, so Polytropos uses it as it is. To let Polytropos "
                  "run it as a service, stop it and run: hermes polytropos adopt --from <its checkout>")
        else:
            try:
                fl.install(pin)
                print(f"FreeLLMAPI {pin.tag} runs as a service on 127.0.0.1:{PORT}.")
            except Exception as err:  # build, node, port or service errors stop this step only
                print(f"FreeLLMAPI was not installed: {err}")
                failed += 1
            if fl.services.path().exists():  # also after a late failure, so the service is not taken for external
                _own_freellmapi(state, profile, pin.tag)
        save_state(state)
    if not only or "ponytail" in only:
        if (home / "plugins" / "ponytail").is_dir():
            failed += not _move_ponytail(home, lock, fx)
        elif "ponytail" in only or _yes(fx, PONYTAIL_QUESTION):
            failed += not _ponytail(home, lock, fx)
    for p in progs:
        chosen = p.name in only if only else not p.optional or args.all_optional or _yes(fx, f"Install {p.name}?")
        if chosen and not _install_program(p, state, profile, fx):
            failed += 1
    if failed:
        print("Some parts were not installed. Fix the errors above and run hermes polytropos install again.")
        return 1
    print("Install finished." + (" Next, run hermes polytropos setup." if not only or "freellmapi" in only else ""))
    return 0


def _adopt(args, fx: Effects) -> int:
    lock = _lock()
    if lock is None:
        return 1
    pin = _pin(lock)
    try:
        fx.fl().adopt(args.from_path.expanduser(), pin)
    except Exception as err:  # port, data, node, build or service errors
        print(f"FreeLLMAPI was not adopted: {err}")
        if isinstance(err, PortBusy):  # adopt never stops or disables anything itself
            if sys.platform == "darwin":
                print("If launchd runs the old FreeLLMAPI, find its label with: launchctl list | grep -i freellm")
                print("Then stop it and keep it from starting again:")
                print("  launchctl bootout gui/$(id -u)/<label>")
                print("  launchctl disable gui/$(id -u)/<label>")
            else:
                print("If systemd runs the old FreeLLMAPI, stop it and keep it from starting again with: "
                      "systemctl --user disable --now <unit>")
            print("If a terminal or a dashboard runs it, stop it there. Then run hermes polytropos adopt again.")
        return 1
    state = load_state()
    _own_freellmapi(state, str(hermes_home()), pin.tag)
    save_state(state)
    print(f"FreeLLMAPI {pin.tag} now runs as the Polytropos service with the data from {args.from_path}.")
    return 0


# --- update and rollback ---

def _update_programs(lock: dict, state: State, profile: str, fx: Effects) -> int:
    """Default programs, plus the optional ones this profile installed, at the new pins."""
    chosen = [p for p in _programs(lock)
              if not p.optional or profile in state.shared.get(p.name, {}).get("profiles", [])]
    return sum(not _install_program(p, state, profile, fx, add_integrations=False) for p in chosen)


def _canary_check(fx: Effects) -> Callable[[], bool]:
    """The update's extra health check, run once the new release answers on /v1/models: stack-canary's tool checks
    on the first cloud model it can reach. Only a fail result rolls the update back. A check that cannot run says
    why and lets the update stand."""
    def check() -> bool:
        try:
            cfg, _ = doctor._router().load_config(hermes_home())
        except ImportError:  # the router plugin is not installed, or its code does not import
            print("The tool-call check was skipped: the router plugin could not be loaded.")
            return True
        slot = next((s for slots in cfg.routes.values() for s in slots
                     if not cfg.upstreams[s.upstream].local and _loopback(cfg.upstreams[s.upstream].base_url)), None)
        if not slot:
            print("The tool-call check was skipped: the router has no cloud model on this machine to test.")
            return True
        verdict = run_canary(cfg, run=fx.run, models=[slot.model]).get(f"{slot.upstream}/{slot.model}")
        if verdict is None:  # run_canary said why
            print("The tool-call check was skipped, so the update stands.")
        return verdict != "fail"
    return check


def _update_freellmapi(lock: dict, state: State, profile: str, fx: Effects) -> int:
    pin = _pin(lock)
    mode = state.shared.get("freellmapi", {}).get("mode")
    if mode != "managed":
        print(f"FreeLLMAPI does not run as the Polytropos service. Polytropos pins {pin.tag}; update it yourself."
              if mode == "external" else "FreeLLMAPI is not installed. Run hermes polytropos install first.")
        return 0
    result = fx.fl().update(pin, check=_canary_check(fx))
    print(f"FreeLLMAPI: {result.message}")
    if result.ok:
        use_shared(state, "freellmapi", profile, version=pin.tag, installed_by_us=True)
        save_state(state)
    return 0 if result.ok else 1


def _update(args, fx: Effects) -> int:
    """Moves ponytail, the programs and FreeLLMAPI to the pins in the installed plugin's versions.lock. It never updates
    the Polytropos plugins themselves: Hermes does that, so every new plugin commit is one that Hermes installs."""
    if (lock := _lock()) is None:
        return 1
    home, state, everything = hermes_home(), load_state(), args.component == "all"
    profile = str(home)
    managed = state.shared.get("freellmapi", {}).get("mode") == "managed"
    old = fx.fl().current_tag() if managed and args.component != "programs" else ""
    new = _pin(lock).tag
    if old == new and args.component == "freellmapi":
        print(f"FreeLLMAPI is already on {new}.")
        return 0
    question = {"all": "Update ponytail, the programs and FreeLLMAPI to the versions Polytropos pins?",
                "programs": "Update the programs to the versions Polytropos pins?",
                "freellmapi": f"Update FreeLLMAPI to {new}?"}[args.component]
    if old and old != new:  # FreeLLMAPI keeps no changelog file, so GitHub's comparison stands in for it
        print(f"FreeLLMAPI {old} is installed, and Polytropos pins FreeLLMAPI {new}.")
        print(f"What changed in FreeLLMAPI: {REPO_URL.removesuffix('.git')}/compare/{old}...{new}")
        if args.component == "freellmapi":
            question = f"Update FreeLLMAPI from {old} to {new}?"
    if not _yes(fx, question):
        print("Nothing changed.")
        return 0
    failed = 0
    if everything:
        if (home / "plugins" / "ponytail").is_dir():
            failed += not _move_ponytail(home, lock, fx)
        elif _yes(fx, PONYTAIL_QUESTION):
            failed += not _ponytail(home, lock, fx)
    if args.component in ("programs", "all"):
        failed += _update_programs(lock, state, profile, fx)
    if args.component in ("freellmapi", "all"):
        failed += _update_freellmapi(lock, state, profile, fx)
    if everything:
        print(PLUGIN_UPDATES)
    return 1 if failed else 0


def _rollback(args, fx: Effects) -> int:
    if not _yes(fx, "Restore FreeLLMAPI from the backup of its last update? The database and keys go back to "
                    "that backup, so changes made since the update are lost."):
        print("Nothing changed.")
        return 0
    result = fx.fl().rollback()
    print(result.message)
    return 0 if result.ok else 1


# --- uninstall ---

def _remove_program(p: programs.Program, fx: Effects) -> None:
    local = Path.home() / ".local"
    first = p.install_cmds[0] if p.install_cmds else []
    if p.asset_url:  # rtk and the macOS Ollama were unpacked into ~/.local
        (local / "bin" / p.name).unlink(missing_ok=True)
        shutil.rmtree(local / "lib" / "polytropos" / p.name, ignore_errors=True)
    elif first[:3] == ["uv", "tool", "install"]:
        spec = first[3]
        name = (spec.rsplit("@", 1)[0].rsplit("/", 1)[-1].removesuffix(".git") if spec.startswith("git+")
                else spec.split("==")[0])
        fx.run(["uv", "tool", "uninstall", name], check=True)
    elif first[:3] == ["npm", "install", "-g"]:
        fx.run(["npm", "uninstall", "-g", first[3].rsplit("@", 1)[0]], check=True)
    else:
        print(f"Polytropos cannot remove {p.name} on this system. Remove it yourself.")


def _undo_integration(item: dict, home: Path, fx: Effects) -> bool:
    """Undo one recorded integration of this profile. False keeps the record, so the next uninstall tries again."""
    ran, added = item["ran"], Path(item["added"])
    if ran[-1:] == ["setup-opencode"]:  # no safe undo: it merged its entries into the user's own OpenCode files
        data_home = os.environ.get("XDG_DATA_HOME", "").strip() or Path.home() / ".local" / "share"
        print(f"FreeLLMAPI's setup-opencode added the freellmapi provider to {added} and set a freellmapi model as "
              f"OpenCode's default, and it stored FreeLLMAPI's key under freellmapi in "
              f"{Path(data_home) / 'opencode' / 'auth.json'}. If OpenCode should stop using FreeLLMAPI, remove that "
              "provider and that entry and change the model there by hand.")
        return True
    if ran[:3] == ["hermes", "skills", "install"]:
        command = ["hermes", "skills", "uninstall", added.name, "--yes"]
    elif ran[1:] == ["init", "--agent", "hermes"]:
        command = [*ran, "--uninstall"]  # rtk's own undo removes its Hermes plugin and its config entry
    elif added.is_relative_to(home):
        shutil.rmtree(added, ignore_errors=True)
        return True
    else:  # graphify writes to ~/.hermes whatever the profile, so the default profile may use this copy
        print(f"{shlex.join(ran)} added {added}, which is outside this profile. "
              "Remove it yourself if no other profile uses it.")
        return True
    if not (done := _run(fx, command)):
        print(f"Undoing {shlex.join(ran)} failed. Run this command yourself: {shlex.join(command)}")
    return done


def _remove_freellmapi(fl: FreeLLM) -> None:
    fl.services.remove()
    shutil.rmtree(fl.root / "releases", ignore_errors=True)
    (fl.root / "current").unlink(missing_ok=True)


def _restorable(changes: list[dict]) -> list[dict]:
    """The recorded changes, each key once with its first old value. Polytropos 0.1.1 recorded the whole model block
    under the key model, API key included; only its non-secret keys come back."""
    restore = {}
    for change in changes:
        if change["key"] != "model":
            restore.setdefault(change["key"], change)
            continue
        old = change["old"]
        old = old if isinstance(old, dict) else {} if old is None else {"default": old}
        if "api_key" in old:
            print("The model section from before the setup held an api_key, which uninstall does not write back. "
                  "If your model needs it, set it again with hermes config set model.api_key.")
        for key in MODEL_KEYS:
            restore.setdefault(key, {"key": key, "old": old.get(key.removeprefix("model.")), "new": None})
    return list(restore.values())


def _uninstall(args, fx: Effects) -> int:
    home, state = hermes_home(), load_state()
    profile, failed = str(home), 0
    current, kept = _read_config(home), []
    for change in reversed(_restorable(config_changes(state, profile))):
        key, old = change["key"], change["old"]
        if _get(current, key) == old:
            continue
        command = ["hermes", "config", "unset", key] if old is None else ["hermes", "config", "set", key, _value(old)]
        if not _run(fx, command):
            if isinstance(old, dict):  # a block can hold an API key, which no output may show
                print(f"Restoring {key} failed. Its old value stays recorded in {paths.state_dir() / 'state.json'}, "
                      "and the next hermes polytropos uninstall tries again.")
            else:
                print(f"Restoring {key} failed. Run this command yourself: {shlex.join(command)}")
            kept.insert(0, change)
            failed += 1
    record = state.profiles.setdefault(profile, {})
    left = [item for item in record.get("integrations", []) if not _undo_integration(item, home, fx)]
    failed += len(left)
    record.update(config_changes=kept, integrations=left)
    state.profiles[profile] = {key: value for key, value in record.items() if value}
    if not state.profiles[profile]:
        del state.profiles[profile]
    save_state(state)
    lock = _repo_file("versions.lock")
    progs = {p.name: p for p in _programs(programs.load_lock(lock))} if lock else {}
    fl = fx.fl()
    for part, entry in list(state.shared.items()):
        if profile not in entry.get("profiles", []) or not release_shared(state, part, profile):
            continue  # another profile still uses it
        try:
            if entry.get("installed_by_us"):
                if part == "freellmapi":
                    _remove_freellmapi(fl)
                elif part in progs:
                    _remove_program(progs[part], fx)
                else:
                    print(f"Polytropos cannot remove {part} without versions.lock. Remove it yourself.")
        except (subprocess.CalledProcessError, OSError, RuntimeError) as err:
            print(f"{part} was not removed: {err}")
            entry["profiles"].append(profile)  # keep the record, so the next uninstall tries again
            failed += 1
            continue
        del state.shared[part]
    save_state(state)
    if args.purge_data:
        if "freellmapi" in state.shared:
            print(f"FreeLLMAPI is still in use or was not removed, so {fl.root} stays.")
        elif _answer(fx, f"Type yes to delete {fl.root} with FreeLLMAPI's database, keys and backups: ") == "yes":
            shutil.rmtree(fl.root, ignore_errors=True)
            print(f"Deleted {fl.root}.")
        else:
            print(f"Nothing was deleted. FreeLLMAPI's data stays in {fl.root / 'data'}.")
    elif (fl.root / "data").is_dir():
        print(f"FreeLLMAPI's data stays in {fl.root / 'data'}. "
              "To delete it, run hermes polytropos uninstall --purge-data.")
    names = [n for n in PACK_PLUGINS if (home / "plugins" / n).is_dir()]
    if names:
        print("Remove the pack plugins with these commands, then restart the gateway with hermes gateway restart:")
        for name in names:
            print(f"  hermes plugins remove {name}")
    return 1 if failed else 0


# --- status and canary ---

def _status(args, fx: Effects) -> int:
    from . import slash  # slash imports this module

    print(slash.handle(""))
    return 0


def _loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_canary(cfg: "RouterConfig", *, run=subprocess.run, models: list[str] | None = None) -> dict[str, str]:
    """Run stack-canary's tool checks once on each cloud model in the routes (or only those in models), store
    "<upstream>/<model>" -> pass or fail with the local results and return the new entries."""
    results = {}
    for up in cfg.upstreams.values():
        names = list(dict.fromkeys(s.model for slots in cfg.routes.values() for s in slots
                                   if s.upstream == up.name and (models is None or s.model in models)))
        if up.local or not names:
            continue
        if not _loopback(up.base_url):
            print(f"Skipped {up.name}: stack-canary tests only endpoints on this machine, and {up.base_url} is not one.")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.json"
            argv = ["canary", "run", "--openai-endpoint", up.base_url]
            argv += ["--api-key-env", up.api_key_env] if up.api_key_env else []  # stack-canary reads the key itself
            for name in names:
                argv += ["--model", name]
            argv += ["--attempts", "1", "--summary-json", str(summary)]
            try:
                run(argv, check=False, cwd=tmp)
            except FileNotFoundError:
                print("stack-canary is not installed, so nothing was tested. "
                      "Install it with: hermes polytropos install")
                break
            except OSError as err:  # there but not runnable
                print(f"stack-canary gave no result for {up.name}: {err}")
                continue
            try:
                rows = [r for r in json.loads(summary.read_text())["results"] if isinstance(r, dict)]
            except (OSError, ValueError, KeyError, TypeError) as err:
                print(f"stack-canary gave no result for {up.name}: {err}")
                continue
        for name in names:
            status = {r.get("check"): r.get("status") for r in rows if r.get("model") == name}
            passed = all(status.get(check) == "pass" for check in canary.TOOL_CHECKS)
            results[f"{up.name}/{name}"] = "pass" if passed else "fail"
    if results:
        canary.save_local_results({**canary.load_local_results(), **results})
    return results


def _canary(args, fx: Effects) -> int:
    try:
        cfg, errors = doctor._router().load_config(hermes_home())
    except ImportError:
        print("The polytropos-router plugin is not installed, so there are no routes to test.")
        return 1
    for error in errors:
        print(f"The router config has an error, so the default routes are tested: {error}")
    results = run_canary(cfg, run=fx.run)
    if not results:
        print("No cloud model was tested.")
        return 1
    print("stack-canary tool checks A1 to A6, one attempt each:")
    for slot, verdict in sorted(results.items()):
        print(f"  {verdict:<6}{slot}")
    return 1 if "fail" in results.values() else 0


_COMMANDS = {"doctor": _doctor, "setup": _setup, "install": _install, "adopt": _adopt, "update": _update,
             "rollback": _rollback, "canary": _canary, "status": _status, "uninstall": _uninstall}


def setup_parser(parser: argparse.ArgumentParser) -> None:
    # The dest must differ from Hermes's own `command`, which names `polytropos` itself.
    sub = parser.add_subparsers(dest="polytropos_command", required=True, metavar="<command>")
    sub.add_parser("doctor", help="check the Polytropos setup")
    sub.add_parser("setup", help="write the router settings for FreeLLMAPI")
    p = sub.add_parser("install", help="install FreeLLMAPI, ponytail and the pinned programs")
    p.add_argument("--all-optional", action="store_true", help="also install every optional program")
    p.add_argument("--only", nargs="+", metavar="NAME", help="install only these parts")
    p = sub.add_parser("adopt", help="run an existing FreeLLMAPI checkout as the Polytropos service")
    p.add_argument("--from", dest="from_path", type=Path, required=True, metavar="PATH",  # Hermes's --source uses "source"
                   help="the FreeLLMAPI checkout whose data to take over")
    p = sub.add_parser("update", help="update ponytail, the programs and FreeLLMAPI to the pinned versions")
    p.add_argument("component", nargs="?", choices=("freellmapi", "programs", "all"), default="all")
    p = sub.add_parser("rollback", help="restore FreeLLMAPI from the backup of its last update")
    p.add_argument("part", choices=("freellmapi",))
    p = sub.add_parser("canary", help="run the stack-canary tool checks on the cloud models")
    p.add_argument("canary_command", choices=("run",))
    sub.add_parser("status", help="show FreeLLMAPI health, the guard verdict and the last router decision")
    p = sub.add_parser("uninstall", help="remove what Polytropos installed and restore the config")
    p.add_argument("--purge-data", action="store_true",
                   help="also delete FreeLLMAPI's database and keys after you type yes")


def handle(args: argparse.Namespace, fx: Effects | None = None) -> int:
    return _COMMANDS[args.polytropos_command](args, fx or Effects())
