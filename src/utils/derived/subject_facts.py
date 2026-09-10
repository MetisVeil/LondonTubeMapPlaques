"""Look up what Wikipedia and Wikidata already know about each plaque's subject.

Open Plaques' roles are free text - 1,091 different spellings of a job, growing
every time a plaque is added - so a category written against them is a list
somebody has to keep extending by hand. These two sources are the way out of
that:

  * Wikidata's occupations are a controlled vocabulary with a subclass tree, so
    asking for `writer` also answers for poets, novelists and playwrights, and
    for whatever occupation is coined next year.
  * Wikipedia's categories are the crowd's own taxonomy, and cover what Wikidata
    deliberately leaves sparse - who was a refugee, who was part of a movement.

`build(conn)` stores both as plain (subject, kind, value) rows, keyed by the
Wikipedia URL the plaque already carries. `derived.categories` matches against
them; nothing else needs to know where a fact came from.
"""

import sqlite3
import time
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TABLE = "subject_facts"

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Wikipedia asks for a descriptive agent and returns 429 to anything anonymous
# that looks like a scraper. This is a monthly job, so it can afford to be slow.
HEADERS = {"User-Agent": "tube_map/1.0 (https://github.com/metisveil/tube_map)"}
PAUSE_S = 0.5

# Titles per Wikipedia request, and items per Wikidata query. Both are the
# documented ceiling for anonymous callers, give or take.
TITLE_BATCH = 50
ITEM_BATCH = 200

# The occupations a category is allowed to ask for, and the word the markdown
# uses for each. Every one is the top of a subclass tree rather than a leaf:
# `Q639669 musician` is what makes a jazz saxophonist a musician here.
#
# `scientist` is the loose one - Wikidata's tree under it reaches historians and
# economists - so prefer a narrower word where one fits.
OCCUPATIONS = {
    "Q36180": "writer", "Q1930187": "journalist", "Q42973": "architect",
    "Q1028181": "painter", "Q1281618": "sculptor", "Q639669": "musician",
    "Q177220": "singer", "Q36834": "composer", "Q33999": "actor",
    "Q245068": "comedian", "Q5716684": "dancer", "Q2066131": "athlete",
    "Q81096": "engineer", "Q205375": "inventor", "Q901": "scientist",
    "Q39631": "physician", "Q47064": "military personnel",
    "Q15253558": "activist", "Q82955": "politician", "Q11900058": "explorer",
}


def session() -> requests.Session:
    """A session that backs off rather than giving up on a rate limit."""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))))
    return s


def subjects(conn: sqlite3.Connection) -> dict[str, str]:
    """The Wikipedia page title behind every plaque that names one, by URL."""
    rows = conn.execute(
        "SELECT DISTINCT lead_subject_wikipedia FROM current_plaques "
        "WHERE lead_subject_wikipedia IS NOT NULL")
    return {url: unquote(url.rsplit("/", 1)[-1]).replace("_", " ") for (url,) in rows}


def from_wikipedia(http: requests.Session, titles: list[str]) -> tuple[dict, dict]:
    """Fetch each title's Wikidata id and its categories.

    A title in the plaque data can be an old name for a page, so the reply is
    followed back through `normalized` and `redirects` to whatever the page is
    called now - that is the name the categories arrive under.
    """
    items, categories, canonical = {}, {}, {}

    for start in range(0, len(titles), TITLE_BATCH):
        batch = titles[start:start + TITLE_BATCH]
        params = {"action": "query", "format": "json", "redirects": "1",
                  "prop": "pageprops|categories", "ppprop": "wikibase_item",
                  "cllimit": "max", "clshow": "!hidden", "titles": "|".join(batch)}
        renamed = {}

        while True:
            reply = http.get(WIKIPEDIA_API, params=params, timeout=60)
            reply.raise_for_status()
            body = reply.json()
            query = body.get("query", {})

            for hop in query.get("normalized", []) + query.get("redirects", []):
                renamed[hop["from"]] = hop["to"]

            for page in query.get("pages", {}).values():
                title = page["title"]
                if item := page.get("pageprops", {}).get("wikibase_item"):
                    items[title] = item
                categories.setdefault(title, []).extend(
                    c["title"].removeprefix("Category:") for c in page.get("categories", []))

            if "continue" not in body:
                break
            params.update(body["continue"])

        for title in batch:
            canonical[title] = follow(title, renamed)
        time.sleep(PAUSE_S)

    return {t: items.get(c) for t, c in canonical.items()}, \
           {t: categories.get(c, []) for t, c in canonical.items()}


