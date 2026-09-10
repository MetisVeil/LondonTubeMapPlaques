"""Read small bits of Wikipedia: a portrait, and the opening of an article.

`fetch_image` scrapes the lead image out of a page's infobox, which is the
portrait most biographies carry. `fetch_intro` asks the API for the opening
paragraphs of an article, which is how a themed map explains the occasion it is
timed to.

The odd one out in this folder: it exposes no SOURCE or load(conn) and stores
nothing, because there is no feed to version - `derived.categories` calls both
while it builds, and the lru_cache is the only memory.
"""

from functools import lru_cache
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tube_map/1.0)"}
API = "https://en.wikipedia.org/w/api.php"

# A build asks Wikipedia for a couple of thousand portraits, and the handful of
# article lookups queue up behind them - close enough together to be rate
# limited. Backing off is the difference between a themed map that explains
# itself and one that quietly cannot.
_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))))


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

    opening = [p for p in (page.get("extract") or "").split("\n") if p.strip()][:paragraphs]
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
    print("ok")