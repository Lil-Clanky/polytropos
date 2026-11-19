"""`hermes polytropos <command>`: doctor, setup, install, adopt, update, rollback, canary, status, uninstall."""

import argparse
import ipaddress
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

from . import doctor, programs
from ._shared import canary, paths
from .freellm import PORT, FreeLLM, Pin, _default_get
from .services import ServiceManager
from .state import (State, config_changes, load_state, record_config_change, release_shared, save_state, set_mode,
                    use_shared)

try:
    from hermes_yaml import safe_load
except ImportError:  # outside Hermes
    from yaml import safe_load

if TYPE_CHECKING:  # the router package is importable only after register() runs
    from polytropos_router.config import RouterConfig

RELEASE_API = "https://api.github.com/repos/Lil-Clanky/polytropos/releases/latest"
RAW = "https://raw.githubusercontent.com/Lil-Clanky/polytropos"
OWN_REPO = "Lil-Clanky/polytropos"
ROUTER_KEY = "plugins.entries.polytropos-router."
PACK_PLUGINS = ("polytropos-router", "ponytail", "superpowers", "polytropos")
_TAG = re.compile(r"[A-Za-z0-9._-]+")
_HERE = Path(__file__).resolve()


def hermes_home() -> Path:
    """The active profile's home inside Hermes; $HERMES_HOME or ~/.hermes outside it."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:  # outside Hermes
        return paths.hermes_home()
    return Path(get_hermes_home())


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (fixed https URLs)
        return response.read()


@dataclass
class Effects:
    """Every external effect of a command, so tests can replace them."""

    run: Callable = subprocess.run
    fetch: Callable[[str], bytes] = _fetch
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


def _yes(fx: Effects, question: str) -> bool:
    return _answer(fx, f"{question} [y/N] ").lower() in ("y", "yes")


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


# --- doctor ---

def _hermes_version(fx: Effects) -> str | None:
    try:
        out = fx.run(["hermes", "--version"], capture_output=True, text=True, timeout=30, check=False).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+\.\d+\.\d+", out)
    return match.group(0) if match else None


def _doctor(args, fx: Effects) -> int:
    lock = _lock()
    if lock is None:
        return 1
    pack_file = _repo_file("hermes-pack.yaml")
    pack = (safe_load(pack_file.read_text()) or {}) if pack_file else {}
    ctx = doctor.DoctorContext(hermes_home(), Path.home(), sys.platform, lock, pack, fx.run, fx.http_get,
                               _hermes_version(fx))
    results = doctor.run_checks(ctx)
    print(doctor.format_report(results))
    if pack_file is None:
        print("The pack file was not found, so the plugin check was skipped.")
    return 1 if any(r.status == "fail" for r in results) else 0


# --- setup ---

def _setup(args, fx: Effects) -> int:
    lock = _lock()
    if lock is None:
        return 1
    home, state = hermes_home(), load_state()
    url = f"http://127.0.0.1:{PORT}"
    command = fx.fl().cli_command(lock) + ["setup-hermes"]
    if state.shared.get("freellmapi", {}).get("mode") == "external":
        url = re.sub(r"/v1$", "", (_answer(fx, f"FreeLLMAPI URL [{url}]: ") or url).rstrip("/"))
        command += ["--url", url]
    if fx.run(command, check=False).returncode != 0:
        print("FreeLLMAPI's setup-hermes failed, so the router settings were not written.")
        return 1
    # In this order every step leaves a router config that parses.
    changes = [(ROUTER_KEY + "routes.complex", ["freellmapi/auto"]),
               (ROUTER_KEY + "routes.simple", ["freellmapi/auto"]),
               (ROUTER_KEY + "classifier", "freellmapi/auto"),
               (ROUTER_KEY + "upstreams.freellmapi", {"base_url": f"{url}/v1", "api_key_env": "FREELLMAPI_API_KEY"})]
    if _yes(fx, "Make the Polytropos router the default model (provider polytropos-router, model auto)?"):
        changes += [("model.provider", "polytropos-router"), ("model.default", "auto")]
    current, profile = _read_config(home), str(home)
    for key, new in changes:
        old = _get(current, key)
        if old == new:
            continue
        command = ["hermes", "config", "set", key, _value(new)]
        if fx.run(command, check=False).returncode != 0:
            print(f"Setting {key} failed. Run this command yourself, then run hermes polytropos setup again:")
            print(f"  {shlex.join(command)}")
            return 1
        if key == "model.provider":  # Hermes drops the old provider's route keys; recorded first, restored last
            for route_key in ("model.base_url", "model.api_mode"):
                if _get(current, route_key) is not None:
                    record_config_change(state, profile, route_key, _get(current, route_key), None)
        record_config_change(state, profile, key, old, new)
        save_state(state)
    print("The router settings are written.")
    return 0


# --- install and adopt ---

def _own_freellmapi(state: State, profile: str, tag: str) -> None:
    use_shared(state, "freellmapi", profile, version=tag, installed_by_us=True)
    state.shared["freellmapi"]["installed_by_us"] = True  # an earlier external record must not hide it from uninstall
    set_mode(state, "freellmapi", "managed")


def _install_program(p: programs.Program, state: State, profile: str, fx: Effects) -> bool:
    try:
        before = programs.installed_version(p, fx.run)
        if programs.needs_install(p, fx.run):
            programs.install(p, fx.run, download=fx.download)
            print(f"{p.name} {p.version} is installed.")
    except (subprocess.CalledProcessError, OSError, ValueError) as err:
        print(f"{p.name} was not installed: {err}")
        return False
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
    if (not only or "ponytail" in only) and not (home / "plugins" / "ponytail").is_dir() and (
            "ponytail" in only or _yes(fx, "ponytail is not installed. Install it from its pinned commit?")):
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
        fx.fl().adopt(args.source.expanduser(), pin)
    except Exception as err:  # port, data, node, build or service errors
        print(f"FreeLLMAPI was not adopted: {err}")
        return 1
    state = load_state()
    _own_freellmapi(state, str(hermes_home()), pin.tag)
    save_state(state)
    print(f"FreeLLMAPI {pin.tag} now runs as the Polytropos service with the data from {args.source}.")
    return 0


# --- update and rollback ---

def _release(fx: Effects) -> tuple[str, str, dict]:
    tag = json.loads(fx.fetch(RELEASE_API))["tag_name"]
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ValueError(f"unexpected release tag {tag!r}")
    pack = fx.fetch(f"{RAW}/{tag}/hermes-pack.yaml").decode()
    lock = tomllib.loads(fx.fetch(f"{RAW}/{tag}/versions.lock").decode())
    return tag, pack, lock


def _update_programs(lock: dict, state: State, profile: str, fx: Effects) -> int:
    """Default programs, plus the optional ones this profile installed, at the new pins."""
    chosen = [p for p in _programs(lock)
              if not p.optional or profile in state.shared.get(p.name, {}).get("profiles", [])]
    return sum(not _install_program(p, state, profile, fx) for p in chosen)


def _canary_check(fx: Effects) -> Callable[[], bool] | None:
    """The update's extra health check: the tool checks pass on the first cloud model stack-canary can reach."""
    try:
        cfg, _ = doctor._router().load_config(hermes_home())
    except ImportError:  # the router plugin is not installed
        return None
    slot = next((s for slots in cfg.routes.values() for s in slots
                 if not cfg.upstreams[s.upstream].local and _loopback(cfg.upstreams[s.upstream].base_url)), None)
    if slot is None:
        return None
    return lambda: run_canary(cfg, run=fx.run, models=[slot.model]).get(f"{slot.upstream}/{slot.model}") == "pass"


