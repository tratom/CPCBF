"""Visualization functions for benchmark results."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def _load_packets(db_path: str, mode_filter: str | None = None) -> pd.DataFrame:
    """Load non-warmup sender packets with run metadata."""
    conn = sqlite3.connect(db_path)
    query = """
        SELECT p.*, r.test_name, r.mode, r.payload_size, r.protocol, r.board
        FROM packets p
        JOIN test_runs r ON p.run_id = r.run_id
        WHERE p.warmup = 0 AND p.source = 'sender'
    """
    if mode_filter:
        query += f" AND r.mode = '{mode_filter}'"
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def plot_rtt_boxplot(db_path: str, output_path: str | Path) -> None:
    """RTT distribution by payload size (ping-pong mode)."""
    df = _load_packets(db_path, mode_filter="ping_pong")
    df = df[(df["lost"] == 0) & (df["rtt_us"] > 0)]

    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.boxplot(data=df, x="payload_size", y="rtt_us", ax=ax, showfliers=False)
    ax.set_xlabel("Payload Size (bytes)")
    ax.set_ylabel("RTT (us)")
    ax.set_title("RTT Distribution by Payload Size")
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def plot_rtt_cdf(db_path: str, output_path: str | Path) -> None:
    """Overlaid CDF curves per payload size (ping-pong mode)."""
    df = _load_packets(db_path, mode_filter="ping_pong")
    df = df[(df["lost"] == 0) & (df["rtt_us"] > 0)]

    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    for size, group in df.groupby("payload_size"):
        sorted_rtt = group["rtt_us"].sort_values()
        cdf = pd.Series(range(1, len(sorted_rtt) + 1)) / len(sorted_rtt)
        ax.plot(sorted_rtt.values, cdf.values, label=f"{size}B")

    ax.set_xlabel("RTT (us)")
    ax.set_ylabel("CDF")
    ax.set_title("RTT CDF by Payload Size")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def plot_throughput_bar(db_path: str, output_path: str | Path) -> None:
    """Throughput by payload size (flood mode)."""
    from .stats import compute_all_stats

    stats_df = compute_all_stats(db_path)
    flood_df = stats_df[stats_df["mode"] == "flood"].copy()

    if flood_df.empty:
        return

    flood_df["throughput_mbps"] = flood_df["throughput_bps"] / 1e6

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.bar(
        flood_df["payload_size"].astype(str),
        flood_df["throughput_mbps"],
        color=sns.color_palette()[0],
    )
    ax.set_xlabel("Payload Size (bytes)")
    ax.set_ylabel("Throughput (Mbps)")
    ax.set_title("Throughput by Payload Size (Flood Mode)")
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def plot_loss_bar(db_path: str, output_path: str | Path) -> None:
    """Packet loss % by payload size, grouped by mode."""
    from .stats import compute_all_stats

    stats_df = compute_all_stats(db_path)

    if stats_df.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    modes = stats_df["mode"].unique()
    x_positions = range(len(stats_df))
    labels = [f"{int(row['payload_size'])}B\n({row['mode']})"
              for _, row in stats_df.sort_values(["mode", "payload_size"]).iterrows()]
    values = stats_df.sort_values(["mode", "payload_size"])["packet_loss_pct"].values

    colors = [sns.color_palette()[0] if m == "ping_pong" else sns.color_palette()[1]
              for m in stats_df.sort_values(["mode", "payload_size"])["mode"]]
    bars = ax.bar(x_positions, values, color=colors)
    ax.set_xticks(x_positions)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Packet Loss (%)")
    ax.set_title("Packet Loss by Payload Size and Mode")
    ax.set_ylim(bottom=0, top=max(max(values) * 1.2, 1.0))

    # Add value labels on bars
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                f"{val:.1f}%", ha="center", va="bottom", fontsize=8)

    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def plot_rssi_timeseries(db_path: str, output_path: str | Path) -> None:
    """RSSI over sequence number."""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        """
        SELECT p.*, r.test_name, r.mode, r.payload_size, r.protocol, r.board
        FROM packets p
        JOIN test_runs r ON p.run_id = r.run_id
        WHERE p.warmup = 0 AND p.source = 'receiver'
        """,
        conn,
    )
    conn.close()
    df = df[(df["lost"] == 0) & (df["rssi"] != 0)]

    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    for (name, size), group in df.groupby(["test_name", "payload_size"]):
        ax.plot(group["seq"], group["rssi"], ".", alpha=0.5, markersize=2,
                label=f"{name} {size}B")

    ax.set_xlabel("Sequence Number")
    ax.set_ylabel("RSSI (dBm)")
    ax.set_title("RSSI Over Time")
    ax.legend(fontsize="small")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)
