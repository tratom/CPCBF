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
        local_ip = "192.168.49.1" if is_sender else "192.168.49.2"
        peer_ip = "192.168.49.2" if is_sender else "192.168.49.1"

        return {
            "iface_name": "wlan0",
            "peer_addr": peer_ip,
            "peer_mac": self.hosts[peer_id].wifi_mac,
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

        # 4. Set up Wi-Fi link: GO (sender) first, then client (receiver)
        logger.info("Setting up Wi-Fi on sender (GO)...")
        resp = manager.send(
            sender_id, {"command": "WIFI_SETUP"}, timeout=30.0
        )
        if resp.get("status") != "ok":
            logger.error("Sender Wi-Fi setup failed: %s", resp)
            return None

        logger.info("Setting up Wi-Fi on receiver (client)...")
        resp = manager.send(
            receiver_id, {"command": "WIFI_SETUP"}, timeout=30.0
        )
        if resp.get("status") != "ok":
            logger.error("Receiver Wi-Fi setup failed: %s", resp)
            return None

        logger.info("Wi-Fi link established, starting test...")

        # 5. Run test: receiver first (2s head start), then sender
        def start_agent(host_id: str, timeout: float) -> dict:
            return manager.send(
                host_id, {"command": "START"}, timeout=timeout
            )

        timeout = test.timeout_ms / 1000 * (test.repetitions + test.warmup) + 60

        with ThreadPoolExecutor(max_workers=2) as pool:
            receiver_future = pool.submit(start_agent, receiver_id, timeout)
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

        # 5. Collect results
        sender_results = manager.send(sender_id, {"command": "GET_RESULTS"})
        receiver_results = manager.send(receiver_id, {"command": "GET_RESULTS"})

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

    def run(self) -> None:
        """Execute all tests in the plan."""
        with AgentManager(self.hosts) as manager:
            # Deploy agent binaries
            for host_id in manager.host_ids:
                manager.start_agent(host_id)

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

                    # Cooldown between tests
                    logger.info("Cooling down for %ds...", test.cooldown_s)
                    time.sleep(test.cooldown_s)

        logger.info("All tests complete. Results in %s", self.output_dir)
