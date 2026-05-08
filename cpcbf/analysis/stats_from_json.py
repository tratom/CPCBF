#!/usr/bin/env python3
"""
Quick JSONL analysis — same rich output as run_analysis.py, no DB required.

Usage:
    python cpcbf/analysis/stats_from_json.py <path>

Where <path> is a .jsonl file or a directory containing .jsonl files.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

# Suppress pandas warning about raw DBAPI2 connections
warnings.filterwarnings("ignore", category=UserWarning, message=".*pandas only supports SQLAlchemy connectable.*")


@dataclass
class RunStats:
    """Computed statistics for a single test run (mirrors stats.py)."""

    test_name: str
    mode: str
    protocol: str
    payload_size: int
    early_aborted: bool
    packets_measured: int
    rtt_mean_us: float | None
    rtt_median_us: float | None
    rtt_std_us: float | None
    rtt_p95_us: float | None
    rtt_p99_us: float | None
    rtt_ci95_low: float | None
    rtt_ci95_high: float | None
    packet_loss_pct: float
    crc_error_pct: float
    jitter_us: float | None
    rssi_mean: float | None
    rssi_std: float | None
    throughput_bps: float | None
    throughput_mean_bps: float | None
    throughput_std_bps: float | None
    throughput_ci95_low_bps: float | None
    throughput_ci95_high_bps: float | None


def merge_receiver_rssi(sender_data: dict, receiver_data: dict) -> dict:
    """Merge receiver RSSI values into sender packets by seq number (from merge_results.py)."""
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


def merge_and_save_records(results_dir: Path, output_file: Path) -> list[dict]:
    """Merge field result files and save to disk, returning the merged records."""
    files = glob.glob(os.path.join(results_dir, "*.jsonl"))
    groups = {}
    for path in files:
        basename = os.path.basename(path)
        match = re.match(r"(.+)_(sender|receiver)\.jsonl$", basename)
        if not match:
            continue
        test_key = match.group(1)
        role = match.group(2)
        groups.setdefault(test_key, {})[role] = path

    output_file.parent.mkdir(parents=True, exist_ok=True)
    merged_records = []

    print(f"Merging {len(groups)} test runs from {results_dir}...")
    with open(output_file, "w") as out:
        for test_key in sorted(groups):
            pair = groups[test_key]
            sides = {}
            meta = {}
            for role in ("sender", "receiver"):
                if role not in pair:
                    continue
                with open(pair[role]) as f:
                    data = json.loads(f.readline())
                sides[role] = data.pop(role, {})
                if not meta:
                    meta = data

            mode = meta.get("mode", "ping_pong")
            sender_data = sides.get("sender", {})
            receiver_data = sides.get("receiver", {})

            if mode == "ping_pong" and receiver_data:
                sender_data = merge_receiver_rssi(sender_data, receiver_data)

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
            merged_records.append(combined)
            present = "+".join(sorted(pair.keys()))
            print(f"  {test_key} ({present})")

    print(f"\nMerged results saved to {output_file}")
    return merged_records


def _compute_chunk_throughput_stats(
    valid_packets: list[dict], protocol: str, payload_size: int, n_chunks: int = 5,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Split packets into n_chunks by rx_us, compute throughput per chunk, return stats."""
    rx_times = sorted(p["rx_us"] for p in valid_packets if p.get("rx_us", 0) > 0)
    if len(rx_times) < n_chunks * 2:
        return None, None, None, None

    overhead = 14 if protocol == "ble" else 42
    wire_bytes = payload_size + overhead

    chunks = np.array_split(rx_times, n_chunks)
    chunk_tps = []
    for chunk in chunks:
        if len(chunk) < 2:
            continue
        duration_us = chunk[-1] - chunk[0]
        if duration_us < 0:
            duration_us += 2**32
        if duration_us > 0:
            total_bits = len(chunk) * wire_bytes * 8
            chunk_tps.append(total_bits / (duration_us / 1e6))

    if len(chunk_tps) < 2:
        return None, None, None, None

    arr = np.array(chunk_tps)
    tp_mean = float(np.mean(arr))
    tp_std = float(np.std(arr, ddof=1))
    ci = sp_stats.t.interval(0.95, len(arr) - 1, loc=tp_mean, scale=sp_stats.sem(arr))
    return tp_mean, tp_std, float(ci[0]), float(ci[1])


