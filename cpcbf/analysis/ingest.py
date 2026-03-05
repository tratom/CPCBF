"""Ingest JSONL results into SQLite for analysis."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the SQLite database and tables."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS test_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_name TEXT NOT NULL,
            mode TEXT NOT NULL,
            protocol TEXT NOT NULL,
            board TEXT NOT NULL,
            payload_size INTEGER NOT NULL,
            repetitions INTEGER NOT NULL,
            warmup INTEGER NOT NULL,
            topology TEXT,
            clock_offset_us REAL,
            timestamp REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES test_runs(run_id),
            source TEXT NOT NULL,  -- 'sender' or 'receiver'
            seq INTEGER NOT NULL,
            tx_us INTEGER,
            rx_us INTEGER,
            rtt_us INTEGER,
            rssi INTEGER,
            crc_ok INTEGER,
            lost INTEGER,
            warmup INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_packets_run_id ON packets(run_id);
        CREATE INDEX IF NOT EXISTS idx_packets_warmup ON packets(run_id, warmup);
        """
    )
    conn.commit()
    return conn


def ingest_run(conn: sqlite3.Connection, result: dict) -> int:
    """Insert one test run (from JSONL line) into the database.

    Returns the run_id.
    """
    cur = conn.execute(
        """
        INSERT INTO test_runs
            (test_name, mode, protocol, board, payload_size,
             repetitions, warmup, topology, clock_offset_us, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
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
    run_id = cur.lastrowid

    # Insert packet records from both sender and receiver
    for source in ("sender", "receiver"):
        side_data = result.get(source, {})
        packets = side_data.get("packets", [])
        rows = [
            (
                run_id,
                source,
                p.get("seq", 0),
                p.get("tx_us"),
                p.get("rx_us"),
                p.get("rtt_us"),
                p.get("rssi"),
                p.get("crc_ok"),
                p.get("lost"),
                p.get("warmup", 0),
            )
            for p in packets
        ]
        conn.executemany(
            """
            INSERT INTO packets
                (run_id, source, seq, tx_us, rx_us, rtt_us, rssi,
                 crc_ok, lost, warmup)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    conn.commit()
    return run_id


def ingest_jsonl(jsonl_path: str | Path, db_path: str | Path) -> int:
    """Ingest all results from a JSONL file into a SQLite database.

    Returns the number of runs ingested.
    """
    conn = init_db(db_path)
    count = 0
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            result = json.loads(line)
            ingest_run(conn, result)
            count += 1
    conn.close()
    return count
