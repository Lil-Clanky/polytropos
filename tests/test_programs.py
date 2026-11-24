import dataclasses
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from polytropos import programs as P
from polytropos.freellm import PORT
from polytropos.state import State, save_state, use_shared

LOCK = P.load_lock(Path(__file__).resolve().parent.parent / "versions.lock")


def by_name(progs):
    return {p.name: p for p in progs}


def proc(out="", code=0):
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


def tar_with_rtk(payload=b"#!/bin/sh\necho rtk 1.2.1\n"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("rtk")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_lock_parses():
    for key in ("freellmapi", "rtk", "graphify", "ast-grep", "stack-canary", "ollama", "optional", "plugins"):
        assert key in LOCK
    assert len(LOCK["plugins"]["ponytail"]["commit"]) == 40
    assert len(LOCK["plugins"]["superpowers"]["commit"]) == 40


@pytest.mark.parametrize(
    "platform,machine,asset",
    [
        ("darwin", "arm64", "darwin-arm64"),
        ("darwin", "x86_64", "darwin-x86_64"),
        ("linux", "x86_64", "linux-x86_64"),
        ("linux", "aarch64", "linux-aarch64"),
        ("win32", "AMD64", "windows-x86_64"),  # what platform.machine() says on 64-bit Windows
    ],
)
def test_rtk_asset_per_platform(platform, machine, asset):
    rtk = by_name(P.programs(LOCK, platform=platform, machine=machine))["rtk"]
    assert rtk.asset_url == LOCK["rtk"]["assets"][asset]["url"]
    assert rtk.asset_sha256 == LOCK["rtk"]["assets"][asset]["sha256"]


def test_unknown_platform_raises():
    with pytest.raises(ValueError):
        P.programs(LOCK, platform="plan9", machine="mips")
    with pytest.raises(ValueError, match="platform 'win32' and machine 'ARM64'"):  # rtk has no Windows arm64 build
        P.programs(LOCK, platform="win32", machine="ARM64")


def test_default_and_optional_sets():
    progs = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))
    assert {n for n, p in progs.items() if not p.optional} == {"rtk", "graphify", "ast-grep", "stack-canary"}
    assert {n for n, p in progs.items() if p.optional} == {"opencode", "qmd", "context7", "ollama"}
    # From the lock, not a literal: the scheduled bump workflow raises the version and then runs these tests.
    assert progs["graphify"].install_cmds[0] == [
        "uv", "tool", "install", f"{LOCK['graphify']['package']}=={LOCK['graphify']['version']}"
    ]
    assert progs["graphify"].install_cmds[1] == ["graphify", "install", "--platform", "hermes"]
    assert progs["ast-grep"].install_cmds[1] == ["hermes", "skills", "install", "official/software-development/ast-grep"]
    assert progs["stack-canary"].install_cmds == [
        ["uv", "tool", "install", f"git+{LOCK['stack-canary']['repo']}@{LOCK['stack-canary']['ref']}"]
    ]


def test_installed_version_parses():
    rtk = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["rtk"]
    assert P.installed_version(rtk, lambda *a, **k: proc("rtk 1.2.0\n")) == "1.2.0"
    sg = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["ast-grep"]
    assert P.installed_version(sg, lambda *a, **k: proc("ast-grep 0.39.1\n")) == "0.39.1"


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_missing_or_unrunnable_binary_is_none(error):
    rtk = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["rtk"]

    def boom(*a, **k):
        raise error

    assert P.installed_version(rtk, boom) is None
    assert P.needs_install(rtk, boom) is True


def test_rtk_is_checked_where_polytropos_installs_it(tmp_home):
    rtk = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["rtk"]
    assert rtk.version_cmd == [str(tmp_home / ".local" / "bin" / "rtk"), "--version"]
    rtk = by_name(P.programs(LOCK, platform="win32", machine="AMD64"))["rtk"]
    assert rtk.version_cmd == [str(tmp_home / ".local" / "bin" / "rtk.exe"), "--version"]


def test_stack_canary_is_current_only_at_the_commit_install_recorded(tmp_home):
    sc = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["stack-canary"]
    assert sc.version_cmd == ["canary", "run", "--help"]  # stack-canary has no --version flag (cli.py at 5063da2)
    works, broken = lambda *a, **k: proc("usage: canary run [-h] ...\n"), lambda *a, **k: proc("", code=2)
    assert P.installed_version(sc, works) == "" and P.needs_install(sc, works) is True  # no record
    state = State()
    for commit, needed in (("0123abc", True), (LOCK["stack-canary"]["ref"][:7], False)):
        use_shared(state, "stack-canary", str(tmp_home), version=commit, installed_by_us=True)
        save_state(state)
        assert P.installed_version(sc, works) == commit and P.needs_install(sc, works) is needed
    assert P.installed_version(sc, broken) is None and P.needs_install(sc, broken) is True


