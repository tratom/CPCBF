"""Ingest JSONL results into PostgreSQL for analysis."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
from pathlib import Path

import psycopg2
import psycopg2.extras

from .db import ensure_database, get_connection
from .metadata_cli import load_experiment_metadata

log = logging.getLogger(__name__)

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id             SERIAL PRIMARY KEY,
    name                      TEXT NOT NULL UNIQUE,
    description               TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    location                  TEXT,
    test_date                 DATE,
    distance_meters           REAL,
    duration_minutes          INTEGER,
    test_procedure            TEXT,
    environment_description   TEXT,
    interference_json         JSONB
);

CREATE TABLE IF NOT EXISTS test_runs (
    run_id          SERIAL PRIMARY KEY,
    experiment_id   INTEGER NOT NULL REFERENCES experiments(experiment_id),
    test_name       TEXT NOT NULL,
    test_idx        INTEGER,
    mode            TEXT NOT NULL,
    protocol        TEXT NOT NULL,
    board           TEXT NOT NULL,
    payload_size    INTEGER NOT NULL,
    repetitions     INTEGER NOT NULL,
    warmup          INTEGER NOT NULL,
    topology        TEXT,
    early_aborted   BOOLEAN NOT NULL DEFAULT FALSE,
    clock_offset_us DOUBLE PRECISION,
    timestamp       DOUBLE PRECISION NOT NULL,
    valid           BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (test_name, timestamp)
);

CREATE TABLE IF NOT EXISTS flood_runs (
    run_id                  INTEGER PRIMARY KEY REFERENCES test_runs(run_id) ON DELETE CASCADE,
    sender_warmup_count     INTEGER,
    sender_measured_count   INTEGER,
    sender_start_us         BIGINT,
    sender_end_us           BIGINT,
    sender_lost             INTEGER,
    sender_crc_errors       INTEGER,
    receiver_warmup_count   INTEGER,
    receiver_measured_count INTEGER,
    receiver_start_us       BIGINT,
    receiver_end_us         BIGINT,
    receiver_lost           INTEGER,
    receiver_crc_errors     INTEGER
);

-- Per-packet data: ONLY for RTT (sender) and RSSI (receiver)
CREATE TABLE IF NOT EXISTS packets (
    id       BIGSERIAL PRIMARY KEY,
    run_id   INTEGER NOT NULL REFERENCES test_runs(run_id),
    source   TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    tx_us    BIGINT,
    rx_us    BIGINT,
    rtt_us   BIGINT,
    rssi     SMALLINT,
    crc_ok   BOOLEAN,
    lost     BOOLEAN,
    warmup   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_packets_run_id ON packets(run_id);
CREATE INDEX IF NOT EXISTS idx_packets_run_source_warmup ON packets(run_id, source, warmup);
CREATE INDEX IF NOT EXISTS idx_test_runs_experiment ON test_runs(experiment_id);
CREATE INDEX IF NOT EXISTS idx_test_runs_valid ON test_runs(valid) WHERE valid = TRUE;

-- Devices per experiment
CREATE TABLE IF NOT EXISTS experiment_devices (
    device_id     SERIAL PRIMARY KEY,
    experiment_id INTEGER NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    device_name   TEXT NOT NULL,
    hardware      TEXT,
    local_ip      TEXT,
    peer_addr     TEXT,
    peer_mac      TEXT,
    ble_mac_peer  TEXT,
    notes         TEXT,
    UNIQUE (experiment_id, role, device_name)
);

-- Media files per experiment
CREATE TABLE IF NOT EXISTS experiment_media (
    media_id      SERIAL PRIMARY KEY,
    experiment_id INTEGER NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    file_name     TEXT NOT NULL,
    description   TEXT,
    relative_path TEXT NOT NULL,
    mime_type     TEXT,
    data          BYTEA,
    UNIQUE (experiment_id, file_name)
);

CREATE INDEX IF NOT EXISTS idx_experiment_devices_exp ON experiment_devices(experiment_id);
CREATE INDEX IF NOT EXISTS idx_experiment_media_exp ON experiment_media(experiment_id);
"""


def init_db(conn):
    """Create tables and indexes if they don't exist."""
    cur = conn.cursor()
    cur.execute(SCHEMA_DDL)
    conn.commit()
    cur.close()


