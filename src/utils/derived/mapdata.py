"""Assemble the JSON that d3-tube-map draws, and write it out for the web page.

`export(conn)` is the entry point. It reads the network views, lays the stations
out on the octilinear grid, applies whatever hand-tuning `layout_overrides.json`
carries, checks the result against the library's own geometry rules, and writes
src/web/map.json.
"""

import collections
import itertools
import json
import math
import re
import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from . import categories, layout

SRC_DIR = Path(__file__).resolve().parents[2]          # src/
MAP_PATH = SRC_DIR / "web" / "map.json"
CATEGORIES_PATH = SRC_DIR / "web" / "categories.json"
CATEGORIES_SOURCE = SRC_DIR / "role_categories.md"
SEASON_PATH = SRC_DIR / "web" / "season.json"
OVERRIDES_PATH = SRC_DIR / "layout_overrides.json"

# Chosen by sweeping: the smallest grid on which every branch still routes, with
# room left for the corners. Bigger only spreads the outskirts further apart.
SCALE = 1.8

# Wider than this and a label starts colliding with its neighbours, so it wraps.
LABEL_WIDTH = 11

# Lines running along the same track have to be drawn side by side or they land
# on top of each other. shiftNormal offsets a line along its own normal, so
# these are the offsets to hand out, nearest the centre line first.
#
# Capped at one width either side. Two things go wrong as the offset grows: a
# line's station markers travel with it, away from the interchange they are
# meant to share, and - worse - the line tears open at its own junctions, since
# each branch is offset along its own normal and branches leave on different
# bearings. A gap reads as the line ending. One width keeps both within what the
# map already lived with; two put a three cell hole in the Central at Woodford.
OFFSETS = [0, 1, -1]

# What an offset is worth paying. Landing on another line is the thing being
# fixed; looking like you stop at a station you run past is nearly as bad; and
# all else equal a line belongs on its own coordinates.
PHANTOM_COST = 5
DRIFT_COST = 1

# How far a line may be torn open at its own junctions, in grid cells. This is
# a limit rather than a price: a gap reads as the line ending, which no amount
# of overlap saved elsewhere makes up for. Lines whose branches leave on very
# different bearings - the Central at Woodford, the Elizabeth at Whitechapel -
# cannot be offset at all without tearing, and are held on the centre line.
MAX_BREAK = 1.2


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


def station_keys(stations: dict) -> dict[str, str]:
    """Map each station to the key the map files use for it.

    A handful of names belong to two genuinely separate stations - there really
    are two Edgware Roads, and the map shows both - so those keys carry the
    Naptan id to tell them apart. The label stays the plain name either way.
    """
    named = collections.Counter(station_key(s["name"]) for s in stations.values())
    return {uid: station_key(s["name"]) + ("_" + uid if named[station_key(s["name"])] > 1 else "")
            for uid, s in stations.items()}


def read(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    """Read the network out of the views: stations, lines, and each branch's stops."""
    stations = {
        uid: {"name": name, "lat": lat, "lon": lon, "n_lines": n_lines}
        for uid, name, lat, lon, n_lines in conn.execute(
            "SELECT uid, name, latitude, longitude, n_lines FROM tfl_network_stations")
    }
    lines = {
        line_id: {"name": name, "colour": colour, "mode": mode}
        for line_id, name, colour, mode in conn.execute(
            "SELECT line_id, name, colour, mode FROM current_lines")
    }

    branches = collections.defaultdict(list)
    for line_id, branch, _, uid in conn.execute(
            "SELECT line_id, branch, seq, station_uid FROM tfl_network_stops "
            "ORDER BY line_id, branch, seq"):
        path = branches[(line_id, branch)]
        if not path or path[-1] != uid:      # the two halves of an interchange
            path.append(uid)

    # The API describes routes, not track, so one stretch can arrive under two
    # branch ids - the Elizabeth reaches Paddington on two platforms and calls
    # the result two branches over the same pair of stations. Drawn, they are
    # one polyline on top of another: no wider, just twice the ink and twice the
    # markers. Direction is not meaningful here either, so a branch that is
    # another read backwards counts as the same one.
    seen, kept = set(), {}
    for (line_id, branch), path in branches.items():
        if len(path) < 2:
            continue
        shape = (line_id, min(tuple(path), tuple(reversed(path))))
        if shape not in seen:
            seen.add(shape)
            kept[(line_id, branch)] = path

    return stations, lines, kept


def travel_axis(before: dict, after: dict) -> tuple[int, int]:
    """The axis a segment runs along, opposite directions folded together.

    Two lines sharing a corridor are drawn alongside each other whichever way
    round they run it, so for holding them apart N and S - or NE and SW - are
    the same thing. Keeping the axis is what stops a plain crossing, where the
    two genuinely belong on the same cell, from being read as a collision.
    """
    dx = after["coords"][0] - before["coords"][0]
    dy = after["coords"][1] - before["coords"][1]
    step = ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))
    return step if step > (0, 0) else (-step[0], -step[1])


