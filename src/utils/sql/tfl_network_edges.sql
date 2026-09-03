-- The network as a graph: which stations each line calls at, and which pairs of
-- them are joined by track.
--
-- The Unified API identifies a stop by its own Naptan code (940GZZLUEUS), but
-- an interchange is stored in `stations` under its hub code (HUBEUS) instead.
-- Resolving through topMostParentId when the stop's own id is unknown is what
-- makes every stop on all 19 lines land on a row we hold coordinates for.
--
-- Named to sort after tfl_coordinates.sql, which the stations view reads:
-- db.apply_sql runs the files in filename order.

DROP VIEW IF EXISTS tfl_network_stops;

CREATE VIEW tfl_network_stops AS
SELECT
    r.line_id,
    r.branch,
    CAST(r.seq AS INTEGER)        AS seq,
    COALESCE(s.UniqueId, h.UniqueId) AS station_uid,
    r.station_name                AS api_name
FROM current_route_sequence r
LEFT JOIN current_stations s ON s.UniqueId = r.station_id
LEFT JOIN current_stations h ON h.UniqueId = r.top_most_parent_id;


DROP VIEW IF EXISTS tfl_network_edges;

-- One row per pair of stations joined by track. Consecutive stops that resolve
-- to the same station (the two halves of an interchange) collapse to nothing.
CREATE VIEW tfl_network_edges AS
SELECT
    a.line_id,
    a.branch,
    a.seq,
    a.station_uid AS from_uid,
    b.station_uid AS to_uid
FROM tfl_network_stops a
JOIN tfl_network_stops b
  ON b.line_id = a.line_id AND b.branch = a.branch AND b.seq = a.seq + 1
WHERE a.station_uid <> b.station_uid;


DROP VIEW IF EXISTS tfl_network_stations;

-- Every station on the map, with the position tfl_coordinates derived and the
-- lines calling there. n_lines is what decides an interchange marker.
CREATE VIEW tfl_network_stations AS
SELECT
    p.station_uid              AS uid,
    s.Name                     AS name,
    c.latitude,
    c.longitude,
    COUNT(DISTINCT p.line_id)  AS n_lines,
    group_concat(DISTINCT p.line_id) AS line_ids
FROM (SELECT DISTINCT line_id, station_uid FROM tfl_network_stops) p
JOIN current_stations s ON s.UniqueId = p.station_uid
LEFT JOIN tfl_coordinates c ON c.StationUniqueId = p.station_uid
GROUP BY p.station_uid, s.Name, c.latitude, c.longitude;
