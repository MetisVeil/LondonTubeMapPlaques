"""Entry point for the whole pipeline.

Run it from src/ as a module, so that `utils` is importable:

    cd src && python -m handler.main

Each source fetches its data and versions it into the database; the .sql files
in utils/sql/ then rebuild the derived views on top. Nothing under data/ is
committed to git, so this is also what you run after cloning the repo.
"""

from utils import db
from utils.sources import tfl

# Every source exposes SOURCE and load(conn). Add new ones here.
SOURCES = [tfl]


def build(db_path=db.DB_PATH) -> None:
    """Refresh every source into the database, then rebuild the derived views."""
    conn = db.get_connection(db_path)
    try:
        for source in SOURCES:
            print(f"{source.SOURCE}:")
            for table, counts in source.load(conn).items():
                print(f"  {table:16} {counts['inserted']:>5} new versions, {counts['closed']:>5} closed")

        print("\nviews:")
        for name in db.apply_sql(conn):
            print(f"  {name}")

        print(f"\nbuilt {db_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    build()
