"""Cross-process arbiter for the shared RPi Bluetooth control interface.

Track A (2_4ghz) and Track B (lora) run as separate systemd units, so an
in-process Lock is insufficient. Backed by fcntl.flock on
/var/lib/cpcbf/bluetooth_control.lock — POSIX advisory lock, kernel
auto-releases on process exit (crash-safe).

Fairness:
  - 2_4ghz holds the lock for the duration of (flash + bridge_sync + test
    plan execution + rfkill restore). At each plan boundary it RELEASES,
    sleeps a 5 s grace window, and only then re-acquires. If the lora
    track is pending it will grab the lock during that window.
  - lora announces intent by touching /var/lib/cpcbf/lora_pending.flag
    BEFORE blocking on the lock. Once it acquires, it removes the flag.
  - 2_4ghz checks for the flag at boundaries and waits until it clears
    (60 s ceiling) before re-acquiring.
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
PENDING_FLAG = os.path.join(STATE_DIR, "lora_pending.flag")

GRACE_S = 5.0
PENDING_CEILING_S = 60.0


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

    def _open_lock(self) -> int:
        return os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)

    def _set_pending(self) -> None:
        try:
            with open(PENDING_FLAG, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

    def _clear_pending(self) -> None:
        try:
            os.unlink(PENDING_FLAG)
        except FileNotFoundError:
            pass

    def _pending_present(self) -> bool:
        return os.path.exists(PENDING_FLAG)

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """Acquire the BT control lock. Returns True on success.

        With blocking=False this returns immediately; with blocking=True and
        timeout=None it waits forever (kernel-blocking flock).
        """
        if self._fd is not None:
            return True

        if self.track == "lora":
            self._set_pending()

        fd = self._open_lock()
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        while True:
            try:
                fcntl.flock(fd, flags)
                self._fd = fd
                if self.track == "lora":
                    self._clear_pending()
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
                    if self.track == "lora":
                        self._clear_pending()
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

    def yield_at_boundary(self) -> None:
        """2_4ghz hook: release, give lora a window, then re-acquire.

        No-op for the lora track. Honours PENDING_CEILING_S so a stuck lora
        process can't starve the 2_4ghz schedule indefinitely.
        """
        if self.track != "2_4ghz":
            return

        had_lock = self._fd is not None
        self.release()
        time.sleep(GRACE_S)

        if self._pending_present():
            _log("2_4ghz", "yield_wait", reason="lora_pending")
            wait_deadline = time.monotonic() + PENDING_CEILING_S
            while self._pending_present() and time.monotonic() < wait_deadline:
                time.sleep(0.5)
            # Always allow the lora task to finish whatever it grabbed; we
            # then re-acquire below as normal.

        if had_lock:
            self.acquire(blocking=True)

    @contextlib.contextmanager
    def held(self, blocking: bool = True, timeout: Optional[float] = None):
        ok = self.acquire(blocking=blocking, timeout=timeout)
        if not ok:
            raise TimeoutError(f"could not acquire BT control lock for {self.track}")
        try:
            yield
        finally:
            self.release()
