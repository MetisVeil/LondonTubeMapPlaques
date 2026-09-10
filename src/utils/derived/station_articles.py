"""Find each station's own Wikipedia article, and keep its opening and its photo.

The map's other panel is about the person a station stands in for; this is the
one for the station itself, which until now the page could say nothing about
beyond a Naptan id and a pair of coordinates.

The work is almost all in the title. Wikipedia files London's stations under
three different conventions - "Oval tube station", "Stratford station", "Abbey
Road DLR station" - and the bare name is usually taken by the district, so
guessing wrong is easy and quiet. Two things settle it:

  * `List of London Underground stations` names the article for every one of the
    272 tube stations, which is authoritative and covers the ambiguous ones -
    "Aldgate station" is a closed station in Somerset.
  * everything else - the Overground, the DLR, the Elizabeth line - is tried
    against the naming conventions in turn, and the first candidate that both
    exists and reads as a station wins.

`build(conn)` resolves them all, fetches the opening paragraph and lead image in
batches, and stores the lot. `derived.mapdata` writes it out for the page.
"""

import collections
import re
import sqlite3

from ..sources import wikipedia

TABLE = "station_articles"

# A couple of stations carry a destination in their name - "Custom House for
# ExCel" - which Wikipedia does not always repeat in the article title.
FOR = re.compile(r"\s+for\s+.*$")

# The naming conventions, in the order they are worth trying. Most of them
# redirect to each other, so the order only decides the handful of names where
# more than one lands somewhere real:
#
#   * "railway station" before "station", because everything left once the tube
#     list has been consulted is on the National Rail side of things, and the
#     shorter name is the one that tends to be a disambiguation page. It is what
#     gets West Hampstead its own article rather than the survey of all three
#     stations in West Hampstead.
#   * a DLR station asks to be one outright, because "Abbey Road station" is a
#     National Rail station in Sussex where "Abbey Road DLR station" can only be
#     the one on the map.
SUFFIXES = [" railway station", " station", " tube station", " DLR station"]
DLR_SUFFIXES = [" DLR station", " station", " railway station"]

# Tried after all of those, for the stations whose plain name belongs to
# somewhere else entirely: there are Sydenhams in Belfast and Melbourne, and the
# undecorated title is the page that lists them.
IN_LONDON = [" railway station (London)", " station (London)"]


