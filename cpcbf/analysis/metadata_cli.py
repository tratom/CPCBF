"""CLI for experiment metadata: init skeletons and validate."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import jsonschema

_SCHEMA_PATH = Path(__file__).parent / "experiment_schema.json"


def _load_schema() -> dict:
    with open(_SCHEMA_PATH) as f:
        return json.load(f)


def _skeleton(scenario_id: str) -> dict:
    """Return a minimal experiment.json skeleton."""
    return {
        "scenario_id": scenario_id,
        "test_procedure_description": "",
        "environmental_description": "",
        "location": "",
        "test_date": date.today().isoformat(),
        "test_configuration": {
            "technology": "",
            "distance_meters": 0,
            "samples_per_scenario": 1,
            "iteration_duration_minutes": 0,
            "measured_metrics": [],
        },
        "devices": [
            {
                "role": "sender",
                "device_id": "",
                "hardware": "",
                "ip_address": "",
                "mac_address": "",
                "notes": "",
            },
            {
                "role": "receiver",
                "device_id": "",
                "hardware": "",
                "ip_address": "",
                "mac_address": "",
                "notes": "",
            },
        ],
        "dynamic_interference": {
            "pedestrians_observed": 0,
            "vehicles_observed": 0,
            "notes": "",
        },
        "media_files": [],
    }


def cmd_init(directory: Path) -> int:
    """Write a skeleton experiment.json into *directory*."""
    directory = directory.resolve()
    if not directory.is_dir():
        print(f"Error: {directory} is not a directory")
        return 1

    out = directory / "experiment.json"
    if out.exists():
        print(f"experiment.json already exists in {directory}")
        return 1

    scenario_id = directory.name
    skeleton = _skeleton(scenario_id)

    with open(out, "w") as f:
        json.dump(skeleton, f, indent=2)
        f.write("\n")

    print(f"Created {out}")
    return 0


def load_experiment_metadata(directory: Path) -> dict | None:
    """Load and validate experiment.json from *directory*. Returns dict or None."""
    exp_file = directory / "experiment.json"
    if not exp_file.exists():
        return None
    with open(exp_file) as f:
        metadata = json.load(f)
    schema = _load_schema()
    jsonschema.validate(instance=metadata, schema=schema)
    return metadata


def cmd_validate(directory: Path) -> int:
    """Validate experiment.json in *directory* against the schema."""
    directory = directory.resolve()
    exp_file = directory / "experiment.json"
    if not exp_file.exists():
        print(f"No experiment.json found in {directory}")
        return 1

    with open(exp_file) as f:
        metadata = json.load(f)

    schema = _load_schema()
    try:
        jsonschema.validate(instance=metadata, schema=schema)
    except jsonschema.ValidationError as e:
        print(f"Validation FAILED: {e.message}")
        print(f"  Path: {' -> '.join(str(p) for p in e.absolute_path)}")
        return 1

    print(f"experiment.json is valid (scenario: {metadata['scenario_id']})")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="cpcbf-metadata",
        description="Manage experiment metadata (experiment.json)",
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    p_init = sub.add_parser("init", help="Create skeleton experiment.json")
    p_init.add_argument("directory", type=Path, help="Experiment directory")

    p_val = sub.add_parser("validate", help="Validate experiment.json against schema")
    p_val.add_argument("directory", type=Path, help="Experiment directory")

    args = parser.parse_args()

    if args.command == "init":
        sys.exit(cmd_init(args.directory))
    elif args.command == "validate":
        sys.exit(cmd_validate(args.directory))


if __name__ == "__main__":
    main()
