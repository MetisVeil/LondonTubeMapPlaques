"""Scrape small bits of data from Wikipedia pages.

The only thing this project needs right now is the lead image from the page's
infobox, which is the portrait shown on the right-hand side of most biography
pages.
"""

from functools import lru_cache
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tube_map/1.0)"}


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
    print("ok")