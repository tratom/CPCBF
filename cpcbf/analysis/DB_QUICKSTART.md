# PostgreSQL Ingest Pipeline

Ingests JSONL benchmark results into a centralized PostgreSQL database.

## Prerequisites

- PostgreSQL instance running (lab server: `10.10.7.93:5432`)
- Python package installed: `cd cpcbf && pip install -e .`

## Configuration

Set the required environment variables (`PGHOST`, `PGUSER`, `PGPASSWORD` are mandatory; `PGPORT` defaults to `5432`, `PGDATABASE` defaults to `cpcbf`):

```bash
export PGHOST=<host>
export PGPORT=5432
export PGDATABASE=cpcbf
export PGUSER=<user>
export PGPASSWORD='<password>'
```

Alternatively, set a single `DATABASE_URL`:

```bash
export DATABASE_URL='postgresql://<user>:<password>@<host>:5432/cpcbf'
```

## Usage

Run from the `cpcbf/` directory.

### Ingest a single JSONL file

```bash
python -m analysis.ingest field/results/garage-20m-center-lane/results.jsonl
```

The experiment name defaults to the parent directory (`garage-20m-center-lane`).

### Ingest a directory of JSONL files

```bash
python -m analysis.ingest field/results/garage-20m-center-lane/
```

Recursively finds all `*.jsonl` files in the directory.

### Specify a custom experiment name

```bash
python -m analysis.ingest field/results/garage-20m-center-lane/results.jsonl --experiment "wifi-garage-20m"
```

### Using the entry point (after `pip install -e .`)

```bash
cpcbf-ingest field/results/garage-20m-center-lane/results.jsonl
```

## Schema

Three tables are auto-created on first run:

| Table | Description |
|---|---|
| `experiments` | One row per field test (e.g. `garage-20m-center-lane`) |
| `test_runs` | One row per JSONL line (test config + metadata) |
| `packets` | Per-packet measurements (~2000 sender + ~2000 receiver per run) |

### Duplicate detection

Re-running ingest on the same file is safe. Duplicate runs are detected by the `(test_name, timestamp)` unique constraint and skipped.

### Marking invalid runs

Exclude faulty runs from analysis without deleting data:

```sql
UPDATE test_runs SET valid = FALSE WHERE run_id = 42;
```

## Querying

```bash
psql -h $PGHOST -U $PGUSER -d cpcbf
```

```sql
-- Row counts
SELECT count(*) FROM experiments;
SELECT count(*) FROM test_runs;
SELECT count(*) FROM packets;

-- Runs per experiment
SELECT e.name, count(r.run_id)
FROM experiments e
JOIN test_runs r USING (experiment_id)
WHERE r.valid = TRUE
GROUP BY e.name;

-- Average RTT per payload size for an experiment
SELECT r.payload_size, avg(p.rtt_us) AS avg_rtt_us
FROM test_runs r
JOIN packets p USING (run_id)
JOIN experiments e USING (experiment_id)
WHERE e.name = 'garage-20m-center-lane'
  AND r.valid = TRUE
  AND p.warmup = FALSE
  AND p.source = 'sender'
  AND p.rtt_us IS NOT NULL
GROUP BY r.payload_size
ORDER BY r.payload_size;
```
