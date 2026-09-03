import numpy as np
import polars as pl

EARTH_RADIUS_M = 6_371_000.0


def haversine_matrix(lat1, lon1, lat2, lon2, radius=EARTH_RADIUS_M):
	"""Return (n_stations, n_plaques) array of distances in meters.

	Inputs may be lists, numpy arrays or Polars Series.
	"""
	a_lat = np.radians(np.asarray(lat1, dtype=float))[:, None]
	a_lon = np.radians(np.asarray(lon1, dtype=float))[:, None]
	b_lat = np.radians(np.asarray(lat2, dtype=float))[None, :]
	b_lon = np.radians(np.asarray(lon2, dtype=float))[None, :]

	dlat = b_lat - a_lat
	dlon = b_lon - a_lon

	sin_dlat = np.sin(dlat / 2.0)
	sin_dlon = np.sin(dlon / 2.0)

	a = sin_dlat ** 2 + np.cos(a_lat) * np.cos(b_lat) * sin_dlon ** 2
	c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))

	return radius * c


def distance_matrix(stations: pl.DataFrame, plaques: pl.DataFrame,
					station_lat: str, station_lon: str,
					plaque_lat: str, plaque_lon: str) -> np.ndarray:
	"""Compute distances between every station and plaque.

	Returns a numpy array with shape (len(stations), len(plaques)).
	"""
	lat1 = stations[station_lat].to_numpy()
	lon1 = stations[station_lon].to_numpy()
	lat2 = plaques[plaque_lat].to_numpy()
	lon2 = plaques[plaque_lon].to_numpy()

	return haversine_matrix(lat1, lon1, lat2, lon2)
