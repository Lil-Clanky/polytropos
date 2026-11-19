"""Pinned programs: read versions.lock, check what is installed, install what is missing."""

import hashlib
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tomllib

_VERSION = re.compile(r"\d+\.\d+\.\d+")
_ASSET_KEYS = {
    ("darwin", "arm64"): "darwin-arm64",
    ("darwin", "x86_64"): "darwin-x86_64",
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "aarch64"): "linux-aarch64",
    ("linux", "arm64"): "linux-aarch64",
}


@dataclass(frozen=True)
class Program:
    name: str
    version: str
    version_cmd: list[str]
    install_cmds: list[list[str]]
    optional: bool = False
    # rtk only: the release archive to download and its expected SHA-256.
    asset_url: str = ""
    asset_sha256: str = ""


def load_lock(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _rtk(lock: dict, platform: str, machine: str) -> Program:
    key = _ASSET_KEYS.get((platform, machine))
    if key is None:
        raise ValueError(f"no rtk asset for platform {platform!r} and machine {machine!r}")
    asset = lock["rtk"]["assets"][key]
    dest = str(Path.home() / ".local" / "bin" / "rtk")
    return Program(
        "rtk", lock["rtk"]["version"], ["rtk", "--version"], [[dest, "init", "--agent", "hermes"]],
        asset_url=asset["url"], asset_sha256=asset["sha256"],
    )


def _ollama(lock: dict, platform: str) -> Program:
    v = lock["ollama"]["version"]
    if platform == "darwin":
        # shortcut: the Homebrew formula cannot pin a version, so macOS gets whatever brew has.
        cmd = ["brew", "install", "ollama"]
    else:
        cmd = ["sh", "-c", f"curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION={v} sh"]
    return Program("ollama", v, ["ollama", "--version"], [cmd], optional=True)


def programs(lock: dict, *, platform: str, machine: str) -> list[Program]:
    g, a, s = lock["graphify"], lock["ast-grep"], lock["stack-canary"]
    opt = lock["optional"]
    oc, qmd, c7 = opt["opencode"], opt["qmd"], opt["context7"]
    npm = lambda o: ["npm", "install", "-g", f"{o['package']}@{o['version']}"]  # noqa: E731
    return [
        _rtk(lock, platform, machine),
        Program("graphify", g["version"], ["graphify", "--version"], [
            ["uv", "tool", "install", f"{g['package']}=={g['version']}"],
            ["graphify", "install", "--platform", "hermes"],
        ]),
        Program("ast-grep", a["version"], ["ast-grep", "--version"], [
            ["uv", "tool", "install", f"{a['package']}=={a['version']}"],
            ["hermes", "skills", "install", "official/software-development/ast-grep"],
        ]),
        # The pin is a commit, so the installed version never equals it and uv decides whether to act.
        Program("stack-canary", s["ref"], ["canary", "--version"], [
            ["uv", "tool", "install", f"git+{s['repo']}@{s['ref']}"],
        ]),
        Program("opencode", oc["version"], ["opencode", "--version"], [
            npm(oc),
            ["npx", "-y", f"freellmapi@{lock['freellmapi']['cli_version']}", "setup-opencode"],
            ["hermes", "skills", "install", "official/autonomous-ai-agents/opencode"],
        ], optional=True),
        Program("qmd", qmd["version"], ["qmd", "--version"], [
            npm(qmd),
            ["hermes", "skills", "install", "official/research/qmd"],
        ], optional=True),
        Program("context7", c7["version"], ["context7-mcp", "--version"], [
            npm(c7),
            ["hermes", "mcp", "add", "context7", "--command", "context7-mcp"],
        ], optional=True),
        _ollama(lock, platform),
    ]


def installed_version(p: Program, run=subprocess.run) -> str | None:
    try:
        r = run(p.version_cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    m = _VERSION.search((r.stdout or "") + (r.stderr or ""))
    return m.group(0) if m else None


def needs_install(p: Program, run=subprocess.run) -> bool:
    v = installed_version(p, run)
    return v is None if p.name == "ollama" else v != p.version


def _download(url: str, dest: Path) -> None:
    urllib.request.urlretrieve(url, dest)  # noqa: S310 (https URL from versions.lock)


def _install_rtk(p: Program, download: Callable[[str, Path], None]) -> None:
    bin_dir = Path.home() / ".local" / "bin"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "rtk.tar.gz"
        download(p.asset_url, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != p.asset_sha256:
            raise ValueError(f"SHA-256 mismatch for {p.asset_url}: expected {p.asset_sha256}, got {digest}")
        with tarfile.open(archive) as tf:
            member = next((m for m in tf.getmembers() if m.isfile() and Path(m.name).name == "rtk"), None)
            if member is None:
                raise ValueError(f"no rtk binary in {p.asset_url}")
            data = tf.extractfile(member).read()
        bin_dir.mkdir(parents=True, exist_ok=True)
        staged = bin_dir / ".rtk.new"
        staged.write_bytes(data)
        os.chmod(staged, 0o755)
        os.replace(staged, bin_dir / "rtk")


def install(p: Program, run=subprocess.run, *, download: Callable[[str, Path], None] | None = None) -> None:
    if p.asset_url:
        _install_rtk(p, download or _download)
    for cmd in p.install_cmds:
        if cmd[:3] == ["hermes", "mcp", "add"]:
            try:
                ok = run(cmd, capture_output=True, text=True).returncode == 0
            except FileNotFoundError:
                ok = False
            if not ok:
                print("Manual step: run `" + " ".join(cmd) + "` once Hermes is installed.")
            continue
        run(cmd, check=True)


def ponytail_fallback(hermes_home: Path, lock: dict, run=subprocess.run) -> None:
    pin = lock["plugins"]["ponytail"]
    target = str(hermes_home / "plugins" / "ponytail")
    run(["git", "clone", pin["repo"], target], check=True)
    run(["git", "-C", target, "checkout", pin["commit"]], check=True)
    run(["hermes", "plugins", "enable", "ponytail"], check=True)
