"""Turn geographic station positions into the octilinear geometry d3-tube-map draws.

The library will not draw an arbitrary polyline. Every segment has to run along
one of the eight compass directions, and a change of direction has to happen at
its own node, placed exactly one incoming plus one outgoing step past the last
node of the run. Its own London data obeys a further rule that is implied rather
than enforced: a station is never a corner. Every one of the corners in
`example/london-tube.json` is an unnamed node between two stations.

So the shape of a line here is: straight runs with the stations sitting on them,
joined by unnamed bend nodes. `chain` builds that for one branch and `validate`
re-derives the library's own direction pass over the result, which turns a
geometry mistake into a build error naming the offending pair of coordinates
rather than an exception in the browser.
"""

import itertools
import math

# Clockwise from north, so neighbouring entries are 45 degrees apart.
COMPASS = {
    "N": (0, 1), "NE": (1, 1), "E": (1, 0), "SE": (1, -1),
    "S": (0, -1), "SW": (-1, -1), "W": (-1, 0), "NW": (-1, 1),
}
BEARINGS = {vector: name for name, vector in COMPASS.items()}

EARTH_RADIUS_M = 6_371_000.0
LONDON_LAT = 51.5

# Beck's map devotes most of its area to the centre and squeezes the ends of the
# lines in. Raising radial distance to a power under 1 does the same thing: it
# leaves the middle roughly alone and pulls Chesham, Epping and Reading in hard.
# Without it the grid would have to be ~1000 cells wide to keep Covent Garden
# and Leicester Square apart, and the result would be mostly empty space.
COMPRESSION = 0.5

# Four 45 degree corners add up to a full reversal, which is the worst a line
# ever asks for - the Central line doubling back around the Hainault loop.
MAX_TURNS = 4


def parallel(a: tuple, b: tuple) -> bool:
    """Whether two vectors point the same way. Opposite directions are not parallel."""
    if a[0] * b[1] - a[1] * b[0] != 0:
        return False
    return all((a[i] > 0) == (b[i] > 0) and (a[i] < 0) == (b[i] < 0) for i in (0, 1))


def bearing(vector: tuple) -> str | None:
    """Return the compass name of a vector, or None if it lies off the eight points."""
    return next((name for name, v in COMPASS.items() if parallel(v, vector)), None)


def octant(vector: tuple) -> tuple:
    """Return the compass vector closest in angle to `vector`."""
    length = math.hypot(*vector) or 1.0
    return max(COMPASS.values(),
               key=lambda v: (v[0] * vector[0] + v[1] * vector[1]) / (length * math.hypot(*v)))


def legal_turn(incoming: tuple, outgoing: tuple) -> bool:
    """Whether the library will accept a corner between these two directions.

    A corner node sits one incoming step plus one outgoing step past the run, and
    the library only accepts that step at (1,1), (1,2) or (2,1). That admits every
    45 degree turn, and a 90 degree turn between two axial directions, but not a
    90 degree turn out of a diagonal - which needs two corners instead.
    """
    if incoming == outgoing:
        return False
    step = (abs(incoming[0] + outgoing[0]), abs(incoming[1] + outgoing[1]))
    return step in {(1, 1), (1, 2), (2, 1)}


# --------------------------------------------------------------------------
# Placing the stations
# --------------------------------------------------------------------------

def project(lat: float, lon: float) -> tuple[float, float]:
    """Equirectangular projection to metres, which is plenty at London's size."""
    return (math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(LONDON_LAT)),
            math.radians(lat) * EARTH_RADIUS_M)


def compress(point: tuple, centre: tuple, power: float = COMPRESSION) -> tuple:
    """Pull a point towards the centre, hard at the edges and barely at all nearby."""
    dx, dy = point[0] - centre[0], point[1] - centre[1]
    radius = math.hypot(dx, dy)
    if radius == 0:
        return centre
    scaled = radius**power
    return (centre[0] + dx / radius * scaled, centre[1] + dy / radius * scaled)


def _spiral(cell: tuple):
    """Yield `cell` and then the cells around it, nearest first."""
    yield cell
    for ring in range(1, 40):
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                if max(abs(dx), abs(dy)) == ring:
                    yield (cell[0] + dx, cell[1] + dy)


def place(stations: dict[str, tuple[float, float]], edges: list[tuple[str, str]],
          scale: float) -> dict[str, tuple[int, int]]:
    """Assign every station an integer grid cell, no two alike.

    Stations are placed busiest first so that interchanges - the ones whose
    position the eye actually checks - get the cell they asked for, and the
    quieter stations shuffle around them.
    """
    points = {uid: project(lat, lon) for uid, (lat, lon) in stations.items()}
    centre = (sum(p[0] for p in points.values()) / len(points),
              sum(p[1] for p in points.values()) / len(points))
    squashed = {uid: compress(point, centre) for uid, point in points.items()}

    degree = {uid: 0 for uid in stations}
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1

    taken, coords = {}, {}
    for uid in sorted(stations, key=lambda u: (-degree[u], u)):
        x, y = squashed[uid]
        ideal = (round((x - centre[0]) * scale), round((y - centre[1]) * scale))
        cell = next(c for c in _spiral(ideal) if c not in taken)
        taken[cell] = uid
        coords[uid] = cell
    return coords


