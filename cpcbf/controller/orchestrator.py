"""Test orchestrator — coordinates agents through the full test lifecycle."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agent_manager import AgentManager
from .clock_sync import estimate_clock_offset
from .models import HostInfo, TestMode, TestPlan, TestSpec
from .radio_isolation import RadioIsolation

logger = logging.getLogger(__name__)


def _wifi_mac_to_p2p_addr(mac: str) -> str:
    """Convert a WiFi MAC to its P2P device address (set locally-administered bit)."""
    octets = mac.split(":")
    first_byte = int(octets[0], 16)
    first_byte |= 0x02  # set locally-administered bit
    octets[0] = f"{first_byte:02x}"
    return ":".join(octets)


class Orchestrator:
    """Runs all tests in a plan against a pair of agents."""

    def __init__(
        self,
        plan: TestPlan,
        hosts: dict[str, HostInfo],
        output_dir: Path,
    ):
        self.plan = plan
        self.hosts = hosts
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _build_configure_params(
        self, test: TestSpec, payload_size: int, role: str, host_id: str
    ) -> dict:
        """Build CONFIGURE command params for one agent."""
        host_ids = list(self.hosts.keys())
        peer_id = host_ids[1] if host_id == host_ids[0] else host_ids[0]

        is_sender = role == "sender"

        if test.protocol == "ble" and test.board == "mkr_wifi_1010":
            return {
                "iface_name": "",
                "peer_addr": "",
                "peer_mac": self.hosts[peer_id].ble_mac,
                "port": 0,
                "channel": 0,
                "essid": "",
                "local_ip": "",
                "netmask": "",
                "role": role,
                "topology": "ble_gatt",
                "protocol": "ble",
                "mode": test.mode.value,
                "payload_size": payload_size,
                "repetitions": test.repetitions,
                "warmup": test.warmup,
                "timeout_ms": test.timeout_ms,
                "inter_packet_us": test.inter_packet_us,
            }

        if test.protocol == "ble":
            return {
                "iface_name": "hci0",
                "peer_addr": "",
                "peer_mac": self.hosts[peer_id].ble_mac,
                "port": test.port,
                "channel": 0,
                "essid": "",
                "local_ip": "",
                "netmask": "",
                "role": role,
                "topology": "ble_l2cap",
                "protocol": "ble",
                "mode": test.mode.value,
                "payload_size": payload_size,
                "repetitions": test.repetitions,
                "warmup": test.warmup,
                "timeout_ms": test.timeout_ms,
                "inter_packet_us": test.inter_packet_us,
                "ble_phy": {"1m": 1, "2m": 2}.get(test.ble_phy, 1),
            }

        # MKR WiFi 1010: SoftAP topology (sender=AP, receiver=STA)
        if test.board == "mkr_wifi_1010":
            local_ip = "192.168.4.1" if is_sender else "192.168.4.2"
            peer_ip = "192.168.4.2" if is_sender else "192.168.4.1"
            essid = getattr(test, "essid", None) or "CPCBF_MKR"

            return {
                "iface_name": "",
                "peer_addr": peer_ip,
                "peer_mac": "",
                "port": test.port,
                "channel": test.channel,
                "essid": essid,
                "local_ip": local_ip,
                "netmask": "255.255.255.0",
                "role": role,
                "topology": "softap",
                "protocol": "wifi",
                "mode": test.mode.value,
                "payload_size": payload_size,
                "repetitions": test.repetitions,
                "warmup": test.warmup,
                "timeout_ms": test.timeout_ms,
                "inter_packet_us": test.inter_packet_us,
            }

        # RPi4 WiFi: P2P / ad-hoc topology
        local_ip = "192.168.49.1" if is_sender else "192.168.49.2"
        peer_ip = "192.168.49.2" if is_sender else "192.168.49.1"

        return {
            "iface_name": "wlan0",
            "peer_addr": peer_ip,
            "peer_mac": _wifi_mac_to_p2p_addr(self.hosts[peer_id].wifi_mac),
            "port": test.port,
            "channel": test.channel,
            "essid": "CPCBF_TEST",
            "local_ip": local_ip,
            "netmask": "255.255.255.0",
            "role": role,
            "topology": test.topology,
            "mode": test.mode.value,
            "payload_size": payload_size,
            "repetitions": test.repetitions,
            "warmup": test.warmup,
            "timeout_ms": test.timeout_ms,
            "inter_packet_us": test.inter_packet_us,
        }

    def _run_single_test(
        self,
        manager: AgentManager,
        test: TestSpec,
        payload_size: int,
    ) -> dict | None:
        """Run a single (test, payload_size) combination."""
        host_ids = manager.host_ids
        sender_id, receiver_id = host_ids[0], host_ids[1]

        logger.info(
            "=== %s | payload=%d | mode=%s ===",
            test.name,
            payload_size,
            test.mode.value,
        )

        # 0. Clean up any stale state from previous runs
        for hid in host_ids:
            try:
                manager.send(hid, {"command": "STOP"}, timeout=5.0)
            except Exception:
                pass  # board may not have anything to stop

        # 1. Radio isolation preflight
        isolation = RadioIsolation(manager, test.protocol)
        isolation.run_preflight()

        # 2. Clock sync (flood mode only)
        clock_offset = None
        if test.mode == TestMode.FLOOD:
            clock_offset = estimate_clock_offset(manager, sender_id, receiver_id)

        # 3. Configure both agents
        sender_params = self._build_configure_params(
            test, payload_size, "sender", sender_id
        )
        receiver_params = self._build_configure_params(
            test, payload_size, "receiver", receiver_id
        )

        resp = manager.send(
            sender_id, {"command": "CONFIGURE", "params": sender_params}
        )
        if resp.get("status") != "ok":
            logger.error("Sender configure failed: %s", resp)
            return None

        resp = manager.send(
            receiver_id, {"command": "CONFIGURE", "params": receiver_params}
        )
        if resp.get("status") != "ok":
            logger.error("Receiver configure failed: %s", resp)
            return None

        # 4. Set up link (protocol-dependent order)
        if test.protocol == "ble":
            # BLE: both sides in parallel — peripheral advertises + accepts
            # while central scans + connects. Sequential won't work because
            # accept() on the peripheral blocks until the central connects.
            # Retry up to 3 times — NINA SPI/HCI can intermittently fail
            # to discover even on a fresh start.
            BLE_SETUP_MAX_RETRIES = 3
            setup_cmd = "BLE_SETUP"
            ble_setup_ok = False

            for attempt in range(BLE_SETUP_MAX_RETRIES):
                if attempt > 0:
                    logger.warning(
                        "BLE setup attempt %d/%d — retrying after STOP...",
                        attempt + 1, BLE_SETUP_MAX_RETRIES,
                    )
                    for hid in host_ids:
                        try:
                            manager.send(hid, {"command": "STOP"}, timeout=5.0)
                        except Exception:
                            pass
                    time.sleep(2)

                logger.info("Setting up BLE on both sides in parallel...")

                def setup_agent(host_id: str) -> dict:
                    return manager.send(
                        host_id, {"command": setup_cmd}, timeout=90.0
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    receiver_future = pool.submit(setup_agent, receiver_id)
                    time.sleep(2)  # give peripheral time to start advertising
                    sender_future = pool.submit(setup_agent, sender_id)

                    receiver_resp = receiver_future.result()
                    sender_resp = sender_future.result()

                if (
                    receiver_resp.get("status") == "ok"
                    and sender_resp.get("status") == "ok"
                ):
                    ble_setup_ok = True
                    break

                logger.warning(
                    "BLE setup failed (attempt %d): receiver=%s sender=%s",
                    attempt + 1,
                    receiver_resp.get("status"),
                    sender_resp.get("status"),
                )

            if not ble_setup_ok:
                logger.error(
                    "BLE setup failed after %d attempts", BLE_SETUP_MAX_RETRIES
                )
                return None
        else:
            # WiFi: sender (GO) first, then receiver (client) — sequential
            setup_cmd = "WIFI_SETUP"
            logger.info("Setting up Wi-Fi on sender (GO)...")
            resp = manager.send(
                sender_id, {"command": setup_cmd}, timeout=90.0
            )
            if resp.get("status") != "ok":
                logger.error("WiFi setup failed on sender (GO): %s", resp)
                return None

            logger.info("Setting up Wi-Fi on receiver (client)...")
            resp = manager.send(
                receiver_id, {"command": setup_cmd}, timeout=90.0
            )
            if resp.get("status") != "ok":
                logger.error("WiFi setup failed on receiver (client): %s", resp)
                return None

        logger.info("%s link established, starting test...", test.protocol.upper())

        # 5. Run test: receiver first (2s head start), then sender
        def start_agent(host_id: str, timeout: float) -> dict:
            return manager.send(
                host_id, {"command": "START"}, timeout=timeout
            )

        timeout = test.timeout_ms / 1000 * (test.repetitions + test.warmup) + 60

        with ThreadPoolExecutor(max_workers=2) as pool:
            receiver_future = pool.submit(start_agent, receiver_id, timeout)
            if test.mode != TestMode.RSSI:
                time.sleep(2)  # receiver head start
            sender_future = pool.submit(start_agent, sender_id, timeout)

            sender_resp = sender_future.result()
            receiver_resp = receiver_future.result()

        if sender_resp.get("status") != "ok":
            logger.error("Sender test failed: %s", sender_resp)
            return None
        if receiver_resp.get("status") != "ok":
            logger.error("Receiver test failed: %s", receiver_resp)
            return None

        # 5. Collect results in parallel (minimise idle time on boards —
        #    SAMD21 USB CDC can stall if the board sits idle too long
        #    between the test end and GET_RESULTS)
        def get_results(host_id: str) -> dict:
            return manager.send(host_id, {"command": "GET_RESULTS"}, timeout=120.0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            sf = pool.submit(get_results, sender_id)
            rf = pool.submit(get_results, receiver_id)
            sender_results = sf.result()
            receiver_results = rf.result()

        combined = {
            "test_name": test.name,
            "mode": test.mode.value,
            "protocol": test.protocol,
            "board": test.board,
            "payload_size": payload_size,
            "repetitions": test.repetitions,
            "warmup": test.warmup,
            "topology": test.topology,
            "sender": sender_results.get("data", {}),
            "receiver": receiver_results.get("data", {}),
            "clock_offset_us": clock_offset.offset_us if clock_offset else None,
            "timestamp": time.time(),
        }

        return combined

    def _save_result(self, result: dict) -> Path:
        """Append result as a JSONL line."""
        jsonl_path = self.output_dir / "results.jsonl"
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(result) + "\n")
        logger.info("Result saved to %s", jsonl_path)
        return jsonl_path

    def _discover_ble_addrs(self, manager: AgentManager) -> None:
        """Query each MKR-BLE board for its actual NINA BLE address.

        The inventory's ble_mac field is brittle: if the physical boards
        are swapped between bridge RPis, the static mapping silently
        becomes wrong and the central tries to direct-connect to its own
        MAC. Ask the firmware what it sees and overwrite the cached
        HostInfo so _build_configure_params picks up the truth.
        """
        needs_ble = any(
            t.protocol == "ble" and t.board == "mkr_wifi_1010"
            for t in self.plan.tests
        )
        if not needs_ble:
            return

        for host_id in manager.host_ids:
            host = self.hosts[host_id]
            if getattr(host, "board_type", "") != "mkr_wifi_1010":
                continue
            try:
                resp = manager.send(
                    host_id, {"command": "GET_BLE_ADDR"}, timeout=15.0
                )
            except Exception as exc:
                logger.warning(
                    "GET_BLE_ADDR failed on %s (%s) — falling back to inventory %r",
                    host_id, exc, host.ble_mac,
                )
                continue
            if resp.get("status") != "ok":
                logger.warning(
                    "GET_BLE_ADDR error on %s: %s — falling back to inventory %r",
                    host_id, resp.get("message"), host.ble_mac,
                )
                continue
            discovered = resp.get("data", {}).get("ble_mac", "").lower()
            if not discovered:
                logger.warning(
                    "GET_BLE_ADDR returned no address on %s — "
                    "falling back to inventory %r",
                    host_id, host.ble_mac,
                )
                continue
            if host.ble_mac and host.ble_mac.lower() != discovered:
                logger.warning(
                    "BLE address mismatch on %s: inventory=%s hardware=%s — "
                    "using hardware (boards may have been swapped)",
                    host_id, host.ble_mac, discovered,
                )
            else:
                logger.info("Discovered BLE address on %s: %s", host_id, discovered)
            host.ble_mac = discovered

    def run(self) -> None:
        """Execute all tests in the plan."""
        with AgentManager(self.hosts) as manager:
            # Deploy agent binaries
            for host_id in manager.host_ids:
                manager.start_agent(host_id)

            self._discover_ble_addrs(manager)

            for test in self.plan.tests:
                for payload_size in test.payload_sizes:
                    try:
                        result = self._run_single_test(
                            manager, test, payload_size
                        )
                        if result:
                            self._save_result(result)
                    except Exception:
                        logger.exception(
                            "Test failed: %s payload=%d",
                            test.name,
                            payload_size,
                        )
                        # Clean up agents after failure
                        for hid in manager.host_ids:
                            try:
                                manager.send(hid, {"command": "STOP"}, timeout=5.0)
                            except Exception:
                                pass

                    # Cooldown between tests
                    logger.info("Cooling down for %ds...", test.cooldown_s)
                    time.sleep(test.cooldown_s)

        logger.info("All tests complete. Results in %s", self.output_dir)
