"""Read small bits of Wikipedia: a portrait, and the opening of an article.

`fetch_image` scrapes the lead image out of a page's infobox, which is the
portrait most biographies carry. `fetch_intro` asks the API for the opening
paragraphs of an article, which is how a themed map explains the occasion it is
timed to.

`fetch_articles` does both at once for a whole list of titles - the opening, the
lead image, and where the title finally redirected to - because the stations are
looked up by the hundred and one page at a time would be hundreds of requests.
`station_titles` reads the article names off the list of Underground stations,
which is the one place they are written down rather than guessed at.

The odd one out in this folder: it exposes no SOURCE or load(conn), because
there is no feed to version - `derived.categories` and `derived.station_articles`
call it while they build, and what is worth keeping they store themselves.
"""

import re
import time
from functools import lru_cache
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Wikipedia returns 429 to anonymous traffic that looks like a scraper, and asks
# for a User-Agent that says who to complain to.
HEADERS = {"User-Agent": "tube_map/1.0 (https://github.com/metisveil/tube_map)"}
API = "https://en.wikipedia.org/w/api.php"
STATION_LIST = "List of London Underground stations"

# Titles per request. `extracts` serves 20 at a time and silently truncates a
# longer list, which is the lowest ceiling of the properties asked for here.
TITLE_BATCH = 20
PAUSE_S = 0.2

# Wide enough to fill the panel on a retina screen without being the full-size
# original, which for a station photograph is often several megabytes.
THUMB_PX = 800

# A build asks Wikipedia for a couple of thousand portraits, and the article
# lookups queue up behind them - close enough together to be rate limited.
# Backing off is the difference between a map that explains itself and one that
# quietly cannot.
#
# allowed_methods is the part that is easy to miss: urllib3 retries only the
# methods it considers idempotent, which does not include POST, and the batched
# lookups here are POSTs because a batch of titles outgrows a query string.
# Without this they take the 429 at face value and come back empty.
_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=Retry.DEFAULT_ALLOWED_METHODS | {"POST"})))


def _pick_image_src(image) -> str | None:
    """Pick the largest image URL from an <img> tag."""
    srcset = image.get("srcset") or ""
    candidates = []

    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        url, _, size = part.rpartition(" ")
        if size.endswith("w") and size[:-1].isdigit():
            candidates.append((int(size[:-1]), url))
        elif size.endswith("x"):
            try:
                candidates.append((float(size[:-1]) * 1000, url))
            except ValueError:
                continue

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    return image.get("src")


def _image_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.select_one('meta[property="og:image"]')
    if meta and meta.get("content"):
        return meta["content"]

    image = soup.select_one("table.infobox img") or soup.select_one(".infobox img")
    if not image:
        return None

    src = _pick_image_src(image)
    if not src:
        return None

    return urljoin("https:", src) if src.startswith("//") else src


@lru_cache(maxsize=64)
def fetch_intro(title: str | None, paragraphs: int = 2) -> dict | None:
    """Return the opening of a Wikipedia article, and where to read the rest.

    This is for the occasion a themed map is timed to - Black History Month, the
    Blitz - rather than for a person, so it asks the API for the intro section
    as plain text instead of scraping the page. Redirects are followed, and the
    title that comes back is the one linked to, so "LGBT History Month" in the
    markdown still points at the article it has since been renamed to.
    """
    if not title:
        return None

    try:
        response = _session.get(API, timeout=30, params={
            "action": "query", "format": "json", "redirects": "1",
            "prop": "extracts", "exintro": "1", "explaintext": "1", "titles": title})
        response.raise_for_status()
        page, = response.json()["query"]["pages"].values()
    except (requests.RequestException, KeyError, ValueError):
        return None

    opening = [tidy(p) for p in (page.get("extract") or "").split("\n") if p.strip()][:paragraphs]
    if not opening:
        return None

    return {"title": page["title"], "extract": opening,
            "url": "https://en.wikipedia.org/wiki/" + quote(page["title"].replace(" ", "_"))}