def test_matching_version_is_skipped():
    sg = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["ast-grep"]
    assert P.needs_install(sg, lambda *a, **k: proc(f"ast-grep {sg.version}\n")) is False
    assert P.needs_install(sg, lambda *a, **k: proc("ast-grep 0.39.1\n")) is True


def test_ollama_only_when_missing():
    ol = by_name(P.programs(LOCK, platform="linux", machine="x86_64"))["ollama"]
    assert P.needs_install(ol, lambda *a, **k: proc("ollama version is 0.30.0\n")) is False


def test_rtk_install_verifies_places_and_inits(tmp_home):
    rtk = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["rtk"]
    data = tar_with_rtk()
    rtk = dataclasses.replace(rtk, asset_sha256=hashlib.sha256(data).hexdigest())
    calls = []
    P.install(rtk, lambda cmd, **k: calls.append(cmd) or proc(), download=lambda url, dest: dest.write_bytes(data))
    dest = tmp_home / ".local" / "bin" / "rtk"
    assert dest.read_bytes().startswith(b"#!/bin/sh")
    assert sys.platform == "win32" or dest.stat().st_mode & 0o777 == 0o755  # Windows keeps no mode bits
    assert calls == [[str(dest), "init", "--agent", "hermes"]]


def test_rtk_init_gets_the_profiles_hermes_home(tmp_path):
    rtk = dataclasses.replace(by_name(P.programs(LOCK, platform="win32", machine="AMD64"))["rtk"], asset_url="")
    envs = []
    P.install(rtk, lambda cmd, **k: envs.append(k["env"]) or proc(), hermes_home=tmp_path / "hermes")
    P.install(rtk, lambda cmd, **k: envs.append(k["env"]) or proc())
    assert envs[0]["HERMES_HOME"] == str(tmp_path / "hermes") and envs[1] is None  # rtk reads $HERMES_HOME


