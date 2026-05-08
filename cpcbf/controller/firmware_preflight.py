"""Firmware marker preflight + optional remote flash for MKR boards.

Runs BEFORE AgentManager opens the serial relay — flashing tears down
/dev/ttyACM_* via 1200-baud touch + bossac re-enumerate, and the relay
holds the port. SSH sessions opened here are one-shot.

Marker contract (mirrors cpcbf/field/firmware_flash.py):
    /var/lib/cpcbf/flashed_<basename(serial_port)>.txt
contains the env name (e.g. "mkrwifi1010_wifi_rssi") that's currently
on the board. The flasher writes it; we read it.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import paramiko

from .models import HostInfo, TestPlan

logger = logging.getLogger(__name__)


class FirmwareMismatch(RuntimeError):
    pass


def _is_mkr(host: HostInfo) -> bool:
    return host.board_type.startswith("mkr_")


def _marker_path(serial_port: str) -> str:
    return f"/var/lib/cpcbf/flashed_{os.path.basename(serial_port)}.txt"


def _connect(host: HostInfo) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict[str, Any] = {
        "hostname": host.hostname,
        "username": host.username,
    }
    if host.password:
        kwargs["password"] = host.password
    if host.key_filename:
        kwargs["key_filename"] = host.key_filename
    client.connect(**kwargs)
    return client


def _read_marker(client: paramiko.SSHClient, host: HostInfo) -> Optional[str]:
    cmd = f"cat {_marker_path(host.serial_port)} 2>/dev/null"
    _, stdout, _ = client.exec_command(cmd)
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        return None
    return stdout.read().decode().strip() or None


def _run_flasher(client: paramiko.SSHClient, host: HostInfo, env: str) -> None:
    if not host.flasher_path or not host.firmware_dir:
        raise FirmwareMismatch(
            f"cannot flash on {host.hostname}: inventory entry must set "
            "`flasher_path` (path to firmware_flash.py on the bridge RPi) "
            "and `firmware_dir` (where the .bin files live there)"
        )
    cmd = (
        f"sudo -n python3 {host.flasher_path} "
        f"{host.serial_port} {env} {host.firmware_dir} /tmp/cpcbf_flash.log"
    )
    logger.info("[%s] flashing %s ...", host.hostname, env)
    _, stdout, stderr = client.exec_command(cmd)
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        raise FirmwareMismatch(
            f"flash failed on {host.hostname} (rc={rc}): {err or out or '(no output)'}"
        )
    logger.info("[%s] flash done", host.hostname)


def _process_host(host_id: str, host: HostInfo, env: str, do_flash: bool) -> None:
    client = _connect(host)
    try:
        marker = _read_marker(client, host)
        if marker == env:
            logger.info("[%s] firmware OK (%s)", host_id, env)
            return
        if not do_flash:
            raise FirmwareMismatch(
                f"FIRMWARE_MISMATCH on {host_id}: expected {env!r}, "
                f"marker says {marker!r}. Run with --flash to fix."
            )
        _run_flasher(client, host, env)
        post = _read_marker(client, host)
        if post != env:
            raise FirmwareMismatch(
                f"FIRMWARE_MISMATCH on {host_id} after flash: marker is {post!r}"
            )
    finally:
        client.close()


def run(hosts: dict[str, HostInfo], plan: TestPlan, do_flash: bool) -> None:
    """Verify (and optionally flash) MKR firmware on every MKR host.

    No-op when the plan has no top-level `firmware:` field, or when no host
    in the inventory is an MKR board. Hosts are processed in parallel; the
    first error wins, but all are logged.
    """
    if not plan.firmware:
        if do_flash:
            logger.info("--flash given but plan has no `firmware:` — skipping")
        return
    mkr_hosts = {hid: h for hid, h in hosts.items() if _is_mkr(h)}
    if not mkr_hosts:
        return

    logger.info(
        "Firmware preflight: env=%s, flash=%s, hosts=%s",
        plan.firmware, do_flash, list(mkr_hosts.keys()),
    )

    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=len(mkr_hosts)) as pool:
        futures = {
            pool.submit(_process_host, hid, h, plan.firmware, do_flash): hid
            for hid, h in mkr_hosts.items()
        }
        for fut, hid in futures.items():
            try:
                fut.result()
            except Exception as e:
                logger.error("preflight failed on %s: %s", hid, e)
                errors.append(e)

    if errors:
        raise errors[0]