def resolve_experiment(conn, name: str) -> int:
    """Get or create an experiment, returning experiment_id."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO experiments (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    conn.commit()
    cur.execute("SELECT experiment_id FROM experiments WHERE name = %s", (name,))
    experiment_id = cur.fetchone()[0]
    cur.close()
    return experiment_id


def _compute_side_aggregates(side_data: dict) -> dict:
    """Compute warmup/measured counts, time span, lost, CRC from packets."""
    packets = side_data.get("packets", [])
    warmup_pkts = [p for p in packets if p.get("warmup")]
    measured_pkts = [p for p in packets if not p.get("warmup")]

    # Time span from measured packets
    role = side_data.get("role", "sender")
    ts_key = "rx_us" if role == "receiver" else "tx_us"
    times = [p[ts_key] for p in measured_pkts if p.get(ts_key, 0) > 0]

    return {
        "warmup_count": len(warmup_pkts),
        "measured_count": len(measured_pkts),
        "start_us": times[0] if times else None,
        "end_us": times[-1] if times else None,
        "lost": sum(1 for p in measured_pkts if p.get("lost")),
        "crc_errors": sum(1 for p in measured_pkts if not p.get("crc_ok")),
    }


def ingest_run(conn, experiment_id: int, result: dict) -> int | None:
    """Insert one test run. Returns run_id or None if duplicate.

    The result dict must have top-level metadata and optionally:
    - sender_agg / receiver_agg: pre-computed aggregates
    - packets: list of packet dicts to insert (mode-filtered by caller)
    - packets_source: 'sender' or 'receiver' (which side the packets came from)
    """
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO test_runs
            (experiment_id, test_name, test_idx, mode, protocol, board,
             payload_size, repetitions, warmup, topology, early_aborted,
             clock_offset_us, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (test_name, timestamp) DO NOTHING
        RETURNING run_id
        """,
        (
            experiment_id,
            result["test_name"],
            result.get("test_idx"),
            result["mode"],
            result["protocol"],
            result["board"],
            result["payload_size"],
            result["repetitions"],
            result["warmup"],
            result.get("topology"),
            bool(result.get("early_aborted", 0)),
            result.get("clock_offset_us"),
            result["timestamp"],
        ),
    )
    row = cur.fetchone()
    if row is None:
        conn.commit()
        cur.close()
        return None
    run_id = row[0]

    # Flood mode: store aggregates in separate table
    if result["mode"] == "flood":
        sender_agg = result.get("sender_agg", {})
        receiver_agg = result.get("receiver_agg", {})
        cur.execute(
            """
            INSERT INTO flood_runs
                (run_id,
                 sender_warmup_count, sender_measured_count,
                 sender_start_us, sender_end_us, sender_lost, sender_crc_errors,
                 receiver_warmup_count, receiver_measured_count,
                 receiver_start_us, receiver_end_us, receiver_lost, receiver_crc_errors)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                sender_agg.get("warmup_count"),
                sender_agg.get("measured_count"),
                sender_agg.get("start_us"),
                sender_agg.get("end_us"),
                sender_agg.get("lost"),
                sender_agg.get("crc_errors"),
                receiver_agg.get("warmup_count"),
                receiver_agg.get("measured_count"),
                receiver_agg.get("start_us"),
                receiver_agg.get("end_us"),
                receiver_agg.get("lost"),
                receiver_agg.get("crc_errors"),
            ),
        )

    # Insert per-packet data (already mode-filtered by caller)
    packets = result.get("packets", [])
    source = result.get("packets_source")
    if packets and source:
        rows = [
            (
                run_id,
                source,
                p.get("seq", 0),
                p.get("tx_us"),
                p.get("rx_us"),
                p.get("rtt_us"),
                p.get("rssi"),
                bool(p["crc_ok"]) if p.get("crc_ok") is not None else None,
                bool(p["lost"]) if p.get("lost") is not None else None,
                bool(p.get("warmup", False)),
            )
            for p in packets
        ]
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO packets
                (run_id, source, seq, tx_us, rx_us, rtt_us, rssi,
                 crc_ok, lost, warmup)
            VALUES %s
            """,
            rows,
            page_size=5000,
        )

    conn.commit()
    cur.close()
    return run_id


def _merge_receiver_rssi(sender_packets: list[dict], receiver_packets: list[dict]) -> None:
    """Merge receiver RSSI values into sender packets by seq number (in-place)."""
    rssi_by_seq = {}
    for p in receiver_packets:
        rssi = p.get("rssi", 0)
        if rssi != 0:
            rssi_by_seq[p["seq"]] = rssi
    for p in sender_packets:
        if p.get("rssi", 0) == 0 and p["seq"] in rssi_by_seq:
            p["rssi"] = rssi_by_seq[p["seq"]]


