"""Pinned programs: read versions.lock, check what is installed, install what is missing."""

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tomllib

from .state import load_state

_VERSION = re.compile(r"\d+\.\d+\.\d+")
# Third-party programs get only what they need, not Hermes's provider keys: npm and uv run the scripts of the packages
# they install, and rtk, graphify and stack-canary are downloaded tools. Hermes itself, the host, keeps its environment.
_ENV_NAMES = {"PATH", "HOME", "USER", "LOGNAME", "TERM", "LANG", "LANGUAGE", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY",
              "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
              "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NVM_DIR", "VOLTA_HOME",
              # Windows: node does not start without SYSTEMROOT, and npm and uv find their folders through the rest.
              "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"}
_ENV_PREFIXES = ("LC_", "NPM_CONFIG_", "UV_", "XDG_", "ASDF_", "MISE_")
_ASSET_KEYS = {
    ("darwin", "arm64"): "darwin-arm64",
    ("darwin", "x86_64"): "darwin-x86_64",
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "aarch64"): "linux-aarch64",
    ("linux", "arm64"): "linux-aarch64",
    ("win32", "AMD64"): "windows-x86_64",
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


def child_env() -> dict[str, str]:
    """The environment for the programs Polytropos starts: paths, locale, proxies, CA certificates and the settings of
    npm, uv and the Node version managers."""
    return {k: v for k, v in os.environ.items() if k.upper() in _ENV_NAMES or k.upper().startswith(_ENV_PREFIXES)}


