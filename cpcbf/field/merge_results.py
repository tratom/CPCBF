#!/usr/bin/env python3
"""Merge sender and receiver JSONL files into combined results for analysis.

Produces the same format as the SSH controller (cpcbf CLI), so that
run_analysis.py works identically on field and lab data.

Usage: python merge_results.py <results_dir> [-o results/results.jsonl]

For ping_pong (RTT): sender has all timing data; receiver RSSI is merged
into sender packets by matching seq numbers.
For flood: both sides are included as-is.
"""

import argparse
import json
import glob
import os
import re


def merge_receiver_rssi(sender_data, receiver_data):
    """Merge receiver RSSI values into sender packets by seq number."""
    # Build seq -> rssi lookup from receiver packets
    rssi_by_seq = {}
    for p in receiver_data.get("packets", []):
        rssi = p.get("rssi", 0)
        if rssi != 0:
            rssi_by_seq[p["seq"]] = rssi

    # Patch sender packets: fill in RSSI where sender has none
    for p in sender_data.get("packets", []):
        if p.get("rssi", 0) == 0 and p["seq"] in rssi_by_seq:
            p["rssi"] = rssi_by_seq[p["seq"]]

    return sender_data


def main():
    parser = argparse.ArgumentParser(description="Merge field result files")
    parser.add_argument("results_dir",
                        help="Directory with sender/receiver JSONL files")
    parser.add_argument("-o", "--output", default="results/results.jsonl",
                        help="Output merged JSONL file (default: results/results.jsonl)")
    args = parser.parse_args()

    files = glob.glob(os.path.join(args.results_dir, "*.jsonl"))

    # Group by test name (strip _sender/_receiver suffix)
    groups = {}
    for path in files:
        basename = os.path.basename(path)
        match = re.match(r"(.+)_(sender|receiver)\.jsonl$", basename)
        if not match:
            continue
        test_key = match.group(1)
        role = match.group(2)
        groups.setdefault(test_key, {})[role] = path

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    count = 0
    with open(args.output, "w") as out:
        for test_key in sorted(groups):
            pair = groups[test_key]

            # Load both sides
            sides = {}
            meta = {}
            for role in ("sender", "receiver"):
                if role not in pair:
                    continue
                with open(pair[role]) as f:
                    data = json.loads(f.readline())
                # Separate metadata from role-specific packet data
                sides[role] = data.pop(role, {})
                if not meta:
                    meta = data  # use first file's metadata

            mode = meta.get("mode", "ping_pong")
            sender_data = sides.get("sender", {})
            receiver_data = sides.get("receiver", {})

            # For ping_pong: merge receiver RSSI into sender packets
            if mode == "ping_pong" and receiver_data:
                sender_data = merge_receiver_rssi(sender_data, receiver_data)

            # Build output matching controller format exactly
            combined = {
                "test_name": meta.get("test_name", test_key),
                "mode": mode,
                "protocol": meta.get("protocol", "wifi"),
                "board": meta.get("board", "rpi4"),
                "payload_size": meta.get("payload_size"),
                "repetitions": meta.get("repetitions"),
                "warmup": meta.get("warmup"),
                "topology": meta.get("topology", "p2p"),
                "sender": sender_data,
                "receiver": receiver_data,
                "clock_offset_us": None,
                "timestamp": meta.get("timestamp"),
            }

            json.dump(combined, out)
            out.write("\n")
            count += 1
            present = "+".join(sorted(pair.keys()))
            print(f"  {test_key} ({present})")

    print(f"\nMerged {count} test runs into {args.output}")


if __name__ == "__main__":
    main()