# --------------------------------------------------------------------------
# Routing a branch
# --------------------------------------------------------------------------

def _solve(dirs: list[tuple], remaining: tuple, minimums: list[int], sweep: int = 7) -> list[int] | None:
    """Find how many steps to take along each direction to cover `remaining`.

    Two directions pin the answer exactly; any beyond that are underdetermined,
    so the extra runs are swept over a short range and the last independent pair
    solved for. Long routes put their length in the first and last run, so a
    short sweep of the middle ones is enough.
    """
    def pair(u, v, target):
        det = u[0] * v[1] - u[1] * v[0]
        if det == 0:
            return None
        a = (target[0] * v[1] - target[1] * v[0]) / det
        b = (u[0] * target[1] - u[1] * target[0]) / det
        return (int(a), int(b)) if a == int(a) and b == int(b) else None

    if len(dirs) == 1:
        if remaining != (0, 0) and not parallel(dirs[0], remaining):
            return None
        steps = max(abs(remaining[0]), abs(remaining[1]))
        return [steps] if steps >= minimums[0] else None

    for i, j in ((0, len(dirs) - 1), (0, 1), (len(dirs) - 2, len(dirs) - 1)):
        if i >= j or dirs[i][0] * dirs[j][1] - dirs[i][1] * dirs[j][0] == 0:
            continue
        others = [k for k in range(len(dirs)) if k not in (i, j)]
        for combination in itertools.product(*(range(minimums[k], minimums[k] + sweep)
                                               for k in others)):
            target = list(remaining)
            for k, count in zip(others, combination):
                target[0] -= count * dirs[k][0]
                target[1] -= count * dirs[k][1]

            found = pair(dirs[i], dirs[j], tuple(target))
            if not found:
                continue

            steps = [0] * len(dirs)
            steps[i], steps[j] = found
            for k, count in zip(others, combination):
                steps[k] = count
            if all(s >= m for s, m in zip(steps, minimums)):
                return steps
    return None


def _legs(dirs: list[tuple], displacement: tuple, first_run_min: int):
    """Turn a candidate sequence of directions into (direction, steps) runs, or None.

    Every corner eats one incoming plus one outgoing step of displacement, so the
    straight runs only have to cover what is left over.
    """
    spent = [0, 0]
    for a, b in zip(dirs, dirs[1:]):
        spent[0] += a[0] + b[0]
        spent[1] += a[1] + b[1]

    minimums = [0] * len(dirs)
    minimums[0] = first_run_min
    minimums[-1] = max(minimums[-1], 1)   # arrive at the station going straight

    steps = _solve(dirs, (displacement[0] - spent[0], displacement[1] - spent[1]), minimums)
    return list(zip(dirs, steps)) if steps else None


def _alignment(vector: tuple, direction: tuple) -> float:
    """Cosine of the angle between a direction and where we are trying to get to."""
    length = math.hypot(*vector)
    return 0.0 if length == 0 else ((direction[0] * vector[0] + direction[1] * vector[1])
                                    / (length * math.hypot(*direction)))


def route(start: tuple, end: tuple, entry: tuple | None, onward: tuple = (0, 0),
          first_run_min: int = 0) -> list[tuple[tuple, int]] | None:
    """Connect two stations with straight runs and corners.

    `entry` is the direction the line is already travelling, which it has to keep
    for at least the first node - a line cannot turn on the spot. Where it goes
    after that is free, so the exit direction is chosen rather than imposed. The
    last run always has at least one step, which is what keeps the arriving
    station off a corner and lets the next edge pick up cleanly.

    Sequences are tried shortest first, so a straight run wins over a route that
    wanders. `onward` points at the station after this one: preferring an exit
    that already faces that way keeps the next edge from having to turn back on
    itself, which is what makes the deep searches rare.

    Returns runs as (direction, steps); a change of direction between two of them
    implies a corner node. None if nothing fits.
    """
    displacement = (end[0] - start[0], end[1] - start[1])
    frontier = [[entry]] if entry else [[d] for d in COMPASS.values()]

    def score(legs):
        # Shortest drawn line first. That is what puts diagonals on the map: a
        # 45 degree run covers a corner in fewer steps than a staircase of
        # axial ones, which is exactly the preference Beck's map shows.
        drawn = sum(steps for _, steps in legs)
        hug = sum(steps * _alignment(displacement, d) for d, steps in legs)
        return (len(legs), drawn, -(hug + 3 * _alignment(onward, legs[-1][0])))

    # Four turns is a full reversal, which is as far as a line ever has to bend.
    for _ in range(MAX_TURNS + 1):
        solved = [legs for legs in
                  (_legs(dirs, displacement, first_run_min) for dirs in frontier)
                  if legs is not None]
        if solved:
            return min(solved, key=score)
        frontier = [dirs + [nxt] for dirs in frontier for nxt in COMPASS.values()
                    if legal_turn(dirs[-1], nxt)]
    return None


