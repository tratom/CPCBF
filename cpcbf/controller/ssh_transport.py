"""SSH transport layer using paramiko for agent communication."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import paramiko

from .models import HostInfo

logger = logging.getLogger(__name__)


class SSHTransport:
    """Manages SSH connection and stdin/stdout JSON communication with an agent."""

    def __init__(self, host: HostInfo):
        self.host = host
        self._client: paramiko.SSHClient | None = None
        self._channel: paramiko.Channel | None = None
        self._stdin = None
        self._stdout = None

    def connect(self) -> None:
        """Establish SSH connection."""
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
        logger.info("Connected to %s", self.host.hostname)

    def start_agent(self, binary_path: str | None = None) -> None:
        """Start the agent binary over SSH."""
        path = binary_path or self.host.agent_binary
        cmd = f"sudo {path} 2>/tmp/cpcbf_agent.log"
        logger.info("Starting agent: %s", cmd)

        self._stdin, self._stdout, _ = self._client.exec_command(
            cmd, get_pty=False
        )

    def send_command(self, cmd_dict: dict, timeout: float = 30.0) -> dict:
        """Send a JSON command and read the JSON response."""
        line = json.dumps(cmd_dict) + "\n"
        self._stdin.write(line)
        self._stdin.flush()

        self._channel = self._stdout.channel
        self._channel.settimeout(timeout)

        response_line = self._stdout.readline()
        if not response_line:
            raise RuntimeError("Agent closed connection unexpectedly")

        return json.loads(response_line)

    def upload_file(self, local_path: str | Path, remote_path: str) -> None:
        """Upload a file via SFTP."""
        sftp = self._client.open_sftp()
        sftp.put(str(local_path), remote_path)
        sftp.close()
        logger.info("Uploaded %s -> %s:%s", local_path, self.host.hostname, remote_path)

    def download_file(self, remote_path: str, local_path: str | Path) -> None:
        """Download a file via SFTP."""
        sftp = self._client.open_sftp()
        sftp.get(remote_path, str(local_path))
        sftp.close()
        logger.info("Downloaded %s:%s -> %s", self.host.hostname, remote_path, local_path)

    def close(self) -> None:
        """Close all connections."""
        if self._stdin:
            try:
                self._stdin.close()
            except Exception:
                pass
        if self._client:
            self._client.close()
        logger.info("Disconnected from %s", self.host.hostname)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
