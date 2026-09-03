"""Work out which station each plaque belongs to.

`build(conn)` is the entry point - it reads the plaques and the stations on the
map, measures every pairing with `distances`, and stores the nearest few
stations for each plaque.

The result is derived rather than fetched, so unlike a source it is rebuilt from
scratch on every run instead of being versioned.
"""

import sqlite3

import numpy as np
import polars as pl

from . import distances

TABLE = "plaque_stations"

# How many stations to keep for each plaque. More than one because the closest
# station is not always the one a person would actually use, and a map that
# wants a different plaque per station needs somewhere to fall back to.
NEAREST = 3

# Past this a plaque is not really "at" a station. A kilometre is about a twelve
# minute walk and takes in 88% of London's plaques; the tail runs to 37km, out
# where the Elizabeth line reaches Reading and there are no plaques nearby.
MAX_DISTANCE_M = 1000


def read(conn: sqlite3.Connection) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Read the stations on the map and the plaques that can be placed on it.

    Roughly one plaque in thirty has never been given a position, and those
    cannot be attributed to anything, so they are left out here.
    """
    stations = pl.read_database(
        "SELECT uid, latitude, longitude FROM tfl_network_stations "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL", conn)

    plaques = pl.read_database(
        "SELECT id, latitude, longitude FROM current_plaques "
        "WHERE latitude IS NOT NULL AND latitude <> '' "
        "AND longitude IS NOT NULL AND longitude <> ''", conn
    ).with_columns(pl.col("latitude").cast(pl.Float64),
                   pl.col("longitude").cast(pl.Float64))

    return stations, plaques


def match(stations: pl.DataFrame, plaques: pl.DataFrame,
          nearest: int = NEAREST, max_distance: float = MAX_DISTANCE_M) -> pl.DataFrame:
    """Return the nearest few stations to each plaque, one row per pairing.

    `distance_matrix` gives every station against every plaque in one go - 421
    by 3675 here - and the ranking is then just a partial sort down each column.
    """
    metres = distances.distance_matrix(stations, plaques,
                                       "latitude", "longitude", "latitude", "longitude")

    nearest = min(nearest, metres.shape[0])
    closest = np.argsort(metres, axis=0)[:nearest]            # (nearest, n_plaques)
    walk = np.take_along_axis(metres, closest, axis=0)

    # Both arrays run rank-major once flattened, so the plaque ids tile and the
    # ranks repeat to line up with them.
    pairs = pl.DataFrame({
        "plaque_id": np.tile(plaques["id"].to_numpy(), nearest),
        "station_uid": stations["uid"].to_numpy()[closest.ravel()],
        "rank": np.repeat(np.arange(1, nearest + 1), plaques.height),
        "distance_m": walk.ravel().round(1),
    })

    return pairs.filter(pl.col("distance_m") <= max_distance).sort(["plaque_id", "rank"])


def store(conn: sqlite3.Connection, pairs: pl.DataFrame, table: str = TABLE) -> None:
    """Replace the table with `pairs`, indexed for looking up a station's plaques."""
    conn.executescript(f'''
        DROP TABLE IF EXISTS "{table}";
        CREATE TABLE "{table}" (
            plaque_id   TEXT NOT NULL,
            station_uid TEXT NOT NULL,
            rank        INTEGER NOT NULL,
            distance_m  REAL NOT NULL,
            PRIMARY KEY (plaque_id, rank)
        );
    ''')
    conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, ?)', pairs.rows())
    conn.execute(f'CREATE INDEX "{table}_station" ON "{table}" (station_uid, rank, distance_m)')
    conn.commit()


def build(conn: sqlite3.Connection, nearest: int = NEAREST,
          max_distance: float = MAX_DISTANCE_M) -> dict:
    """Attribute every plaque to its nearest stations, and report how it went."""
    stations, plaques = read(conn)
    pairs = match(stations, plaques, nearest, max_distance)
    store(conn, pairs)

    closest = pairs.filter(pl.col("rank") == 1)
    return {
        "plaques placed": plaques.height,
        "attributed": f"{closest.height} within {max_distance:.0f}m",
        "stations served": f'{closest["station_uid"].n_unique()} of {stations.height}',
        "median walk": f'{closest["distance_m"].median():.0f} m',
    }
