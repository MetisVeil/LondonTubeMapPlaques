"""Point-in-time (SCD type 2) storage for the TfL station data.

Each table keeps the full history of every row. A row version records the load
it first appeared in (`valid_from_load`) and the load that replaced or removed
it (`valid_to_load`, which stays NULL while the version is the live one), so
re-ingesting an unchanged CSV writes nothing at all.

Every table also gets a `current_<table>` view showing only the live versions.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "london.db"
SQL_DIR = Path(__file__).resolve().parent / "sql"


def _cols(names: list[str]) -> str:
    """Comma-separate column names, quoted so keywords like "From" stay safe."""
    return ", ".join(f'"{n}"' for n in names)


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the `loads` table on first use."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loads (
            load_id      INTEGER PRIMARY KEY,
            source       TEXT NOT NULL,
            feed_version TEXT,
            loaded_at    TEXT NOT NULL
        )
    """)
    return conn


def start_load(conn: sqlite3.Connection, source: str, feed_version: str | None = None) -> int:
    """Record an ingestion run and return its load_id."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return conn.execute(
        "INSERT INTO loads (source, feed_version, loaded_at) VALUES (?, ?, ?)",
        (source, feed_version, now),
    ).lastrowid


def ingest(conn: sqlite3.Connection, df: pl.DataFrame, table: str,
           key_cols: list[str], attr_cols: list[str], load_id: int) -> dict[str, int]:
    """Store `df` in `table`, writing new versions only for rows that changed.

    The comparison is one SQL EXCEPT in each direction, which compares whole
    rows and counts two NULLs as equal:
      * a CSV row that is not already live -> inserted as a new version
      * a live row that is not in the CSV  -> closed off (it changed, or it is gone)
    """
    cols, keys = _cols(key_cols + attr_cols), _cols(key_cols)
    typed = ", ".join(f'"{c}" TEXT' for c in key_cols + attr_cols)
    df = df.select(key_cols + attr_cols).cast(pl.Utf8)

    if df.select(key_cols).null_count().sum_horizontal().item():
        raise ValueError(f"{table}: {key_cols} contains blanks, so those rows have no identity to track")

    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS "{table}" (
            row_id INTEGER PRIMARY KEY,
            {typed},
            valid_from_load INTEGER NOT NULL REFERENCES loads(load_id),
            valid_to_load   INTEGER REFERENCES loads(load_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS "{table}_live_key"
            ON "{table}" ({keys}) WHERE valid_to_load IS NULL;
        CREATE VIEW IF NOT EXISTS "current_{table}" AS
            SELECT * FROM "{table}" WHERE valid_to_load IS NULL;
    """)

    conn.execute(f'CREATE TEMP TABLE "new_{table}" ({typed})')
    conn.executemany(
        f'INSERT INTO "new_{table}" ({cols}) VALUES ({", ".join(["?"] * len(df.columns))})',
        df.rows(),
    )

    # Close before inserting: a changed row would briefly have two live versions,
    # which the unique index forbids.
    closed = conn.execute(f"""
        UPDATE "{table}" SET valid_to_load = ?
        WHERE valid_to_load IS NULL AND ({keys}) IN (
            SELECT {keys} FROM (
                SELECT {cols} FROM "current_{table}"
                EXCEPT
                SELECT {cols} FROM "new_{table}"
            )
        )
    """, (load_id,)).rowcount

    inserted = conn.execute(f"""
        INSERT INTO "{table}" ({cols}, valid_from_load)
        SELECT {cols}, ? FROM (
            SELECT {cols} FROM "new_{table}"
            EXCEPT
            SELECT {cols} FROM "current_{table}"
        )
    """, (load_id,)).rowcount

    conn.execute(f'DROP TABLE "new_{table}"')
    return {"inserted": inserted, "closed": closed}


def ingest_csv(conn: sqlite3.Connection, csv_path: Path, table: str,
               key_cols: list[str], attr_cols: list[str], load_id: int) -> dict[str, int]:
    """Read a CSV and store it with `ingest`."""
    return ingest(conn, pl.read_csv(csv_path), table, key_cols, attr_cols, load_id)


def apply_sql(conn: sqlite3.Connection, sql_dir: Path = SQL_DIR) -> list[str]:
    """Run every .sql file in `sql_dir`, in filename order.

    The files drop and recreate their views, so this is safe to run on every
    build and picks up any edits to a view definition.
    """
    applied = []
    for path in sorted(sql_dir.glob("*.sql")):
        conn.executescript(path.read_text())
        applied.append(path.name)
    conn.commit()
    return applied
