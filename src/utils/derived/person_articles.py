"""Keep the opening of each plaque subject's Wikipedia article, and their portrait.

The station panel says what a station is; this is the same thing for the person
a themed map puts on one, so that clicking either gives the same kind of answer.

Simpler than `station_articles`, because there is nothing to guess: a plaque
already carries the subject's Wikipedia URL, and the title is the last segment of
it. What the lookup is for is the content - the opening paragraph, and the lead
image, which is also the portrait `derived.categories` puts on the map.

Fetching the portrait here rather than page by page is most of the point. The
categories used to scrape one Wikipedia page per person, which is over a
thousand requests a build; this asks for fifty at a time. The scrape stays as
the fallback, because `pageimages` will not serve a non-free image and a few
hundred subjects have nothing else - Arthur Haynes among them.
"""

import sqlite3
from urllib.parse import unquote

from ..sources import wikipedia

TABLE = "person_articles"


def subjects(conn: sqlite3.Connection) -> dict[str, str]:
    """The Wikipedia page title behind every plaque that names one, by URL."""
    return {url: unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
            for (url,) in conn.execute(
                "SELECT DISTINCT lead_subject_wikipedia FROM current_plaques "
                "WHERE lead_subject_wikipedia IS NOT NULL")}


def store(conn: sqlite3.Connection, rows: list[tuple], table: str = TABLE) -> None:
    """Replace the table with `rows`."""
    conn.executescript(f'''
        DROP TABLE IF EXISTS "{table}";
        CREATE TABLE "{table}" (
            subject TEXT PRIMARY KEY,   -- plaques.lead_subject_wikipedia, as stored
            title   TEXT NOT NULL,      -- the article it resolved to
            url     TEXT NOT NULL,
            image   TEXT,               -- the lead portrait, if it has a free one
            summary TEXT                -- the article's opening paragraph
        );
    ''')
    conn.executemany(f'INSERT OR REPLACE INTO "{table}" VALUES (?, ?, ?, ?, ?)', rows)
    conn.commit()


def portraits(conn: sqlite3.Connection, table: str = TABLE) -> dict[str, str]:
    """The portrait for each subject that has one, by the URL the plaque carries.

    Read back by `derived.categories`, which runs after this and would otherwise
    go and fetch the same images one page at a time.
    """
    try:
        return {url: image for url, image in conn.execute(
            f'SELECT subject, image FROM "{table}" WHERE image IS NOT NULL')}
    except sqlite3.OperationalError:      # no build has written it yet
        return {}


def build(conn: sqlite3.Connection) -> dict:
    """Look up every subject and store what came back.

    Like `subject_facts`, the table is only replaced once the lookup has
    succeeded: a rebuild that runs while Wikipedia is unreachable should leave
    last month's articles in place rather than emptying every panel.
    """
    pages = subjects(conn)
    articles = wikipedia.fetch_articles(sorted(set(pages.values())))

    rows = []
    for url, title in pages.items():
        article = articles.get(title)
        if not article or article["disambiguation"]:
            continue
        rows.append((url, article["title"], article["url"], article["image"],
                     article["extract"][0] if article["extract"] else None))

    if not rows:
        return {"subjects": len(pages), "kept": "existing articles - nothing came back"}

    store(conn, rows)
    return {
        "subjects": f"{len(rows)} of {len(pages)} matched to an article",
        "portraits": sum(1 for row in rows if row[3]),
        "summaries": sum(1 for row in rows if row[4]),
    }


if __name__ == "__main__":
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE current_plaques (lead_subject_wikipedia TEXT);
        INSERT INTO current_plaques VALUES
            ('https://en.wikipedia.org/wiki/Ada_Lovelace'),
            ('https://en.wikipedia.org/wiki/Ada_Lovelace'),
            ('https://en.wikipedia.org/wiki/Jimi_Hendrix'),
            (NULL);
    """)
    assert subjects(conn) == {
        "https://en.wikipedia.org/wiki/Ada_Lovelace": "Ada Lovelace",
        "https://en.wikipedia.org/wiki/Jimi_Hendrix": "Jimi Hendrix"}

    # A percent-encoded title, which is how an apostrophe reaches the dump.
    conn.execute("INSERT INTO current_plaques VALUES "
                 "('https://en.wikipedia.org/wiki/Bridget_O%27Connor')")
    assert subjects(conn)["https://en.wikipedia.org/wiki/Bridget_O%27Connor"] \
        == "Bridget O'Connor"

    assert portraits(conn) == {}         # no table yet
    store(conn, [("u", "Ada Lovelace", "url", "portrait.jpg", "Ada was."),
                 ("v", "Nobody", "url", None, "Nobody was.")])
    assert portraits(conn) == {"u": "portrait.jpg"}
    print("ok")
