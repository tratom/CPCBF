"""Auto-flash MKR boards via 1200-baud touch + bossac.

The MKR firmwares are split per (protocol x mode) so the autonomous field
runner must reflash between plans. Sequence:

    1200-baud touch on the runtime port -> SAMD21 jumps to bootloader
    new ttyACM* node enumerates as the bossa bootloader CDC port
    bossac -e -w -v -R writes the .bin and resets the chip
    udev re-creates the runtime symlink (ttyACM_WIFI / ttyACM_WAN)

Persistent marker at /var/lib/cpcbf/flashed_<port-basename>.txt avoids
no-op reflashes across restarts of the same plan.
"""
from __future__ import annotations

import glob
import os
import subprocess
import time
from typing import Optional

import serial


MARKER_DIR = "/var/lib/cpcbf"
TOUCH_BAUD = 1200
BOOTLOADER_WAIT_S = 10.0
RUNTIME_WAIT_S = 15.0
BOSSAC_INFO_TIMEOUT_S = 5
BOSSAC_TIMEOUT_S = 60


class FlashError(Exception):
    def __init__(self, phase: str, message: str):
        super().__init__(f"[{phase}] {message}")
        self.phase = phase


def _read_marker(runtime_port: str) -> Optional[str]:
    path = _marker_path(runtime_port)
    try:
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _write_marker(runtime_port: str, env: str) -> None:
    os.makedirs(MARKER_DIR, exist_ok=True)
    with open(_marker_path(runtime_port), "w") as f:
        f.write(env)


def _marker_path(runtime_port: str) -> str:
    # Key on the input path's basename (e.g. "ttyACM_WIFI" for the udev
    # symlink), NOT the realpath. The kernel can renumber the backing
    # /dev/ttyACMN node across a 1200-baud touch + bossac re-enumerate,
    # so the realpath isn't stable; the udev symlink IS.
    base = os.path.basename(runtime_port)
    return os.path.join(MARKER_DIR, f"flashed_{base}.txt")


def _list_acm() -> set[str]:
    return set(glob.glob("/dev/ttyACM*"))


def _touch_1200(port: str, double_tap: bool = False) -> None:
    """Open serial at 1200 baud and immediately close — triggers SAMD bootloader."""
    try:
        s = serial.Serial(port, TOUCH_BAUD)
        s.setDTR(False)
        s.close()
    except (serial.SerialException, OSError) as e:
        raise FlashError("touch", f"1200-baud open failed on {port}: {e}")

    if double_tap:
        time.sleep(0.1)
        try:
            s = serial.Serial(port, TOUCH_BAUD)
            s.setDTR(False)
            s.close()
        except (serial.SerialException, OSError):
            pass


def _is_in_bootloader(port: str) -> bool:
    """Probe with `bossac -i` — succeeds only if the SAM-BA bootloader is up."""
    try:
        res = subprocess.run(
            ["bossac", "-i", "--port=" + port],
            capture_output=True, timeout=BOSSAC_INFO_TIMEOUT_S,
        )
        return res.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _wait_for_bootloader_port(before: set[str], runtime_port: str,
                              timeout_s: float) -> str:
    """Find the bossa bootloader port after a 1200-baud touch.

    Two valid outcomes:
      (a) a NEW /dev/ttyACM* node appears — kernel allocated a new minor
      (b) the runtime port disappears briefly then reappears at the same
          path with bossa speaking on it — kernel reused the minor

    Either is the bootloader. We poll for both and return whichever wins.
    """
    subprocess.run(["udevadm", "settle"], capture_output=True, timeout=10)
    deadline = time.monotonic() + timeout_s
    saw_disappear = False
    while time.monotonic() < deadline:
        now = _list_acm()
        new = now - before
        if new:
            time.sleep(0.3)
            return sorted(new)[0]

        if not os.path.exists(runtime_port):
            saw_disappear = True
        elif saw_disappear and _is_in_bootloader(runtime_port):
            return runtime_port

        time.sleep(0.2)
    raise FlashError("touch", f"bootloader port did not appear within {timeout_s}s")


