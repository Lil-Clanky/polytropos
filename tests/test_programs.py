import dataclasses
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from polytropos import programs as P

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
    ],
)
def test_rtk_asset_per_platform(platform, machine, asset):
    rtk = by_name(P.programs(LOCK, platform=platform, machine=machine))["rtk"]
    assert rtk.asset_url == LOCK["rtk"]["assets"][asset]["url"]
    assert rtk.asset_sha256 == LOCK["rtk"]["assets"][asset]["sha256"]


def test_unknown_platform_raises():
    with pytest.raises(ValueError):
        P.programs(LOCK, platform="plan9", machine="mips")


def test_default_and_optional_sets():
    progs = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))
    assert {n for n, p in progs.items() if not p.optional} == {"rtk", "graphify", "ast-grep", "stack-canary"}
    assert {n for n, p in progs.items() if p.optional} == {"opencode", "qmd", "context7", "ollama"}
    assert progs["graphify"].install_cmds[0] == ["uv", "tool", "install", "graphifyy==0.9.112"]
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


def test_missing_binary_is_none():
    rtk = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["rtk"]

    def boom(*a, **k):
        raise FileNotFoundError

    assert P.installed_version(rtk, boom) is None
    assert P.needs_install(rtk, boom) is True


def test_matching_version_is_skipped():
    sg = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["ast-grep"]
    assert P.needs_install(sg, lambda *a, **k: proc(f"ast-grep {sg.version}\n")) is False
    assert P.needs_install(sg, lambda *a, **k: proc("ast-grep 0.39.1\n")) is True


def test_ollama_only_when_missing():
    ol = by_name(P.programs(LOCK, platform="linux", machine="x86_64"))["ollama"]
    assert P.needs_install(ol, lambda *a, **k: proc("ollama version is 0.30.0\n")) is False
    assert "OLLAMA_VERSION=0.40.16" in ol.install_cmds[0][-1]


def test_rtk_install_verifies_places_and_inits(tmp_home):
    rtk = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["rtk"]
    data = tar_with_rtk()
    rtk = dataclasses.replace(rtk, asset_sha256=hashlib.sha256(data).hexdigest())
    calls = []
    P.install(rtk, lambda cmd, **k: calls.append(cmd) or proc(), download=lambda url, dest: dest.write_bytes(data))
    dest = tmp_home / ".local" / "bin" / "rtk"
    assert dest.read_bytes().startswith(b"#!/bin/sh")
    assert dest.stat().st_mode & 0o777 == 0o755
    assert calls == [[str(dest), "init", "--agent", "hermes"]]


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


def test_context7_mcp_step_falls_back_to_manual(capsys):
    c7 = by_name(P.programs(LOCK, platform="darwin", machine="arm64"))["context7"]

    def run(cmd, **k):
        if cmd[:2] == ["hermes", "mcp"]:
            raise FileNotFoundError
        return proc()

    P.install(c7, run)
    assert "hermes mcp add context7" in capsys.readouterr().out


def test_ponytail_fallback_order(tmp_home):
    calls = []
    P.ponytail_fallback(tmp_home / ".hermes", LOCK, lambda cmd, **k: calls.append(cmd) or proc())
    target = str(tmp_home / ".hermes" / "plugins" / "ponytail")
    commit = LOCK["plugins"]["ponytail"]["commit"]
    assert calls[0][:2] == ["git", "clone"] and calls[0][-1] == target
    assert calls[1] == ["git", "-C", target, "checkout", commit]
    assert calls[2] == ["hermes", "plugins", "enable", "ponytail"]
