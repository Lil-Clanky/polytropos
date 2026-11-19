"""Isolated Hermes homes for spikes and end-to-end tests.

A sandbox root holds ``hermes/`` (the HERMES_HOME), ``home/`` (the HOME) and ``work/`` (the
working directory of a run). Runs start from a short allowlist of the parent environment, so
API keys and Hermes settings of the machine never reach the sandboxed Hermes.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

# Resolved at import: tests later point HOME at a temp folder.
HERMES_BIN = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"
_KEEP_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL")


def make_home(root: Path, config: dict, plugins: list[Path]) -> Path:
    """Create ``<root>/hermes`` with ``config.yaml`` from *config* and a copy of each plugin
    folder in ``plugins/`` (folder names kept, an older copy replaced); also create
    ``<root>/home``. Returns the home."""
    home = root / "hermes"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir(exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    for plugin in plugins:
        target = home / "plugins" / plugin.name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(plugin, target, ignore=shutil.ignore_patterns("__pycache__"))
    return home


def sandbox_env(home: Path) -> dict[str, str]:
    """Environment for any Hermes command in the sandbox of *home*: the allowlisted parent
    variables plus ``HERMES_HOME=home``, ``HOME=<root>/home``, ``TMPDIR=<root>/tmp`` (Hermes
    puts its frozen provider copies there) and no bytecode writes."""
    env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    tmp = home.parent / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update(HERMES_HOME=str(home), HOME=str(home.parent / "home"), TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE="1")
    return env


def run_oneshot(home: Path, prompt: str, *, provider: str, model: str, timeout_s: int = 180) -> subprocess.CompletedProcess:
    """Run one turn with ``hermes -z PROMPT --provider P -m M`` (one-shot mode: final text on
    stdout) in ``<root>/work``, with the sandbox environment and stdin closed. The gateway daemon
    of *home* is stopped before and after the run, so no run is served with older plugin code or
    ``.env`` values and none is left behind."""
    work = home.parent / "work"
    work.mkdir(exist_ok=True)
    stop_gateway(home)
    try:
        return subprocess.run(
            [str(HERMES_BIN), "-z", prompt, "--provider", provider, "-m", model],
            cwd=work, env=sandbox_env(home), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout_s)
    finally:
        stop_gateway(home)


def stop_gateway(home: Path, timeout_s: float = 30) -> bool:
    """Stop the unmanaged ``gateway run --idle-exit`` daemon that a client run (``hermes -z``)
    starts for *home* and that otherwise lingers for ``gateway.unmanaged_idle_exit_seconds``.
    Signals only the pid in ``<home>/gateway.pid`` when that file names *home* and the live
    command line ends with the arguments the file records. True when a daemon was stopped."""
    try:
        info = json.loads((home / "gateway.pid").read_text())
        pid, argv = int(info["pid"]), info["argv"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    command = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
    # Arguments after the program only: Hermes's launcher runs main.py from inline source, so argv[0] is not shown.
    if (Path(info.get("hermes_home") or "").resolve() != home.resolve() or not isinstance(argv, list) or len(argv) < 2
            or not command.endswith(" " + " ".join(map(str, argv[1:])))):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith("Z"):  # gone, or a zombie its parent has yet to reap
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    (home / "gateway.pid").unlink(missing_ok=True)  # a killed daemon cannot remove it, and its pid may be reused
    return True


def launch_snapshot() -> set[str]:
    """Labels from ``launchctl list`` that contain ``hermes`` or ``polytropos``, plus the file
    names in ``~/Library/LaunchAgents``; an empty set off macOS."""
    if sys.platform != "darwin":
        return set()
    listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=True).stdout
    labels = {line.split("\t")[-1] for line in listing.splitlines()[1:]}
    agents = Path.home() / "Library" / "LaunchAgents"
    files = {p.name for p in agents.iterdir()} if agents.is_dir() else set()
    return {label for label in labels if "hermes" in label.lower() or "polytropos" in label.lower()} | files