def _update_freellmapi(lock: dict, state: State, profile: str, fx: Effects) -> int:
    pin = _pin(lock)
    mode = state.shared.get("freellmapi", {}).get("mode")
    if mode != "managed":
        print(f"FreeLLMAPI does not run as the Polytropos service. This release pins {pin.tag}; update it yourself."
              if mode == "external" else "FreeLLMAPI is not installed. Run hermes polytropos install first.")
        return 0
    result = fx.fl().update(pin, check=_canary_check(fx))
    print(f"FreeLLMAPI: {result.message}")
    if result.ok:
        use_shared(state, "freellmapi", profile, version=pin.tag, installed_by_us=True)
        save_state(state)
    return 0 if result.ok else 1


def _update(args, fx: Effects) -> int:
    try:
        tag, pack_text, lock = _release(fx)
        pack = safe_load(pack_text) or {}
        own = [e for e in pack.get("plugins", []) if doctor._repo_subdir(str(e["repo"]))[0] == OWN_REPO]
    except Exception as err:  # network, JSON, TOML or YAML errors
        print(f"Could not read the latest Polytropos release from GitHub: {err}")
        return 1
    home, state, everything = hermes_home(), load_state(), args.component == "all"
    profile = str(home)
    ctx = doctor.DoctorContext(home, Path.home(), sys.platform, lock, {"plugins": own}, fx.run, fx.http_get, None)
    changes = [r.label for r in doctor.check_plugins(ctx) if r.status != "ok"] if everything else []
    print(f"The latest Polytropos release is {tag}.")
    for label in changes:
        print(f"  changes: {label}")
    what = {"all": "the plugins, programs and FreeLLMAPI", "programs": "the programs", "freellmapi": "FreeLLMAPI"}
    if not _yes(fx, f"Update {what[args.component]} to the versions of {tag}?"):
        print("Nothing changed.")
        return 0
    if changes:
        with tempfile.TemporaryDirectory() as tmp:
            pack_file = Path(tmp) / "hermes-pack.yaml"
            pack_file.write_text(pack_text)
            # Exit status 1 is expected on Hermes v0.23.2, where ponytail always fails; the own entries decide.
            fx.run(["hermes", "plugins", "pack", "install", "--force", str(pack_file)], check=False)
        left = [r.label for r in doctor.check_plugins(ctx) if r.status != "ok"]
        if left:
            print("The plugin update did not finish, so nothing else was updated:")
            for label in left:
                print(f"  {label}")
            return 1
    failed = 0
    if everything and not (home / "plugins" / "ponytail").is_dir():
        failed += not _ponytail(home, lock, fx)
    if args.component in ("programs", "all"):
        failed += _update_programs(lock, state, profile, fx)
    if args.component in ("freellmapi", "all"):
        failed += _update_freellmapi(lock, state, profile, fx)
    if changes:
        print("Restart the gateway to load the new plugin code: hermes gateway restart")
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


