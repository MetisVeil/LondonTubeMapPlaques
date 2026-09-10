"""Turn role_categories.md into the themed maps the page offers.

The markdown is the thing to edit when adding a category - nothing here needs
changing to add one. Its shape is three levels:

    ## Arts & Literature          a group, which becomes a map of its own
    - Music                       a category within it
      - composer                  a role, matched against the plaque's own
      - subject_type: woman       or a column and value, for anything not a role

`build(conn)` reads it, works out which plaque belongs at which station for each
category, and hands back something the page can switch between.
"""

import collections
import re
import sqlite3
import unicodedata

from ..sources.wikipedia import fetch_image as wikipedia_image

GROUP = re.compile(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$")
CATEGORY = re.compile(r"^-\s+(.+?)\s*$")
ENTRY = re.compile(r"^\s+-\s+(.+?)\s*$")
FILTER = re.compile(r"^(\w+):\s*(.+)$")

# Columns a "field: value" entry is allowed to match on, so a typo names a
# column that does not exist rather than quietly matching nothing.
FIELDS = {"subject_type": "lead_subject_type", "gender": "lead_subject_sex",
          "colour": "colour", "area": "area"}


def normalise(role: str) -> str:
    """Fold a role to match on. The dump uses curly apostrophes, the file straight ones."""
    return unicodedata.normalize("NFKC", role).replace("’", "'").strip().lower()


def parse(path) -> list[dict]:
    """Read the markdown into a list of categories, each with its group and rules."""
    categories, group, current = [], None, None

    for line in open(path, encoding="utf-8"):
        if found := GROUP.match(line):
            group = found.group(1)
        elif found := ENTRY.match(line) if line.startswith((" ", "\t")) else None:
            if current is None:
                continue
            entry = found.group(1)
            if rule := FILTER.match(entry):
                field, value = rule.group(1), rule.group(2)
                if field not in FIELDS:
                    raise ValueError(f"{entry!r}: unknown field {field!r}, expected one of {sorted(FIELDS)}")
                current["filters"].append((FIELDS[field], value))
            else:
                current["roles"].append(normalise(entry))
        elif found := CATEGORY.match(line):
            current = {"name": found.group(1), "group": group, "roles": [], "filters": []}
            categories.append(current)

    return [c for c in categories if c["roles"] or c["filters"]]


def whole_groups(categories: list[dict]) -> list[dict]:
    """Add a category per group, merging everything under it.

    Several of the categories are small - five stations for Education - which
    makes a thin map on its own but a good one combined with its neighbours.
    """
    merged = collections.OrderedDict()
    for category in categories:
        if category["group"] is None or category["group"] == "Special":
            continue
        whole = merged.setdefault(category["group"],
                                  {"name": category["group"], "group": None,
                                   "roles": [], "filters": []})
        whole["roles"] += category["roles"]
        whole["filters"] += category["filters"]
    return list(merged.values())


def plaques_for(conn: sqlite3.Connection, category: dict, people_only: bool = True) -> list[str]:
    """The ids of the plaques a category covers."""
    where, params = [], []
    if category["roles"]:
        where.append("lower(replace(lead_subject_primary_role, char(8217), char(39))) IN (%s)"
                     % ",".join("?" * len(category["roles"])))
        params += category["roles"]
    for column, value in category["filters"]:
        where.append(f'"{column}" = ?')
        params.append(value)

    # A category is a union of its rules: any role in the list, or any filter.
    clause = " OR ".join(where)
    if people_only:
        clause = f"({clause}) AND lead_subject_type IN ('man', 'woman')"

    return [row[0] for row in conn.execute(
        f"SELECT id FROM current_plaques WHERE {clause}", params)]


def assign(conn: sqlite3.Connection, plaque_ids: list[str]) -> dict[str, dict]:
    """Give each station one plaque from `plaque_ids`, and each plaque one station.

    Both directions matter. A station wants a single name on it, and a person
    should appear once rather than at every station they happen to be near - so
    the nearest pairings are taken first and the rest fall back to the second or
    third station stored for them, which is what those were kept for.
    """
    if not plaque_ids:
        return {}

    pairs = conn.execute(f"""
        SELECT a.station_uid, a.plaque_id, a.rank, a.distance_m,
               p.lead_subject_name, p.lead_subject_primary_role,
               p.lead_subject_wikipedia, p.lead_subject_image, p.main_photo
        FROM plaque_stations a
        JOIN current_plaques p ON p.id = a.plaque_id
        WHERE a.plaque_id IN ({",".join("?" * len(plaque_ids))})
          AND p.lead_subject_name IS NOT NULL
        ORDER BY a.rank, a.distance_m
    """, plaque_ids).fetchall()

    chosen, used = {}, set()
    for uid, plaque, rank, metres, person, role, wiki, portrait, photo in pairs:
        if uid in chosen or plaque in used:
            continue
        image = portrait or wikipedia_image(wiki)
        chosen[uid] = {"plaque": plaque, "person": person, "role": role,
                       "distance_m": round(metres), "wikipedia": wiki,
                       "person_image": image, "plaque_photo": photo}
        used.add(plaque)

    return chosen


def build(conn: sqlite3.Connection, path) -> list[dict]:
    """Read the markdown and resolve every category to its station-by-station names."""
    categories = parse(path)
    everything = whole_groups(categories) + categories

    built = []
    for category in everything:
        stations = assign(conn, plaques_for(conn, category))
        if stations:
            built.append({"name": category["name"], "group": category["group"],
                          "stations": stations})
    return built
