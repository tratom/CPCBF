#!/usr/bin/env python3
"""
CPCBF — Complete Analysis Script
Ingests JSONL results into SQLite, computes statistics, and generates plots.

Usage: python run_analysis.py [--results results/results.jsonl] [--output results/]
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from analysis.ingest import ingest_jsonl, init_db
from analysis.stats import compute_all_stats, compute_run_stats
from analysis.plots import (
    plot_rtt_boxplot,
    plot_rtt_cdf,
    plot_throughput_bar,
    plot_loss_bar,
    plot_rssi_timeseries,
)
from analysis.compare import comparison_table


def main():
    parser = argparse.ArgumentParser(description="CPCBF Analysis Pipeline")
    parser.add_argument(
        "--results", type=Path, default=Path("results/results.jsonl"),
        help="Path to JSONL results file",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("results/"),
        help="Output directory for DB, plots, and reports",
    )
    args = parser.parse_args()

    if not args.results.exists():
        print(f"Error: {args.results} not found")
        sys.exit(1)

    args.output.mkdir(parents=True, exist_ok=True)
    db_path = str(args.output / "benchmark.db")
    plots_dir = args.output / "plots"
    plots_dir.mkdir(exist_ok=True)

    # ── Step 1: Ingest ──────────────────────────────────────────────
    print("=" * 60)
    print("STEP 1: Ingesting results into SQLite")
    print("=" * 60)
    count = ingest_jsonl(args.results, db_path)
    print(f"  Ingested {count} test runs into {db_path}")

    # ── Step 2: Compute statistics ──────────────────────────────────
    print()
    print("=" * 60)
    print("STEP 2: Computing statistics")
    print("=" * 60)

    stats_df = compute_all_stats(db_path)

    # Print RTT stats
    rtt_df = stats_df[stats_df["mode"] == "ping_pong"]
    if not rtt_df.empty:
        print()
        print("── Ping-Pong (RTT) Results ──")
        print()
        fmt = "{:<10} {:>8} {:>10} {:>10} {:>10} {:>10} {:>10} {:>8} {:>10}"
        print(fmt.format(
            "Payload", "Samples", "Mean(us)", "Med(us)",
            "Std(us)", "P95(us)", "P99(us)", "Loss%", "Jitter(us)"
        ))
        print("-" * 105)
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
        print("── 95% Confidence Intervals ──")
        print()
        fmt_ci = "{:<10} {:>15} {:>15} {:>15}"
        print(fmt_ci.format("Payload", "CI95 Low(us)", "CI95 High(us)", "Width(us)"))
        print("-" * 60)
        for _, row in rtt_df.sort_values("payload_size").iterrows():
            if row["rtt_ci95_low"] and row["rtt_ci95_high"]:
                width = row["rtt_ci95_high"] - row["rtt_ci95_low"]
                print(fmt_ci.format(
                    f"{int(row['payload_size'])}B",
                    f"{row['rtt_ci95_low']:.0f}",
                    f"{row['rtt_ci95_high']:.0f}",
                    f"{width:.0f}",
                ))

    # Print flood stats
    flood_df = stats_df[stats_df["mode"] == "flood"]
    if not flood_df.empty:
        print()
        print("── Flood (Throughput) Results ──")
        print()
        fmt_f = "{:<10} {:>8} {:>12} {:>10} {:>8}"
        print(fmt_f.format("Payload", "Samples", "Throughput", "Loss%", "CRC Err%"))
        print("-" * 55)
        for _, row in flood_df.sort_values("payload_size").iterrows():
            if row["throughput_bps"] and row["throughput_bps"] > 0:
                tp = row["throughput_bps"]
                if tp > 1e6:
                    tp_str = f"{tp / 1e6:.2f} Mbps"
                elif tp > 1e3:
                    tp_str = f"{tp / 1e3:.2f} Kbps"
                else:
                    tp_str = f"{tp:.0f} bps"
            else:
                tp_str = "N/A"
            print(fmt_f.format(
                f"{int(row['payload_size'])}B",
                int(row["packets_measured"]),
                tp_str,
                f"{row['packet_loss_pct']:.1f}",
                f"{row['crc_error_pct']:.1f}",
            ))

    # RSSI summary
    rssi_rows = stats_df[stats_df["rssi_mean"].notna()]
    if not rssi_rows.empty:
        print()
        print("── RSSI Summary ──")
        print()
        for _, row in rssi_rows.iterrows():
            print(f"  {row['test_name']} ({int(row['payload_size'])}B): "
                  f"mean={row['rssi_mean']:.1f} dBm, std={row['rssi_std']:.1f}")

    # ── Step 3: Comparison table ────────────────────────────────────
    print()
    print("=" * 60)
    print("STEP 3: Comparison table")
    print("=" * 60)
    print()
    table = comparison_table(db_path)
    print(table.to_string(index=False))

    # Save to CSV
    csv_path = args.output / "comparison.csv"
    table.to_csv(csv_path, index=False)
    print(f"\n  Saved to {csv_path}")

    # ── Step 4: Generate plots ──────────────────────────────────────
    print()
    print("=" * 60)
    print("STEP 4: Generating plots")
    print("=" * 60)

    plot_rtt_boxplot(db_path, plots_dir / "rtt_boxplot.png")
    print(f"  RTT boxplot       -> {plots_dir / 'rtt_boxplot.png'}")

    plot_rtt_cdf(db_path, plots_dir / "rtt_cdf.png")
    print(f"  RTT CDF           -> {plots_dir / 'rtt_cdf.png'}")

    plot_throughput_bar(db_path, plots_dir / "throughput.png")
    print(f"  Throughput bar     -> {plots_dir / 'throughput.png'}")

    plot_loss_bar(db_path, plots_dir / "loss.png")
    print(f"  Packet loss bar   -> {plots_dir / 'loss.png'}")

    plot_rssi_timeseries(db_path, plots_dir / "rssi.png")
    print(f"  RSSI timeseries   -> {plots_dir / 'rssi.png'}")

    # ── Step 5: Raw data export ─────────────────────────────────────
    print()
    print("=" * 60)
    print("STEP 5: Exporting raw data")
    print("=" * 60)

    stats_csv = args.output / "stats.csv"
    stats_df.to_csv(stats_csv, index=False)
    print(f"  Full stats        -> {stats_csv}")

    # Export per-packet RTT data for external tools
    import sqlite3
    conn = sqlite3.connect(db_path)
    pkt_df = pd.read_sql_query(
        """
        SELECT r.test_name, r.mode, r.payload_size,
               p.source, p.seq, p.tx_us, p.rx_us, p.rtt_us,
               p.rssi, p.crc_ok, p.lost, p.warmup
        FROM packets p
        JOIN test_runs r ON p.run_id = r.run_id
        ORDER BY r.run_id, p.source, p.seq
        """,
        conn,
    )
    conn.close()
    pkt_csv = args.output / "packets.csv"
    pkt_df.to_csv(pkt_csv, index=False)
    print(f"  Per-packet data   -> {pkt_csv} ({len(pkt_df)} rows)")

    # ── Summary ─────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)
    print(f"  Database:    {db_path}")
    print(f"  Plots:       {plots_dir}/")
    print(f"  Stats CSV:   {stats_csv}")
    print(f"  Packets CSV: {pkt_csv}")
    print(f"  Comparison:  {csv_path}")


if __name__ == "__main__":
    main()
