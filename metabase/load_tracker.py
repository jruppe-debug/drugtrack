"""Copy Clinical Tracker SQLite tables into the local AACT Postgres.

Each SQLite file becomes one Postgres schema (clinical_tracker.db -> tracker,
source_index.db -> source_index), so Metabase can join tracker rows to
ctgov.* on nct_id. Tables are dropped and reloaded on every run; ctgov is
never touched.

Env overrides: TRACKER_DIR, PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD.
"""
from __future__ import annotations

import io
import csv
import os
import sqlite3
from pathlib import Path

import psycopg2

TRACKER_DIR = Path(os.environ.get(
    "TRACKER_DIR", Path(__file__).resolve().parents[1] / "AACT_bridge" / "inputs"))
SOURCES = {"tracker": "clinical_tracker.db", "source_index": "source_index.db"}

# SQLite declared type -> Postgres type. Unknown or empty types load as text.
PG_TYPE = {"INTEGER": "bigint", "INT": "bigint", "REAL": "double precision",
           "FLOAT": "double precision", "NUMERIC": "numeric", "BOOLEAN": "boolean"}


def pg_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "aact"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", "aact_admin"),
    )


def load_db(pg, schema: str, sqlite_path: Path) -> None:
    src = sqlite3.connect(sqlite_path)
    with pg.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for t in tables:
            info = list(src.execute(f'PRAGMA table_info("{t}")'))
            cols = [r[1] for r in info]
            ddl = ", ".join(
                f'"{name}" {PG_TYPE.get((decl or "").upper(), "text")}'
                for _, name, decl, *_ in info)
            cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{t}"')
            cur.execute(f'CREATE TABLE "{schema}"."{t}" ({ddl})')

            buf = io.StringIO()
            w = csv.writer(buf)
            for row in src.execute(f'SELECT * FROM "{t}"'):
                # Postgres text cannot hold NUL; SQLite can.
                w.writerow(["\\N" if v is None
                            else v.replace("\x00", "") if isinstance(v, str)
                            else v for v in row])
            buf.seek(0)
            col_list = ", ".join(f'"{c}"' for c in cols)
            cur.copy_expert(
                f'COPY "{schema}"."{t}" ({col_list}) FROM STDIN '
                "WITH (FORMAT csv, NULL '\\N')", buf)
            if "nct_id" in cols:
                cur.execute(f'CREATE INDEX ON "{schema}"."{t}" (nct_id)')
            cur.execute(f'SELECT count(*) FROM "{schema}"."{t}"')
            print(f"{schema}.{t}: {cur.fetchone()[0]} rows")
    pg.commit()
    src.close()


def main() -> None:
    pg = pg_conn()
    for schema, fname in SOURCES.items():
        load_db(pg, schema, TRACKER_DIR / fname)
    pg.close()


if __name__ == "__main__":
    main()