def compute_stats_from_record(rec: dict) -> RunStats:
    """Compute summary statistics from a single JSONL record (dict)."""
    test_name = rec.get("test_name", "unknown")
    mode = rec.get("mode", "unknown")
    protocol = rec.get("protocol", "wifi")
    payload_size = rec.get("payload_size", 0)
    # early_aborted lives on the side sub-objects (sender/receiver), not top-level
    early_aborted = bool(rec.get("receiver", {}).get("early_aborted", 0)) \
                    or bool(rec.get("sender", {}).get("early_aborted", 0))

    # Source selection: ping_pong → sender (has RTT), rssi → receiver, flood → receiver
    if mode == "ping_pong":
        source = "sender"
    else:
        source = "receiver"
    side_data = rec.get(source, {})
    packets = side_data.get("packets", [])
    is_aggregate = bool(side_data.get("aggregate_only"))

    # Aggregate-only flood (constrained devices — no per-packet data)
    if is_aggregate and mode == "flood":
        sender_data = rec.get("sender", {})
        receiver_data = rec.get("receiver", {})
        packets_sent = sender_data.get("packets_sent", 0)
        packets_rcv = receiver_data.get("packets_received", 0)
        crc_errs = receiver_data.get("crc_errors", 0)
        start_us = receiver_data.get("start_us", 0)
        end_us = receiver_data.get("end_us", 0)

        packet_loss_pct = ((packets_sent - packets_rcv) / packets_sent * 100) if packets_sent > 0 else 0.0
        crc_error_pct = (crc_errs / packets_rcv * 100) if packets_rcv > 0 else 0.0

        overhead = 14 if protocol == "ble" else 42
        wire_bytes = payload_size + overhead

        throughput = None
        duration_us = end_us - start_us
        if duration_us < 0:
            # 32-bit microsecond timer wraparound
            duration_us += 2**32
        if duration_us > 0 and packets_rcv > 0:
            total_bits = packets_rcv * wire_bytes * 8
            throughput = total_bits / (duration_us / 1e6)

        # Agent-provided per-chunk summaries (e.g. MKR flood with aggregate_only=1).
        tp_mean = tp_std = tp_ci_low = tp_ci_high = None
        agent_chunks = receiver_data.get("flood_chunks") or sender_data.get("flood_chunks")
        if agent_chunks:
            chunk_tps = []
            for c in agent_chunks:
                pc = c.get("packet_count", 0)
                if pc < 2:
                    continue
                dur = c.get("end_us", 0) - c.get("start_us", 0)
                if dur < 0:
                    dur += 2**32
                if dur > 0:
                    chunk_tps.append(pc * wire_bytes * 8 / (dur / 1e6))
            if len(chunk_tps) >= 2:
                arr = np.array(chunk_tps)
                tp_mean = float(np.mean(arr))
                tp_std = float(np.std(arr, ddof=1))
                ci = sp_stats.t.interval(0.95, len(arr) - 1, loc=tp_mean, scale=sp_stats.sem(arr))
                tp_ci_low, tp_ci_high = float(ci[0]), float(ci[1])

        return RunStats(
            test_name=test_name,
            mode=mode,
            protocol=protocol,
            payload_size=payload_size,
            early_aborted=early_aborted,
            packets_measured=packets_rcv,
            rtt_mean_us=None, rtt_median_us=None, rtt_std_us=None,
            rtt_p95_us=None, rtt_p99_us=None,
            rtt_ci95_low=None, rtt_ci95_high=None,
            packet_loss_pct=packet_loss_pct,
            crc_error_pct=crc_error_pct,
            jitter_us=None, rssi_mean=None, rssi_std=None,
            throughput_bps=throughput,
            throughput_mean_bps=tp_mean,
            throughput_std_bps=tp_std,
            throughput_ci95_low_bps=tp_ci_low,
            throughput_ci95_high_bps=tp_ci_high,
        )

    # Per-packet analysis path (original)
    # Filter out warmup packets
    measured = [p for p in packets if not p.get("warmup")]
    total = len(measured)

    lost_count = sum(1 for p in measured if p.get("lost"))
    crc_err_count = sum(1 for p in measured if not p.get("crc_ok"))

    packet_loss_pct = (lost_count / total * 100) if total > 0 else 0.0
    crc_error_pct = (crc_err_count / total * 100) if total > 0 else 0.0

    # Valid packets: not lost (and has RTT for ping_pong)
    if mode == "ping_pong":
        valid = [p for p in measured if not p.get("lost") and p.get("rtt_us", 0) > 0]
    else:
        valid = [p for p in measured if not p.get("lost")]

    # RTT stats (ping_pong only)
    rtt_mean = rtt_median = rtt_std = rtt_p95 = rtt_p99 = None
    ci95_low = ci95_high = None
    jitter = None

    if len(valid) > 1 and mode == "ping_pong":
        rtts = np.array([p["rtt_us"] for p in valid], dtype=float)
        rtt_mean = float(np.mean(rtts))
        rtt_median = float(np.median(rtts))
        rtt_std = float(np.std(rtts, ddof=1))
        rtt_p95 = float(np.percentile(rtts, 95))
        rtt_p99 = float(np.percentile(rtts, 99))

        ci = sp_stats.t.interval(0.95, len(rtts) - 1, loc=rtt_mean, scale=sp_stats.sem(rtts))
        ci95_low, ci95_high = float(ci[0]), float(ci[1])

        diffs = np.abs(np.diff(rtts))
        jitter = float(np.mean(diffs)) if len(diffs) > 0 else None

    # RSSI stats (non-zero values from valid packets)
    rssi_vals = [p.get("rssi", 0) for p in valid if p.get("rssi", 0) != 0]
    rssi_mean = float(np.mean(rssi_vals)) if rssi_vals else None
    rssi_std = float(np.std(rssi_vals, ddof=1)) if len(rssi_vals) > 1 else None

    # Throughput (flood mode)
    throughput = None
    tp_mean = tp_std = tp_ci_low = tp_ci_high = None
    if mode == "flood" and len(valid) > 1:
        rx_times = sorted(p["rx_us"] for p in valid if p.get("rx_us", 0) > 0)
        if len(rx_times) > 1:
            duration_us = rx_times[-1] - rx_times[0]
            if duration_us < 0:
                duration_us += 2**32
            if duration_us > 0:
                overhead = 14 if protocol == "ble" else 42
                wire_bytes = payload_size + overhead
                total_bits = len(rx_times) * wire_bytes * 8
                throughput = total_bits / (duration_us / 1e6)
        # Per-chunk throughput stats
        tp_mean, tp_std, tp_ci_low, tp_ci_high = _compute_chunk_throughput_stats(
            valid, protocol, payload_size,
        )

    return RunStats(
        test_name=test_name,
        mode=mode,
        protocol=protocol,
        payload_size=payload_size,
        early_aborted=early_aborted,
        packets_measured=len(valid),
        rtt_mean_us=rtt_mean,
        rtt_median_us=rtt_median,
        rtt_std_us=rtt_std,
        rtt_p95_us=rtt_p95,
        rtt_p99_us=rtt_p99,
        rtt_ci95_low=ci95_low,
        rtt_ci95_high=ci95_high,
        packet_loss_pct=packet_loss_pct,
        crc_error_pct=crc_error_pct,
        jitter_us=jitter,
        rssi_mean=rssi_mean,
        rssi_std=rssi_std,
        throughput_bps=throughput,
        throughput_mean_bps=tp_mean,
        throughput_std_bps=tp_std,
        throughput_ci95_low_bps=tp_ci_low,
        throughput_ci95_high_bps=tp_ci_high,
    )


