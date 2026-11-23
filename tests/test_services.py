import plistlib
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from polytropos.services import LAUNCHD_LABEL, SYSTEMD_UNIT, ServiceManager, ServiceSpec, render_launchd, render_systemd

# Paths in a unit or a launchctl command are POSIX paths; Polytropos runs no service on Windows.
posix_paths = pytest.mark.skipif(sys.platform == "win32", reason="service files hold POSIX paths")

SPEC = ServiceSpec(
    program=["/opt/my node/bin/node", "/data/with space/current/server/dist/index.js"],
    env={"HOST": "127.0.0.1", "PORT": "3001"},
    workdir=Path("/data/with space/current"),
    log_path=Path("/state/freellmapi.log"),
)


class Runner:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def test_plist_roundtrip():
    data = plistlib.loads(render_launchd(SPEC))
    assert data["Label"] == LAUNCHD_LABEL
    assert data["EnvironmentVariables"]["HOST"] == "127.0.0.1"
    assert data["EnvironmentVariables"]["PORT"] == "3001"
    assert data["ProgramArguments"] == SPEC.program
    assert data["WorkingDirectory"] == str(SPEC.workdir)
    assert data["RunAtLoad"] is True and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["StandardOutPath"] == data["StandardErrorPath"] == str(SPEC.log_path)


@posix_paths
def test_systemd_quotes_spaces():
    text = render_systemd(SPEC)
    assert 'ExecStart="/opt/my node/bin/node" "/data/with space/current/server/dist/index.js"' in text
    assert "WorkingDirectory=/data/with space/current\n" in text  # systemd does not strip quotes here
    assert 'Environment="PORT=3001"' in text
    assert "Description=Polytropos FreeLLMAPI" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text


def test_macos_install_start_stop_remove(tmp_path):
    run = Runner(stdout="state = running")
    mgr = ServiceManager(platform="darwin", run=run, home=tmp_path, uid=501)
    plist = mgr.install(replace(SPEC, log_path=tmp_path / 'log/f.log'))
    assert plist == tmp_path / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    assert plistlib.loads(plist.read_bytes())["Label"] == LAUNCHD_LABEL
    target = f"gui/501/{LAUNCHD_LABEL}"
    assert run.calls == [["launchctl", "bootout", target], ["launchctl", "bootstrap", "gui/501", str(plist)]]
    run.calls.clear()
    mgr.start()
    assert mgr.is_running() is True
    mgr.remove()
    assert run.calls == [["launchctl", "print", target], ["launchctl", "kickstart", "-k", target],
                         ["launchctl", "print", target], ["launchctl", "bootout", target]]
    assert not plist.exists()
    assert ServiceManager(platform="darwin", run=Runner(stdout="state = waiting"), home=tmp_path, uid=1).is_running() is False


def test_linux_install_start_stop_remove(tmp_path):
    run = Runner(stdout="active\n")
    mgr = ServiceManager(platform="linux", run=run, home=tmp_path)
    unit = mgr.install(replace(SPEC, log_path=tmp_path / 'log/f.log'))
    assert unit == tmp_path / ".config/systemd/user" / SYSTEMD_UNIT
    assert "Restart=on-failure" in unit.read_text()
    assert run.calls == [["systemctl", "--user", "daemon-reload"],
                         ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT]]
    run.calls.clear()
    mgr.start()
    mgr.stop()
    assert mgr.is_running() is True
    mgr.remove()
    assert run.calls == [["systemctl", "--user", "restart", SYSTEMD_UNIT],
                         ["systemctl", "--user", "stop", SYSTEMD_UNIT],
                         ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
                         ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT],
                         ["systemctl", "--user", "daemon-reload"]]
    assert not unit.exists()
    assert ServiceManager(platform="linux", run=Runner(stdout="inactive\n"), home=tmp_path).is_running() is False


def test_macos_start_reloads_a_stopped_job(tmp_path):
    run = Runner()
    mgr = ServiceManager(platform="darwin", run=run, home=tmp_path, uid=501)
    run.stdout = ""
    orig = run.__call__
    run_calls = run.calls

    def fake(argv, **kw):
        res = orig(argv, **kw)
        return SimpleNamespace(returncode=113 if argv[1] == "print" else 0, stdout="", stderr="")

    mgr = ServiceManager(platform="darwin", run=fake, home=tmp_path, uid=501)
    mgr.start()
    assert [c[1] for c in run_calls] == ["print", "bootstrap", "kickstart"]


@posix_paths
def test_manual_start_hint(tmp_path):
    mac = ServiceManager(platform="darwin", run=Runner(), home=tmp_path, uid=501).manual_start_hint()
    # `;`: kickstart also runs when bootstrap fails because the job is already loaded
    assert mac == (f"launchctl bootstrap gui/501 {tmp_path}/Library/LaunchAgents/{LAUNCHD_LABEL}.plist; "
                   f"launchctl kickstart -k gui/501/{LAUNCHD_LABEL}")
    assert ServiceManager(platform="linux", run=Runner(), home=tmp_path).manual_start_hint() == \
        f"systemctl --user restart {SYSTEMD_UNIT}"
