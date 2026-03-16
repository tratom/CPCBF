"""Serial bridge transport — SSHes to a bridge RPi and runs serial_relay.py."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import paramiko

from .models import HostInfo

logger = logging.getLogger(__name__)

# Path to the relay script (shipped with cpcbf)
_RELAY_SCRIPT = Path(__file__).resolve().parent.parent / "field" / "serial_relay.py"
_REMOTE_RELAY = "/tmp/cpcbf_serial_relay.py"


class SerialBridgeTransport:
    """SSH to a bridge RPi, upload + run serial_relay.py, then JSON over stdin/stdout."""

    def __init__(self, host: HostInfo):
        self.host = host
        self._client: paramiko.SSHClient | None = None
        self._stdin = None
        self._stdout = None

    def connect(self) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": self.host.hostname,
            "username": self.host.username,
        }
        if self.host.password:
            connect_kwargs["password"] = self.host.password
        if self.host.key_filename:
            connect_kwargs["key_filename"] = self.host.key_filename

        self._client.connect(**connect_kwargs)
        logger.info("Connected to bridge %s", self.host.hostname)

    def start_agent(self, binary_path: str | None = None) -> None:
        """Upload serial_relay.py and start it on the bridge RPi."""
        # Upload relay script
        sftp = self._client.open_sftp()
        sftp.put(str(_RELAY_SCRIPT), _REMOTE_RELAY)
        sftp.close()
        logger.info("Uploaded relay to %s:%s", self.host.hostname, _REMOTE_RELAY)

        # Start relay
        port = self.host.serial_port or "/dev/ttyACM0"
        baud = self.host.serial_baud or 115200
        cmd = f"python3 {_REMOTE_RELAY} {port} {baud}"
        logger.info("Starting serial relay: %s", cmd)

        self._stdin, self._stdout, _ = self._client.exec_command(
            cmd, get_pty=False
        )

    def send_command(self, cmd_dict: dict, timeout: float = 30.0) -> dict:
        """Send a JSON command through the relay to the Arduino."""
        line = json.dumps(cmd_dict) + "\n"
        self._stdin.write(line)
        self._stdin.flush()

        channel = self._stdout.channel
        channel.settimeout(timeout)

        response_line = self._stdout.readline()
        if not response_line:
            raise RuntimeError("Serial relay closed connection unexpectedly")

        return json.loads(response_line)

    def upload_file(self, local_path: str | Path, remote_path: str) -> None:
        sftp = self._client.open_sftp()
        sftp.put(str(local_path), remote_path)
        sftp.close()

    def close(self) -> None:
        if self._stdin:
            try:
                self._stdin.close()
            except Exception:
                pass
        if self._client:
            self._client.close()
        logger.info("Disconnected from bridge %s", self.host.hostname)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
