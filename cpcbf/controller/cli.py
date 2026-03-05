"""CLI entry point for CPCBF."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .orchestrator import Orchestrator
from .plan_parser import parse_inventory, parse_plan


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="cpcbf",
        description="Cross-Platform Communication Benchmarking Framework",
    )
    parser.add_argument("plan", type=Path, help="Path to test plan YAML file")
    parser.add_argument(
        "-i",
        "--inventory",
        type=Path,
        required=True,
        help="Path to inventory YAML file",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("./results"),
        help="Output directory for results (default: ./results)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args(argv)

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Parse plan and inventory
    plan = parse_plan(args.plan)
    hosts = parse_inventory(args.inventory)
    plan.hosts = hosts

    logging.info(
        "Loaded plan with %d tests, %d hosts", len(plan.tests), len(hosts)
    )

    # Run
    orchestrator = Orchestrator(plan, hosts, args.output)
    orchestrator.run()


if __name__ == "__main__":
    main()
