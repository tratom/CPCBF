#!/usr/bin/env python3
"""
CPCBF — Analysis Script (DB-only, no ingestion)
Reads from PostgreSQL, computes statistics grouped by protocol, and generates
plots + CSV exports.

Usage: python -m cpcbf.analysis.run_analysis --experiment <name> [--output results/]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from .stats import compute_all_stats
from .compare import comparison_table
from .plots import (
    plot_rtt_boxplot,
    plot_rtt_cdf,
    plot_throughput_boxplot,
    plot_loss_bar,
    plot_rssi_timeseries,
)
from .db import get_connection


def _fmt_throughput(bps: float | None) -> str:
    if bps is None or bps <= 0:
        return "N/A"
    if bps > 1e6:
        return f"{bps / 1e6:.2f} Mbps"
    if bps > 1e3:
        return f"{bps / 1e3:.2f} Kbps"
    return f"{bps:.0f} bps"


def _resolve_experiment(name: str) -> int:
    """Look up experiment by name, return its ID or exit with error."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT experiment_id FROM experiments WHERE name = %s", (name,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row is None:
        print(f"Error: experiment '{name}' not found in database")
        print("Hint: ingest first with  cpcbf-ingest <results-dir> --experiment <name>")
        sys.exit(1)
    return row[0]


def _print_protocol_stats(stats_df: pd.DataFrame) -> None:
    """Print stats grouped by protocol, matching stats_from_json.py format."""
    if stats_df.empty:
        print("  No valid runs found.")
        return

    for protocol, proto_df in stats_df.groupby("protocol"):
        print()
        print(f"── {protocol} " + "─" * (50 - len(str(protocol))))

        # Ping-Pong (RTT) Results
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

        # Flood (Throughput) Results
        flood_df = proto_df[proto_df["mode"] == "flood"]
        if not flood_df.empty:
            print()
            print("  ── Flood (Throughput) Results ──")
            print()
            fmt_f = "  {:<10} {:>8} {:>14} {:>14} {:>14} {:>10} {:>8}"
            print(fmt_f.format("Payload", "Samples", "Mean TP", "Std TP", "95% CI", "Loss%", "CRC Err%"))
            print("  " + "-" * 93)
            for _, row in flood_df.sort_values("payload_size").iterrows():
                mean_bps = row.get("throughput_mean_bps")
                # Fall back to aggregate throughput when no per-chunk data (e.g. Arduino)
                if mean_bps is None or pd.isna(mean_bps):
                    mean_s = _fmt_throughput(row.get("throughput_bps")) + "*"
                    std_s = "N/A"
                    ci_s = "N/A"
                else:
                    mean_s = _fmt_throughput(mean_bps)
                    std_s = _fmt_throughput(row.get("throughput_std_bps"))
                    ci_low = row.get("throughput_ci95_low_bps")
                    ci_high = row.get("throughput_ci95_high_bps")
                    if ci_low and ci_high:
                        ci_s = f"[{_fmt_throughput(ci_low)}, {_fmt_throughput(ci_high)}]"
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
            print("  * = aggregate throughput only (no per-chunk data)")

        # RSSI Summary
        rssi_rows = proto_df[proto_df["rssi_mean"].notna()]
        if not rssi_rows.empty:
            print()
            print("  ── RSSI Summary ──")
            print()
            for _, row in rssi_rows.iterrows():
                std_str = f", std={row['rssi_std']:.1f}" if pd.notna(row["rssi_std"]) else ""
                print(f"    {row['test_name']} ({int(row['payload_size'])}B): "
                      f"mean={row['rssi_mean']:.1f} dBm{std_str}")


def main():
    parser = argparse.ArgumentParser(description="CPCBF Analysis Pipeline (DB-only)")
    parser.add_argument(
        "--experiment", type=str, required=True,
        help="Experiment name to analyze (must already be ingested)",
    )
    parser.add_argument(
        "--protocol", type=str, default=None,
        help="Filter by protocol (e.g. wifi, ble). Default: all protocols",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("results/"),
        help="Output directory for plots and reports",
    )
    args = parser.parse_args()

    # ── Resolve experiment ───────────────────────────────────────
    experiment_id = _resolve_experiment(args.experiment)
    protocol = args.protocol
    args.output.mkdir(parents=True, exist_ok=True)

    header = f"{'═' * 2} Experiment: {args.experiment}"
    if protocol:
        header += f" | Protocol: {protocol}"
    header += " " + "═" * max(1, 60 - len(header))
    print(header)

    # ── Compute statistics ───────────────────────────────────────
    stats_df = compute_all_stats(experiment_id=experiment_id, protocol=protocol)
    _print_protocol_stats(stats_df)

    # ── Comparison table ─────────────────────────────────────────
    print()
    print("=" * 60)
    print("Comparison Table")
    print("=" * 60)
    print()
    table = comparison_table(experiment_id=experiment_id, protocol=protocol)
    if not table.empty:
        print(table.to_string(index=False))

    csv_path = args.output / "comparison.csv"
    table.to_csv(csv_path, index=False)
    print(f"\n  Saved to {csv_path}")

    # ── Generate plots (per protocol) ────────────────────────────
    print()
    print("Generating plots...")

    plots_dir = args.output / "plots"
    plots_dir.mkdir(exist_ok=True)

    # Determine which protocols to plot
    if protocol:
        protocols = [protocol]
    else:
        protocols = sorted(stats_df["protocol"].unique()) if not stats_df.empty else []

    for proto in protocols:
        proto_dir = plots_dir / proto
        proto_dir.mkdir(exist_ok=True)
        plot_rtt_boxplot(proto_dir / "rtt_boxplot.png", experiment_id=experiment_id, protocol=proto)
        plot_rtt_cdf(proto_dir / "rtt_cdf.png", experiment_id=experiment_id, protocol=proto)
        plot_throughput_boxplot(proto_dir / "throughput.png", experiment_id=experiment_id, protocol=proto)
        plot_loss_bar(proto_dir / "loss.png", experiment_id=experiment_id, protocol=proto)
        plot_rssi_timeseries(proto_dir / "rssi.png", experiment_id=experiment_id, protocol=proto)
        print(f"  {proto} -> {proto_dir}/")

    # ── Export CSVs ──────────────────────────────────────────────
    stats_csv = args.output / "stats.csv"
    stats_df.to_csv(stats_csv, index=False)
    print(f"  Stats CSV:      {stats_csv}")

    conn = get_connection()
    pkt_query = """
        SELECT r.test_name, r.mode, r.protocol, r.payload_size,
               p.source, p.seq, p.tx_us, p.rx_us, p.rtt_us,
               p.rssi, p.crc_ok, p.lost, p.warmup
        FROM packets p
        JOIN test_runs r ON p.run_id = r.run_id
        WHERE r.valid = TRUE AND r.experiment_id = %s
    """
    pkt_params: list = [experiment_id]
    if protocol is not None:
        pkt_query += " AND r.protocol = %s"
        pkt_params.append(protocol)
    pkt_query += " ORDER BY r.run_id, p.source, p.seq"
    pkt_df = pd.read_sql_query(pkt_query, conn, params=pkt_params)
    conn.close()
    pkt_csv = args.output / "packets.csv"
    pkt_df.to_csv(pkt_csv, index=False)
    print(f"  Packets CSV:    {pkt_csv} ({len(pkt_df)} rows)")
    print(f"  Comparison CSV: {csv_path}")

    print()
    print("Analysis complete.")


if __name__ == "__main__":
    main()
