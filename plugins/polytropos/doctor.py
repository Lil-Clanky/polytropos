"""The checks behind `hermes polytropos doctor`."""

import fnmatch
import importlib
import json
import os
import platform as _platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ._shared import canary, guard
from .freellm import PORT, FreeLLM, api_key
from .programs import installed_version, needs_install, programs
from .services import ServiceManager
from .state import load_state

try:
    from hermes_yaml import safe_load
except ImportError:  # outside Hermes
    from yaml import safe_load

ADVICE_RAM = 16 * 1024**3  # the spec gives advice for 16 GB only
ADVICE_PARAMS_B = 3.0
ADVICE_WINDOW = 8192
_SIZE_TAG = re.compile(r"(?<![a-z0-9])(\d+(?:\.\d+)?)b(?![a-z0-9])", re.IGNORECASE)
_KEY_PATTERNS = ("*key*.json", "*keys*.json", "*.env.bak", ".env.backup*", "*.env.backup*")
_SLEEP_LINE = re.compile(r"^\s*sleep\s+(\d+)", re.MULTILINE)


@dataclass(frozen=True)
class CheckResult:
    status: str  # ok | warn | fail | info
    label: str
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorContext:
    hermes_home: Path
    home: Path
    platform: str
    lock: dict
    pack: dict
    run: Callable
    http_get: Callable  # (url, headers) -> (status, body); 0 refused, -1 not HTTP
    hermes_version: str | None
    tested_hermes: tuple[str, ...] = ("0.23.2",)
    machine: str = field(default_factory=_platform.machine)


def _router():
    # shortcut: imports the router plugin by package name, Task 17 makes that name resolve in a real Hermes home once the spike shows how plugins load
    return importlib.import_module("polytropos_router.config")


def _router_config(ctx: DoctorContext):
    return _router().load_config(ctx.hermes_home)


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text))


def check_hermes(ctx: DoctorContext) -> list[CheckResult]:
    v = ctx.hermes_version
    if not v:
        return [CheckResult("fail", "Hermes was not found", fix="Install Hermes Agent, then run the doctor again")]
    if v in ctx.tested_hermes:
        return [CheckResult("ok", f"Hermes v{v} (tested)")]
    newest = max(ctx.tested_hermes, key=_version_tuple)
    kind = "newer than" if _version_tuple(v) > _version_tuple(newest) else "older than"
    return [CheckResult("warn", f"Hermes v{v} is {kind} the tested versions ({', '.join(ctx.tested_hermes)})")]