def stations(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every station, with the lines through it and the modes they run on."""
    found = {uid: {"name": name, "lines": set(), "modes": set()}
             for uid, name in conn.execute(
                 "SELECT uid, name FROM tfl_network_stations")}

    for uid, line, mode in conn.execute("""
            SELECT DISTINCT s.station_uid, l.name, l.mode
            FROM tfl_network_stops s JOIN current_lines l ON l.line_id = s.line_id"""):
        if uid in found:
            found[uid]["lines"].add(line)
            found[uid]["modes"].add(mode)

    return found


def candidates(station: dict, listed: dict[str, list[str]]) -> list[str]:
    """The article names to try for a station, best guess first.

    The list is consulted only for a station the Underground actually calls at,
    because a name on it is not proof of anything: Bethnal Green and West
    Hampstead each name a tube station and a separate Overground one a few
    streets away, and the Overground ones have articles of their own.

    Where the list names more than one article for a station - the two Edgware
    Roads, and the pairs of platforms that Wikipedia splits Hammersmith and
    Paddington into - the one naming the most of the lines actually through it
    goes first.
    """
    if "tube" in station["modes"] and (named := listed.get(station["name"])):
        lines = {line.lower() for line in station["lines"]}
        return sorted(named, key=lambda title: -sum(
            line in title.lower() for line in lines))

    plain = FOR.sub("", station["name"]).strip()
    order = DLR_SUFFIXES if station["modes"] == {"dlr"} else SUFFIXES
    return [plain + suffix for suffix in order + IN_LONDON]


def choose(tried: list[str], articles: dict[str, dict]) -> dict | None:
    """The first candidate with an article behind it that is about a station.

    Two things to get past. The title test keeps the districts out: "Cyprus
    station" is an article, "Cyprus" is a place in the Mediterranean, and only
    one of them belongs in a panel about a stop on the DLR. The disambiguation
    test keeps out the pages that exist precisely because the name is shared -
    "Woolwich station may refer to:" is a photographless sentence, and the
    Elizabeth line one is a paragraph further on.
    """
    for title in tried:
        article = articles.get(title)
        if article and "station" in article["title"].lower() \
                and not article["disambiguation"]:
            return article
    return None


def store(conn: sqlite3.Connection, rows: list[tuple], table: str = TABLE) -> None:
    """Replace the table with `rows`."""
    conn.executescript(f'''
        DROP TABLE IF EXISTS "{table}";
        CREATE TABLE "{table}" (
            station_uid TEXT PRIMARY KEY,   -- tfl_network_stations.uid
            title       TEXT NOT NULL,      -- the article it resolved to
            url         TEXT NOT NULL,
            image       TEXT,               -- the lead photograph, if it has one
            summary     TEXT                -- the article's opening paragraph
        );
    ''')
    conn.executemany(f'INSERT OR REPLACE INTO "{table}" VALUES (?, ?, ?, ?, ?)', rows)
    conn.commit()


def build(conn: sqlite3.Connection) -> dict:
    """Resolve every station to an article and store what came back.

    Like `subject_facts`, the table is only replaced once the lookup has
    succeeded: a rebuild that runs while Wikipedia is unreachable should leave
    last month's articles in place rather than emptying every panel.
    """
    known = stations(conn)
    listed = wikipedia.station_titles()
    tried = {uid: candidates(station, listed) for uid, station in known.items()}

    # One lookup for every distinct guess, which is also the test of whether the
    # guess exists at all - a separate existence pass would cost the same again.
    articles = wikipedia.fetch_articles(
        sorted({title for names in tried.values() for title in names}))

    rows, missing = [], []
    for uid, station in known.items():
        article = choose(tried[uid], articles)
        if not article:
            missing.append(station["name"])
            continue
        rows.append((uid, article["title"], article["url"], article["image"],
                     article["extract"][0] if article["extract"] else None))

    if not rows:
        return {"stations": len(known), "kept": "existing articles - nothing came back"}

    store(conn, rows)
    shared = collections.Counter(row[1] for row in rows)

    return {
        "stations": f"{len(rows)} of {len(known)} matched to an article",
        "from the list": f"{sum(1 for s in known.values() if s['name'] in listed)} named "
                         f"by {wikipedia.STATION_LIST!r}",
        "photographs": sum(1 for row in rows if row[3]),
        "summaries": sum(1 for row in rows if row[4]),
        "no article": ", ".join(sorted(missing)) or "none",
        "shared": ", ".join(f"{title} x{n}" for title, n in shared.items() if n > 1) or "none",
    }


if __name__ == "__main__":
    listed = {"Edgware Road": ["Edgware Road tube station (Bakerloo line)",
                               "Edgware Road tube station (Circle, District and "
                               "Hammersmith & City lines)"]}

    bakerloo = {"name": "Edgware Road", "lines": {"Bakerloo"}, "modes": {"tube"}}
    circle = {"name": "Edgware Road", "lines": {"Circle", "District"}, "modes": {"tube"}}
    assert candidates(bakerloo, listed)[0] == listed["Edgware Road"][0]
    assert candidates(circle, listed)[0] == listed["Edgware Road"][1]

    # Same name, no tube through it: the list is not about this station.
    overground = {"name": "Edgware Road", "lines": {"Mildmay"}, "modes": {"overground"}}
    assert candidates(overground, listed)[0] == "Edgware Road railway station"

    excel = {"name": "Custom House for ExCel", "lines": {"DLR"}, "modes": {"dlr"}}
    assert candidates(excel, listed)[:3] == ["Custom House DLR station",
                                             "Custom House station",
                                             "Custom House railway station"]
    assert candidates({"name": "Anerley", "lines": set(), "modes": {"overground"}},
                      listed)[0] == "Anerley railway station"
    assert candidates(excel, listed)[-1] == "Custom House station (London)"

    found = {"Cyprus": {"title": "Cyprus", "disambiguation": False},
             "Woolwich station": {"title": "Woolwich station", "disambiguation": True},
             "Woolwich railway station": {"title": "Woolwich railway station",
                                          "disambiguation": False},
             "Cyprus DLR station": {"title": "Cyprus DLR station", "disambiguation": False}}
    assert choose(["Cyprus", "Cyprus DLR station"], found)["title"] == "Cyprus DLR station"
    assert choose(["Woolwich station", "Woolwich railway station"],
                  found)["title"] == "Woolwich railway station"
    assert choose(["Nowhere station"], found) is None
    print("ok")
