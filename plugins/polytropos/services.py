"""LaunchAgent (macOS) and systemd user unit (Linux) for the managed FreeLLMAPI."""

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LAUNCHD_LABEL = "io.github.lil-clanky.polytropos.freellmapi"
SYSTEMD_UNIT = "polytropos-freellmapi.service"


@dataclass(frozen=True)
class ServiceSpec:
    program: list[str]
    env: dict[str, str]
    workdir: Path
    log_path: Path


def render_launchd(spec: ServiceSpec) -> bytes:
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": list(spec.program),
        "EnvironmentVariables": dict(spec.env),
        "WorkingDirectory": str(spec.workdir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
    })


def _q(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def render_systemd(spec: ServiceSpec) -> str:
    env = "".join(f"Environment={_q(f'{k}={v}')}\n" for k, v in spec.env.items())
    return (
        "[Unit]\nDescription=Polytropos FreeLLMAPI\n\n"
        f"[Service]\nExecStart={' '.join(_q(a) for a in spec.program)}\n{env}"
        f"WorkingDirectory={_q(str(spec.workdir))}\nRestart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


class ServiceManager:
    def __init__(self, *, platform: str = sys.platform, run=subprocess.run,
                 home: Path | None = None, uid: int | None = None):
        self._mac = platform == "darwin"
        self._run = run
        self._home = home or Path.home()
        self._uid = os.getuid() if uid is None else uid

    def path(self) -> Path:
        if self._mac:
            return self._home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        return self._home / ".config/systemd/user" / SYSTEMD_UNIT

    def _exec(self, *argv: str):
        return self._run(list(argv), capture_output=True, text=True, check=False)

    def _ctl(self, *args: str):
        return self._exec("systemctl", "--user", *args)

    @property
    def _target(self) -> str:
        return f"gui/{self._uid}/{LAUNCHD_LABEL}"

    def install(self, spec: ServiceSpec) -> Path:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self._mac:
            path.write_bytes(render_launchd(spec))
            self._exec("launchctl", "bootout", self._target)  # a loaded older copy would make bootstrap fail
            self._check(self._exec("launchctl", "bootstrap", f"gui/{self._uid}", str(path)))
        else:
            path.write_text(render_systemd(spec))
            self._ctl("daemon-reload")
            self._check(self._ctl("enable", "--now", SYSTEMD_UNIT))
        return path

    @staticmethod
    def _check(result) -> None:
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "service command failed").strip())

    def start(self) -> None:
        if not self._mac:
            return self._check(self._ctl("restart", SYSTEMD_UNIT))
        if self._exec("launchctl", "print", self._target).returncode != 0:  # stop() unloads the job
            self._check(self._exec("launchctl", "bootstrap", f"gui/{self._uid}", str(self.path())))
        self._check(self._exec("launchctl", "kickstart", "-k", self._target))

    def stop(self) -> None:
        if self._mac:
            self._exec("launchctl", "bootout", self._target)
        else:
            self._ctl("stop", SYSTEMD_UNIT)

    def is_running(self) -> bool:
        if self._mac:
            return "state = running" in (self._exec("launchctl", "print", self._target).stdout or "")
        return (self._ctl("is-active", SYSTEMD_UNIT).stdout or "").strip() == "active"

    def remove(self) -> None:
        if self._mac:
            self.stop()
        else:
            self._ctl("disable", "--now", SYSTEMD_UNIT)
        self.path().unlink(missing_ok=True)
        if not self._mac:
            self._ctl("daemon-reload")
