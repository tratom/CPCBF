"""Statistical analysis of benchmark results."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats


@dataclass
class RunStats:
    """Computed statistics for a single test run."""

    run_id: int
    test_name: str
    mode: str
    payload_size: int
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


def compute_run_stats(conn: sqlite3.Connection, run_id: int) -> RunStats:
    """Compute summary statistics for a single test run."""
    # Get run metadata
    meta = conn.execute(
        "SELECT test_name, mode, payload_size, repetitions, protocol FROM test_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    test_name, mode, payload_size, repetitions, protocol = meta

    # For ping_pong, use sender packets (has RTT). For flood, use receiver packets.
    source = "sender" if mode == "ping_pong" else "receiver"
    df = pd.read_sql_query(
        """
        SELECT seq, tx_us, rx_us, rtt_us, rssi, crc_ok, lost
        FROM packets
        WHERE run_id = ? AND source = ? AND warmup = 0
        """,
        conn,
        params=(run_id, source),
    )

    total = len(df)
    lost_df = df[df["lost"] == 1]
    crc_err_df = df[df["crc_ok"] == 0]
    # For ping_pong, valid = not lost and has RTT. For flood, valid = not lost.
    if mode == "ping_pong":
        valid = df[(df["lost"] == 0) & (df["rtt_us"] > 0)]
    else:
        valid = df[df["lost"] == 0]

    packet_loss_pct = (len(lost_df) / total * 100) if total > 0 else 0.0
    crc_error_pct = (len(crc_err_df) / total * 100) if total > 0 else 0.0

    rtt_mean = rtt_median = rtt_std = rtt_p95 = rtt_p99 = None
    ci95_low = ci95_high = None
    jitter = None

    if len(valid) > 1 and mode == "ping_pong":
        rtts = valid["rtt_us"].values.astype(float)
        rtt_mean = float(np.mean(rtts))
        rtt_median = float(np.median(rtts))
        rtt_std = float(np.std(rtts, ddof=1))
        rtt_p95 = float(np.percentile(rtts, 95))
        rtt_p99 = float(np.percentile(rtts, 99))

        # 95% confidence interval
        ci = stats.t.interval(0.95, len(rtts) - 1, loc=rtt_mean, scale=stats.sem(rtts))
        ci95_low, ci95_high = float(ci[0]), float(ci[1])

        # Jitter: mean absolute difference of consecutive RTTs
        diffs = np.abs(np.diff(rtts))
        jitter = float(np.mean(diffs)) if len(diffs) > 0 else None

    # RSSI stats (from all non-warmup, non-lost packets)
    rssi_data = valid["rssi"].dropna()
    rssi_data = rssi_data[rssi_data != 0]
    rssi_mean = float(rssi_data.mean()) if len(rssi_data) > 0 else None
    rssi_std = float(rssi_data.std()) if len(rssi_data) > 1 else None

    # Throughput (flood mode)
    throughput = None
    if mode == "flood" and len(valid) > 1:
        rx_df = pd.read_sql_query(
            """
            SELECT rx_us FROM packets
            WHERE run_id = ? AND source = 'receiver' AND warmup = 0 AND lost = 0
            ORDER BY rx_us
            """,
            conn,
            params=(run_id,),
        )
        if len(rx_df) > 1:
            rx_times = rx_df["rx_us"].values.astype(float)
            duration_us = rx_times[-1] - rx_times[0]
            if duration_us > 0:
                # Wire bytes per packet depends on protocol:
                # WiFi (UDP): payload + 14B bench hdr/CRC + 8B UDP + 20B IP = payload + 42
                # BLE (L2CAP CoC): payload + 14B bench hdr/CRC (no IP/UDP layer)
                overhead = 14 if protocol == "ble" else 42
                wire_bytes = payload_size + overhead
                total_bits = len(rx_df) * wire_bytes * 8
                throughput = total_bits / (duration_us / 1e6)  # bits per second

    return RunStats(
        run_id=run_id,
        test_name=test_name,
        mode=mode,
        payload_size=payload_size,
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
    )


def compute_all_stats(db_path: str) -> pd.DataFrame:
    """Compute stats for all runs in the database."""
    conn = sqlite3.connect(db_path)
    run_ids = [
        row[0] for row in conn.execute("SELECT run_id FROM test_runs").fetchall()
    ]

    results = []
    for run_id in run_ids:
        s = compute_run_stats(conn, run_id)
        results.append(s.__dict__)

    conn.close()
    return pd.DataFrame(results)
