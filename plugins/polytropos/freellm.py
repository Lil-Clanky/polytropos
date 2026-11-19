"""Managed FreeLLMAPI: build a pinned release, run it as a service, update and roll back."""

import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from ._shared import paths
from .services import ServiceManager, ServiceSpec
from .state import load_state

REPO_URL = "https://github.com/tashfeenahmed/freellmapi.git"
PORT = 3001
_URL = f"http://127.0.0.1:{PORT}/v1/models"
_DATA_FILES = ("freeapi.db", "freeapi.db-wal", "freeapi.db-shm", ".encryption-key", ".env")  # WAL is on in FreeLLMAPI
_SECRET_FILES = {".encryption-key", ".env"}


@dataclass(frozen=True)
class Pin:
    tag: str
    commit: str


@dataclass(frozen=True)
class Detected:
    listening: bool
    is_freellmapi: bool
    healthy: bool
    managed: bool


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    from_tag: str
    to_tag: str
    rolled_back: bool
    message: str


class BuildError(Exception):
    pass


class PortBusy(Exception):
    pass


class NodeMissing(Exception):
    pass


def _default_get(url: str, headers: dict) -> tuple[int, bytes]:
    """(0, b"") when nothing answers."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (urllib.error.URLError, OSError):
        return 0, b""


class FreeLLM:
    ready_timeout = 60.0

    def __init__(self, *, root: Path | None = None, run=subprocess.run,
                 services: ServiceManager | None = None,
                 http_get: Callable[[str, dict], tuple[int, bytes]] | None = None,
                 node: str | None = None):
        self.root = root or paths.data_dir() / "freellmapi"
        self.run = run
        self.services = services or ServiceManager()
        self.http_get = http_get or _default_get
        self.node = node or shutil.which("node") or "node"

    # --- detection ---

    def _is_managed_mode(self) -> bool:
        return load_state().shared.get("freellmapi", {}).get("mode") == "managed"

    def detect(self) -> Detected:
        status, body = self.http_get(_URL, {})
        is_api = status in (200, 401) and _is_json(body)
        return Detected(listening=status != 0, is_freellmapi=is_api, healthy=is_api and status == 200,
                        managed=self._is_managed_mode() and self.services.is_running())

    def health(self, api_key: str | None = None) -> bool:
        if api_key:
            status, body = self.http_get(_URL, {"Authorization": f"Bearer {api_key}"})
            return status == 200 and _is_json(body)
        return self.detect().is_freellmapi

    def node_ok(self) -> tuple[bool, str]:
        try:
            out = (self.run([self.node, "--version"], capture_output=True, text=True, check=False).stdout or "").strip()
        except OSError:
            return False, "node was not found"
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", out)
        if not match:
            return False, f"unreadable node version: {out!r}"
        version = tuple(int(x) for x in match.groups())
        if (20, 18, 0) <= version < (25, 0, 0):
            return True, out
        return False, f"node {out} is outside the supported range >=20.18.0 <25.0.0"

    # --- build and install ---

    def _step(self, argv: list[str], cwd: Path | None = None) -> str:
        res = self.run(argv, capture_output=True, text=True, check=False, cwd=cwd)
        if res.returncode != 0:
            tail = (res.stderr or res.stdout or "").strip()[-500:]
            raise BuildError(f"`{' '.join(argv)}` failed: {tail}")
        return res.stdout or ""

    def build(self, pin: Pin) -> Path:
        dest = self.root / "releases" / pin.tag
        data = self.root / "data"
        try:
            if dest.exists() or dest.is_symlink():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            self._step(["git", "clone", "--depth", "1", "--branch", pin.tag, REPO_URL, str(dest)])
            head = self._step(["git", "rev-parse", "HEAD"], cwd=dest).strip()
            if head != pin.commit:
                raise BuildError(f"tag {pin.tag} points at commit {head}, expected {pin.commit}")
            server_data = dest / "server" / "data"
            if server_data.is_symlink() or server_data.is_file():
                server_data.unlink()
            elif server_data.exists():
                shutil.rmtree(server_data)
            server_data.symlink_to(data)
            self._step(["npm", "ci"], cwd=dest)
            self._step(["npm", "run", "build"], cwd=dest)
        except (BuildError, OSError) as err:
            shutil.rmtree(dest, ignore_errors=True)
            raise err if isinstance(err, BuildError) else BuildError(str(err)) from err
        return dest

    def ensure_env(self) -> Path:
        data = self.root / "data"
        data.mkdir(parents=True, exist_ok=True)
        env = data / ".env"
        text = env.read_text() if env.exists() else ""
        if not re.search(r"^ENCRYPTION_KEY=\S", text, re.M):
            key_file = data / ".encryption-key"
            key = key_file.read_text().strip() if key_file.exists() else ""
            key = key or secrets.token_hex(32)
            if text and not text.endswith("\n"):
                text += "\n"
            fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{text}ENCRYPTION_KEY={key}\n")
        env.chmod(0o600)
        return env

    def current_tag(self) -> str:
        link = self.root / "current"
        return Path(os.readlink(link)).name if link.is_symlink() else ""

    def _point(self, tag: str) -> None:
        tmp = self.root / ".current.new"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(self.root / "releases" / tag)
        os.replace(tmp, self.root / "current")

    def _wait_ready(self) -> bool:
        deadline = time.monotonic() + self.ready_timeout
        while True:
            if self.detect().is_freellmapi:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)

    def _spec(self) -> ServiceSpec:
        return ServiceSpec(
            program=[self.node, str(self.root / "current/server/dist/index.js")],
            env={"PORT": str(PORT), "HOST": "127.0.0.1", "NODE_ENV": "production",
                 "FREEAPI_ENV_PATH": str(self.root / "data/.env")},
            workdir=self.root / "current",
            log_path=paths.state_dir() / "freellmapi.log",
        )

    def install(self, pin: Pin) -> None:
        detected = self.detect()
        if detected.listening and not detected.managed:
            raise PortBusy(f"port {PORT} is already in use; stop the program that uses it first")
        ok, why = self.node_ok()
        if not ok:
            raise NodeMissing(why)
        self.build(pin)
        self.ensure_env()
        self._point(pin.tag)
        self.services.install(self._spec())
        if not self._wait_ready():
            raise BuildError(f"FreeLLMAPI did not answer on port {PORT} within {self.ready_timeout:.0f} s")

    def adopt(self, source_checkout: Path, pin: Pin) -> None:
        if self.detect().listening:
            raise PortBusy(f"port {PORT} still answers; stop the old FreeLLMAPI first")
        data = self.root / "data"
        data.mkdir(parents=True, exist_ok=True)
        for name in _DATA_FILES:
            candidates = [source_checkout / "server" / "data" / name]
            if name == ".env":
                candidates = [source_checkout / "server" / ".env", source_checkout / ".env"]
            src = next((c for c in candidates if c.is_file()), None)
            if src:
                shutil.copy2(src, data / name)
                if name in _SECRET_FILES:
                    (data / name).chmod(0o600)
        self.install(pin)

    # --- backups, update, rollback ---

    def backup(self, label: str) -> Path:
        dest = self.root / "backups" / f"{datetime.now().strftime('%Y%m%dT%H%M%S%f')}-{label}"
        dest.mkdir(parents=True)
        self._copy_data(self.root / "data", dest)
        old = sorted(p for p in dest.parent.iterdir() if p.is_dir())
        for stale in old[:-5]:
            shutil.rmtree(stale, ignore_errors=True)
        return dest

    @staticmethod
    def _copy_data(src: Path, dst: Path) -> None:
        for name in _DATA_FILES:
            if (src / name).is_file():
                shutil.copy2(src / name, dst / name)
                if name in _SECRET_FILES:
                    (dst / name).chmod(0o600)

    def _restore(self, backup: Path) -> None:
        print("The backup replaces the database and keys. Changes made after the update are lost.")
        data = self.root / "data"
        for name in ("freeapi.db-wal", "freeapi.db-shm"):  # stale WAL files would corrupt the restored database
            (data / name).unlink(missing_ok=True)
        self._copy_data(backup, data)

    def _revert(self, backup: Path, old_tag: str) -> bool:
        self.services.stop()
        self._restore(backup)
        if old_tag:
            self._point(old_tag)
        self.services.start()
        return self._wait_ready()

    def update(self, pin: Pin, *, check: Callable[[], bool] | None = None) -> UpdateResult:
        old = self.current_tag()
        if old == pin.tag:
            return UpdateResult(True, old, old, False, f"already on {old}")
        try:
            self.build(pin)  # the running service stays up
        except BuildError as err:
            return UpdateResult(False, old, pin.tag, False, f"build failed, nothing changed: {err}")
        self.services.stop()
        backup = self.backup(f"{old or 'none'}-to-{pin.tag}")
        self._point(pin.tag)
        self.services.start()
        healthy = self._wait_ready() and self.health()
        if healthy and check is not None:
            try:
                healthy = bool(check())
            except Exception:  # a crashing check counts as a failed check
                healthy = False
        if healthy:
            return UpdateResult(True, old, pin.tag, False, f"updated to {pin.tag}")
        back = self._revert(backup, old)
        return UpdateResult(False, old, pin.tag, True,
                            f"health check failed; restored {old or 'the backup'}" + ("" if back else ", which does not answer yet"))

    def rollback(self) -> UpdateResult:
        backups = sorted(p for p in (self.root / "backups").glob("*-to-*") if p.is_dir())
        if not backups:
            return UpdateResult(False, self.current_tag(), "", False, "no update backup found")
        newest = backups[-1]
        old, new = newest.name.split("-", 1)[1].split("-to-", 1)
        if not (self.root / "releases" / old).is_dir() and old != "none":
            return UpdateResult(False, new, old, False, f"release {old} is no longer on disk")
        back = self._revert(newest, "" if old == "none" else old)
        return UpdateResult(True, new, old, True, f"restored {old}" + ("" if back else ", which does not answer yet"))

    def cli_command(self, lock: dict) -> list[str]:
        if self._is_managed_mode():
            return [self.node, str(self.root / "current/cli/dist/index.js")]
        return ["npx", f"freellmapi@{lock['freellmapi']['cli_version']}"]


def _is_json(body: bytes) -> bool:
    try:
        json.loads(body)
        return True
    except ValueError:
        return False
