"""Assemble the JSON that d3-tube-map draws, and write it out for the web page.

`export(conn)` is the entry point. It reads the network views, lays the stations
out on the octilinear grid, applies whatever hand-tuning `layout_overrides.json`
carries, checks the result against the library's own geometry rules, and writes
src/web/map.json.
"""

import collections
import json
import re
import sqlite3
import textwrap
from pathlib import Path

from . import layout

SRC_DIR = Path(__file__).resolve().parents[1]          # src/
MAP_PATH = SRC_DIR / "web" / "map.json"
OVERRIDES_PATH = SRC_DIR / "layout_overrides.json"

# Chosen by sweeping: the smallest grid on which every branch still routes, with
# room left for the corners. Bigger only spreads the outskirts further apart.
SCALE = 1.8

# Wider than this and a label starts colliding with its neighbours, so it wraps.
LABEL_WIDTH = 11

# Lines running along the same track have to be drawn side by side or they land
# on top of each other. shiftNormal offsets a line along its own normal, so
# these are the offsets to hand out, nearest the centre line first.
OFFSETS = [0, 1, -1, 2, -2, 3, -3, 4, -4]


def station_key(name: str) -> str:
    """Turn a station name into a key usable as an SVG id and readable in a diff.

    Raw Naptan ids would do as far as uniqueness goes, but they start with a
    digit, which makes them invalid in a CSS selector, and nobody can hand-edit
    an overrides file full of 940GZZLUOXC.
    """
    # Apostrophes are dropped rather than split on, so King's Cross comes out as
    # KingsCrossStPancras rather than KingSCrossStPancras.
    plain = re.sub(r"[^0-9A-Za-z ]", " ", name.replace("'", "").replace("\u2019", ""))
    return "".join(word.capitalize() for word in plain.split())


def label(name: str) -> str:
    """Wrap a long station name, the way the library's own data does."""
    return "\n".join(textwrap.wrap(name, LABEL_WIDTH, break_long_words=False))


