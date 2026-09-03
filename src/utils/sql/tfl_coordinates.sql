-- One average position per TfL station, derived from its individual platform,
-- entrance and interchange points.
--
-- Bus areas are excluded so a station's position reflects the station itself
-- rather than the bus stops around it. LIKE is case-insensitive in SQLite, so
-- this catches Bus, BUS, busp and BusEF alike; COALESCE keeps points whose
-- AreaName is blank instead of silently dropping them.
--
-- Reads from current_station_points, so it always reflects the latest load.

DROP VIEW IF EXISTS tfl_coordinates;

CREATE VIEW tfl_coordinates AS
SELECT
    StationUniqueId,
    AVG(Lat)  AS latitude,
    AVG(Lon)  AS longitude,
    COUNT(*)  AS n_points
FROM current_station_points
WHERE COALESCE(AreaName, '') NOT LIKE '%Bus%'
GROUP BY StationUniqueId;
