"""Turn role_categories.md into the themed maps the page offers.

The markdown is the thing to edit when adding a category - nothing here needs
changing to add one. Its shape is three levels:

    ## Arts & Literature          a group, which becomes a map of its own
    - Music                       a category within it
      - composer                  a role, matched against the plaque's own
      - subject_type: woman       or a column and value, for anything not a role
      - occupation: musician      or what Wikidata says the subject did
      - wikipedia: jazz musicians or a Wikipedia category the subject is in
      - when: August - Carnival   and the month this map is the one on show
      - about: Notting Hill Carnival  the article explaining that occasion
      - except: film scores       less anything a loose match wrongly reached

`build(conn)` reads it, works out which plaque belongs at which station for each
category, and hands back something the page can switch between.
"""

import collections
import re
import sqlite3
import unicodedata

from ..sources.wikipedia import fetch_image as wikipedia_image
from ..sources.wikipedia import fetch_intro as wikipedia_intro

GROUP = re.compile(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$")
CATEGORY = re.compile(r"^-\s+(.+?)\s*$")
ENTRY = re.compile(r"^\s+-\s+(.+?)\s*$")
FILTER = re.compile(r"^(\w+):\s*(.+)$")
DASH = re.compile(r"\s[-–—]\s")

# What a "field: value" entry is allowed to match on, as the test it becomes.
# Listed rather than built from the field name so a typo names a field that does
# not exist rather than quietly matching nothing. `organisations` holds a JSON
# array, so the one plaque body that erected it is looked for inside the string.
FIELDS = {"subject_type": '"lead_subject_type" = ?',
          "gender": '"lead_subject_sex" = ?',
          "colour": '"colour" = ?',
          "area": '"area" = ?',
          "series": '"series" = ?',
          "organisation": '"organisations" LIKE \'%\' || ? || \'%\''}

# Entries answered by `derived.subject_facts` rather than by the plaque itself.
# An occupation is matched whole, because it comes from a fixed vocabulary; a
# Wikipedia category is matched loosely, because "LGBTQ" has to find "British
# LGBTQ writers" and "20th-century English LGBTQ people" alike.
FACTS = {"occupation": "value = ?", "wikipedia": "value LIKE '%' || ? || '%'"}

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


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
                if field == "when":
                    current["when"] = when(value)
                elif field == "about":
                    current["about"] = value
                elif field == "except":
                    current["except"].append(value)
                elif field in FACTS:
                    current["facts"].append((field, value))
                elif field in FIELDS:
                    current["filters"].append((FIELDS[field], value))
                else:
                    raise ValueError(f"{entry!r}: unknown field {field!r}, expected one of "
                                     f"{sorted([*FIELDS, *FACTS, 'when', 'about', 'except'])}")
            else:
                current["roles"].append(normalise(entry))
        elif found := CATEGORY.match(line):
            current = {"name": found.group(1), "group": group, "when": None, "about": None,
                       "roles": [], "filters": [], "facts": [], "except": []}
            categories.append(current)

    return [c for c in categories if c["roles"] or c["filters"] or c["facts"]]


def when(text: str) -> dict:
    """"March - International Women's Day" -> the month, and why it is that month.

    The note is what the page says when it opens the map on its own, so it is
    written here beside the rules rather than kept in a calendar of its own.
    """
    month, *note = DASH.split(text, maxsplit=1)
    return {"month": MONTHS.index(month.strip().lower()) + 1,
            "note": note[0].strip() if note else ""}


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
                                   "when": None, "about": None,
                                   "roles": [], "filters": [], "facts": [], "except": []})
        whole["roles"] += category["roles"]
        whole["filters"] += category["filters"]
        whole["facts"] += category["facts"]
        whole["except"] += category["except"]
    return list(merged.values())


def plaques_for(conn: sqlite3.Connection, category: dict, people_only: bool = True) -> list[str]:
    """The ids of the plaques a category covers."""
    where, params = [], []
    if category["roles"]:
        where.append("lower(replace(lead_subject_primary_role, char(8217), char(39))) IN (%s)"
                     % ",".join("?" * len(category["roles"])))
        params += category["roles"]
    for test, value in category["filters"]:
        where.append(test)
        params.append(value)
    for kind, value in category["facts"]:
        where.append("lead_subject_wikipedia IN (SELECT subject FROM subject_facts "
                     f"WHERE kind = '{kind}' AND {FACTS[kind]})")
        params.append(value)

    # A category is a union of its rules: any role in the list, or any filter.
    clause = " OR ".join(where)

    # ...less whatever it disowns. Matching a Wikipedia category loosely is what
    # makes one line cover a whole family of them, and this is the price: now
    # and then it reaches something it should not, and that is said here rather
    # than by giving up and listing people by hand.
    for value in category["except"]:
        clause = (f"({clause}) AND (lead_subject_wikipedia IS NULL OR lead_subject_wikipedia "
                  "NOT IN (SELECT subject FROM subject_facts WHERE kind = 'wikipedia' "
                  "AND value LIKE '%' || ? || '%'))")
        params.append(value)

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

    # Keyed by the person rather than the plaque: Dickens has twenty plaques and
    # Hendrix three, and a map that reads them as different people would put the
    # same name on three stations.
    chosen, used = {}, set()
    for uid, plaque, rank, metres, person, role, wiki, portrait, photo in pairs:
        if uid in chosen or (wiki or person) in used:
            continue
        image = portrait or wikipedia_image(wiki)
        chosen[uid] = {"plaque": plaque, "person": person, "role": role,
                       "distance_m": round(metres), "wikipedia": wiki,
                       "person_image": image, "plaque_photo": photo}
        used.add(wiki or person)

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
                          "when": category["when"], "stations": stations,
                          "about": wikipedia_intro(category["about"])})
    return built


if __name__ == "__main__":
    assert when("March") == {"month": 3, "note": ""}
    assert when("August — Notting Hill Carnival") == {"month": 8, "note": "Notting Hill Carnival"}
    assert when("october - Black History Month") == {"month": 10, "note": "Black History Month"}
    print("ok")