def _run_bossac(bootloader_port: str, bin_path: str, log_path: str) -> None:
    # MKR boards link the application at 0x2000 (the SAM-BA bootloader sits at
    # 0x0-0x1FFF). Without --offset bossac writes the app's vector table over
    # the bootloader region and SAM-BA aborts the first page write.
    cmd = ["bossac", "--port=" + bootloader_port,
           "-e", "-w", "-v", "--offset=0x2000", "-R", bin_path]
    try:
        with open(log_path, "ab") as logf:
            logf.write(b"# bossac: " + " ".join(cmd).encode() + b"\n")
            logf.flush()
            res = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                 timeout=BOSSAC_TIMEOUT_S)
        if res.returncode != 0:
            raise FlashError("bossac", f"exit code {res.returncode} (see {log_path})")
    except FileNotFoundError:
        raise FlashError("bossac", "bossac binary not found — apt install bossa-cli")
    except subprocess.TimeoutExpired:
        raise FlashError("bossac", f"timed out after {BOSSAC_TIMEOUT_S}s")


def _wait_for_runtime_symlink(runtime_port: str, timeout_s: float) -> None:
    subprocess.run(["udevadm", "settle"], capture_output=True, timeout=10)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(runtime_port):
            time.sleep(0.5)
            return
        time.sleep(0.2)
    raise FlashError("reenumerate",
                     f"runtime port {runtime_port} did not reappear within {timeout_s}s")


def flash(port: str, env: str, firmware_dir: str, log_path: str,
          force: bool = False, double_tap: bool = False) -> None:
    """Flash <firmware_dir>/<env>.bin onto the board behind <port>.

    Idempotent: if the persistent marker matches <env> and force is False,
    returns immediately. After a successful flash, the marker is updated.
    """
    bin_path = os.path.join(firmware_dir, f"{env}.bin")
    if not os.path.isfile(bin_path):
        raise FlashError("preflight", f"firmware not found: {bin_path}")

    if not force and _read_marker(port) == env:
        return

    if not os.path.exists(port):
        raise FlashError("preflight", f"runtime port {port} does not exist")

    # Skip the 1200-baud touch when the board is ALREADY in bootloader mode
    # (factory-fresh, or recovering from a previous half-completed flash).
    # Otherwise the touch is a no-op and we'd time out waiting for a port
    # that's already there.
    if _is_in_bootloader(port):
        _run_bossac(port, bin_path, log_path)
    else:
        before = _list_acm()
        _touch_1200(port, double_tap=double_tap)
        bootloader = _wait_for_bootloader_port(before, port, BOOTLOADER_WAIT_S)
        _run_bossac(bootloader, bin_path, log_path)

    _wait_for_runtime_symlink(port, RUNTIME_WAIT_S)
    _write_marker(port, env)


def flash_with_retry(port: str, env: str, firmware_dir: str, log_path: str,
                     attempts: int = 3, cooldown_s: float = 15.0) -> None:
    """Flash with N retries; second and later attempts use double-tap touch.

    Preflight errors (missing .bin / missing port) are deterministic and not
    worth retrying — re-raised on the first failure so the caller can move on.
    """
    last_err: Optional[FlashError] = None
    for i in range(1, attempts + 1):
        try:
            flash(port, env, firmware_dir, log_path,
                  double_tap=(i > 1))
            return
        except FlashError as e:
            if e.phase == "preflight":
                raise
            last_err = e
            if i < attempts:
                time.sleep(cooldown_s)
    assert last_err is not None
    raise last_err


def _main():
    import argparse
    p = argparse.ArgumentParser(
        description="Flash a prebuilt MKR firmware via 1200-baud touch + bossac.",
    )
    p.add_argument("port", help="runtime serial port (e.g. /dev/ttyACM_WIFI)")
    p.add_argument("env", help="firmware basename without .bin "
                               "(e.g. mkrwifi1010_ble_rssi)")
    p.add_argument("firmware_dir", nargs="?",
                   default=os.path.join(os.path.dirname(__file__), "firmware"),
                   help="dir holding <env>.bin (default: ./firmware)")
    p.add_argument("log_path", nargs="?", default="/tmp/cpcbf_flash.log",
                   help="bossac output log (default: /tmp/cpcbf_flash.log)")
    p.add_argument("--force", action="store_true",
                   help="reflash even if the marker says <env> is current")
    p.add_argument("--attempts", type=int, default=3)
    args = p.parse_args()
    flash_with_retry(args.port, args.env, args.firmware_dir, args.log_path,
                     attempts=args.attempts)
    if args.force:
        # flash_with_retry skips on marker match; force path goes through flash().
        flash(args.port, args.env, args.firmware_dir, args.log_path, force=True)
    print(f"flashed {args.env} on {args.port}")


if __name__ == "__main__":
    _main()
