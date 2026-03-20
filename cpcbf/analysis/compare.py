"""Cross-run comparison tables."""

from __future__ import annotations

import pandas as pd

from .stats import compute_all_stats


def comparison_table(experiment_id: int | None = None, protocol: str | None = None) -> pd.DataFrame:
    """Build a comparison DataFrame with key metrics across all runs.

    Columns: protocol, test_name, mode, payload_size, rtt_median_us, rtt_p95_us,
             packet_loss_pct, jitter_us, throughput_bps, rssi_mean
    """
    stats_df = compute_all_stats(experiment_id=experiment_id, protocol=protocol)

    if stats_df.empty:
        return stats_df

    columns = [
        "protocol",
        "test_name",
        "mode",
        "payload_size",
        "packets_measured",
        "rtt_median_us",
        "rtt_p95_us",
        "rtt_ci95_low",
        "rtt_ci95_high",
        "packet_loss_pct",
        "crc_error_pct",
        "jitter_us",
        "throughput_bps",
        "rssi_mean",
    ]
    available = [c for c in columns if c in stats_df.columns]
    return stats_df[available].sort_values(
        ["protocol", "test_name", "payload_size"]
    ).reset_index(drop=True)
