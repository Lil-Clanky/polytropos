"""Keep the machine awake while a Hermes turn runs."""

import logging
import subprocess
import sys
import threading

log = logging.getLogger(__name__)


class AwakeHolder:
    """One sleep-hold process shared by all held keys; the last release ends it."""

    def __init__(self, *, platform: str = sys.platform, popen=subprocess.Popen, max_hold_s: int = 7200):
        self._popen, self._lock = popen, threading.Lock()
        self._keys: set[str] = set()
        self._proc = None
        self._warned = False
        hold = str(max_hold_s)
        if platform == "darwin":
            self._argv = ["caffeinate", "-i", "-t", hold]
        elif platform.startswith("linux"):
            self._argv = ["systemd-inhibit", "--what=idle:sleep", "--who=polytropos",
                          "--why=Hermes turn", "sleep", hold]
        else:
            self._argv = None

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._keys)

    def acquire(self, key: str) -> None:
        if self._argv is None:
            return
        with self._lock:
            # the process ends by itself after max_hold_s, so start a new one if it is gone
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._proc = self._popen(self._argv, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError as exc:
                    self._proc = None
                    if not self._warned:
                        self._warned = True
                        log.debug("sleep hold program did not start: %s", type(exc).__name__)
                    return
            self._keys.add(key)

    def release(self, key: str) -> None:
        with self._lock:
            self._keys.discard(key)
            if not self._keys and self._proc is not None:
                self._proc.terminate()
                self._proc = None
