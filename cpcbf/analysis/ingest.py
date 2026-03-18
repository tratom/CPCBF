"""Ingest JSONL results into PostgreSQL for analysis."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import psycopg2.extras

from .db import ensure_database, get_connection
from .metadata_cli import load_experiment_metadata

log = logging.getLogger(__name__)

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id   SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS test_runs (
    run_id          SERIAL PRIMARY KEY,
    experiment_id   INTEGER NOT NULL REFERENCES experiments(experiment_id),
    test_name       TEXT NOT NULL,
    mode            TEXT NOT NULL,
    protocol        TEXT NOT NULL,
    board           TEXT NOT NULL,
    payload_size    INTEGER NOT NULL,
    repetitions     INTEGER NOT NULL,
    warmup          INTEGER NOT NULL,
    topology        TEXT,
    clock_offset_us DOUBLE PRECISION,
    timestamp       DOUBLE PRECISION NOT NULL,
    valid           BOOLEAN NOT NULL DEFAULT TRUE,
    sender_start_us      BIGINT,
    sender_end_us        BIGINT,
    sender_packets_sent  INTEGER,
    receiver_start_us    BIGINT,
    receiver_end_us      BIGINT,
    receiver_packets_rcv INTEGER,
    receiver_crc_errors  INTEGER,
    aggregate_only       BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (test_name, timestamp)
);

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

-- Experiment metadata columns (nullable for backward compatibility)
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS test_date DATE;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS technology TEXT;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS distance_meters REAL;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS test_procedure TEXT;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS environment_description TEXT;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS test_config_json JSONB;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS interference_json JSONB;

-- Devices per experiment
CREATE TABLE IF NOT EXISTS experiment_devices (
    device_id     SERIAL PRIMARY KEY,
    experiment_id INTEGER NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    device_name   TEXT NOT NULL,
    hardware      TEXT,
    ip_address    TEXT,
    mac_address   TEXT,
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


def ingest_run(conn, experiment_id: int, result: dict) -> int | None:
    """Insert one test run from a JSONL line. Returns run_id or None if duplicate."""
    cur = conn.cursor()
    sender_data = result.get("sender", {})
    receiver_data = result.get("receiver", {})
    is_aggregate = bool(sender_data.get("aggregate_only") or receiver_data.get("aggregate_only"))

    cur.execute(
        """
        INSERT INTO test_runs
            (experiment_id, test_name, mode, protocol, board, payload_size,
             repetitions, warmup, topology, clock_offset_us, timestamp,
             sender_start_us, sender_end_us, sender_packets_sent,
             receiver_start_us, receiver_end_us, receiver_packets_rcv,
             receiver_crc_errors, aggregate_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (test_name, timestamp) DO NOTHING
        RETURNING run_id
        """,
        (
            experiment_id,
            result["test_name"],
            result["mode"],
            result["protocol"],
            result["board"],
            result["payload_size"],
            result["repetitions"],
            result["warmup"],
            result.get("topology"),
            result.get("clock_offset_us"),
            result["timestamp"],
            sender_data.get("start_us"),
            sender_data.get("end_us"),
            sender_data.get("packets_sent"),
            receiver_data.get("start_us"),
            receiver_data.get("end_us"),
            receiver_data.get("packets_received"),
            receiver_data.get("crc_errors"),
            is_aggregate,
        ),
    )
    row = cur.fetchone()
    if row is None:
        conn.commit()
        cur.close()
        return None
    run_id = row[0]

    for source in ("sender", "receiver"):
        side_data = result.get(source, {})
        packets = side_data.get("packets", [])
        if not packets:
            continue
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
        )

    conn.commit()
    cur.close()
    return run_id


def ingest_experiment_metadata(conn, experiment_id: int, metadata: dict) -> None:
    """Write experiment metadata into DB (UPDATE experiments + UPSERT children)."""
    cur = conn.cursor()

    test_config = metadata.get("test_configuration", {})

    cur.execute(
        """
        UPDATE experiments SET
            location = %s,
            test_date = %s,
            technology = %s,
            distance_meters = %s,
            test_procedure = %s,
            environment_description = %s,
            test_config_json = %s,
            interference_json = %s
        WHERE experiment_id = %s
        """,
        (
            metadata.get("location"),
            metadata.get("test_date"),
            test_config.get("technology"),
            test_config.get("distance_meters"),
            metadata.get("test_procedure_description"),
            metadata.get("environmental_description"),
            json.dumps(test_config) if test_config else None,
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
                (experiment_id, role, device_name, hardware, ip_address, mac_address, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (experiment_id, role, device_name) DO UPDATE SET
                hardware   = EXCLUDED.hardware,
                ip_address = EXCLUDED.ip_address,
                mac_address = EXCLUDED.mac_address,
                notes      = EXCLUDED.notes
            """,
            (
                experiment_id,
                dev.get("role", ""),
                dev.get("device_id", ""),
                dev.get("hardware"),
                dev.get("ip_address"),
                dev.get("mac_address"),
                dev.get("notes"),
            ),
        )

    for media in metadata.get("media_files", []):
        cur.execute(
            """
            INSERT INTO experiment_media
                (experiment_id, file_name, description, relative_path)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (experiment_id, file_name) DO UPDATE SET
                description   = EXCLUDED.description,
                relative_path = EXCLUDED.relative_path
            """,
            (
                experiment_id,
                media.get("file_name", ""),
                media.get("description"),
                media.get("path", ""),
            ),
        )

    conn.commit()
    cur.close()


def ingest_jsonl(jsonl_path: str | Path, experiment_name: str | None = None) -> int:
    """Ingest all results from a JSONL file into PostgreSQL.

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
            ingest_experiment_metadata(conn, experiment_id, metadata)
            log.info("Ingested experiment metadata for %s", experiment_name)
    except Exception as e:
        log.warning("Skipping experiment metadata: %s", e)

    count = 0
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            result = json.loads(line)
            run_id = ingest_run(conn, experiment_id, result)
            if run_id is not None:
                count += 1

    conn.close()
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Ingest JSONL benchmark results into PostgreSQL"
    )
    parser.add_argument(
        "path",
        type=Path,
        help="JSONL file or directory containing JSONL files",
    )
    parser.add_argument(
        "--experiment",
        type=str,
        default=None,
        help="Experiment name (default: parent directory name)",
    )
    args = parser.parse_args()

    path = args.path
    if path.is_dir():
        files = sorted(path.glob("**/*.jsonl"))
    else:
        files = [path]

    total = 0
    for f in files:
        n = ingest_jsonl(f, experiment_name=args.experiment)
        print(f"{f}: {n} new runs ingested")
        total += n

    print(f"Total: {total} new runs ingested")


if __name__ == "__main__":
    main()