def load_jsonl(path: Path) -> list[dict]:
    """Load all JSON records from a .jsonl file."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def collect_records(path: Path) -> list[dict]:
    """Collect JSONL records from a file or directory."""
    path = path.resolve()
    if path.is_file():
        return load_jsonl(path)
    elif path.is_dir():
        records = []
        for f in sorted(path.glob("**/*.jsonl")):
            records.extend(load_jsonl(f))
        return records
    else:
        print(f"Error: {path} is not a file or directory")
        sys.exit(1)


def fmt_throughput(bps: float | None) -> str:
    if bps is None or bps <= 0:
        return "N/A"
    if bps > 1e6:
        return f"{bps / 1e6:.2f} Mbps"
    if bps > 1e3:
        return f"{bps / 1e3:.2f} Kbps"
    return f"{bps:.0f} bps"


def print_results(all_stats: list[RunStats]) -> None:
    """Print formatted output matching run_analysis.py steps 2–3."""
    df = pd.DataFrame([asdict(s) for s in all_stats])
    if df.empty:
        print("  No valid runs found.")
        return

    for protocol, proto_df in df.groupby("protocol"):
        print()
        print(f"── {protocol} " + "─" * (50 - len(str(protocol))))

        # ── Ping-Pong (RTT) Results ──
        rtt_df = proto_df[proto_df["mode"] == "ping_pong"]
        if not rtt_df.empty:
            print()
            print("  ── Ping-Pong (RTT) Results ──")
            print()
            fmt = "  {:<10} {:>8} {:>10} {:>10} {:>10} {:>10} {:>10} {:>8} {:>10}"
            print(fmt.format(
                "Payload", "Samples", "Mean(us)", "Med(us)",
                "Std(us)", "P95(us)", "P99(us)", "Loss%", "Jitter(us)",
            ))
            print("  " + "-" * 103)
            for _, row in rtt_df.sort_values("payload_size").iterrows():
                print(fmt.format(
                    f"{int(row['payload_size'])}B",
                    int(row["packets_measured"]),
                    f"{row['rtt_mean_us']:.0f}" if row["rtt_mean_us"] else "N/A",
                    f"{row['rtt_median_us']:.0f}" if row["rtt_median_us"] else "N/A",
                    f"{row['rtt_std_us']:.0f}" if row["rtt_std_us"] else "N/A",
                    f"{row['rtt_p95_us']:.0f}" if row["rtt_p95_us"] else "N/A",
                    f"{row['rtt_p99_us']:.0f}" if row["rtt_p99_us"] else "N/A",
                    f"{row['packet_loss_pct']:.1f}",
                    f"{row['jitter_us']:.0f}" if row["jitter_us"] else "N/A",
                ))

            # 95% Confidence Intervals
            print()
            print("  ── 95% Confidence Intervals ──")
            print()
            fmt_ci = "  {:<10} {:>15} {:>15} {:>15}"
            print(fmt_ci.format("Payload", "CI95 Low(us)", "CI95 High(us)", "Width(us)"))
            print("  " + "-" * 58)
            for _, row in rtt_df.sort_values("payload_size").iterrows():
                if row["rtt_ci95_low"] and row["rtt_ci95_high"]:
                    width = row["rtt_ci95_high"] - row["rtt_ci95_low"]
                    print(fmt_ci.format(
                        f"{int(row['payload_size'])}B",
                        f"{row['rtt_ci95_low']:.0f}",
                        f"{row['rtt_ci95_high']:.0f}",
                        f"{width:.0f}",
                    ))

        # ── Flood (Throughput) Results ──
        flood_df = proto_df[proto_df["mode"] == "flood"]
        if not flood_df.empty:
            print()
            print("  ── Flood (Throughput) Results ──")
            print()
            fmt_f = "  {:<10} {:>8} {:>14} {:>14} {:>14} {:>10} {:>8}"
            print(fmt_f.format("Payload", "Samples", "Mean TP", "Std TP", "95% CI", "Loss%", "CRC Err%"))
            print("  " + "-" * 93)
            has_aggregate_only = False
            has_aborted = False
            for _, row in flood_df.sort_values("payload_size").iterrows():
                mean_bps = row.get("throughput_mean_bps")
                aborted = bool(row.get("early_aborted"))
                if mean_bps is None or pd.isna(mean_bps):
                    # Aggregate-only flood (Arduino): no per-chunk stats
                    mean_s = fmt_throughput(row["throughput_bps"]) + "*"
                    std_s = "N/A"
                    ci_s = "N/A"
                    has_aggregate_only = True
                else:
                    mean_s = fmt_throughput(mean_bps)
                    std_s = fmt_throughput(row["throughput_std_bps"])
                    if row["throughput_ci95_low_bps"] and row["throughput_ci95_high_bps"]:
                        ci_s = f"[{fmt_throughput(row['throughput_ci95_low_bps'])}, {fmt_throughput(row['throughput_ci95_high_bps'])}]"
                    else:
                        ci_s = "N/A"
                print(fmt_f.format(
                    f"{int(row['payload_size'])}B",
                    int(row["packets_measured"]),
                    mean_s,
                    std_s,
                    ci_s,
                    f"{row['packet_loss_pct']:.1f}",
                    f"{row['crc_error_pct']:.1f}",
                ))
            if has_aggregate_only:
                print("  * = aggregate throughput only (no per-chunk data)")
            if has_aborted:
                print("  † = run aborted early (receiver hit MAX_CONSECUTIVE_TIMEOUTS); "
                      "throughput suppressed as the recv window only covers the initial burst")

        # ── RSSI Summary ──
        rssi_rows = proto_df[proto_df["rssi_mean"].notna()]
        if not rssi_rows.empty:
            print()
            print("  ── RSSI Summary ──")
            print()
            for _, row in rssi_rows.iterrows():
                std_str = f", std={row['rssi_std']:.1f}" if pd.notna(row["rssi_std"]) else ""
                print(f"    {row['test_name']} ({int(row['payload_size'])}B): "
                      f"mean={row['rssi_mean']:.1f} dBm{std_str}")

    # ── Comparison Table ──
    print()
    print("=" * 60)
    print("Comparison Table")
    print("=" * 60)
    print()
    cols = [
        "test_name", "mode", "protocol", "payload_size", "packets_measured",
        "rtt_median_us", "rtt_p95_us", "rtt_ci95_low", "rtt_ci95_high",
        "packet_loss_pct", "crc_error_pct", "jitter_us", "throughput_bps",
        "rssi_mean",
    ]
    available = [c for c in cols if c in df.columns]
    table = df[available].sort_values(["protocol", "test_name", "payload_size"])
    print(table.to_string(index=False))
    print()


def _print_metadata_header(path: Path) -> None:
    """Print experiment metadata header if experiment.json exists."""
    search_dir = path if path.is_dir() else path.parent
    exp_file = search_dir / "experiment.json"
    if not exp_file.exists():
        return
    try:
        with open(exp_file) as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    print()
    print(f"Experiment: {meta.get('scenario_id', 'N/A')}")
    if meta.get("location"):
        print(f"Location:   {meta['location']}")
    if meta.get("test_date"):
        print(f"Date:       {meta['test_date']}")

    parts = []
    if meta.get("distance_meters"):
        parts.append(f"Distance: {meta['distance_meters']}m")
    if parts:
        print(f"Config:     {' | '.join(parts)}")


def main():
    parser = argparse.ArgumentParser(description="Quick JSONL analysis (same rich output as run_analysis.py)")
    parser.add_argument("path", type=Path, help="JSONL file or directory containing .jsonl files")
    parser.add_argument("--merge", action="store_true", help="Merge split field result files before analysis")
    parser.add_argument("--output", type=Path, default=Path("results/results.jsonl"),
                        help="Output merged JSONL file path (default: results/results.jsonl)")
    parser.add_argument("--protocol", type=str, default=None, help="Filter by protocol (e.g. wifi, ble)")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"Error: {args.path} not found")
        sys.exit(1)

    _print_metadata_header(args.path)

    if args.merge:
        if not args.path.is_dir():
            print("Error: --merge requires a directory path containing _sender/_receiver.jsonl files")
            sys.exit(1)
        records = merge_and_save_records(args.path, args.output)
    else:
        records = collect_records(args.path)

    if not records:
        print("No JSONL records found.")
        sys.exit(1)

    # Filter by protocol if requested
    if args.protocol:
        records = [r for r in records if r.get("protocol") == args.protocol]
        if not records:
            print(f"No records found for protocol: {args.protocol}")
            sys.exit(1)

    print(f"Processing {len(records)} test runs...")

    all_stats = [compute_stats_from_record(rec) for rec in records]
    print_results(all_stats)


if __name__ == "__main__":
    main()