def drawn_lanes(nodes: list[dict], shift: float):
    """Every (cell, axis) pair a branch is drawn through at `shift`."""
    for before, after in zip(nodes, nodes[1:]):
        along = travel_axis(before, after)
        for cell in layout.cells([before, after], shift):
            yield cell, along


def phantom_stops(branches: list, shift: float, stations: dict, served: set) -> int:
    """Count the stations a line is drawn beside but does not call at.

    A line passing within a cell of a station reads as stopping there: the
    Bakerloo runs one cell under Great Portland Street and looks for all the
    world like it serves it. Cells are dilated by one to catch that, since at
    0.8 line widths a neighbouring cell is already touching the marker.
    """
    drawn = {cell for nodes in branches for cell, _ in drawn_lanes(nodes, shift)}
    beside = {(x + dx, y + dy) for x, y in drawn
              for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    return sum(1 for key, cell in stations.items()
               if cell in beside and key not in served)


def worst_junction_break(branches: list, shift: float) -> float:
    """The widest gap a line is torn open by at its own junctions.

    shiftNormal moves each segment along its *own* normal, so where two branches
    of one line meet at a station on different bearings their shifted ends no
    longer coincide and the line is drawn with a gap in it - Woodford, Hainault,
    North Acton. The gap grows with the offset, which is what keeps a line with
    branches spreading in all directions near its own coordinates.
    """
    drawn = collections.defaultdict(list)
    for nodes in branches:
        for index, node in enumerate(nodes):
            if not node.get("name"):
                continue
            for start in (index - 1, index):
                if 0 <= start < len(nodes) - 1:
                    (x, y) = nodes[start]["coords"]
                    (to_x, to_y) = nodes[start + 1]["coords"]
                    length = math.hypot(to_x - x, to_y - y) or 1.0
                    drawn[node["name"]].append((
                        node["coords"][0] + layout.LINE_WIDTH_MULTIPLIER * shift * (to_y - y) / length,
                        node["coords"][1] - layout.LINE_WIDTH_MULTIPLIER * shift * (to_x - x) / length))

    return max((max(math.dist(a, b) for a in points for b in points)
                for points in drawn.values() if len(points) > 1), default=0.0)


def side_by_side(routed: dict, stations: dict, rounds: int = 6) -> dict[str, int]:
    """Offset the lines that would otherwise be drawn on top of each other.

    This asks where the lines are actually *drawn*, which is not the same
    question as which of them share track. The Bakerloo and the Victoria have no
    pair of stations in common between Regent's Park and Oxford Circus, yet the
    router sends both of them down the same column into it; matching on shared
    track sees no conflict there and leaves both on the centre line.

    Each line is scored against the cells the others already occupy, plus the
    stations it would appear to stop at, plus how far it has strayed. Offsets are
    revisited a few times because the cheapest place for an early line depends on
    where the later ones end up, and one pass cannot know that.

    An offset that would tear a line open at one of its own junctions is not
    scored at all but withheld, so the branchiest lines - the Central, the
    District, the Elizabeth - stay on the centre line and the ones with room to
    move are the ones that move.
    """
    branches, served = collections.defaultdict(list), collections.defaultdict(set)
    for (line_id, _), nodes in routed.items():
        branches[line_id].append(nodes)
        served[line_id] |= {n["name"] for n in nodes if n.get("name")}

    intact = {line_id: [shift for shift in OFFSETS
                        if worst_junction_break(paths, shift) <= MAX_BREAK]
              for line_id, paths in branches.items()}
    footprint = {line_id: {shift: set(itertools.chain.from_iterable(
                     drawn_lanes(nodes, shift) for nodes in paths))
                 for shift in intact[line_id]}
                 for line_id, paths in branches.items()}
    ghosts = {line_id: {shift: phantom_stops(paths, shift, stations, served[line_id])
                        for shift in intact[line_id]}
              for line_id, paths in branches.items()}

    # Busiest first: a line with the most drawing on the map has the least room
    # to move once everything else is down.
    order = sorted(branches, key=lambda l: (-sum(map(len, branches[l])), l))
    offsets = {line_id: 0 for line_id in order}

    for _ in range(rounds):
        settled = True
        for line_id in order:
            taken = collections.Counter()
            for other in order:
                if other != line_id:
                    taken.update(footprint[other][offsets[other]])

            def cost(shift, line_id=line_id, taken=taken):
                return (sum(taken[key] for key in footprint[line_id][shift])
                        + ghosts[line_id][shift] * PHANTOM_COST
                        + abs(shift) * DRIFT_COST)

            # OFFSETS runs nearest the centre first, so a tie stays put.
            best = min(intact[line_id], key=cost)
            if best != offsets[line_id]:
                offsets[line_id], settled = best, False
        if settled:
            break

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

    keys = station_keys(stations)
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

    # Route everything before choosing offsets: which lines need holding apart is
    # a question about the cells they are drawn on, and routing is what decides
    # those. Doing it the other way round is how two lines end up on one column.
    routed = {}
    for branch, path in sorted(branches.items()):
        nodes = layout.chain([coords[uid] for uid in path], [keys[uid] for uid in path])
        layout.validate(nodes)
        routed[branch] = nodes

    offsets = side_by_side(routed, {keys[uid]: tuple(coords[uid]) for uid in stations})
    numbering = collections.Counter()
    drawn, occupied = [], set()

    for (line_id, _), nodes in sorted(routed.items()):
        shift = tuning.get("lines", {}).get(line_id, {}).get(
            "shiftNormal", offsets.get(line_id, 0))
        occupied |= set(layout.cells(nodes, shift))

        numbering[line_id] += 1
        drawn.append({
            "name": f"{line_id}-{numbering[line_id]}",
            "label": lines[line_id]["name"],
            "color": lines[line_id]["colour"],
            # Carried through so the page can offer the tube on its own, without
            # having to keep its own list of which lines are the overground.
            "mode": lines[line_id]["mode"],
            "shiftCoords": [0, 0],
            "shiftNormal": shift,
            "nodes": nodes,
        })

    hide_opposed_markers(drawn)

    # A station takes its label settings from the first line that mentions it,
    # so that is the line whose shift the label has to be corrected for.
    anchor = {}
    for line in drawn:
        for node, direction in zip(line["nodes"], layout.directions(line["nodes"])):
            if node.get("name") and node["name"] not in anchor:
                anchor[node["name"]] = (direction, line["shiftNormal"])

    label_at = layout.label_positions(
        {uid: (coords[uid], label(station["name"])) for uid, station in stations.items()},
        occupied)

    for line in drawn:
        for node in line["nodes"]:
            key = node.get("name")
            if not key:
                continue
            uid = by_key[key]
            side, extra = label_at[uid]
            direction, shift = anchor[key]
            node["labelPos"] = side
            node["labelShiftCoords"] = layout.label_shift(extra, direction, shift)
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


def label_report(built: dict) -> dict[str, int]:
    """Count the labels the finished map draws close to a line, or over each other.

    Read back off the built map rather than out of the placement, so it reports
    what will be drawn rather than what the placer was aiming for. The overlap
    count is exact. The line count is deliberately pessimistic - a line is
    treated as filling every cell it crosses, where the ink is thinner than that
    - so it reads high against the rendered page, and is worth watching as a
    number that should go down rather than as a tally of visible collisions.
    """
    occupied, anchor = set(), {}
    for line in built["lines"]:
        occupied |= set(layout.cells(line["nodes"], line["shiftNormal"]))
        for node, direction in zip(line["nodes"], layout.directions(line["nodes"])):
            if node.get("name") and node["name"] not in anchor:
                anchor[node["name"]] = (direction, line["shiftNormal"])

    boxes = {}
    for line in built["lines"]:
        for node in line["nodes"]:
            key = node.get("name")
            if not key or key in boxes:
                continue
            direction, shift = anchor[key]
            tangent = layout.COMPASS[direction]
            length = math.hypot(*tangent)
            shift_x, shift_y = node["labelShiftCoords"]
            extra = ((shift_x + shift * tangent[1] / length) * layout.LINE_WIDTH_MULTIPLIER,
                     (shift_y - shift * tangent[0] / length) * layout.LINE_WIDTH_MULTIPLIER)
            # Measured without the placement padding, so this reports the text
            # as it is actually drawn rather than the margin kept around it.
            boxes[key] = layout.label_box(tuple(node["coords"]),
                                          layout.COMPASS[node["labelPos"]],
                                          built["stations"][key]["label"], extra, padding=0.0)

    over_lines = sum(any(c in occupied for c in layout.box_cells(box))
                     for box in boxes.values())

    covering = collections.defaultdict(list)
    for key, box in boxes.items():
        for cell in layout.box_cells(box):
            covering[cell].append(key)

    clashes = set()
    for keys in covering.values():
        for a, b in itertools.combinations(sorted(set(keys)), 2):
            if not (boxes[a][2] <= boxes[b][0] or boxes[b][2] <= boxes[a][0]
                    or boxes[a][3] <= boxes[b][1] or boxes[b][3] <= boxes[a][1]):
                clashes.add((a, b))

    return {"close to a line": over_lines, "overlapping": len(clashes)}


def export_categories(conn: sqlite3.Connection, path: Path = CATEGORIES_PATH,
                      source: Path = CATEGORIES_SOURCE) -> dict:
    """Write the themed maps the page's menu switches between.

    Kept out of map.json so the map still draws if this fails, and so adding a
    category does not rewrite the map itself.
    """
    stations, _, _ = read(conn)
    keys = station_keys(stations)

    themes = []
    for theme in categories.build(conn, source):
        named = {}
        for uid, plaque in theme["stations"].items():
            if uid in keys:
                named[keys[uid]] = dict(plaque, label=label(plaque["person"]))
        if named:
            themes.append({"name": theme["name"], "group": theme["group"],
                           "when": theme["when"], "stations": named})

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(themes, indent=1))

    return {"categories": len(themes),
            "widest": max((f'{t["name"]} ({len(t["stations"])} stations)' for t in themes),
                          key=lambda t: int(t.split("(")[1].split()[0])),
            "in season": export_season(themes),
            "path": str(path)}


def export_season(themes: list[dict], path: Path = SEASON_PATH, month: int | None = None) -> str:
    """Write the one theme whose month this is, for the page to open itself with.

    A file of its own because categories.json is 1.2MB - four times the map -
    and is only worth fetching once someone opens the menu. Which month it is
    gets decided here rather than in the browser: the data is rebuilt on the 1st
    of every month anyway, so the answer is already fresh by the time it ships.
    """
    month = month or datetime.now(timezone.utc).month
    in_season = [t for t in themes if t["when"] and t["when"]["month"] == month]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(in_season[0] if in_season else None, indent=1))

    return f'{in_season[0]["name"]} ({len(in_season[0]["stations"])} stations)' if in_season else "nothing"


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
    labels = label_report(built)

    return {
        "stations": len(built["stations"]),
        "paths": len(built["lines"]),
        "nodes": sum(len(line["nodes"]) for line in built["lines"]),
        "labels": f'{labels["overlapping"]} overlapping, {labels["close to a line"]} close to a line',
        "grid": (max(c[0] for c in cells) - min(c[0] for c in cells),
                 max(c[1] for c in cells) - min(c[1] for c in cells)),
        "path": str(path),
    }