def follow(title: str, renamed: dict[str, str], limit: int = 5) -> str:
    """Walk a title through however many renames the reply reported."""
    for _ in range(limit):
        if title not in renamed:
            break
        title = renamed[title]
    return title


def from_wikidata(http: requests.Session, item_ids: list[str]) -> dict[str, set[str]]:
    """Which of `OCCUPATIONS` each item has, following the subclass tree up.

    P106 is the occupation, P279* is "or anything that is a kind of it", so one
    query covers every specialisation of the twenty words we offer.
    """
    targets = " ".join("wd:" + q for q in OCCUPATIONS)
    found: dict[str, set[str]] = {}

    for start in range(0, len(item_ids), ITEM_BATCH):
        values = " ".join("wd:" + q for q in item_ids[start:start + ITEM_BATCH])
        query = ("SELECT DISTINCT ?item ?occupation WHERE { "
                 f"VALUES ?item {{ {values} }} VALUES ?occupation {{ {targets} }} "
                 "?item wdt:P106/wdt:P279* ?occupation }")

        reply = http.post(WIKIDATA_SPARQL, data={"query": query, "format": "json"},
                          headers={"Accept": "application/sparql-results+json"}, timeout=180)
        reply.raise_for_status()

        for row in reply.json()["results"]["bindings"]:
            item = row["item"]["value"].rsplit("/", 1)[-1]
            occupation = row["occupation"]["value"].rsplit("/", 1)[-1]
            found.setdefault(item, set()).add(OCCUPATIONS[occupation])
        time.sleep(PAUSE_S)

    return found


def store(conn: sqlite3.Connection, facts: list[tuple[str, str, str]], table: str = TABLE) -> None:
    """Replace the table with `facts`."""
    conn.executescript(f'''
        DROP TABLE IF EXISTS "{table}";
        CREATE TABLE "{table}" (
            subject TEXT NOT NULL,   -- the plaque's lead_subject_wikipedia URL
            kind    TEXT NOT NULL,   -- 'occupation' or 'wikipedia'
            value   TEXT NOT NULL,
            PRIMARY KEY (subject, kind, value)
        );
    ''')
    conn.executemany(f'INSERT OR IGNORE INTO "{table}" VALUES (?, ?, ?)', facts)
    conn.execute(f'CREATE INDEX "{table}_lookup" ON "{table}" (kind, value)')
    conn.commit()


def build(conn: sqlite3.Connection) -> dict:
    """Look up every subject and store what came back.

    The table is only replaced once the whole lookup has succeeded: a rebuild
    that runs while Wikipedia is unreachable should leave last month's facts in
    place rather than emptying every category that depends on them.
    """
    pages = subjects(conn)
    http = session()

    items, categories = from_wikipedia(http, sorted(set(pages.values())))
    occupations = from_wikidata(http, sorted({i for i in items.values() if i}))

    facts = []
    for url, title in pages.items():
        for occupation in occupations.get(items.get(title), ()):
            facts.append((url, "occupation", occupation))
        for category in categories.get(title, ()):
            facts.append((url, "wikipedia", category))

    if not facts:
        return {"looked up": 0, "kept": "existing facts - nothing came back"}

    store(conn, facts)
    return {
        "subjects": f"{len(pages)} with a Wikipedia page",
        "identified": f"{sum(1 for i in items.values() if i)} found on Wikidata",
        "occupations": sum(len(v) for v in occupations.values()),
        "categories": sum(len(v) for v in categories.values()),
    }


if __name__ == "__main__":
    renamed = {"Old Name": "Newer Name", "Newer Name": "Current Name"}
    assert follow("Old Name", renamed) == "Current Name"
    assert follow("Untouched", renamed) == "Untouched"
    assert follow("Old Name", {"Old Name": "Old Name"}) == "Old Name"  # self-loop
    print("ok")
