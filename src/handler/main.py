"""Entry point for the whole pipeline.

Run it from src/ as a module, so that `utils` is importable:

    cd src && python -m handler.main

Each source fetches its data and versions it into the database; the .sql files
in utils/sql/ then rebuild the derived views on top, and the map the web page
draws is written out last. Nothing under data/ is committed to git, so this is
also what you run after cloning the repo.
"""

import shutil
from pathlib import Path

from utils import db
from utils.derived import mapdata, plaque_stations
from utils.sources import plaques, tfl_network, tfl_stations

# Every source exposes SOURCE and load(conn). Add new ones here.
SOURCES = [tfl_stations, tfl_network, plaques]


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

        print("\nplaques:")
        for field, value in plaque_stations.build(conn).items():
            print(f"  {field:16} {value}")

        print("\ncategories:")
        for field, value in mapdata.export_categories(conn).items():
            print(f"  {field:16} {value}")

        print("\nmap:")
        for field, value in mapdata.export(conn).items():
            print(f"  {field:16} {value}")

        print(f"\nbuilt {db_path}")

        # Clean up downloaded CSVs; they're now in the database.
        tmp_dir = Path(db_path).parent / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
            print(f"cleaned {tmp_dir}")
    finally:
        conn.close()


if __name__ == "__main__":
    build()
