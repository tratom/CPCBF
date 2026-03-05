"""Radio isolation preflight checks."""

from __future__ import annotations

import logging

from .agent_manager import AgentManager

logger = logging.getLogger(__name__)


class RadioIsolation:
    """Ensures only the test radio is active on both agents."""

    def __init__(self, manager: AgentManager, protocol: str):
        self._manager = manager
        self._protocol = protocol

    def _disable_subsystem(self, host_id: str, subsystem: str) -> None:
        resp = self._manager.send(
            host_id,
            {"command": "RADIO_DISABLE", "params": {"subsystem": subsystem}},
        )
        if resp.get("status") != "ok":
            raise RuntimeError(
                f"Failed to disable {subsystem} on {host_id}: {resp.get('message')}"
            )

    def _check_status(self, host_id: str) -> dict:
        resp = self._manager.send(host_id, {"command": "RADIO_STATUS"})
        if resp.get("status") != "ok":
            raise RuntimeError(f"Failed to get radio status from {host_id}")
        return resp.get("data", {})

    def run_preflight(self) -> None:
        """Disable non-test radios and verify isolation.

        For WiFi tests: disable Bluetooth, ensure WiFi is active.
        """
        for host_id in self._manager.host_ids:
            logger.info("Running isolation preflight on %s", host_id)

            if self._protocol == "wifi":
                self._disable_subsystem(host_id, "bluetooth")
            elif self._protocol == "bluetooth":
                self._disable_subsystem(host_id, "wifi")

            # Verify
            status = self._check_status(host_id)
            logger.info("Radio status on %s: %s", host_id, status)

            if self._protocol == "wifi":
                if status.get("bluetooth_active", False):
                    raise RuntimeError(
                        f"Bluetooth still active on {host_id} during WiFi test"
                    )
            elif self._protocol == "bluetooth":
                if status.get("wifi_active", False):
                    raise RuntimeError(
                        f"WiFi still active on {host_id} during Bluetooth test"
                    )

        logger.info("Radio isolation preflight passed for all hosts")
