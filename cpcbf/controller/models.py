"""CPCBF data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TestMode(str, Enum):
    PING_PONG = "ping_pong"
    FLOOD = "flood"
    RSSI = "rssi"


@dataclass
class GlobalConfig:
    """Global defaults applied to all tests unless overridden."""

    repetitions: int = 100
    warmup: int = 5
    timeout_ms: int = 5000
    inter_packet_us: int = 0
    port: int = 5201
    channel: int = 2437
    topology: str = "p2p"
    cooldown_s: int = 5


@dataclass
class TestSpec:
    """A single test specification."""

    name: str
    mode: TestMode
    protocol: str
    board: str
    payload_sizes: list[int]
    repetitions: int = 100
    warmup: int = 5
    timeout_ms: int = 5000
    inter_packet_us: int = 0
    port: int = 5201
    channel: int = 2437
    topology: str = "p2p"
    cooldown_s: int = 5


@dataclass
class HostInfo:
    """SSH connection details for one agent host."""

    hostname: str
    username: str = "pi"
    password: Optional[str] = None
    key_filename: Optional[str] = None
    wifi_mac: str = ""
    ble_mac: str = ""
    agent_binary: str = "/tmp/cpcbf_agent"


@dataclass
class TestPlan:
    """Parsed test plan with global config and test list."""

    global_config: GlobalConfig
    tests: list[TestSpec]
    hosts: dict[str, HostInfo] = field(default_factory=dict)
