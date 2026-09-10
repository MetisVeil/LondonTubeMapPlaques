"""How the stations connect: which stations each line serves, and in what order.

`load(conn)` is the entry point - it fetches every line's route sequence from
the TfL Unified API and versions it into the database.

The companion source is `tfl_stations`, which carries where the stations are.
Unlike that feed, this comes back as JSON over HTTPS rather than a zip of CSVs,
so nothing is written to or read from disk: the API response goes straight into
a DataFrame and on to `db.ingest`.
"""

import hashlib
import json
import os
import sqlite3
import time

import polars as pl
import requests

from .. import db

API = "https://api.tfl.gov.uk"
SOURCE = "tfl_network"

# Every mode that appears on the printed map. Asking for them by mode rather
# than naming lines means a future rename - as happened when the Overground
# lines were christened in 2024 - is picked up without a code change.
MODES = "tube,elizabeth-line,dlr,overground"

# The Unified API reports no colour for a line, so they are pinned here from
# TfL's colour standard, keyed by the line id the API uses.
COLOURS = {
    "bakerloo": "#B36305",
    "central": "#E32017",
    "circle": "#FFD300",
    "district": "#00782A",
    "hammersmith-city": "#F3A9BB",
    "jubilee": "#A0A5A9",
    "metropolitan": "#9B0056",
    "northern": "#000000",
    "piccadilly": "#003688",
    "victoria": "#0098D4",
    "waterloo-city": "#95CDBA",
    "elizabeth": "#6950A1",
    "dlr": "#00A4A7",
    "liberty": "#5D6061",
    "lioness": "#FAA61A",
    "mildmay": "#0077AD",
    "suffragette": "#5BBD72",
    "weaver": "#823A62",
    "windrush": "#ED1B00",
}

LINES = {
    "table": "lines",
    "key": ["line_id"],
    "attrs": ["name", "mode", "colour"],
}
ROUTE_SEQUENCE = {
    "table": "route_sequence",
    "key": ["line_id", "branch", "seq"],
    "attrs": ["station_id", "top_most_parent_id", "station_name"],
}


def _get(path: str, attempts: int = 6) -> list | dict:
    """Call the Unified API, passing an app key if one is in the environment.

    Unregistered callers get 50 requests a minute and a full fetch makes about
    40, so two builds in quick succession will trip the limit. The waits climb
    past a minute for that reason - a 429 means wait, not fail. Setting
    TFL_APP_KEY raises the limit and skips the waiting altogether.
    """
    key = os.environ.get("TFL_APP_KEY")

    for attempt in range(attempts):
        response = requests.get(f"{API}{path}", params={"app_key": key} if key else None)
        if response.status_code != 429 or attempt == attempts - 1:
            break
        pause = response.headers.get("Retry-After")
        time.sleep(min(60, int(pause)) if pause and pause.isdigit() else min(60, 5 * 2**attempt))

    response.raise_for_status()
    return response.json()


def _runs(stops: list[tuple], covered: set[frozenset]) -> list[list[tuple]]:
    """Split a branch into its maximal runs of track not already covered.

    TfL reports the same physical branch once per direction, with the stops
    reversed and the branch ids renumbered, so the second direction is almost
    entirely a repeat. Comparing undirected pairs of stations lets us keep only
    the track a direction genuinely adds, rather than drawing most of the
    network twice.
    """
    runs, run = [], []
    for a, b in zip(stops, stops[1:]):
        if frozenset((a[0], b[0])) in covered:
            if len(run) > 1:
                runs.append(run)
            run = []
            continue
        run = (run or [a]) + [b]

    if len(run) > 1:
        runs.append(run)
    return runs


def branches(line_id: str) -> list[list[tuple]]:
    """Return each distinct run of consecutive stations on a line.

    A branch is a list of (station_id, top_most_parent_id, name) tuples in the
    order a train calls at them.
    """
    covered, found = set(), []

    for direction in ("inbound", "outbound"):
        payload = _get(f"/Line/{line_id}/Route/Sequence/{direction}")

        for sequence in payload["stopPointSequences"]:
            stops = [
                (stop["stationId"], stop.get("topMostParentId"), stop["name"])
                for stop in sequence["stopPoint"]
            ]
            for run in _runs(stops, covered):
                covered |= {frozenset((a[0], b[0])) for a, b in zip(run, run[1:])}
                found.append(run)

    return found


def fetch() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return the lines and their ordered stops, ready to be versioned."""
    line_rows, sequence_rows = [], []

    for line in sorted(_get(f"/Line/Mode/{MODES}/Route"), key=lambda l: l["id"]):
        line_id = line["id"]
        line_rows.append({
            "line_id": line_id,
            "name": line["name"],
            "mode": line["modeName"],
            "colour": COLOURS.get(line_id),
        })

        # Branches are named for their endpoints rather than numbered, because
        # TfL's own branch ids are positional and shuffle between fetches -
        # which would rewrite every row's history for no real change.
        used = set()
        for run in branches(line_id):
            branch = f"{run[0][0]}-{run[-1][0]}"
            suffix = 1
            while branch in used:
                suffix += 1
                branch = f"{run[0][0]}-{run[-1][0]}-{suffix}"
            used.add(branch)

            for seq, (station_id, top_most_parent_id, name) in enumerate(run):
                sequence_rows.append({
                    "line_id": line_id,
                    "branch": branch,
                    "seq": seq,
                    "station_id": station_id,
                    "top_most_parent_id": top_most_parent_id,
                    "station_name": name,
                })

    return pl.DataFrame(line_rows), pl.DataFrame(sequence_rows)


def load(conn: sqlite3.Connection) -> dict[str, dict]:
    """Fetch the network and version it into the database.

    Returns the per-table counts of row versions inserted and closed, so an
    unchanged network reports zeros across the board.
    """
    lines, sequence = fetch()

    # The API stamps no feed version, so we make one: an unchanged network
    # hashes the same, which is what keeps a re-run from writing anything.
    payload = json.dumps([lines.to_dicts(), sequence.to_dicts()], sort_keys=True)
    load_id = db.start_load(conn, SOURCE, hashlib.sha256(payload.encode()).hexdigest()[:16])

    counts = {}
    for spec, frame in ((LINES, lines), (ROUTE_SEQUENCE, sequence)):
        counts[spec["table"]] = db.ingest(
            conn, frame, spec["table"], spec["key"], spec["attrs"], load_id
        )
    conn.commit()
    return counts