def ingest_directory(dir_path: Path, experiment_name: str | None = None) -> int:
    """Ingest a flat directory of auto_bench sender/receiver JSONL files.

    Groups files by test name, computes aggregates, and stores only the
    relevant per-packet data per mode:
    - ping_pong: sender packets only (has rtt_us)
    - rssi: receiver packets only (has rssi values)
    - flood: no packets (aggregates only)

    Returns the number of new runs ingested.
    """
    dir_path = Path(dir_path).resolve()
    if experiment_name is None:
        experiment_name = dir_path.name

    ensure_database()
    conn = get_connection()
    init_db(conn)
    experiment_id = resolve_experiment(conn, experiment_name)

    # Auto-load experiment metadata if present
    try:
        metadata = load_experiment_metadata(dir_path)
        if metadata is not None:
            ingest_experiment_metadata(conn, experiment_id, metadata, base_dir=dir_path)
            log.info("Ingested experiment metadata for %s", experiment_name)
    except Exception as e:
        log.warning("Skipping experiment metadata: %s", e)

    # Group JSONL files by test name
    groups: dict[str, dict[str, Path]] = {}
    for f in dir_path.glob("*.jsonl"):
        match = re.match(r"(.+)_(sender|receiver)\.jsonl$", f.name)
        if not match:
            continue
        test_key = match.group(1)
        role = match.group(2)
        groups.setdefault(test_key, {})[role] = f

    count = 0
    for test_key in sorted(groups):
        pair = groups[test_key]

        # Load both sides
        sides: dict[str, dict] = {}
        meta: dict = {}
        for role in ("sender", "receiver"):
            if role not in pair:
                continue
            with open(pair[role]) as fh:
                data = json.loads(fh.readline())
            sides[role] = data.pop(role, {})
            if not meta:
                meta = data

        mode = meta.get("mode", "ping_pong")
        sender_data = sides.get("sender", {})
        receiver_data = sides.get("receiver", {})

        # Compute aggregates only for flood (RTT/RSSI have all packets stored)
        if mode == "flood":
            sender_agg = _compute_side_aggregates(sender_data) if sender_data else {}
            receiver_agg = _compute_side_aggregates(receiver_data) if receiver_data else {}
        else:
            sender_agg = {}
            receiver_agg = {}

        # Mode-aware packet selection
        packets = []
        packets_source = None
        if mode == "ping_pong":
            # Merge receiver RSSI into sender packets for ping_pong
            if receiver_data:
                _merge_receiver_rssi(
                    sender_data.get("packets", []),
                    receiver_data.get("packets", []),
                )
            packets = sender_data.get("packets", [])
            packets_source = "sender"
        elif mode == "rssi":
            packets = receiver_data.get("packets", [])
            packets_source = "receiver"
        # flood: no packets stored

        # Build the combined record
        combined = {
            "test_name": meta.get("test_name", test_key),
            "test_idx": meta.get("test_idx"),
            "mode": mode,
            "protocol": meta.get("protocol", "wifi"),
            "board": meta.get("board", "rpi4"),
            "payload_size": meta.get("payload_size", 0),
            "repetitions": meta.get("repetitions", 0),
            "warmup": meta.get("warmup", 0),
            "topology": meta.get("topology"),
            "early_aborted": meta.get("early_aborted", 0),
            "clock_offset_us": meta.get("clock_offset_us"),
            "timestamp": meta.get("timestamp", 0),
            "sender_agg": sender_agg,
            "receiver_agg": receiver_agg,
            "packets": packets,
            "packets_source": packets_source,
        }

        run_id = ingest_run(conn, experiment_id, combined)
        if run_id is not None:
            count += 1
            present = "+".join(sorted(pair.keys()))
            log.info("  %s (%s) -> run_id=%d", test_key, present, run_id)

    conn.close()
    return count