def _remove_freellmapi(fl: FreeLLM) -> None:
    fl.services.remove()
    shutil.rmtree(fl.root / "releases", ignore_errors=True)
    (fl.root / "current").unlink(missing_ok=True)


def _uninstall(args, fx: Effects) -> int:
    home, state = hermes_home(), load_state()
    profile, failed = str(home), 0
    current, kept = _read_config(home), []
    for change in reversed(config_changes(state, profile)):
        key, old = change["key"], change["old"]
        if _get(current, key) == old:
            continue
        command = ["hermes", "config", "unset", key] if old is None else ["hermes", "config", "set", key, _value(old)]
        if fx.run(command, check=False).returncode != 0:
            print(f"Restoring {key} failed. Run this command yourself: {shlex.join(command)}")
            kept.insert(0, change)
    record = state.profiles.setdefault(profile, {})
    if kept:
        record["config_changes"] = kept
    else:
        record.pop("config_changes", None)
        if not record:
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
    sub.add_parser("setup", help="connect Hermes to FreeLLMAPI and write the router settings")
    p = sub.add_parser("install", help="install FreeLLMAPI, ponytail and the pinned programs")
    p.add_argument("--all-optional", action="store_true", help="also install every optional program")
    p.add_argument("--only", nargs="+", metavar="NAME", help="install only these parts")
    p = sub.add_parser("adopt", help="run an existing FreeLLMAPI checkout as the Polytropos service")
    p.add_argument("--from", dest="source", type=Path, required=True, metavar="PATH",
                   help="the FreeLLMAPI checkout whose data to take over")
    p = sub.add_parser("update", help="update to the latest Polytropos release")
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
