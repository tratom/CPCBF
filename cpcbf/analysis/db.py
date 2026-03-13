"""PostgreSQL connection factory for CPCBF analysis."""

from __future__ import annotations

import os

import psycopg2


def get_connection(dbname: str | None = None):
    """Return a psycopg2 connection using env vars or DATABASE_URL."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=dbname or os.environ.get("PGDATABASE", "cpcbf"),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


def ensure_database():
    """Create the cpcbf database if it doesn't exist."""
    dbname = os.environ.get("PGDATABASE", "cpcbf")
    conn = get_connection(dbname="postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    if not cur.fetchone():
        cur.execute(f'CREATE DATABASE "{dbname}"')
    cur.close()
    conn.close()