@lru_cache(maxsize=256)
def fetch_image(page_url: str | None) -> str | None:
    """Return the lead image URL from a Wikipedia page, if there is one."""
    if not page_url:
        return None

    try:
        response = requests.get(page_url, timeout=20, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return None

    return _image_from_html(response.text)


# What a stripped pronunciation leaves behind. `explaintext` renders an article
# without its markup, and the IPA that a fifth of the station articles open with
# is markup - so "Aldgate (/ˈɔːldɡeɪt/) is a station" arrives as "Aldgate () is
# a station", and "King's Cross (; also known as...)" keeps the separator that
# used to follow it. Neither is worth showing anybody.
HOLLOW = [(re.compile(r"\s*\(\s*[;,]?\s*\)"), ""),      # ( ), (;)
          (re.compile(r"\(\s*[;,]\s*"), "("),             # (; also known as
          (re.compile(r"\s*[;,]\s*\)"), ")"),             # also known as X;)
          (re.compile(r"\s{2,}"), " ")]


def tidy(text: str) -> str:
    """Clean up after a stripped pronunciation."""
    for pattern, replacement in HOLLOW:
        text = pattern.sub(replacement, text)
    return text.strip()


def article_url(title: str) -> str:
    """The canonical URL of an article, from its title."""
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))


def _follow(title: str, renamed: dict[str, str], limit: int = 5) -> str:
    """Walk a title through however many normalisations and redirects the reply reported."""
    for _ in range(limit):
        if title not in renamed:
            break
        title = renamed[title]
    return title


def fetch_articles(titles: list[str], paragraphs: int = 1) -> dict[str, dict]:
    """Look up many articles at once: the opening, the lead image, and the URL.

    Keyed by the title asked for rather than the one that came back, so a caller
    with a guess at a name can look up the answer under the guess. A title with
    no article behind it is simply absent from the result, which is what makes
    this double as a test of whether a guessed name exists at all.

    `pageimages` is asked for a thumbnail rather than the original: the original
    of a station photograph is regularly several megabytes, and this is going
    into a panel a couple of hundred pixels wide. `pageprops` is asked whether
    the page is a disambiguation, because a guessed name lands on one often
    enough to matter and "Woolwich station may refer to:" is not an answer.

    Unlike the rest of this module a failed request raises rather than coming
    back empty. One missing portrait is a gap in a panel; a rate limit halfway
    through several hundred titles would otherwise be a result that looks whole
    and is four fifths absent.
    """
    found, resolved = {}, {}

    for start in range(0, len(titles), TITLE_BATCH):
        batch = titles[start:start + TITLE_BATCH]
        params = {"action": "query", "format": "json", "redirects": "1",
                  "titles": "|".join(batch), "prop": "extracts|pageimages|pageprops",
                  "exintro": "1", "explaintext": "1", "ppprop": "disambiguation",
                  "piprop": "thumbnail", "pithumbsize": str(THUMB_PX), "pilimit": "max"}
        renamed, pages = {}, {}

        # Properties continue independently of each other - the extracts can run
        # out before the images do - so a batch is not done until the reply says
        # so, and what has arrived so far is merged rather than replaced.
        while True:
            response = _session.post(API, data=params, timeout=60)
            response.raise_for_status()
            body = response.json()

            query = body.get("query", {})
            for hop in query.get("normalized", []) + query.get("redirects", []):
                renamed[hop["from"]] = hop["to"]

            for page in query.get("pages", {}).values():
                if "missing" in page:
                    continue
                known = pages.setdefault(page["title"], {})
                if page.get("extract"):
                    known["extract"] = page["extract"]
                if page.get("thumbnail"):
                    known["image"] = page["thumbnail"]["source"]
                if "disambiguation" in page.get("pageprops", {}):
                    known["disambiguation"] = True

            if "continue" not in body:
                break
            params.update(body["continue"])

        for title in batch:
            resolved[title] = _follow(title, renamed)
        found.update({title: pages[resolved[title]] for title in batch
                      if resolved[title] in pages})
        time.sleep(PAUSE_S)

    return {asked: {
        "title": resolved[asked],
        "url": article_url(resolved[asked]),
        "image": page.get("image"),
        "disambiguation": bool(page.get("disambiguation")),
        "extract": [tidy(p) for p in (page.get("extract") or "").split("\n")
                    if p.strip()][:paragraphs],
    } for asked, page in found.items()}


