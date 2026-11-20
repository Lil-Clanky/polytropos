"""Pinned programs: read versions.lock, check what is installed, install what is missing."""

import hashlib
import os
import re
import shutil
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
    dest = str(Path.home() / ".local" / "bin" / "rtk")  # checked there too, so another rtk on PATH does not count
    return Program(
        "rtk", lock["rtk"]["version"], [dest, "--version"], [[dest, "init", "--agent", "hermes"]],
        asset_url=asset["url"], asset_sha256=asset["sha256"],
    )


def _ollama(lock: dict, platform: str) -> Program:
    o = lock["ollama"]
    v = o["version"]
    if platform == "darwin":
        url = f"https://github.com/ollama/ollama/releases/download/v{v}/ollama-darwin.tgz"
        return Program("ollama", v, ["ollama", "--version"], [], optional=True,
                       asset_url=url, asset_sha256=o["darwin_sha256"])
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
        # stack-canary has no version flag; installed_version counts a working `canary run` as the pinned commit.
        Program("stack-canary", s["ref"][:7], ["canary", "run", "--help"], [
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
    except (OSError, subprocess.TimeoutExpired):  # missing, or present but not runnable
        return None
    if p.name == "stack-canary":
        return p.version if r.returncode == 0 else None
    m = _VERSION.search((r.stdout or "") + (r.stderr or ""))
    return m.group(0) if m else None


def needs_install(p: Program, run=subprocess.run) -> bool:
    v = installed_version(p, run)
    return v is None if p.name == "ollama" else v != p.version


def _download(url: str, dest: Path) -> None:
    urllib.request.urlretrieve(url, dest)  # noqa: S310 (https URL from versions.lock)


def _fetch(p: Program, download: Callable[[str, Path], None], tmp: str) -> Path:
    """Download the pinned archive into tmp and return it once its SHA-256 matches."""
    archive = Path(tmp) / "asset.tgz"
    download(p.asset_url, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != p.asset_sha256:
        raise ValueError(f"SHA-256 mismatch for {p.asset_url}: expected {p.asset_sha256}, got {digest}")
    return archive


def _link(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.new")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, link)


def _install_rtk(p: Program, download: Callable[[str, Path], None]) -> None:
    bin_dir = Path.home() / ".local" / "bin"
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(_fetch(p, download, tmp)) as tf:
            member = next((m for m in tf.getmembers() if m.isfile() and Path(m.name).name == "rtk"), None)
            if member is None:
                raise ValueError(f"no rtk binary in {p.asset_url}")
            data = tf.extractfile(member).read()
        bin_dir.mkdir(parents=True, exist_ok=True)
        staged = bin_dir / ".rtk.new"
        staged.write_bytes(data)
        os.chmod(staged, 0o755)
        os.replace(staged, bin_dir / "rtk")


def _install_ollama_tree(p: Program, download: Callable[[str, Path], None]) -> None:
    """Unpack the whole archive (binary and libraries) and link the binary into ~/.local/bin."""
    lib_root = Path.home() / ".local" / "lib" / "polytropos" / "ollama"
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(_fetch(p, download, tmp)) as tf:
            members = tf.getmembers()
            for m in members:
                parts = Path(m.name).parts
                if Path(m.name).is_absolute() or ".." in parts:
                    raise ValueError(f"unsafe archive path: {m.name!r}")
                if not (m.isdir() or m.isreg() or m.issym() or m.islnk()):
                    raise ValueError(f"unsupported archive member: {m.name!r}")
            if not any(m.isreg() and Path(m.name).parts[-1:] == ("ollama",) and len(Path(m.name).parts) <= 2
                       for m in members):
                raise ValueError(f"no ollama binary in {p.asset_url}")
            lib_root.mkdir(parents=True, exist_ok=True)
            staged = Path(tempfile.mkdtemp(dir=lib_root, prefix=".stage-"))
            try:
                tf.extractall(staged, filter="data")  # the data filter also rejects links that leave the tree
                binary = staged / "ollama"
                if not binary.is_file():
                    binary = staged / "bin" / "ollama"
                if not binary.is_file():
                    raise ValueError(f"no ollama binary in {p.asset_url}")
                rel = binary.relative_to(staged)
                final = lib_root / p.version
                shutil.rmtree(final, ignore_errors=True)
                os.replace(staged, final)
            except BaseException:
                shutil.rmtree(staged, ignore_errors=True)
                raise
    _link(final / rel, Path.home() / ".local" / "bin" / "ollama")
    print("Ollama is installed. Start it with `ollama serve`.")


def install(p: Program, run=subprocess.run, *, download: Callable[[str, Path], None] | None = None) -> None:
    if p.asset_url:
        (_install_ollama_tree if p.name == "ollama" else _install_rtk)(p, download or _download)
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
