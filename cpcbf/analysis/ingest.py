"""Ingest JSONL results into PostgreSQL for analysis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg2.extras

from .db import ensure_database, get_connection

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
    cur.execute(
        """
        INSERT INTO test_runs
            (experiment_id, test_name, mode, protocol, board, payload_size,
             repetitions, warmup, topology, clock_offset_us, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
