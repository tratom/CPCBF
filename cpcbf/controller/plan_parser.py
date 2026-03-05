"""Parse and validate YAML test plans."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import GlobalConfig, HostInfo, TestMode, TestPlan, TestSpec


def _merge_defaults(global_cfg: GlobalConfig, test_dict: dict) -> dict:
    """Merge global defaults into a per-test dict (test overrides win)."""
    defaults = {
        "repetitions": global_cfg.repetitions,
        "warmup": global_cfg.warmup,
        "timeout_ms": global_cfg.timeout_ms,
        "inter_packet_us": global_cfg.inter_packet_us,
        "port": global_cfg.port,
        "channel": global_cfg.channel,
        "topology": global_cfg.topology,
        "cooldown_s": global_cfg.cooldown_s,
    }
    merged = {**defaults, **test_dict}
    return merged


def _validate_plan(plan: TestPlan) -> None:
    """Validate the test plan and raise ValueError on issues."""
    for test in plan.tests:
        # Minimum repetitions
        if test.repetitions < 30:
            raise ValueError(
                f"Test '{test.name}': minimum 30 repetitions required, got {test.repetitions}"
            )

        # Payload sizes must be positive
        for size in test.payload_sizes:
            if size < 0 or size > 8192:
                raise ValueError(
                    f"Test '{test.name}': payload size {size} out of range [0, 8192]"
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
        merged = _merge_defaults(global_cfg, t)
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