@lru_cache(maxsize=1)
def station_titles(page: str = STATION_LIST) -> dict[str, list[str]]:
    """Every Underground station's article, read off the list of them.

    The names are worth having from the horse's mouth rather than guessed at:
    "Aldgate station" is a closed railway station in Somerset, and only the list
    knows that the Underground one is filed as "Aldgate tube station".

    A name can appear twice - there really are two Edgware Roads - so each maps
    to every article listed under it, in the order the table gives them.
    """
    try:
        response = _session.get(API, timeout=30, params={
            "action": "parse", "format": "json", "page": page, "prop": "text"})
        response.raise_for_status()
        html = response.json()["parse"]["text"]["*"]
    except (requests.RequestException, KeyError, ValueError):
        return {}

    soup = BeautifulSoup(html, "html.parser")
    listed: dict[str, list[str]] = {}

    for row in soup.select("table.wikitable tr")[1:]:
        cell = row.find(["th", "td"])
        link = cell.find("a") if cell else None
        if link and link.get("title"):
            listed.setdefault(cell.get_text(strip=True), []).append(link["title"])

    return listed


if __name__ == "__main__":
    meta_sample = '<meta property="og:image" content="https://upload.wikimedia.org/full.jpg">'
    sample = (
        '<table class="infobox"><tr><td><a class="image" href="/wiki/Foo">'
        '<img src="//upload.wikimedia.org/a.jpg" '
        'srcset="//upload.wikimedia.org/a.jpg 1x, //upload.wikimedia.org/b.jpg 2x">'
        '</a></td></tr></table>'
    )
    assert _image_from_html(meta_sample) == "https://upload.wikimedia.org/full.jpg"
    assert _image_from_html(sample) == "https://upload.wikimedia.org/b.jpg"

    blitz = fetch_intro("The Blitz")
    assert blitz["title"] == "The Blitz" and len(blitz["extract"]) == 2
    assert blitz["url"] == "https://en.wikipedia.org/wiki/The_Blitz"
    assert fetch_intro("LGBT History Month")["title"] == "LGBTQ History Month"  # a redirect
    assert fetch_intro(None) is None

    assert tidy("Aldgate () is a station.") == "Aldgate is a station."
    assert tidy("King's Cross (; also known as X) is") == "King's Cross (also known as X) is"
    assert tidy("Bank (/bæŋk/) is") == "Bank (/b\u00e6\u014bk/) is"     # a real one survives
    assert tidy("Foo (born 1900;) is") == "Foo (born 1900) is"

    assert article_url("Oval tube station") == "https://en.wikipedia.org/wiki/Oval_tube_station"
    assert _follow("Old", {"Old": "Newer", "Newer": "Current"}) == "Current"
    assert _follow("Loop", {"Loop": "Loop"}) == "Loop"

    # A real name, a name that only redirects, and a name with no article at all.
    got = fetch_articles(["Oval tube station", "Angel station", "Nowhere tube station",
                          "Woolwich station"])
    assert "Nowhere tube station" not in got
    assert got["Woolwich station"]["disambiguation"] is True
    assert got["Oval tube station"]["disambiguation"] is False
    assert got["Angel station"]["title"] == "Angel tube station"
    assert got["Oval tube station"]["image"].startswith("https://")
    assert len(got["Oval tube station"]["extract"]) == 1
    assert "Northern line" in got["Oval tube station"]["extract"][0]

    listed = station_titles()
    assert listed["Aldgate"] == ["Aldgate tube station"]
    assert len(listed["Edgware Road"]) == 2       # there really are two of them
    print("ok")