def chain(path: list[tuple[int, int]], names: list[str]) -> list[dict]:
    """Build one branch's node list: stations on straight runs, corners between them.

    Raises if two stations cannot be joined, naming them, so a layout that the
    library would reject fails the build instead of the browser.
    """
    nodes = [{"coords": list(path[0]), "name": names[0]}]
    position, arriving = path[0], None

    for i in range(len(path) - 1):
        # The opening segment of a branch has to be a plain compass step: the
        # library reads it to find the direction the line starts out in.
        onward = ((path[i + 2][0] - path[i + 1][0], path[i + 2][1] - path[i + 1][1])
                  if i + 2 < len(path) else (0, 0))
        legs = route(path[i], path[i + 1], arriving, onward,
                     first_run_min=1 if arriving is None else 0)
        if legs is None:
            raise ValueError(
                f"no octilinear route from {path[i]} to {path[i + 1]} "
                f"({names[i]} -> {names[i + 1]})"
            )

        previous = None
        for direction, steps in legs:
            if previous is not None:
                position = (position[0] + previous[0] + direction[0],
                            position[1] + previous[1] + direction[1])
                nodes.append({"coords": list(position)})
            if steps:
                position = (position[0] + steps * direction[0],
                            position[1] + steps * direction[1])
                nodes.append({"coords": list(position)})
            previous = direction

        if position != path[i + 1]:
            raise ValueError(f"route drifted: reached {position}, wanted {path[i + 1]}")
        nodes[-1]["name"] = names[i + 1]
        arriving = legs[-1][0]

    return nodes


def opposed(a: str, b: str) -> bool:
    """Whether two compass bearings point in exactly opposite directions."""
    return COMPASS[a] == (-COMPASS[b][0], -COMPASS[b][1])


def directions(nodes: list[dict]) -> list[str]:
    """Return the direction the line is travelling at each node.

    This is a port of populateLineDirections from d3-tube-map's curve.js, so it
    raises on exactly what the library would raise on - which is the point: a
    geometry mistake fails the build here rather than the browser. The
    directions it works out are worth keeping, because the library decides where
    to put an interchange marker from them.
    """
    found, direction = [], None

    for i in range(1, len(nodes)):
        previous, current = nodes[i - 1]["coords"], nodes[i]["coords"]
        step = (current[0] - previous[0], current[1] - previous[1])

        if step == (0, 0):
            raise ValueError(f"repeated coordinates at {current}")

        if i == 1:
            direction = bearing(step)
            if direction is None:
                raise ValueError(
                    f"opening segment {previous} -> {current} is not a compass direction")
            found.append(direction)          # the first node takes the opening bearing
        elif not parallel(COMPASS[direction], step):
            if (abs(step[0]), abs(step[1])) not in {(1, 1), (1, 2), (2, 1)}:
                raise ValueError(f"cannot draw a corner between {previous} and {current}")
            vector = COMPASS[direction]
            direction = bearing((step[0] - vector[0], step[1] - vector[1]))
            if direction is None:
                raise ValueError(f"corner at {current} turns further than 45 degrees")

        found.append(direction)

    return found


def validate(nodes: list[dict]) -> None:
    """Raise if the library would refuse to draw this branch."""
    directions(nodes)


def cells(nodes: list[dict]):
    """Yield every grid cell a branch's line passes through, not just its nodes.

    Nodes only appear where a run starts, ends or turns, so a label placed by
    node positions alone happily lands on top of a long straight stretch of line.
    """
    for before, after in zip(nodes, nodes[1:]):
        (x, y), (to_x, to_y) = before["coords"], after["coords"]
        steps = max(abs(to_x - x), abs(to_y - y))
        for step in range(steps + 1):
            yield (x + round((to_x - x) * step / steps),
                   y + round((to_y - y) * step / steps))


def label_positions(stations: dict[str, tuple[int, int]], occupied: set,
                    reach: int = 5) -> dict[str, str]:
    """Point each station's label at the emptiest patch of grid around it.

    Everything already drawn counts as clutter - other stations, and the lines
    themselves - so labels tend to fall on the outside of a curve rather than
    across the line they belong to. Near misses count for more than distant ones.
    """
    positions = {}
    for uid, (x, y) in stations.items():
        def crowding(vector):
            total = 0
            for step in range(1, reach + 1):
                spot = (x + vector[0] * step, y + vector[1] * step)
                for across in ((0, 0), (vector[1], -vector[0]), (-vector[1], vector[0])):
                    if (spot[0] + across[0], spot[1] + across[1]) in occupied:
                        total += reach + 1 - step
            return total

        positions[uid] = min(COMPASS, key=lambda name: (crowding(COMPASS[name]), name))
    return positions
