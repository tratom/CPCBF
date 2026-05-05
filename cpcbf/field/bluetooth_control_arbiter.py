"""Cross-process mutex around the shared RPi Bluetooth controller.

The 2_4ghz and lora services run as separate systemd units on each RPi
but share the single built-in BT controller for the brief bridge_sync
window at boot. This module is a plain `flock`-based mutex that
serialises that window. No fairness, no priority, no plan-boundary
yielding — the v2 single-plan-per-boot runner does at most one
bridge_sync per service per boot, so contention is bounded to a few
seconds total.

Backed by fcntl.flock on /var/lib/cpcbf/bluetooth_control.lock — POSIX
advisory lock, kernel auto-releases on process exit (crash-safe).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import sys
import time
from typing import Optional


STATE_DIR = "/var/lib/cpcbf"
LOCK_PATH = os.path.join(STATE_DIR, "bluetooth_control.lock")


def _ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _log(track: str, event: str, **kw) -> None:
    rec = {"ts": time.time(), "track": track, "event": event, **kw}
    sys.stderr.write("# [arbiter] " + json.dumps(rec) + "\n")
    sys.stderr.flush()


class BluetoothControlArbiter:
    def __init__(self, track: str):
        if track not in ("2_4ghz", "lora"):
            raise ValueError(f"unknown track {track!r}")
        self.track = track
        self._fd: Optional[int] = None
        _ensure_state_dir()

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """Acquire the BT control lock. Returns True on success."""
        if self._fd is not None:
            return True

        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        while True:
            try:
                fcntl.flock(fd, flags)
                self._fd = fd
                _log(self.track, "acquired", pid=os.getpid())
                return True
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    os.close(fd)
                    raise
                if not blocking:
                    os.close(fd)
                    return False
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.2)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            _log(self.track, "released", pid=os.getpid())

    @contextlib.contextmanager
    def held(self, blocking: bool = True, timeout: Optional[float] = None):
        ok = self.acquire(blocking=blocking, timeout=timeout)
        if not ok:
            raise TimeoutError(f"could not acquire BT control lock for {self.track}")
        try:
            yield
        finally:
            self.release()
