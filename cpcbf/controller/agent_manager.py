"""Manages multiple agent connections."""

from __future__ import annotations

import logging
from typing import Any, Union

from .models import HostInfo
from .serial_bridge_transport import SerialBridgeTransport
from .ssh_transport import SSHTransport

logger = logging.getLogger(__name__)

Transport = Union[SSHTransport, SerialBridgeTransport]


class AgentManager:
    """Context manager that connects to all agents from an inventory."""

    def __init__(self, hosts: dict[str, HostInfo]):
        self._hosts = hosts
        self._transports: dict[str, Transport] = {}

    def __enter__(self):
        for host_id, host_info in self._hosts.items():
            logger.info("Connecting to %s (%s)...", host_id, host_info.hostname)
            if host_info.transport == "serial_bridge":
                transport: Transport = SerialBridgeTransport(host_info)
            else:
                transport = SSHTransport(host_info)
            transport.connect()
            self._transports[host_id] = transport
        return self

    def __exit__(self, *exc):
        for host_id, transport in self._transports.items():
            logger.info("Closing connection to %s", host_id)
            transport.close()
        self._transports.clear()

    def start_agent(self, host_id: str, binary_path: str | None = None) -> None:
        """Start the agent binary on the given host."""
        self._transports[host_id].start_agent(binary_path)

    def send(self, host_id: str, cmd_dict: dict, timeout: float = 30.0) -> dict:
        """Send a command to an agent and return the response."""
        return self._transports[host_id].send_command(cmd_dict, timeout)

    def get_transport(self, host_id: str) -> Transport:
        """Get the raw transport for a host."""
        return self._transports[host_id]

    def ensure_agent_binary(self, host_id: str, local_binary: str) -> None:
        """Upload the agent binary if needed."""
        transport = self._transports[host_id]
        if isinstance(transport, SSHTransport):
            remote_path = transport.host.agent_binary
            transport.upload_file(local_binary, remote_path)
            transport._client.exec_command(f"chmod +x {remote_path}")
            logger.info("Agent binary deployed to %s:%s", host_id, remote_path)

    def get_wifi_mac(self, host_id: str) -> str:
        """Return the WiFi MAC address for a host."""
        return self._hosts[host_id].wifi_mac

    def get_host_info(self, host_id: str) -> HostInfo:
        """Return the HostInfo for a host."""
        return self._hosts[host_id]

    @property
    def host_ids(self) -> list[str]:
        return list(self._transports.keys())
