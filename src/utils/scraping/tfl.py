"""Download and unpack the TfL detailed station data feed."""

import zipfile
from io import BytesIO
from pathlib import Path

import polars as pl
import requests

STATION_DATA_URL = "https://api.tfl.gov.uk/stationdata/tfl-stationdata-detailed.zip"


def download_station_data(dest_dir: Path, url: str = STATION_DATA_URL) -> Path:
    """Download the TfL station data zip and extract its CSVs into `dest_dir`.

    Returns `dest_dir`.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    response = requests.get(url)
    response.raise_for_status()

    with zipfile.ZipFile(BytesIO(response.content)) as zf:
        zf.extractall(dest_dir)

    return dest_dir


def read_feed_version(dest_dir: Path) -> str:
    """Return the FeedStartDate TfL stamped on this download's FeedInfo.csv."""
    feed_info = pl.read_csv(Path(dest_dir) / "FeedInfo.csv")
    return feed_info["FeedStartDate"][0]
