"""Fetch and ingest London blue plaques from OpenPlaques.

This module finds the latest London CSV dump on the OpenPlaques data page,
downloads it with retries and a friendly User-Agent, writes the CSV to the
`data/tmp` directory, and exposes a `load(conn)` entrypoint which versions the
rows into the database using `utils.db.ingest`.
"""

import re
from pathlib import Path

import polars as pl
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .. import db

SOURCE = "openplaques_london"
BLUE_PLAQUES_PAGE = "https://openplaques.org/posts/data"
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "tmp"   # src/data/tmp

# The columns are pinned rather than read off the CSV so the table has a known
# shape: if OpenPlaques adds a field, it is ignored instead of failing the
# insert against a table that was created without it.
PLAQUES = {
    "table": "plaques",
    "key": ["id"],
    "attrs": [
        "machine_tag", "title", "inscription", "latitude", "longitude",
        "country", "area", "address", "erected", "main_photo", "colour",
        "organisations", "language", "series", "series_ref",
        "geolocated?", "photographed?",
        "number_of_subjects", "number_of_male_subjects",
        "number_of_female_subjects", "number_of_inanimate_subjects",
        "lead_subject_id", "lead_subject_machine_tag", "lead_subject_name",
        "lead_subject_surname", "lead_subject_sex", "lead_subject_born_in",
        "lead_subject_died_in", "lead_subject_type", "lead_subject_roles",
        "lead_subject_primary_role", "lead_subject_wikipedia",
        "lead_subject_dbpedia", "lead_subject_image", "subjects",
    ],
}


def _session_with_retries() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def find_latest_london_csv(url: str = BLUE_PLAQUES_PAGE) -> str | None:
    """Scrape the OpenPlaques data page and return the absolute CSV URL for London."""
    s = _session_with_retries()
    resp = s.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; tube_map/1.0)"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    article = soup.select_one("article.article") or soup
    # Look for links whose text or href mentions 'london' and ends with .csv.
    # The UK and worldwide dumps sit alongside it and must not be picked up.
    for a in article.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").lower()
        if (".csv" in href.lower() and "london" in href.lower()) or (".csv" in text and "london" in text):
            return href if href.startswith("http") else requests.compat.urljoin(url, href)

    # fallback: search for filenames like open-plaques-london-YYYY-MM-DD.csv in page
    found = re.search(r"(https?://[^\s\"']*open-plaques[-_][^\s\"']*london[^\s\"']*\.csv)",
                      resp.text, re.IGNORECASE)
    return found.group(1) if found else None


def download(dest_dir: Path = DATA_DIR, url: str | None = None) -> Path:
    """Download the London plaques CSV into `dest_dir` and return its path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if url is None:
        url = find_latest_london_csv()
        if not url:
            raise RuntimeError("Could not find the London CSV on the OpenPlaques data page")

    session = _session_with_retries()
    resp = session.get(url, timeout=60, headers={
        "User-Agent": "Mozilla/5.0 (compatible; tube_map/1.0)",
        "Accept": "text/csv,application/octet-stream,*/*;q=0.8",
    })
    resp.raise_for_status()

    filename = Path(requests.utils.urlparse(url).path).name
    if not filename.lower().endswith(".csv"):
        # try to infer from Content-Disposition
        disposition = re.search(r'filename="?([^";]+)"?', resp.headers.get("content-disposition", ""))
        filename = disposition.group(1) if disposition else "open-plaques-london.csv"

    out_path = dest_dir / filename
    out_path.write_bytes(resp.content)
    return out_path


def load(conn, dest_dir: Path = DATA_DIR) -> dict[str, dict]:
    """Download the latest London plaques dump and version it into the database.

    Returns the same counts dict shape as other sources, so an unchanged dump
    reports zeros across the board.
    """
    csv_path = download(dest_dir)

    # The dump is dated in its filename, which makes a natural feed version.
    load_id = db.start_load(conn, SOURCE, csv_path.name)

    # Read every column as text. The storage is text anyway, and inferring types
    # from the first rows misreads columns that only turn non-numeric later on.
    frame = pl.read_csv(csv_path, infer_schema_length=0)

    # The dump writes an absent value as an empty field, which reads back as ""
    # rather than null. Left alone those reach SQLite as '' and quietly defeat
    # every IS NULL test downstream - a role that looks missing but is not - so
    # they are made null here, where it is fixed once for every column at once.
    frame = frame.with_columns(
        pl.when(pl.col(column).str.strip_chars() == "")
          .then(None).otherwise(pl.col(column)).alias(column)
        for column in frame.columns
    )

    counts = {"plaques": db.ingest(
        conn, frame, PLAQUES["table"], PLAQUES["key"], PLAQUES["attrs"], load_id)}
    conn.commit()
    return counts
