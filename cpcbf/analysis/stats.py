"""Statistical analysis of benchmark results from PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .db import get_connection


@dataclass
class RunStats:
    """Computed statistics for a single test run."""

    run_id: int
    test_name: str
    mode: str
    protocol: str
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


def _compute_flood_throughput(protocol: str, payload_size: int,
                              packets_received: int, duration_us: float) -> float | None:
    """Compute throughput in bits/sec from aggregate flood data."""
    if duration_us <= 0 or packets_received <= 0:
        return None
    overhead = 14 if protocol == "ble" else 42
    wire_bytes = payload_size + overhead
    total_bits = packets_received * wire_bytes * 8
    return total_bits / (duration_us / 1e6)


def compute_run_stats(conn, run_id: int) -> RunStats:
    """Compute summary statistics for a single test run."""
    cur = conn.cursor()
    cur.execute(
        """SELECT r.test_name, r.mode, r.payload_size, r.repetitions, r.protocol,
                  f.sender_measured_count, f.sender_lost, f.sender_crc_errors,
                  f.receiver_measured_count, f.receiver_lost, f.receiver_crc_errors,
                  f.receiver_start_us, f.receiver_end_us
           FROM test_runs r
           LEFT JOIN flood_runs f ON r.run_id = f.run_id
           WHERE r.run_id = %s""",
        (run_id,),
    )
    meta = cur.fetchone()
    (test_name, mode, payload_size, repetitions, protocol,
     sender_measured, sender_lost, sender_crc_errors,
     receiver_measured, receiver_lost, receiver_crc_errors,
     rx_start_us, rx_end_us) = meta
    cur.close()

    # Flood mode: always aggregate, no per-packet data
    if mode == "flood":
        packets_sent = sender_measured or 0
        packets_rcv = receiver_measured or 0
        r_crc_errs = receiver_crc_errors or 0
        packet_loss_pct = ((packets_sent - packets_rcv) / packets_sent * 100) if packets_sent > 0 else 0.0
        crc_error_pct = (r_crc_errs / packets_rcv * 100) if packets_rcv > 0 else 0.0

        duration_us = (rx_end_us - rx_start_us) if (rx_start_us and rx_end_us) else 0
        throughput = _compute_flood_throughput(protocol, payload_size, packets_rcv, duration_us)

        return RunStats(
            run_id=run_id,
            test_name=test_name,
            mode=mode,
            protocol=protocol,
            payload_size=payload_size,
            packets_measured=packets_rcv,
            rtt_mean_us=None, rtt_median_us=None, rtt_std_us=None,
            rtt_p95_us=None, rtt_p99_us=None,
            rtt_ci95_low=None, rtt_ci95_high=None,
            packet_loss_pct=packet_loss_pct,
            crc_error_pct=crc_error_pct,
            jitter_us=None, rssi_mean=None, rssi_std=None,
            throughput_bps=throughput,
        )

    # Per-packet analysis: ping_pong -> sender, rssi -> receiver
    source = "sender" if mode == "ping_pong" else "receiver"
    df = pd.read_sql_query(
        """
        SELECT seq, tx_us, rx_us, rtt_us, rssi, crc_ok, lost
        FROM packets
        WHERE run_id = %s AND source = %s AND warmup = FALSE
        """,
        conn,
        params=(run_id, source),
    )

    total = len(df)
    lost_df = df[df["lost"] == True]  # noqa: E712
    crc_err_df = df[df["crc_ok"] == False]  # noqa: E712

    if mode == "ping_pong":
        valid = df[(df["lost"] == False) & (df["rtt_us"] > 0)]  # noqa: E712
    else:
        valid = df[df["lost"] == False]  # noqa: E712

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

        ci = stats.t.interval(0.95, len(rtts) - 1, loc=rtt_mean, scale=stats.sem(rtts))
        ci95_low, ci95_high = float(ci[0]), float(ci[1])

        diffs = np.abs(np.diff(rtts))
        jitter = float(np.mean(diffs)) if len(diffs) > 0 else None

    # RSSI stats (from all non-warmup, non-lost packets)
    rssi_data = valid["rssi"].dropna()
    rssi_data = rssi_data[rssi_data != 0]
    rssi_mean = float(rssi_data.mean()) if len(rssi_data) > 0 else None
    rssi_std = float(rssi_data.std()) if len(rssi_data) > 1 else None

    return RunStats(
        run_id=run_id,
        test_name=test_name,
        mode=mode,
        protocol=protocol,
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
        throughput_bps=None,
    )


def compute_all_stats(
    experiment_id: int | None = None,
    protocol: str | None = None,
) -> pd.DataFrame:
    """Compute stats for all valid runs in the database.

    When *experiment_id* is provided, only runs belonging to that experiment
    are included.  When *protocol* is provided, only runs matching that
    protocol are included.
    """
    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT run_id FROM test_runs WHERE valid = TRUE"
    params: list = []
    if experiment_id is not None:
        query += " AND experiment_id = %s"
        params.append(experiment_id)
    if protocol is not None:
        query += " AND protocol = %s"
        params.append(protocol)
    cur.execute(query, params)
    run_ids = [row[0] for row in cur.fetchall()]
    cur.close()

    results = []
    for run_id in run_ids:
        s = compute_run_stats(conn, run_id)
        results.append(s.__dict__)

    conn.close()
    return pd.DataFrame(results)
