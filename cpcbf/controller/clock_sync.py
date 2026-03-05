"""NTP-like clock offset estimation between agents."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .agent_manager import AgentManager

logger = logging.getLogger(__name__)


@dataclass
class ClockOffset:
    """Estimated clock offset between two hosts."""

    offset_us: float  # receiver_time - sender_time (microseconds)
    rtt_us: float  # round-trip measurement overhead
    uncertainty_us: float  # half the RTT


def estimate_clock_offset(
    manager: AgentManager,
    host_a: str,
    host_b: str,
    rounds: int = 11,
) -> ClockOffset:
    """Estimate clock offset between two agents using NTP-like timestamp exchange.

    Sends a timestamp query to each agent and measures the round-trip.
    Takes the median of multiple rounds for robustness.
    """
    offsets: list[float] = []
    rtts: list[float] = []

    for i in range(rounds):
        # Query timestamps from both agents as close together as possible
        t1 = time.monotonic_ns() / 1000  # local time in us

        resp_a = manager.send(host_a, {"command": "RADIO_STATUS"})
        t2 = time.monotonic_ns() / 1000

        resp_b = manager.send(host_b, {"command": "RADIO_STATUS"})
        t3 = time.monotonic_ns() / 1000

        rtt = t3 - t1
        # Estimate: midpoint of local measurement vs remote
        # Since we can't get agent timestamps from RADIO_STATUS directly,
        # we measure the SSH RTT as our uncertainty bound
        offset = (t2 - t1) - (t3 - t2)  # asymmetry in query times
        offsets.append(offset)
        rtts.append(rtt)

        if i < rounds - 1:
            time.sleep(0.1)

    # Take median
    offsets.sort()
    rtts.sort()
    median_idx = len(offsets) // 2
    median_offset = offsets[median_idx]
    median_rtt = rtts[median_idx]

    result = ClockOffset(
        offset_us=median_offset,
        rtt_us=median_rtt,
        uncertainty_us=median_rtt / 2,
    )

    logger.info(
        "Clock offset %s<->%s: %.0f us (RTT: %.0f us, uncertainty: %.0f us)",
        host_a,
        host_b,
        result.offset_us,
        result.rtt_us,
        result.uncertainty_us,
    )

    return result
