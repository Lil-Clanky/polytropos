"""Managed FreeLLMAPI: build a pinned release, run it as a service, update and roll back."""

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
from pathlib import Path
from typing import Callable

from ._shared import paths
from .programs import child_env
from .services import ServiceManager, ServiceSpec
from .state import load_state

REPO_URL = "https://github.com/tashfeenahmed/freellmapi.git"
PORT = 3001
_URL = f"http://127.0.0.1:{PORT}/v1/models"
_DATA_FILES = ("freeapi.db", "freeapi.db-wal", "freeapi.db-shm", ".encryption-key", ".env")  # WAL is on in FreeLLMAPI
_SECRET_FILES = {".encryption-key", ".env"}
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?ENCRYPTION_KEY(?:\s*=|:\s)\s*([^\s#]*)")
_PLACEHOLDER = "your-64-char-hex-key-here"  # crypto.ts treats this value as no key


def _usable_key(raw: str) -> bool:
    value = raw.strip("'\"")
    return bool(value) and value != _PLACEHOLDER


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


class DataExists(Exception):
    pass


class RestartFailed(Exception):
    """The data and release were restored but the service did not start."""


def api_key() -> str | None:
    """This profile's FREELLMAPI_API_KEY, read the way the router reads it. Callers never print it."""
    try:
        from agent.secret_scope import get_secret  # the active profile's secret, as Hermes resolves it
    except ImportError:  # outside Hermes
        return os.environ.get("FREELLMAPI_API_KEY")
    return get_secret("FREELLMAPI_API_KEY")


def _default_get(url: str, headers: dict) -> tuple[int, bytes]:
    """(0, b"") when the connection is refused, (-1, b"") when something listens but does not answer HTTP."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read()
        except (OSError, http.client.HTTPException):  # cut-off body: the status code still counts
            return err.code, b""
    except urllib.error.URLError as err:
        return (0 if isinstance(err.reason, ConnectionRefusedError) else -1), b""
    except ConnectionRefusedError:
        return 0, b""
    except (OSError, http.client.HTTPException):
        return -1, b""


class FreeLLM:
    ready_timeout = 60.0
    stop_timeout = 30.0
    retry_delay = 1.0

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

    def _step(self, argv: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
        res = self.run(argv, capture_output=True, text=True, check=False, cwd=cwd, env=env)
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
            self._data_dir()
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
            self._step(["npm", "ci"], cwd=dest, env=child_env())
            self._step(["npm", "run", "build"], cwd=dest, env=child_env())
        except (BuildError, OSError) as err:
            shutil.rmtree(dest, ignore_errors=True)
            raise err if isinstance(err, BuildError) else BuildError(str(err)) from err
        return dest

    def _data_dir(self) -> Path:
        data = self.root / "data"
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        return data

    def ensure_env(self) -> Path:
        data = self._data_dir()
        env = data / ".env"
        lines = env.read_text(encoding="utf-8-sig").splitlines() if env.exists() else []
        found = [i for i, line in enumerate(lines) if _KEY_LINE.match(line)]  # dotenv uses the last definition
        if not found or not _usable_key(_KEY_LINE.match(lines[found[-1]]).group(1)):
            key_file = data / ".encryption-key"
            key = key_file.read_text().strip() if key_file.exists() else ""
            key = key if _usable_key(key) else secrets.token_hex(32)
            if found:
                lines[found[-1]] = f"ENCRYPTION_KEY={key}"
            else:
                lines.append(f"ENCRYPTION_KEY={key}")
            fd, tmp = tempfile.mkstemp(dir=data, prefix=".env.")  # mode 0o600; replace keeps the old file safe
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write("\n".join(lines) + "\n")
                os.replace(tmp, env)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
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
                 "FREEAPI_ENV_PATH": str(self.root / "data/.env"),
                 "FREEAPI_DB_PATH": str(self.root / "data/freeapi.db")},
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
        data = self._data_dir()
        present = [name for name in _DATA_FILES if (data / name).exists()]
        if present:
            raise DataExists(f"{data} already holds {', '.join(present)}; clear it by hand before adopting")
        ok, why = self.node_ok()  # check before copying, so a failed adopt can be retried
        if not ok:
            raise NodeMissing(why)
        for name in _DATA_FILES:
            src = source_checkout / (".env" if name == ".env" else f"server/data/{name}")  # .env: where env.ts looks
            if src.is_file():
                shutil.copy2(src, data / name)
                if name in _SECRET_FILES:
                    (data / name).chmod(0o600)
        self.install(pin)

    # --- backups, update, rollback ---

    def backup(self, label: str) -> Path:
        backups = self.root / "backups"
        backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        dest = backups / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{label}"
        dest.mkdir(mode=0o700)
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

    def _stop(self) -> None:
        """Stop the service and wait until port 3001 is free, so nobody copies a database in use."""
        self.services.stop()
        deadline = time.monotonic() + self.stop_timeout
        while self.detect().listening:
            if time.monotonic() >= deadline:
                raise BuildError(f"the service did not stop; port {PORT} still answers")
            time.sleep(0.5)

    def _revert(self, backup: Path, old_tag: str) -> bool:
        self._stop()
        self._restore(backup)
        if old_tag:
            self._point(old_tag)
        try:
            self.services.start()
        except Exception:  # launchd can still be busy with the bootout
            time.sleep(self.retry_delay)
            try:
                self.services.start()
            except Exception as err:
                raise RestartFailed(
                    f"restored {old_tag or 'the backup'} but it did not start: {err}. "
                    f"Start it by hand with: {self.services.manual_start_hint()}") from err
        return self._wait_ready()

    def _restart_old(self) -> str:
        try:
            self.services.start()
            return ""
        except Exception as err:
            return f" The old service did not restart: {err}"

    def update(self, pin: Pin, *, check: Callable[[], bool] | None = None) -> UpdateResult:
        old = self.current_tag()
        if old == pin.tag:
            return UpdateResult(True, old, old, False, f"already on {old}")
        try:
            self.build(pin)  # the running service stays up
        except BuildError as err:
            return UpdateResult(False, old, pin.tag, False, f"build failed, nothing changed: {err}")
        try:
            self._stop()
        except BuildError as err:
            return UpdateResult(False, old, pin.tag, False, f"nothing changed: {err}")
        try:
            backup = self.backup(f"{old or 'none'}-to-{pin.tag}")
        except OSError as err:
            return UpdateResult(False, old, pin.tag, False,
                                f"backup failed, nothing changed: {err}." + self._restart_old())
        try:
            self._point(pin.tag)
            self.services.start()
            healthy = self._wait_ready() and self.health()
            if healthy and check is not None:
                healthy = bool(check())
        except Exception:  # any failure after the stop counts as a failed health check
            healthy = False
        if healthy:
            return UpdateResult(True, old, pin.tag, False, f"updated to {pin.tag}")
        try:
            back = self._revert(backup, old)
        except RestartFailed as err:
            return UpdateResult(False, old, pin.tag, True, f"health check failed; {err}")
        except Exception as err:
            return UpdateResult(False, old, pin.tag, False, f"health check failed and the restore failed ({err})")
        return UpdateResult(False, old, pin.tag, True,
                            f"health check failed; restored {old or 'the backup'}" + ("" if back else ", which does not answer yet"))

    def rollback(self) -> UpdateResult:
        current = self.current_tag()
        backups = sorted(p for p in (self.root / "backups").glob("*-to-*") if p.is_dir())
        if not backups:
            return UpdateResult(False, current, "", False, "no update backup found")
        newest = backups[-1]
        old, new = newest.name.split("-", 1)[1].split("-to-", 1)
        if current != new:
            return UpdateResult(False, current, old, False,
                                f"the newest backup is for the update to {new}, but {current or 'no release'} is current; "
                                "it was already restored or is out of date, so nothing changed")
        if not (self.root / "releases" / old).is_dir() and old != "none":
            return UpdateResult(False, new, old, False, f"release {old} is no longer on disk")
        try:
            back = self._revert(newest, "" if old == "none" else old)
        except BuildError as err:
            return UpdateResult(False, new, old, False, f"nothing changed: {err}")
        except RestartFailed as err:
            return UpdateResult(False, new, old, True, str(err))
        return UpdateResult(True, new, old, True, f"restored {old}" + ("" if back else ", which does not answer yet"))


def _is_json(body: bytes) -> bool:
    try:
        json.loads(body)
        return True
    except ValueError:
        return False