_GITHUB_HTTPS = re.compile(r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s#]+?)(?:\.git)?$")


def _repo_subdir(source: str, subdir: str | None = None) -> tuple[str, str | None]:
    """Same normalisation as hermes_cli/plugin_packs.py:430-440 (_source_to_repo_subdir)."""
    base, _, sub = source.partition("#")
    sub = (subdir if subdir is not None else sub).strip("/") or None
    m = _GITHUB_HTTPS.match(base.strip())
    return (f"{m.group('owner')}/{m.group('repo')}" if m else base.strip()), sub


def install_records(hermes_home: Path) -> dict:
    """Hermes's plugin install records by the (repo, subdir) of their source. OSError or ValueError if unreadable."""
    try:
        metadata = json.loads((hermes_home / "plugins" / ".install-metadata.json").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("expected a JSON object")
    # Records are keyed by the installed folder name, which the manifest decides, so match on source.
    return {_repo_subdir(str(r.get("source") or "")): (name, r) for name, r in metadata.items() if isinstance(r, dict)}


def _git_head(run: Callable, folder: Path) -> str:
    try:
        return run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True,
                   check=False).stdout.strip()
    except OSError:  # git is not installed
        return ""


def check_plugins(ctx: DoctorContext) -> list[CheckResult]:
    """Each pack entry against its install record. An entry with a name may also be a git clone in plugins/<name>.
    An entry without a ref counts as not installed: the own plugins have none when the general plugin has no record."""
    try:
        installed = install_records(ctx.hermes_home)
    except (OSError, ValueError) as e:
        return [CheckResult("fail", "plugin install metadata is unreadable",
                            str(e) if type(e) is ValueError else type(e).__name__)]
    results, good = [], []
    for entry in ctx.pack.get("plugins", []):
        key = _repo_subdir(str(entry["repo"]), entry.get("subdir") or "")
        pinned = str(entry["ref"]).lower()
        label = key[0] + (f"#{key[1]}" if key[1] else "")
        folder = ctx.hermes_home / "plugins" / entry.get("name", "")
        if key not in installed and entry.get("name") and (folder / ".git").exists():
            # programs.ponytail_fallback clones without Hermes's installer, which leaves no record
            installed[key] = (entry["name"], {"revision": _git_head(ctx.run, folder)})
        if key not in installed or not pinned:
            results.append(CheckResult("fail", f"plugin {label} is not installed", fix="hermes polytropos update"))
            continue
        name, record = installed[key]
        revision = str(record.get("revision") or "").lower()
        if revision != pinned:
            results.append(CheckResult("warn", f"plugin {name} is at {revision[:7] or 'an unknown commit'}, "
                                               f"the pack pins {pinned[:7]}"))
        else:
            good.append(name)
    return ([CheckResult("ok", "plugins: " + ", ".join(good))] if good else []) + results


def check_freellmapi(ctx: DoctorContext) -> list[CheckResult]:
    services = ServiceManager(platform=ctx.platform, run=ctx.run, home=ctx.home)
    found = FreeLLM(run=ctx.run, http_get=ctx.http_get, services=services).detect()
    if not found.listening:
        return [CheckResult("fail", f"FreeLLMAPI is not running on 127.0.0.1:{PORT}",
                            fix="hermes polytropos install")]
    if not found.is_freellmapi:
        return [CheckResult("fail", f"port {PORT} is used by a program that is not FreeLLMAPI",
                            "FreeLLMAPI cannot start while it is taken",
                            f"Stop that program or free port {PORT}, then run hermes polytropos install")]
    key = api_key()  # never printed
    if not key:
        return [CheckResult("warn", "this profile has no FREELLMAPI_API_KEY", fix="hermes polytropos setup")]
    status, _ = ctx.http_get(f"http://127.0.0.1:{PORT}/v1/models", {"Authorization": f"Bearer {key}"})
    if status == 401:
        return [CheckResult("warn", "FreeLLMAPI does not accept this profile's FREELLMAPI_API_KEY",
                            fix="hermes polytropos setup")]
    if status != 200:
        return [CheckResult("warn", f"FreeLLMAPI answers on port {PORT} but /v1/models is not healthy",
                            f"it returned {status}")]
    mode = load_state().shared.get("freellmapi", {}).get("mode") or "mode unknown"
    return [CheckResult("ok", f"FreeLLMAPI {ctx.lock['freellmapi']['tag']}, {mode}, 127.0.0.1:{PORT}")]


def check_node(ctx: DoctorContext) -> list[CheckResult]:
    ok, found = FreeLLM(run=ctx.run).node_ok()
    if ok:
        return [CheckResult("ok", f"Node {found}")]
    if load_state().shared.get("freellmapi", {}).get("mode") == "external":
        return [CheckResult("info", found, "FreeLLMAPI runs in external mode, so Polytropos does not run it")]
    return [CheckResult("fail", found, fix="Install Node 22 or 24 for FreeLLMAPI, for example from "
                                           "https://nodejs.org/en/download")]


def check_router(ctx: DoctorContext) -> list[CheckResult]:
    cfg, errors = _router_config(ctx)
    results = [CheckResult("fail", "router config", e, "Fix the polytropos-router entry in config.yaml")
               for e in errors]
    floor = _router()
    for slot in floor.cloud_slots_below_floor(cfg):
        results.append(CheckResult("warn", f"cloud model {slot.upstream}/{slot.model} reports a window of "
                                           f"{floor.window(cfg, slot):,} tokens",
                                   f"Hermes refuses models below {floor.FLOOR:,} tokens"))
    for up in cfg.upstreams.values():
        status, _ = ctx.http_get(up.base_url.rstrip("/") + "/models", {})
        if status <= 0:
            results.append(CheckResult("warn", f"upstream {up.name} does not answer on {up.base_url}/models"))
    return results or [CheckResult("ok", f"router config, {len(cfg.upstreams)} upstreams answer")]


def check_old_router_section(ctx: DoctorContext) -> list[CheckResult]:
    try:
        data = safe_load((ctx.hermes_home / "config.yaml").read_text())
    except Exception:  # a missing or unparsable file is the router check's business
        return []
    if isinstance(data, dict) and "router" in data:
        return [CheckResult("info", "config.yaml has a top-level router: section",
                            "Polytropos ignores it and reads plugins.entries.polytropos-router")]
    return []


def check_programs(ctx: DoctorContext) -> list[CheckResult]:
    results, present = [], []
    for p in programs(ctx.lock, platform=ctx.platform, machine=ctx.machine):
        if not needs_install(p, ctx.run):
            present.append(f"{p.name} {p.version}")
            continue
        found = installed_version(p, ctx.run)
        what = f"{p.name} is not installed" if found is None else f"{p.name} {found} differs from pinned {p.version}"
        results.append(CheckResult("info" if p.optional and found is None else "warn", what,
                                   fix="hermes polytropos install"))
    return ([CheckResult("ok", ", ".join(present))] if present else []) + results


def _local_slots(cfg):
    seen = {}
    for slots in cfg.routes.values():
        for s in slots:
            if cfg.upstreams[s.upstream].local:
                seen[s] = cfg.upstreams[s.upstream]
    return seen


def check_canary(ctx: DoctorContext) -> list[CheckResult]:
    cfg, _ = _router_config(ctx)
    matrix, local = canary.load_matrix(), canary.load_local_results()
    ollama = next((p for p in programs(ctx.lock, platform=ctx.platform, machine=ctx.machine)
                   if p.name == "ollama"), None)
    version = installed_version(ollama, ctx.run) if ollama else None
    results = []
    for slot, up in _local_slots(cfg).items():
        if up.runtime != "ollama" or version is None:
            continue
        verdict = matrix.verdict(version, slot.model)
        if verdict == "fail":
            good = matrix.passing_versions(slot.model)
            hint = f"passes on Ollama {good[0]}" if good else "no tested Ollama version passes"
            results.append(CheckResult("warn", f"Ollama {version} + {slot.model} fails the stack-canary tool checks; "
                                               "the router skips it for tool turns", hint))
        elif verdict == "unknown":
            results.append(CheckResult("info", f"Ollama {version} + {slot.model} has no stack-canary verdict"))
    for slots in cfg.routes.values():
        for s in slots:
            if local.get(f"{s.upstream}/{s.model}") == "fail":
                results.append(CheckResult("warn", f"{s.upstream}/{s.model} failed the local tool checks; "
                                                   "the router skips it for tool turns"))
    return list(dict.fromkeys(results))


def _params_b(model: str) -> float | None:
    m = _SIZE_TAG.search(model)
    return float(m.group(1)) if m else None


def check_fit(ctx: DoctorContext) -> list[CheckResult]:
    ram = guard.total_ram_bytes(platform=ctx.platform, run=ctx.run)
    if ram is None:
        return [CheckResult("info", "RAM could not be read")]
    gb = ram / 1024**3
    if ram > ADVICE_RAM:
        return [CheckResult("info", f"{gb:.0f} GB RAM, no sizing advice for this amount")]
    cfg, _ = _router_config(ctx)
    results = []
    for slot, up in _local_slots(cfg).items():
        size = _params_b(slot.model)
        if (size is not None and size > ADVICE_PARAMS_B) or up.context_window > ADVICE_WINDOW:
            results.append(CheckResult(
                "warn", f"{slot.upstream}/{slot.model} may be too large for {gb:.0f} GB RAM",
                f"advice for 16 GB is a model of {ADVICE_PARAMS_B:g}B or smaller with a window of {ADVICE_WINDOW}"))
    return results or [CheckResult("info", f"{gb:.0f} GB RAM, local models fit the advice")]


def check_key_files(ctx: DoctorContext) -> list[CheckResult]:
    results = []
    for folder in (ctx.home / "Downloads", ctx.home / "Desktop", ctx.home):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        found = sorted(
            f"{e.name} ({e.stat().st_mode & 0o777:04o})" for e in entries
            if e.is_file() and any(fnmatch.fnmatch(e.name.lower(), pat) for pat in _KEY_PATTERNS))
        if found:
            shown = "~" if folder == ctx.home else f"~/{folder.name}"
            results.append(CheckResult("warn", f"{len(found)} files in {shown} look like key exports "
                                               "(file names checked, contents not opened)", ", ".join(found)))
    return results or [CheckResult("ok", "no files that look like key exports (file names checked)")]


def _hermes_root(ctx: DoctorContext) -> Path:
    home = ctx.hermes_home
    return home.parent.parent if home.parent.name == "profiles" else home


def check_profiles(ctx: DoctorContext) -> list[CheckResult]:
    try:
        profiles = sorted(p for p in (_hermes_root(ctx) / "profiles").iterdir() if p.is_dir())
    except OSError:
        return []
    return [CheckResult("info", f"profile {p.name}: Polytropos is "
                                f"{'installed' if (p / 'plugins' / 'polytropos').is_dir() else 'not installed'}")
            for p in profiles]


def check_ponytail(ctx: DoctorContext) -> list[CheckResult]:
    if not (ctx.hermes_home / "plugins" / "ponytail").is_dir():
        return []
    return [CheckResult("info", "ponytail adds text to every turn, which costs tokens in gateway chats",
                        "To turn it off for the gateway, set PONYTAIL_DEFAULT_MODE=off in the gateway environment")]


def check_superpowers(ctx: DoctorContext) -> list[CheckResult]:
    if not (ctx.hermes_home / "plugins" / "superpowers").is_dir():
        return []
    return [CheckResult("info", "superpowers: its bootstrap after compaction is not verified on this Hermes version")]


def check_sleep(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.platform != "darwin" or not list((ctx.home / "Library" / "LaunchAgents").glob("ai.hermes.gateway*.plist")):
        return []
    out = ctx.run(["pmset", "-g"], capture_output=True, text=True, check=False).stdout or ""
    m = _SLEEP_LINE.search(out)
    if m and int(m.group(1)) > 0:
        return [CheckResult("warn", f"the Mac sleeps after {m.group(1)} minutes, which stops the gateway from "
                                    "receiving messages", fix="sudo pmset -a sleep 0 (run it yourself)")]
    return [CheckResult("ok", "the Mac does not sleep while the gateway runs")]


CHECKS = (check_hermes, check_plugins, check_freellmapi, check_node, check_router, check_old_router_section,
          check_programs, check_canary, check_fit, check_key_files, check_profiles, check_ponytail, check_superpowers,
          check_sleep)


def run_checks(ctx: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results += check(ctx)
        except Exception as e:  # one broken check must not hide the others
            results.append(CheckResult("fail", f"check {check.__name__.removeprefix('check_')} failed",
                                       f"{type(e).__name__}: {e}"))
    return results


def format_report(results: list[CheckResult]) -> str:
    lines = ["Polytropos doctor"]
    for r in results:
        lines.append(f"  {r.status:<6}{r.label}" + (f": {r.detail}" if r.detail else ""))
        if r.fix:
            lines.append(f"        {r.fix}")
    return "\n".join(lines)
