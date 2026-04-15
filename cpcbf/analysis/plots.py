"""Visualization functions for benchmark results."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from .db import get_connection


def _load_packets(
    mode_filter: str | None = None,
    source: str = "sender",
    experiment_id: int | None = None,
    protocol: str | None = None,
) -> pd.DataFrame:
    """Load non-warmup packets with run metadata from PostgreSQL."""
    conn = get_connection()
    query = """
        SELECT p.*, r.test_name, r.mode, r.payload_size, r.protocol, r.board
        FROM packets p
        JOIN test_runs r ON p.run_id = r.run_id
        WHERE p.warmup = FALSE AND p.source = %s AND r.valid = TRUE
    """
    params: list = [source]
    if mode_filter:
        query += " AND r.mode = %s"
        params.append(mode_filter)
    if experiment_id is not None:
        query += " AND r.experiment_id = %s"
        params.append(experiment_id)
    if protocol is not None:
        query += " AND r.protocol = %s"
        params.append(protocol)
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


def plot_rtt_boxplot(output_path: str | Path, experiment_id: int | None = None, protocol: str | None = None) -> None:
    """RTT distribution by payload size (ping-pong mode)."""
    df = _load_packets(mode_filter="ping_pong", source="sender", experiment_id=experiment_id, protocol=protocol)
    df = df[(df["lost"] == False) & (df["rtt_us"] > 0)]  # noqa: E712

    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.boxplot(data=df, x="payload_size", y="rtt_us", hue="protocol", ax=ax, showfliers=False)
    ax.set_xlabel("Payload Size (bytes)")
    ax.set_ylabel("RTT (us)")
    ax.set_title("RTT Distribution by Payload Size")
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def plot_rtt_cdf(output_path: str | Path, experiment_id: int | None = None, protocol: str | None = None) -> None:
    """Overlaid CDF curves per payload size (ping-pong mode)."""
    df = _load_packets(mode_filter="ping_pong", source="sender", experiment_id=experiment_id, protocol=protocol)
    df = df[(df["lost"] == False) & (df["rtt_us"] > 0)]  # noqa: E712

    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    for (protocol, size), group in df.groupby(["protocol", "payload_size"]):
        sorted_rtt = group["rtt_us"].sort_values()
        cdf = pd.Series(range(1, len(sorted_rtt) + 1)) / len(sorted_rtt)
        ax.plot(sorted_rtt.values, cdf.values, label=f"{protocol} {size}B")

    ax.set_xlabel("RTT (us)")
    ax.set_ylabel("CDF")
    ax.set_title("RTT CDF by Payload Size")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def _fmt_throughput_label(mbps: float) -> str:
    """Format throughput for bar label."""
    if mbps >= 1:
        return f"{mbps:.1f} Mbps"
    return f"{mbps * 1e3:.0f} Kbps"


def plot_throughput_bar(output_path: str | Path, experiment_id: int | None = None, protocol: str | None = None) -> None:
    """Backward-compatible alias — delegates to boxplot."""
    plot_throughput_boxplot(output_path, experiment_id=experiment_id, protocol=protocol)


def _wire_overhead(protocol: str) -> int:
    return 14 if protocol == "ble" else 42


def plot_throughput_boxplot(output_path: str | Path, experiment_id: int | None = None, protocol: str | None = None) -> None:
    """Throughput boxplot: one box per payload size.

    Pulls per-chunk samples from `flood_chunks` when available, and falls back
    to a single aggregate sample from `flood_runs` for runs without chunks
    (e.g. Arduino aggregate-only flood).
    """
    conn = get_connection()

    base_where = "WHERE r.valid = TRUE AND r.mode = 'flood'"
    params_exp: list = []
    if experiment_id is not None:
        base_where += " AND r.experiment_id = %s"
        params_exp.append(experiment_id)
    if protocol is not None:
        base_where += " AND r.protocol = %s"
        params_exp.append(protocol)

    # Chunks (5-per-run)
    chunk_query = f"""
        SELECT r.run_id, r.payload_size, r.protocol,
               fc.packet_count, fc.start_us, fc.end_us
        FROM flood_chunks fc
        JOIN test_runs r ON fc.run_id = r.run_id
        {base_where}
    """
    chunk_df = pd.read_sql_query(chunk_query, conn, params=params_exp)

    # Aggregate flood_runs — use for runs with no chunks
    agg_query = f"""
        SELECT r.run_id, r.payload_size, r.protocol,
               f.receiver_measured_count AS packet_count,
               f.receiver_start_us AS start_us,
               f.receiver_end_us AS end_us
        FROM flood_runs f
        JOIN test_runs r ON f.run_id = r.run_id
        {base_where}
    """
    agg_df = pd.read_sql_query(agg_query, conn, params=params_exp)
    conn.close()

    chunked_run_ids = set(chunk_df["run_id"].unique()) if not chunk_df.empty else set()
    agg_only_df = agg_df[~agg_df["run_id"].isin(chunked_run_ids)] if not agg_df.empty else agg_df

    def compute_tp_mbps(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        durations = df["end_us"].astype("Int64") - df["start_us"].astype("Int64")
        # 32-bit wraparound correction
        durations = durations.where(durations >= 0, durations + 2**32)
        overhead = df["protocol"].map(_wire_overhead)
        df["throughput_mbps"] = (
            df["packet_count"] * (df["payload_size"] + overhead) * 8
            / (durations.astype(float) / 1e6)
            / 1e6
        )
        df["duration_us"] = durations
        return df[df["throughput_mbps"].notna() & (df["duration_us"] > 0) & (df["packet_count"] > 0)]

    chunk_tp = compute_tp_mbps(chunk_df)
    agg_tp = compute_tp_mbps(agg_only_df)

    combined = pd.concat([chunk_tp, agg_tp], ignore_index=True) if not chunk_tp.empty or not agg_tp.empty else pd.DataFrame()
    if combined.empty:
        return

    sizes = sorted(combined["payload_size"].unique())
    combined["payload_label"] = combined["payload_size"].astype(str) + "B"
    label_order = [f"{s}B" for s in sizes]

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.boxplot(
        data=combined, x="payload_label", y="throughput_mbps",
        order=label_order, ax=ax, showfliers=True,
    )

    # Add median labels
    for i, size in enumerate(sizes):
        subset = combined[combined["payload_size"] == size]["throughput_mbps"]
        if not subset.empty:
            med = subset.median()
            ax.text(i, med, _fmt_throughput_label(med),
                    ha="center", va="bottom", fontsize=9, fontweight="bold")

    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Throughput (Mbps)")
    ax.set_title("Throughput Distribution by Payload Size (Flood Mode)")
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def plot_loss_bar(output_path: str | Path, experiment_id: int | None = None, protocol: str | None = None) -> None:
    """Packet loss % by payload size, averaged across runs. Only rtt + flood."""
    from .stats import compute_all_stats

    stats_df = compute_all_stats(experiment_id=experiment_id, protocol=protocol)
    stats_df = stats_df[stats_df["mode"].isin(["ping_pong", "flood"])]

    if stats_df.empty:
        return

    # Map mode names for display
    mode_labels = {"ping_pong": "rtt", "flood": "flood"}
    stats_df = stats_df.copy()
    stats_df["mode_label"] = stats_df["mode"].map(mode_labels)

    # Aggregate across runs: mean loss per (mode, payload_size)
    agg = stats_df.groupby(["mode_label", "payload_size"], as_index=False)["packet_loss_pct"].mean()
    agg = agg.sort_values(["mode_label", "payload_size"])

    fig, ax = plt.subplots(figsize=(10, 6))

    x_positions = range(len(agg))
    labels = [f"{int(row['payload_size'])}B\n({row['mode_label']})"
              for _, row in agg.iterrows()]
    values = agg["packet_loss_pct"].values

    palette = sns.color_palette()
    mode_colors = {m: palette[i] for i, m in enumerate(agg["mode_label"].unique())}
    colors = [mode_colors[row["mode_label"]] for _, row in agg.iterrows()]

    bars = ax.bar(x_positions, values, color=colors)
    ax.set_xticks(x_positions)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Packet Loss (%)")
    ax.set_title("Packet Loss by Payload Size and Mode")
    ax.set_ylim(bottom=0, top=max(values.max() * 1.2, 1.0))

    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                f"{val:.2f}%", ha="center", va="bottom", fontsize=9)

    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)


def plot_rssi_timeseries(output_path: str | Path, experiment_id: int | None = None, protocol: str | None = None) -> None:
    """RSSI over sequence number."""
    df = _load_packets(source="receiver", experiment_id=experiment_id, protocol=protocol)
    df = df[(df["lost"] == False) & (df["rssi"] != 0)]  # noqa: E712

    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    for (protocol, name, size), group in df.groupby(["protocol", "test_name", "payload_size"]):
        ax.plot(group["seq"], group["rssi"], ".", alpha=0.5, markersize=2,
                label=f"{protocol} {name} {size}B")

    ax.set_xlabel("Sequence Number")
    ax.set_ylabel("RSSI (dBm)")
    ax.set_title("RSSI Over Time")
    ax.legend(fontsize="small")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)