def run_command(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run that also starts npm's .cmd files on Windows, which a bare name does not find there."""
    return subprocess.run([shutil.which(cmd[0]) or cmd[0], *cmd[1:]], **kwargs)


def load_lock(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _rtk(lock: dict, platform: str, machine: str) -> Program:
    key = _ASSET_KEYS.get((platform, machine))
    if key is None:
        raise ValueError(f"no rtk asset for platform {platform!r} and machine {machine!r}")
    asset = lock["rtk"]["assets"][key]
    # checked there too, so another rtk on PATH does not count; uv puts its tools there on Windows as well
    dest = str(Path.home() / ".local" / "bin" / ("rtk.exe" if platform == "win32" else "rtk"))
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
    return Program("ollama", v, ["ollama", "--version"], [], optional=True)  # install() names the official script


def programs(lock: dict, *, platform: str, machine: str) -> list[Program]:
    g, a, s = lock["graphify"], lock["ast-grep"], lock["stack-canary"]
    opt = lock["optional"]
    oc, qmd, c7 = opt["opencode"], opt["qmd"], opt["context7"]
    npm = lambda o: ["npm", "install", "-g", f"{o['package']}@{o['version']}"]  # noqa: E731
    return [
        _rtk(lock, platform, machine),
        Program("graphify", g["version"], ["graphify", "--version"], [
            ["uv", "tool", "install", f"{g['package']}=={g['version']}"],
            ["graphify", "install", "--platform", "hermes", "--project"],  # see _install_graphify
        ]),
        Program("ast-grep", a["version"], ["ast-grep", "--version"], [
            ["uv", "tool", "install", f"{a['package']}=={a['version']}"],
            ["hermes", "skills", "install", "official/software-development/ast-grep"],
        ]),
        # stack-canary has no version flag; its version is the commit that install recorded in state.json.
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


def installed_version(p: Program, run=run_command) -> str | None:
    try:
        r = run(p.version_cmd, capture_output=True, text=True, timeout=30, env=child_env())
    except (OSError, subprocess.TimeoutExpired):  # missing, or present but not runnable
        return None
    if p.name == "stack-canary":  # a working `canary run` and the commit on record, "" when there is no record
        return load_state().shared.get(p.name, {}).get("version", "") if r.returncode == 0 else None
    m = _VERSION.search((r.stdout or "") + (r.stderr or ""))
    return m.group(0) if m else None


def is_current(p: Program, version: str | None) -> bool:
    """Whether the version installed_version read counts as the pinned one. Ollama passes on presence alone."""
    return version is not None if p.name == "ollama" else version == p.version


def needs_install(p: Program, run=run_command) -> bool:
    return not is_current(p, installed_version(p, run))


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
    dest = Path(p.version_cmd[0])  # ~/.local/bin/rtk, rtk.exe on Windows
    with tempfile.TemporaryDirectory() as tmp:
        archive = _fetch(p, download, tmp)
        if p.asset_url.endswith(".zip"):  # the Windows release
            with zipfile.ZipFile(archive) as zf:
                name = next((n for n in zf.namelist() if Path(n).name == dest.name), None)
                data = zf.read(name) if name else None
        else:
            with tarfile.open(archive) as tf:
                member = next((m for m in tf.getmembers() if m.isfile() and Path(m.name).name == dest.name), None)
                data = tf.extractfile(member).read() if member else None
        if data is None:
            raise ValueError(f"no rtk binary in {p.asset_url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        staged, aside, moved = dest.with_name(f".{dest.name}.new"), dest.with_name(f"{dest.name}.old"), False
        try:
            staged.write_bytes(data)
            os.chmod(staged, 0o755)
            if dest.suffix == ".exe" and dest.exists():  # Windows cannot overwrite a running .exe, but can rename it
                with contextlib.suppress(OSError):  # the copy an earlier install set aside, unless it still runs
                    aside.unlink(missing_ok=True)
                os.replace(dest, aside)
                moved = True
            os.replace(staged, dest)  # also on Windows: the file is replaced, never linked
        except BaseException:
            staged.unlink(missing_ok=True)
            if moved:
                os.replace(aside, dest)  # put the working copy back
            raise


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


LINUX_OLLAMA = "curl -fsSL https://ollama.com/install.sh | sh"
WINDOWS_OLLAMA = "irm https://ollama.com/install.ps1 | iex"


def install(p: Program, run=run_command, *, download: Callable[[str, Path], None] | None = None,
            hermes_home: Path | None = None) -> None:
    if p.name == "ollama" and not p.asset_url:
        # Ollama's install scripts have no checksum to pin, and the Linux one asks for sudo, so the user runs them.
        if sys.platform == "win32":
            raise ValueError(f"Polytropos does not install Ollama on Windows. Install it in PowerShell with: "
                             f"{WINDOWS_OLLAMA}")
        raise ValueError(f"Polytropos does not install Ollama on Linux. Install it with: {LINUX_OLLAMA}")
    if p.asset_url:
        (_install_ollama_tree if p.name == "ollama" else _install_rtk)(p, download or _download)
    for cmd in p.install_cmds:
        if cmd[:3] == ["hermes", "mcp", "add"]:
            try:
                # No input: when Hermes asks before it overwrites an entry, the question fails at once.
                ok = run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL).returncode == 0
            except FileNotFoundError:
                ok = False
            if not ok:
                print(f"The MCP server {cmd[3]} was not added. To add it, run: " + " ".join(cmd))
            continue
        env = None if cmd[0] == "hermes" else child_env()  # Hermes is the host and keeps its own environment
        if cmd[1:] == ["init", "--agent", "hermes"] and hermes_home:  # rtk writes to $HERMES_HOME, else ~/.hermes
            env["HERMES_HOME"] = str(hermes_home)
        if cmd[-1] == "setup-opencode":
            from .freellm import PORT, api_key  # here, because freellm imports this module

            # The CLI looks for FreeLLMAPI at FREELLMAPI_URL, or http://localhost:3001 (cli/src/index.ts)
            env["FREELLMAPI_URL"] = f"http://127.0.0.1:{PORT}"
            if key := api_key():  # FreeLLMAPI's own CLI reads FreeLLMAPI's key, and asks for it without one
                env["FREELLMAPI_API_KEY"] = key
        if cmd[:2] == ["graphify", "install"]:
            _install_graphify(cmd, run, env, hermes_home)
            continue
        run(cmd, check=True, env=env)


def _install_graphify(cmd: list[str], run, env: dict[str, str], hermes_home: Path) -> None:
    """graphify ignores HERMES_HOME and writes its skill to Hermes's default home, whatever the profile. With --project
    it writes the skill under its working folder, so it runs in a temporary folder and the skill goes into the
    profile's skills/. The profile's copy goes in first, so graphify keeps a .bak of an edited one as it does at home."""
    dest = hermes_home / "skills" / "graphify"
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / ".hermes" / "skills" / "graphify"  # graphify's project destination (install.py)
        if dest.is_dir():
            shutil.copytree(dest, staged)
        bak = staged / "SKILL.md.bak"
        before = bak.read_bytes() if bak.is_file() else None
        # Its output names the temporary folder. graphify writes UTF-8 whatever the Windows code page.
        r = run(cmd, env=env, cwd=tmp, capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode:
            print((r.stdout or "") + (r.stderr or ""), end="")
            raise subprocess.CalledProcessError(r.returncode, cmd)
        shutil.copytree(staged, dest, dirs_exist_ok=True)
        for old in sorted(dest.rglob("*"), reverse=True):  # what graphify removed, such as an older references/
            if not (staged / old.relative_to(dest)).exists():
                if old.is_dir() and not old.is_symlink():
                    old.rmdir()  # its contents went first, in reverse order
                else:
                    old.unlink()
        if bak.is_file() and bak.read_bytes() != before:
            print(f"graphify kept the previous SKILL.md as {dest / 'SKILL.md.bak'}, because it differed from the "
                  "packaged skill.")


def ponytail_fallback(lock: dict, run=run_command) -> None:
    """Install ponytail at the lock's commit through Hermes's installer, which scans it and records the install."""
    pin = lock["plugins"]["ponytail"]
    run(["hermes", "plugins", "install", pin["repo"], "--ref", pin["commit"], "--enable"], check=True)