def ingest_jsonl(jsonl_path: str | Path, experiment_name: str | None = None) -> int:
    """Ingest all results from a merged JSONL file into PostgreSQL.

    Returns the number of new runs ingested.
    """
    jsonl_path = Path(jsonl_path)
    if experiment_name is None:
        experiment_name = jsonl_path.parent.name

    ensure_database()
    conn = get_connection()
    init_db(conn)
    experiment_id = resolve_experiment(conn, experiment_name)

    # Auto-load experiment metadata if present
    try:
        metadata = load_experiment_metadata(jsonl_path.parent)
        if metadata is not None:
            ingest_experiment_metadata(conn, experiment_id, metadata, base_dir=jsonl_path.parent)
            log.info("Ingested experiment metadata for %s", experiment_name)
    except Exception as e:
        log.warning("Skipping experiment metadata: %s", e)

    count = 0
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)

            # Convert merged JSONL format to ingest_run format
            mode = raw.get("mode", "ping_pong")
            sender_data = raw.get("sender", {})
            receiver_data = raw.get("receiver", {})

            if mode == "flood":
                sender_agg = _compute_side_aggregates(sender_data) if sender_data else {}
                receiver_agg = _compute_side_aggregates(receiver_data) if receiver_data else {}
            else:
                sender_agg = {}
                receiver_agg = {}

            packets = []
            packets_source = None
            if mode == "ping_pong":
                packets = sender_data.get("packets", [])
                packets_source = "sender"
            elif mode == "rssi":
                packets = receiver_data.get("packets", [])
                packets_source = "receiver"

            result = {
                "test_name": raw["test_name"],
                "test_idx": raw.get("test_idx"),
                "mode": mode,
                "protocol": raw["protocol"],
                "board": raw["board"],
                "payload_size": raw["payload_size"],
                "repetitions": raw["repetitions"],
                "warmup": raw["warmup"],
                "topology": raw.get("topology"),
                "early_aborted": raw.get("early_aborted", 0),
                "clock_offset_us": raw.get("clock_offset_us"),
                "timestamp": raw["timestamp"],
                "sender_agg": sender_agg,
                "receiver_agg": receiver_agg,
                "packets": packets,
                "packets_source": packets_source,
            }

            run_id = ingest_run(conn, experiment_id, result)
            if run_id is not None:
                count += 1

    conn.close()
    return count


def ingest_experiment_metadata(
    conn, experiment_id: int, metadata: dict, *, base_dir: Path | None = None,
) -> None:
    """Write experiment metadata into DB (UPDATE experiments + UPSERT children)."""
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE experiments SET
            location = %s,
            test_date = %s,
            distance_meters = %s,
            duration_minutes = %s,
            test_procedure = %s,
            environment_description = %s,
            interference_json = %s
        WHERE experiment_id = %s
        """,
        (
            metadata.get("location"),
            metadata.get("test_date"),
            metadata.get("distance_meters"),
            metadata.get("duration_minutes"),
            metadata.get("test_procedure_description"),
            metadata.get("environmental_description"),
            json.dumps(metadata["dynamic_interference"])
            if metadata.get("dynamic_interference")
            else None,
            experiment_id,
        ),
    )

    for dev in metadata.get("devices", []):
        cur.execute(
            """
            INSERT INTO experiment_devices
                (experiment_id, role, device_name, hardware,
                 local_ip, peer_addr, peer_mac, ble_mac_peer, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (experiment_id, role, device_name) DO UPDATE SET
                hardware     = EXCLUDED.hardware,
                local_ip     = EXCLUDED.local_ip,
                peer_addr    = EXCLUDED.peer_addr,
                peer_mac     = EXCLUDED.peer_mac,
                ble_mac_peer = EXCLUDED.ble_mac_peer,
                notes        = EXCLUDED.notes
            """,
            (
                experiment_id,
                dev.get("role", ""),
                dev.get("device_id", ""),
                dev.get("hardware"),
                dev.get("local_ip"),
                dev.get("peer_addr"),
                dev.get("peer_mac"),
                dev.get("ble_mac_peer"),
                dev.get("notes"),
            ),
        )

    for media in metadata.get("media_files", []):
        rel_path = media.get("path", "")
        file_data = None
        mime_type = None
        if base_dir and rel_path:
            abs_path = base_dir / rel_path
            if abs_path.is_file():
                file_data = psycopg2.Binary(abs_path.read_bytes())
                mime_type = mimetypes.guess_type(abs_path.name)[0]

        cur.execute(
            """
            INSERT INTO experiment_media
                (experiment_id, file_name, description, relative_path, mime_type, data)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (experiment_id, file_name) DO UPDATE SET
                description   = EXCLUDED.description,
                relative_path = EXCLUDED.relative_path,
                mime_type     = EXCLUDED.mime_type,
                data          = EXCLUDED.data
            """,
            (
                experiment_id,
                media.get("file_name", ""),
                media.get("description"),
                rel_path,
                mime_type,
                file_data,
            ),
        )

    conn.commit()
    cur.close()


def main():
    parser = argparse.ArgumentParser(
        description="Ingest JSONL benchmark results into PostgreSQL"
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Directory of sender/receiver JSONL files, or a merged JSONL file",
    )
    parser.add_argument(
        "--experiment",
        type=str,
        default=None,
        help="Experiment name (default: directory name)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    path = args.path
    if path.is_dir():
        n = ingest_directory(path, experiment_name=args.experiment)
        print(f"Ingested {n} new runs from {path}")
    elif path.is_file() and path.suffix == ".jsonl":
        n = ingest_jsonl(path, experiment_name=args.experiment)
        print(f"Ingested {n} new runs from {path}")
    else:
        print(f"Error: {path} is not a directory or JSONL file")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
