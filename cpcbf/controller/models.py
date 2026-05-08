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
    ble_phy: str = "1m"
    essid: str = ""
    # LoRa knobs — defaults match cpcbf/agent/platforms/arduino_mkr_wan1300/
    # src/config.h. Override per-plan to retune SF/BW/CR/Power without
    # rebuilding firmware. LDRO is auto-managed by the Sandeep lib on the
    # SX1276 (MKR WAN 1300) and will become a real knob once the LR1110
    # (Seeed WM1110) adapter lands; not exposed yet.
    lora_tx_power_dbm: int = 14
    lora_sf: int = 7
    lora_bw_hz: int = 125000
    lora_cr: int = 5


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
    ble_phy: str = "1m"
    essid: str = ""
    # LoRa knobs (mirrored from GlobalConfig). See GlobalConfig for context.
    lora_tx_power_dbm: int = 14
    lora_sf: int = 7
    lora_bw_hz: int = 125000
    lora_cr: int = 5


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
    transport: str = "ssh"          # "ssh" or "serial_bridge"
    serial_port: str = ""           # e.g. "/dev/ttyACM0"
    serial_baud: int = 115200
    board_type: str = "rpi4"        # "rpi4", "mkr_wifi_1010", or "mkr_wan_1300"
    # Where firmware_flash.py lives on the bridge RPi, and the dir holding
    # the .bin files. Used by the orchestrator's firmware preflight (--flash).
    flasher_path: str = ""
    firmware_dir: str = ""


@dataclass
class TestPlan:
    """Parsed test plan with global config and test list."""

    global_config: GlobalConfig
    tests: list[TestSpec]
    hosts: dict[str, HostInfo] = field(default_factory=dict)
    # Top-level `firmware:` field — names the .bin (without extension) that
    # MKR boards must be running. Checked by the orchestrator's preflight.
    firmware: Optional[str] = None