def test_windows_rtk_comes_out_of_the_zip_as_rtk_exe(tmp_home):
    rtk = by_name(P.programs(LOCK, platform="win32", machine="AMD64"))["rtk"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:  # the layout of rtk-x86_64-pc-windows-msvc.zip
        zf.writestr("LICENSE", "license")
        zf.writestr("rtk.exe", "MZ binary")
    data = buf.getvalue()
    rtk = dataclasses.replace(rtk, asset_sha256=hashlib.sha256(data).hexdigest())
    calls = []
    P.install(rtk, lambda cmd, **k: calls.append(cmd) or proc(), download=lambda url, dest: dest.write_bytes(data))
    dest = tmp_home / ".local" / "bin" / "rtk.exe"
    assert dest.read_bytes() == b"MZ binary" and not dest.is_symlink()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["rtk.exe"]
    assert calls == [[str(dest), "init", "--agent", "hermes"]]


def windows_rtk(payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("rtk.exe", payload)
    data = buf.getvalue()
    rtk = by_name(P.programs(LOCK, platform="win32", machine="AMD64"))["rtk"]
    rtk = dataclasses.replace(rtk, asset_sha256=hashlib.sha256(data).hexdigest())
    return rtk, lambda url, dest: dest.write_bytes(data)


def test_windows_rtk_update_sets_the_old_exe_aside_and_puts_it_back_on_failure(tmp_home, monkeypatch):
    rtk, download = windows_rtk(b"MZ new")
    bin_dir = tmp_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "rtk.exe").write_bytes(b"MZ running")  # Windows can rename a running exe, but not overwrite it
    (bin_dir / "rtk.exe.old").write_bytes(b"MZ older")  # what an earlier update set aside
    P.install(rtk, lambda cmd, **k: proc(), download=download)
    assert (bin_dir / "rtk.exe").read_bytes() == b"MZ new" and (bin_dir / "rtk.exe.old").read_bytes() == b"MZ running"
    replace = os.replace

    def refuse_the_new_exe(src, dst):
        if Path(src).name == ".rtk.exe.new":
            raise PermissionError(13, "Access is denied")
        replace(src, dst)

    monkeypatch.setattr(P.os, "replace", refuse_the_new_exe)
    with pytest.raises(PermissionError):
        P.install(rtk, lambda cmd, **k: proc(), download=download)
    assert (bin_dir / "rtk.exe").read_bytes() == b"MZ new"  # the working copy is back
    assert sorted(f.name for f in bin_dir.iterdir()) == ["rtk.exe"]  # and no staged file is left


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to overwrite a running exe")
def test_windows_rtk_update_replaces_a_running_exe(tmp_home):
    rtk, download = windows_rtk(b"MZ new")
    exe = tmp_home / ".local" / "bin" / "rtk.exe"
    exe.parent.mkdir(parents=True)
    shutil.copy(Path(os.environ["SYSTEMROOT"]) / "System32" / "PING.EXE", exe)  # any exe that keeps running
    running = subprocess.Popen([str(exe), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        P.install(rtk, lambda cmd, **k: proc(), download=download)
        assert exe.read_bytes() == b"MZ new" and running.poll() is None
        assert sorted(f.name for f in exe.parent.iterdir()) == ["rtk.exe", "rtk.exe.old"]
    finally:
        running.kill()
        running.wait()

def test_rtk_sha_mismatch_raises_and_leaves_nothing(tmp_home):
    rtk = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["rtk"]
    calls = []
    with pytest.raises(ValueError, match="SHA-256"):
        P.install(
            rtk,
            lambda cmd, **k: calls.append(cmd) or proc(),
            download=lambda url, dest: dest.write_bytes(tar_with_rtk()),
        )
    assert not (tmp_home / ".local" / "bin").exists() or not any((tmp_home / ".local" / "bin").iterdir())
    assert calls == []


def test_install_runs_commands_in_order():
    g = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["graphify"]
    calls = []
    P.install(g, lambda cmd, **k: calls.append(cmd) or proc())
    assert calls == g.install_cmds


def test_context7_mcp_step_gets_no_terminal_input():
    c7 = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["context7"]
    calls = []
    P.install(c7, lambda cmd, **k: calls.append((cmd, k)) or proc())
    [mcp] = [k for cmd, k in calls if cmd[:3] == ["hermes", "mcp", "add"]]
    assert mcp["stdin"] is subprocess.DEVNULL and mcp["capture_output"]  # an overwrite prompt fails at once


def test_npm_npx_and_uv_get_no_provider_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider-test-key")
    monkeypatch.setenv("FREELLMAPI_API_KEY", "sk-freellmapi-test-key")
    monkeypatch.setenv("npm_config_cache", "/tmp/npm-cache")
    monkeypatch.setenv("https_proxy", "http://proxy.test:3128")
    monkeypatch.setenv("ASDF_DATA_DIR", "/tmp/asdf")
    progs = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))
    envs = {}

    def run(cmd, **k):
        envs.setdefault(cmd[0], k.get("env"))
        return proc()

    for name in ("graphify", "opencode"):
        P.install(progs[name], run)
    for tool in ("uv", "npm", "npx"):
        env = {k.upper(): v for k, v in envs[tool].items()}  # Windows spells every name in capitals
        assert "OPENAI_API_KEY" not in env and env["PATH"] == os.environ["PATH"]
        assert env["NPM_CONFIG_CACHE"] == "/tmp/npm-cache"
        assert env["HTTPS_PROXY"] == "http://proxy.test:3128" and env["ASDF_DATA_DIR"] == "/tmp/asdf"
    assert "FREELLMAPI_API_KEY" not in envs["npm"] and "FREELLMAPI_API_KEY" not in envs["uv"]
    assert envs["npx"]["FREELLMAPI_API_KEY"] == "sk-freellmapi-test-key"  # only FreeLLMAPI's setup-opencode gets it
    assert envs["npx"]["FREELLMAPI_URL"] == f"http://127.0.0.1:{PORT}"  # 31415 on Windows, its default is 3001
    assert "FREELLMAPI_URL" not in envs["npm"]
    assert envs["hermes"] is None and envs["graphify"] is None  # these work on the profile with Hermes's environment


def test_npm_and_uv_keep_the_windows_folders_and_shell(monkeypatch):
    for name in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        monkeypatch.setenv(name, f"value of {name}")
    env = {k.upper(): v for k, v in P.child_env().items()}
    assert env["SYSTEMROOT"] == "value of SYSTEMROOT" and env["APPDATA"] == "value of APPDATA"  # node, npm's prefix
    assert env["LOCALAPPDATA"] == "value of LOCALAPPDATA" and env["COMSPEC"] == "value of COMSPEC"  # caches, scripts


def test_run_command_starts_what_which_finds(monkeypatch):
    seen = []
    monkeypatch.setattr(P.shutil, "which", lambda name: r"C:\nodejs\npm.CMD" if name == "npm" else None)
    monkeypatch.setattr(P.subprocess, "run", lambda argv, **k: seen.append((argv, k)) or proc())
    P.run_command(["npm", "install", "-g", "x"], check=True)
    P.run_command(["missing", "--version"])  # not found: the name stays, so the error names it
    assert seen == [([r"C:\nodejs\npm.CMD", "install", "-g", "x"], {"check": True}), (["missing", "--version"], {})]


def test_context7_mcp_step_falls_back_to_manual(capsys):
    c7 = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["context7"]

    def run(cmd, **k):
        if cmd[:2] == ["hermes", "mcp"]:
            raise FileNotFoundError
        return proc()

    P.install(c7, run)
    out = capsys.readouterr().out
    assert "The MCP server context7 was not added" in out and "hermes mcp add context7" in out
    assert "once Hermes is installed" not in out


def test_ponytail_goes_through_hermes_plugins_install():
    calls = []
    P.ponytail_fallback(LOCK, lambda cmd, **k: calls.append(cmd) or proc())
    assert calls == [["hermes", "plugins", "install", LOCK["plugins"]["ponytail"]["repo"],
                      "--ref", LOCK["plugins"]["ponytail"]["commit"], "--enable"]]


def tar_ollama(extra=()):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload in (("ollama", b"BIN"), ("libggml.dylib", b"LIB"), *extra):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo("libggml.0.dylib")
        link.type = tarfile.SYMTYPE
        link.linkname = "libggml.dylib"
        tf.addfile(link)
    return buf.getvalue()


def darwin_ollama(data=None):
    ol = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["ollama"]
    assert ol.asset_url.endswith(f"/v{LOCK['ollama']['version']}/ollama-darwin.tgz")
    assert len(ol.asset_sha256) == 64
    return dataclasses.replace(ol, asset_sha256=hashlib.sha256(data).hexdigest()) if data else ol


@pytest.mark.skipif(sys.platform == "win32", reason="the macOS Ollama install links files, which Windows allows "
                                                    "only with a privilege")
def test_macos_ollama_installs_lock_version_tree(tmp_home, capsys):
    data = tar_ollama()
    calls = []
    P.install(darwin_ollama(data), lambda cmd, **k: calls.append(cmd) or proc(),
              download=lambda url, dest: dest.write_bytes(data))
    root = tmp_home / ".local" / "lib" / "polytropos" / "ollama" / LOCK["ollama"]["version"]
    assert (root / "libggml.dylib").read_bytes() == b"LIB"
    assert (root / "libggml.0.dylib").is_symlink()
    link = tmp_home / ".local" / "bin" / "ollama"
    assert link.is_symlink() and link.resolve() == (root / "ollama").resolve()
    assert calls == []
    assert "ollama serve" in capsys.readouterr().out


def test_macos_ollama_sha_mismatch_leaves_nothing(tmp_home):
    ol = darwin_ollama()
    with pytest.raises(ValueError, match="SHA-256"):
        P.install(ol, lambda *a, **k: proc(), download=lambda url, dest: dest.write_bytes(tar_ollama()))
    assert not (tmp_home / ".local").exists()


def test_macos_ollama_unsafe_member_leaves_nothing(tmp_home):
    data = tar_ollama([("../evil", b"x")])
    with pytest.raises(ValueError, match="unsafe"):
        P.install(darwin_ollama(data), lambda *a, **k: proc(), download=lambda url, dest: dest.write_bytes(data))
    assert not (tmp_home / ".local" / "bin").exists()
    assert not (tmp_home / "evil").exists()


@pytest.mark.parametrize("platform, machine, says", [
    ("linux", "x86_64", "does not install Ollama on Linux. Install it with: curl -fsSL https://ollama.com/install.sh | sh"),
    ("win32", "AMD64", "does not install Ollama on Windows. Install it in PowerShell with: "
                       "irm https://ollama.com/install.ps1 | iex"),
])
def test_linux_and_windows_ollama_is_left_to_the_user_and_runs_nothing(monkeypatch, platform, machine, says):
    monkeypatch.setattr(P.sys, "platform", platform)
    ol = by_name(P.programs(LOCK, platform=platform, machine=machine))["ollama"]
    calls = []
    with pytest.raises(ValueError) as raised:
        P.install(ol, lambda cmd, **k: calls.append(cmd) or proc())
    assert says in str(raised.value) and calls == []
