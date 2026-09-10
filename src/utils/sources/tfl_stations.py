"""Where the stations are: TfL's detailed station data feed, a zip of CSVs.

`coordinates(conn)` is the entry point - it downloads the feed, versions it, and
hands back the averaged position of each station.

The companion source is `tfl_network`, which carries how the stations connect.
This one knows nothing about lines; that one knows nothing about positions.
"""

import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path

import polars as pl
import requests

from .. import db

STATION_DATA_URL = "https://api.tfl.gov.uk/stationdata/tfl-stationdata-detailed.zip"
SOURCE = "tfl_stationdata"

UTILS_DIR = Path(__file__).resolve().parent.parent      # src/utils
SQL_DIR = UTILS_DIR / "sql"
DATA_DIR = UTILS_DIR.parent / "data" / "tmp"            # src/data/tmp

# For each CSV: the natural key that identifies a row, and the attributes whose
# changes we want to track over time.
STATIONS = {
    "csv": "Stations.csv",
    "table": "stations",
    "key": ["UniqueId"],
    "attrs": ["Name", "FareZones", "HubNaptanCode", "Wifi", "OutsideStationUniqueId",
              "BlueBadgeCarParking", "BlueBadgeCarParkSpaces", "TaxiRanksOutsideStation",
              "MainBusInterchange", "PierInterchange", "NationalRailInterchange",
              "AirportInterchange", "EmiratesAirLineInterchange"],
}
STATION_POINTS = {
    "csv": "StationPoints.csv",
    "table": "station_points",
    "key": ["UniqueId"],
    "attrs": ["StationUniqueId", "AreaName", "AreaId", "Level", "Lat", "Lon", "FriendlyName"],
}


def download(dest_dir: Path = DATA_DIR, url: str = STATION_DATA_URL) -> Path:
    """Download the TfL station data zip and extract its CSVs into `dest_dir`."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    response = requests.get(url)
    response.raise_for_status()

    with zipfile.ZipFile(BytesIO(response.content)) as zf:
        zf.extractall(dest_dir)

    return dest_dir


def read_feed_version(dest_dir: Path = DATA_DIR) -> str:
    """Return the FeedStartDate TfL stamped on this download's FeedInfo.csv."""
    return pl.read_csv(Path(dest_dir) / "FeedInfo.csv")["FeedStartDate"][0]


def load(conn: sqlite3.Connection, dest_dir: Path = DATA_DIR) -> dict[str, dict]:
    """Download the feed and version it into the database.

    Returns the per-table counts of row versions inserted and closed, so an
    unchanged feed reports zeros across the board.
    """
    download(dest_dir)
    load_id = db.start_load(conn, SOURCE, read_feed_version(dest_dir))

    counts = {}
    for spec in (STATIONS, STATION_POINTS):
        counts[spec["table"]] = db.ingest_csv(
            conn, dest_dir / spec["csv"], spec["table"], spec["key"], spec["attrs"], load_id
        )
    conn.commit()
    return counts


def coordinates(conn: sqlite3.Connection, refresh: bool = True) -> pl.DataFrame:
    """Return one average position per station, doing all the pre-work first.

    By default this downloads the latest TfL feed and versions any changes into
    the database before rebuilding the view, so the table you get back always
    reflects the live feed. Pass `refresh=False` to skip the download and just
    re-read what is already stored.
    """
    if refresh:
        load(conn)

    conn.executescript((SQL_DIR / "tfl_coordinates.sql").read_text())
    conn.commit()
    return pl.read_database("SELECT * FROM tfl_coordinates", conn)
