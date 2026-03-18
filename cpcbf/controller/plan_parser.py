"""Parse and validate YAML test plans."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import GlobalConfig, HostInfo, TestMode, TestPlan, TestSpec


def _merge_defaults(global_cfg: GlobalConfig, gc_raw: dict, test_dict: dict) -> dict:
    """Merge global and per-test config.  Explicit global keys win; for keys
    not set in the YAML global section, per-test values are used, falling back
    to GlobalConfig dataclass defaults."""
    defaults = {
        "repetitions": global_cfg.repetitions,
        "warmup": global_cfg.warmup,
        "timeout_ms": global_cfg.timeout_ms,
        "inter_packet_us": global_cfg.inter_packet_us,
        "port": global_cfg.port,
        "channel": global_cfg.channel,
        "topology": global_cfg.topology,
        "cooldown_s": global_cfg.cooldown_s,
        "ble_phy": global_cfg.ble_phy,
        "essid": global_cfg.essid,
    }
    # Start from dataclass defaults, layer test values, then global overrides
    merged = {**defaults, **test_dict, **{k: defaults[k] for k in gc_raw if k in defaults}}
    return merged


def _validate_plan(plan: TestPlan) -> None:
    """Validate the test plan and raise ValueError on issues."""
    for test in plan.tests:
        # Minimum repetitions
        if test.repetitions < 30:
            raise ValueError(
                f"Test '{test.name}': minimum 30 repetitions required, got {test.repetitions}"
            )

        # Payload sizes must be in valid range for the target board
        max_payload = 1024 if test.board == "mkr_wifi_1010" else 8192
        for size in test.payload_sizes:
            if size < 0 or size > max_payload:
                raise ValueError(
                    f"Test '{test.name}': payload size {size} out of range [0, {max_payload}]"
                )


def parse_plan(plan_path: str | Path) -> TestPlan:
    """Parse a YAML test plan file."""
    with open(plan_path) as f:
        raw = yaml.safe_load(f)

    # Parse global config
    gc_raw = raw.get("global", {})
    global_cfg = GlobalConfig(**{k: v for k, v in gc_raw.items() if k in GlobalConfig.__dataclass_fields__})

    # Parse tests
    tests = []
    for t in raw.get("tests", []):
        merged = _merge_defaults(global_cfg, gc_raw, t)
        mode = TestMode(merged.pop("mode"))
        tests.append(TestSpec(mode=mode, **merged))

    plan = TestPlan(global_config=global_cfg, tests=tests)
    _validate_plan(plan)
    return plan


def parse_inventory(inventory_path: str | Path) -> dict[str, HostInfo]:
    """Parse an inventory YAML file into a dict of host_id -> HostInfo."""
    with open(inventory_path) as f:
        raw = yaml.safe_load(f)

    hosts: dict[str, HostInfo] = {}
    for host_id, info in raw.get("hosts", {}).items():
        hosts[host_id] = HostInfo(**info)

    return hosts
