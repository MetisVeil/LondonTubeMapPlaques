-- The plaques attributed to each station, with everything a map needs to label
-- one: who it commemorates, what they did, and how far the walk is.
--
-- The attribution itself lives in the plaque_stations table, which is computed
-- rather than queried - see utils/plaque_stations.py.
--
-- The filter drops plaques with nobody named or no role recorded, which is what
-- makes this the view to build a themed map from. Query plaque_stations instead
-- to see every attribution, including the plaques for buildings and events.
--
-- Note that plenty of what survives commemorates a place or a thing rather than
-- a person - a housing estate, a bicycle club, a tube station. subject_type is
-- what separates them: 'man' and 'woman' are the people. Prefer it to gender,
-- which records 95 men as 'object' and so cannot be trusted for that.

DROP VIEW IF EXISTS station_plaques;

CREATE VIEW station_plaques AS
SELECT
    s.uid                        AS station_uid,
    s.name                       AS station_name,
    a.rank,
    a.distance_m,
    p.id                         AS plaque_id,
    p.title,
    p.inscription,
    p.address,
    p.colour,
    p.erected,
    p.latitude,
    p.longitude,
    p.lead_subject_name          AS person,
    p.lead_subject_primary_role  AS role,
    p.lead_subject_roles         AS roles,
    p.lead_subject_sex           AS gender,
    p.lead_subject_type          AS subject_type,
    p.lead_subject_born_in       AS born,
    p.lead_subject_died_in       AS died,
    p.lead_subject_wikipedia     AS wikipedia,
    p.lead_subject_image         AS person_image,
    p.main_photo                 AS plaque_photo
FROM plaque_stations a
JOIN tfl_network_stations s ON s.uid = a.station_uid
JOIN current_plaques p ON p.id = a.plaque_id
WHERE p.lead_subject_name IS NOT NULL AND p.lead_subject_primary_role IS NOT NULL;
