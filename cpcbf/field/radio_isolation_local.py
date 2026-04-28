"""Local rfkill helpers for the autonomous field runner.

Distinct from cpcbf/controller/radio_isolation.py (which sends RADIO_DISABLE
commands to remote agents over SSH). This one runs on the bridge RPi and
toggles its OWN wifi+bluetooth via rfkill so they can't interfere with
nearby MKR antennas during a 2.4 GHz test.

The 2_4ghz track wraps its test execution in isolated_2_4ghz(); the LoRa
track does not isolate (it shares 868 MHz with no on-board RPi radio, and
the bridge_sync handshake actually NEEDS RPi BT enabled).
"""
from __future__ import annotations

import contextlib
import subprocess
import sys


def _rfkill(action: str, ident: str) -> None:
    try:
        subprocess.run(["rfkill", action, ident],
                       capture_output=True, timeout=5, check=False)
    except FileNotFoundError:
        sys.stderr.write("# [radio_isolation] rfkill not installed\n")


def block_2_4ghz() -> None:
    _rfkill("block", "wifi")
    _rfkill("block", "bluetooth")


def unblock_all() -> None:
    _rfkill("unblock", "wifi")
    _rfkill("unblock", "bluetooth")
    subprocess.run(["systemctl", "start", "NetworkManager"],
                   capture_output=True, timeout=10)


@contextlib.contextmanager
def isolated_2_4ghz():
    """Block RPi wifi+bt for the duration; always restore on exit."""
    block_2_4ghz()
    try:
        yield
    finally:
        unblock_all()


@contextlib.contextmanager
def bt_temporarily_unblocked():
    """Briefly unblock BT for a bridge-side sync, then re-block on exit.

    Used by the 2_4ghz track around the bridge_sync handshake — sync needs
    BT, but the rest of the plan must keep RPi BT off so it doesn't bleed
    into the MKR's BLE measurements.
    """
    _rfkill("unblock", "bluetooth")
    try:
        yield
    finally:
        _rfkill("block", "bluetooth")