def read(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    """Read the network out of the views: stations, lines, and each branch's stops."""
    stations = {
        uid: {"name": name, "lat": lat, "lon": lon, "n_lines": n_lines}
        for uid, name, lat, lon, n_lines in conn.execute(
            "SELECT uid, name, latitude, longitude, n_lines FROM tfl_network_stations")
    }
    lines = {
        line_id: {"name": name, "colour": colour}
        for line_id, name, colour in conn.execute(
            "SELECT line_id, name, colour FROM current_lines")
    }

    branches = collections.defaultdict(list)
    for line_id, branch, _, uid in conn.execute(
            "SELECT line_id, branch, seq, station_uid FROM tfl_network_stops "
            "ORDER BY line_id, branch, seq"):
        path = branches[(line_id, branch)]
        if not path or path[-1] != uid:      # the two halves of an interchange
            path.append(uid)

    return stations, lines, {k: v for k, v in branches.items() if len(v) > 1}


def side_by_side(branches: dict) -> dict[str, int]:
    """Give lines that share track a different offset, so both stay visible.

    Busiest first, each line takes the offset nearest the centre that none of the
    lines it shares track with has already claimed.
    """
    neighbours = collections.defaultdict(set)
    users = collections.defaultdict(set)
    for (line_id, _), path in branches.items():
        for pair in zip(path, path[1:]):
            users[frozenset(pair)].add(line_id)
    for sharing in users.values():
        for line_id in sharing:
            neighbours[line_id] |= sharing - {line_id}

    offsets = {}
    for line_id in sorted(neighbours, key=lambda l: (-len(neighbours[l]), l)):
        taken = {offsets[other] for other in neighbours[line_id] if other in offsets}
        offsets[line_id] = next(o for o in OFFSETS if o not in taken)
    return offsets


def hide_opposed_markers(drawn: list[dict]) -> int:
    """Drop the markers that would make the library place a station at the origin.

    An interchange is positioned by interchangeShift, which divides by the cross
    product of the first two non-parallel directions running through it. Its idea
    of parallel excludes opposites, so two lines passing through a station in
    exactly opposite directions give it a cross product of zero, a translate of
    NaN, and a marker that silently falls to the top left corner of the map.

    Hiding the later of the two opposed markers is enough to avoid it. The
    station keeps its ring - the library reads only the first marker to decide
    that - and where nothing is opposed every marker survives, so interchanges
    still sit centred across the lines that meet them.
    """
    kept = collections.defaultdict(list)
    hidden = 0

    for line in drawn:
        for node, direction in zip(line["nodes"], layout.directions(line["nodes"])):
            key = node.get("name")
            if not key:
                continue
            if any(layout.opposed(direction, other) for other in kept[key]):
                node["hide"] = True
                hidden += 1
            else:
                kept[key].append(direction)

    return hidden


def overrides(path: Path = OVERRIDES_PATH) -> dict:
    """Read the hand-tuning file, which is optional and may be empty."""
    if not path.exists():
        return {}
    return json.loads(path.read_text() or "{}")


def build(conn: sqlite3.Connection, scale: float = SCALE) -> dict:
    """Lay the network out and return it in the shape d3-tube-map expects."""
    stations, lines, branches = read(conn)
    tuning = overrides()

    # A handful of names belong to two genuinely separate stations - there really
    # are two Edgware Roads, and the map shows both - so those keys carry the
    # Naptan id to tell them apart. The label stays the plain name either way.
    named = collections.Counter(station_key(s["name"]) for s in stations.values())
    keys = {uid: station_key(s["name"]) + ("_" + uid if named[station_key(s["name"])] > 1 else "")
            for uid, s in stations.items()}
    by_key = {key: uid for uid, key in keys.items()}
    if len(by_key) != len(keys):
        raise ValueError("station keys are not unique")

    edges = [(path[i], path[i + 1]) for path in branches.values() for i in range(len(path) - 1)]
    coords = layout.place({uid: (s["lat"], s["lon"]) for uid, s in stations.items()},
                          edges, scale)

    # Hand-placed stations win over the generated position, and because every
    # line reads this one dict they cannot disagree about where a station is.
    for key, tuned in tuning.get("stations", {}).items():
        if "coords" in tuned:
            coords[by_key[key]] = tuple(tuned["coords"])

    offsets = side_by_side(branches)
    numbering = collections.Counter()
    drawn, occupied = [], set()

    for (line_id, _), path in sorted(branches.items()):
        nodes = layout.chain([coords[uid] for uid in path], [keys[uid] for uid in path])
        layout.validate(nodes)
        occupied |= set(layout.cells(nodes))

        numbering[line_id] += 1
        drawn.append({
            "name": f"{line_id}-{numbering[line_id]}",
            "label": lines[line_id]["name"],
            "color": lines[line_id]["colour"],
            "shiftCoords": [0, 0],
            "shiftNormal": tuning.get("lines", {}).get(line_id, {}).get(
                "shiftNormal", offsets.get(line_id, 0)),
            "nodes": nodes,
        })

    hide_opposed_markers(drawn)

    label_at = layout.label_positions({uid: coords[uid] for uid in stations}, occupied)
    for line in drawn:
        for node in line["nodes"]:
            if node.get("name"):
                uid = by_key[node["name"]]
                node["labelPos"] = label_at[uid]
                if stations[uid]["n_lines"] > 1:
                    node["marker"] = "interchange"

    built = {
        "stations": {
            keys[uid]: {
                "label": label(station["name"]),
                "uid": uid,
                "lat": station["lat"],
                "lon": station["lon"],
            }
            for uid, station in stations.items()
        },
        "lines": drawn,
    }

    for key, tuned in tuning.get("stations", {}).items():
        if "labelPos" in tuned:
            for line in drawn:
                for node in line["nodes"]:
                    if node.get("name") == key:
                        node["labelPos"] = tuned["labelPos"]

    return built


def export(conn: sqlite3.Connection, path: Path = MAP_PATH, scale: float = SCALE) -> dict:
    """Write the map out for the web page, and report what went into it."""
    built = build(conn, scale)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(built, indent=1))

    # The library lets whichever line is drawn last decide where a station sits,
    # so any disagreement would quietly misplace it rather than fail.
    placed = {}
    for line in built["lines"]:
        for node in line["nodes"]:
            key = node.get("name")
            if key and placed.setdefault(key, node["coords"]) != node["coords"]:
                raise ValueError(f"{key} is in two places: {placed[key]} and {node['coords']}")
    if set(placed) != set(built["stations"]):
        raise ValueError("some stations were never drawn on a line")

    cells = [node["coords"] for line in built["lines"] for node in line["nodes"]]
    return {
        "stations": len(built["stations"]),
        "paths": len(built["lines"]),
        "nodes": sum(len(line["nodes"]) for line in built["lines"]),
        "grid": (max(c[0] for c in cells) - min(c[0] for c in cells),
                 max(c[1] for c in cells) - min(c[1] for c in cells)),
        "path": str(path),
    }
