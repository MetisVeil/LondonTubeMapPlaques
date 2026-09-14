# London Tube Map: Blue Plaques

[![View the live map](https://img.shields.io/badge/View_the_live_map-0019A8?style=for-the-badge&logo=githubpages&logoColor=white)](https://metisveil.github.io/LondonTubeMapPlaques/)

London's [blue plaques](https://www.english-heritage.org.uk/visit/blue-plaques/)
mark the buildings where notable people lived and worked — over 1,000 across the
capital, from a scheme begun in 1866 and now run by English Heritage. They are
scattered in a way that makes them hard to visit deliberately.

The Underground — opened 1863, now 11 lines and 272 stations — is the shape
Londoners already use to think about the city. This project puts the two
together: every plaque is attributed to the stations nearest it, and the result
is drawn as a tube map. Data from [Open Plaques](https://openplaques.org/posts/data)
and [TfL](https://api.tfl.gov.uk); the diagram is generated, not TfL's.

## Running it

The virtualenv lives at the repo root, the code in `src/`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Download the feeds, version changes into SQLite, rebuild views, write the map
PYTHONPATH=src .venv/bin/python -m handler.main

# View it
cd src/web && python3 -m http.server 8000
```

The build writes `src/data/london.db`, which is gitignored — so run it after
cloning. Use the venv's `python` and `-m` (not a file path), so `src/` lands on
`sys.path` rather than `src/handler/`.

The JSON under `src/web/` is generated but committed, so <http://localhost:8000>
works straight from a clone without a build. Drag to pan, scroll to zoom, click
a station for what the map knows about it — the station itself, or the person a
themed map has put there.

Options, top left, holds two switches and a link to `src/web/methodology.html`,
a standalone page explaining where the data comes from, how a plaque is
attributed to a station, how the diagram is drawn, how the people are chosen and
how the calendar picks the map that opens:

* **Overground lines** — off to start with. Hiding the six Overground lines
  takes with them the 83 stations only they call at, and drops the interchange
  rings from 23 tube stops whose only connection was one of them.
* **Dark mode** — on by default; the toggle is the way back to light. TfL's
  line colours mostly survive the flip, bar the few too dark to make out and
  the Northern line's black, which is ink rather than a colour. The choice is
  recorded in `localStorage` so the methodology page opens in the same theme;
  the map itself still starts dark either way.

The page is plain HTML — no build step, no node_modules — loading
[d3-tube-map](https://github.com/johnwalley/d3-tube-map) from a CDN. It pins
d3 v6: d3-tube-map 1.5.0 depends on `d3: "5 - 6"` and breaks on v7.

## Layout

```
src/
├── handler/main.py                entry point: every source, then the views, then the map
├── layout_overrides.json          hand-tuning, applied on top of the generated layout
├── role_categories.md             the themed maps on offer — edit this to add one
├── utils/
│   ├── db.py                      versioned (SCD type 2) SQLite storage
│   ├── derived/
│   │   ├── distances.py           haversine distances between coordinates
│   │   ├── layout.py              the octilinear geometry: place, route, validate, label
│   │   ├── mapdata.py             assembles src/web/map.json
│   │   ├── categories.py          role_categories.md -> the themed maps
│   │   ├── station_articles.py    each station's own Wikipedia article
│   │   ├── person_articles.py     each plaque subject's, and their portrait
│   │   └── plaque_stations.py     which station each plaque belongs to
│   ├── sources/
│   │   ├── plaques.py             Open Plaques London dump: find, download, load
│   │   ├── tfl_stations.py        TfL station data: names, coordinates
│   │   ├── tfl_network.py         TfL route sequences: which stations each line calls at
│   │   └── wikipedia.py           portraits, station photographs, article openings
│   └── sql/                       derived views, reapplied on every build
└── web/                           the map, as a static page
```

## Where the shape comes from

The 19 lines, 421 stations and 580 stretches of track are live TfL data. The
*positions* are not: the real Tube map is a copyrighted design, licensed through
[Pindar Creative](https://www.pindarcreative.co.uk/tfl-map-licensing-packages.html)
on terms forbidding any alteration — renaming stations included — so this draws
its own diagram of the same network.

d3-tube-map will not accept an arbitrary polyline: every segment must run along
one of the eight compass points, and a change of direction needs its own node
one incoming plus one outgoing step past the end of the run. So
`utils/derived/layout.py` earns the geometry in four passes:

* **place** — project each station, then pull it towards the centre by the
  square root of its distance. Beck's map gives the centre most of the room and
  squeezes the ends of the lines; without that compression the grid would need
  ~1000 cells to keep Covent Garden and Leicester Square apart, and would be
  mostly empty. Stations then take an integer cell, busiest first, so
  interchanges land where they asked to.
* **route** — join each pair with straight runs and corners, trying short
  sequences before long ones and preferring the shortest drawn line, which is
  what puts the diagonals in. A line leaves a station the way it arrived, so
  turning happens in between; four corners is a full reversal, the worst any
  line asks for.
* **validate** — a port of the library's own `populateLineDirections`, run over
  every branch before the JSON is written, so a geometry mistake fails the build
  rather than the browser.
* **label** — measure each name into a rectangle and score it on what it would
  cover. Landing on another name costs more than crossing a line: two names on
  top of each other are unreadable, where a name over a line is merely untidy.
  Crowded stations choose first, then the worst few are re-placed.

The build reports `0 overlapping, 56 close to a line`. The overlap count is
exact; the line count is pessimistic, treating a line as filling every cell it
crosses.

Improve the generated layout in `src/layout_overrides.json`, applied last and so
surviving a rebuild. Stations are keyed as `map.json` keys them, and
`shiftNormal` holds two lines apart where they share track:

```json
{
  "stations": { "Bank": { "coords": [104, -66], "labelPos": "E" } },
  "lines":    { "circle": { "shiftNormal": 1 } }
}
```

## The plaques

`sources/plaques.py` finds the latest London dump linked from
[Open Plaques](https://openplaques.org/posts/data) — 3,781 plaques, 3,675
positioned — and versions it into the `plaques` table.

`derived/plaque_stations.py` attributes each to the **three** nearest stations:
one call measures every station against every plaque, 421 by 3,675, and the
ranking is a sort down each column. Anything over a kilometre out is dropped,
keeping 3,238. Three rather than one, because the closest station is not always
the one a person would use, and a map wanting a *different* plaque at every
station needs somewhere to fall back to.

The `station_plaques` view joins the three, and is the one to query:

```sql
SELECT station_name, person, role, distance_m
FROM station_plaques
WHERE rank = 1 AND role = 'composer'
ORDER BY distance_m;
```

`role` is `lead_subject_primary_role`, filled in for 3,186 plaques and running
to 1,091 distinct values — poet, novelist, actor, composer, singer — which is
what a map of musicians or politicians gets built from.

The view drops plaques with nobody named or no role. Plenty of what survives
commemorates a place or a thing rather than a person; `subject_type` separates
them, where `man` and `woman` are the people. Prefer it to `gender`, which
records 95 men as `object`. An absent value in the dump is an empty field, which
would reach SQLite as `''` and quietly defeat every `IS NULL` test, so the
loader nulls those.

## The themed maps

The triangle at the top right opens the list — Music, Women of London,
Politician & government. Picking one relabels every station it has somebody for
and fades the rest.

That list comes from `src/role_categories.md`, and **that file is the only thing
to edit to add a category** — no code changes. Three levels:

```
## Arts & Literature          a group, which also becomes a map of its own
- Music                       a category within it
  - composer                  a role, matched against the plaque's
  - subject_type: woman       or a column and value, for anything not a role
```

Roles match case-insensitively with curly apostrophes folded to straight ones —
otherwise an easy way to list a role that silently matches nothing. A
`field: value` entry may use `subject_type`, `gender`, `colour` or `area`;
anything else is rejected by name rather than ignored.

Each group gets a combined map as well as its parts, since several categories
are thin alone but good together. Only `man` and `woman` are included, so
plaques for churches, public houses and bomb sites stay out.

A station shows one name, and a person appears once. The nearest pairings go
first and the rest fall back to the second or third station recorded for that
plaque — which is what those were kept for, so Music fills 100 stations where
only 80 have a musician as their single nearest plaque.

The result goes to `src/web/categories.json`, separate from the map so adding a
category does not rewrite it, and fetched only when the menu is first opened.

## What the panel says

Clicking anything on the map answers the same way, because from the outside it
is the same click: a photograph, a name, a line saying what it is, Wikipedia's
opening paragraph and a link to the rest. What changes is only which of the two
things a station is at that moment — under a themed map it stands for whoever
the theme put there, and everywhere else, including at every station a theme has
nobody for, it is just itself.

For a person that is `derived/person_articles.py`, and it is the easy half: the
plaque already carries the subject's Wikipedia URL, so there is nothing to
resolve and the lookup is only for the content. 2,047 of the 2,049 subjects the
plaques name have an article; the 1,159 who reach a themed map go to
`src/web/people.json`, 500KB.

That module also fetches the **portraits**, which is most of the point of it.
`derived/categories.py` used to scrape one Wikipedia page per person — over a
thousand requests a build — and now reads them out of the table fifty at a time.
The scrape survives as the fallback, because `pageimages` will not serve a
non-free image and a few hundred subjects have nothing else; it runs for about
350 people rather than all of them, and a category rebuild went from minutes to
24 seconds.

### Finding a station's article

The station half is the hard one. Wikipedia files London's stations under
three conventions — `Oval tube station`, `Stratford station`, `Abbey Road DLR
station` — and the bare name usually belongs to the district instead, so
guessing is both easy and quiet: `Aldgate station` is a closed station in
Somerset. `derived/station_articles.py` settles it two ways.

[List of London Underground stations][list] names the article for every one of
the 272 tube stations, which is authoritative and covers the ambiguous ones. Its
table is keyed by the station's display name, and where a name appears twice —
the two Edgware Roads, and the pairs of platforms Wikipedia splits Hammersmith
and Paddington into — the article naming the most of the lines actually through
that station wins. The list is consulted only for stations the Underground calls
at: Bethnal Green and West Hampstead each name a tube station *and* a separate
Overground one, which has an article of its own.

[list]: https://en.wikipedia.org/wiki/List_of_London_Underground_stations

Everything else — the Overground, the DLR, the Elizabeth line — is tried against
each convention in turn, and the first candidate that both exists and reads as a
station is taken. Two tests do the filtering. Requiring "station" in the resolved
title keeps the districts out: `Cyprus DLR station` is a stop on the map,
`Cyprus` is in the Mediterranean. Rejecting Wikipedia's disambiguation pages
keeps out the ones that exist *because* the name is shared — `Woolwich station`
is one sentence saying it may refer to two others, and `Woolwich railway
station`, a candidate further down, is the Elizabeth line stop with a photograph
on it.

All 421 stations resolve, every one with a photograph. One call to
`wikipedia.fetch_articles` does the lookup and the content together — a title
with no article behind it is simply absent from the reply, which makes the same
request the test of whether a guessed name exists.

The result goes to `src/web/stations.json`, 300KB. Like `people.json` it is
fetched only on demand — the two together on the first click, since neither is
any use to a map nobody has clicked yet, and either one failing leaves the panel
a paragraph short rather than breaking it.

## The database

`src/data/london.db` keeps the full history of every row. A version records the
load it first appeared in (`valid_from_load`) and the load that replaced it
(`valid_to_load`), so re-running against an unchanged feed writes nothing at
all. Each table has a `current_<table>` view of live rows, and `loads` records
every ingestion run.

Two sources feed it. `tfl_stationdata` is the zip of CSVs TfL publish, giving
station names and coordinates; the extracted CSVs are deleted once loaded.
`tfl_network` is the Unified API, giving lines and their running order.
Unregistered callers get 50 requests a minute and a build makes about 40, so two
builds in quick succession will pause and retry; `TFL_APP_KEY` avoids the wait.

The views in `utils/sql/` join the two. The wrinkle: the API calls Euston
`940GZZLUEUS` while the station data files it under its hub code `HUBEUS`.
Falling back to `topMostParentId` is what resolves every stop on all 19 lines
onto a row with coordinates.

## Deployment

`.github/workflows/deploy.yml` publishes `src/web/` to GitHub Pages at
<https://metisveil.github.io/LondonTubeMapPlaques/>. A push to
`main` deploys what is committed; a monthly schedule (04:00 UTC on the 1st) and
`workflow_dispatch` refresh the feeds first, commit the regenerated JSON with
`[skip ci]`, then deploy. Since `src/data/` is gitignored, the workflow caches
`london.db` between runs so the version history rolls forward instead of
resetting each time.

## Credits

The page carries these under Options, top left; in full:

* **[Open Plaques](https://openplaques.org)** — the reason this project exists.
  It started from noticing how many blue plaques London has and how scattered
  they are to go and see; Open Plaques had already catalogued thousands, and the
  tube map was the shape that let all of them be looked at together. The data is
  public domain under [PDDL 1.0](https://opendatacommons.org/licenses/pddl/1-0/)
  and they ask for no credit at all, which is all the more reason to give it.
  Being a volunteer catalogue rather than a company, the useful way to support
  it is not money but [adding a missing plaque](https://openplaques.org/plaques/new),
  a photo to one unphotographed, or
  [flagging an error](https://openplaques.org/posts/contact).
* **[d3-tube-map](https://github.com/johnwalley/d3-tube-map)** by John Walley,
  BSD-3-Clause — draws tube maps in the London Underground style, and is what
  makes a diagram of one's own possible rather than a re-drawing of TfL's.
* **Transport for London** — Powered by TfL Open Data. Contains OS data ©
  Crown copyright and database rights 2016, and Geomni UK Map data © and
  database rights 2019. The [Unified API](https://api.tfl.gov.uk) and station
  data feed supply every line, stop and coordinate here, under the Open
  Government Licence v2.0 with TfL's own amendments.
* **[D3](https://d3js.org)** by Mike Bostock — v6.7.0, BSD-3-Clause, which is
  what d3-tube-map 1.5.0 wants. (D3 relicensed to ISC at v7.)
* **Wikipedia** — the portrait beside each name, from the page's infobox; each
  station's own photograph and the opening of its article; and the article
  behind a calendar occasion. Those images carry their own licences, which vary
  image by image and are not all free for re-use; anyone publishing this map
  publicly should check rather than assume.
