#!/usr/bin/env python3
import json
import re
import sys
import os
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.churchofjesuschrist.org"
MANUAL_PATH = "/study/manual/come-follow-me-for-home-and-church-old-testament-2026/{week:02d}?lang=eng"
OUT_JSON = "data/come_follow_me_this_week.json"


def iso_week_number(d: date) -> int:
    """
    ISO week can be 1..53. Your manual appears to be 1..52.
    Clamp to 52 so week 53 doesn't break the URL.
    """
    wk = d.isocalendar().week
    return min(int(wk), 52)


def absolute_url(u: str) -> str:
    if not u:
        return ""
    return urljoin(BASE, u)


def get_text_or_empty(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _largest_from_srcset(srcset: str) -> str:
    """
    srcset like: 'url1 60w, url2 100w, url3 640w'
    Return the URL with the largest width.
    """
    if not srcset:
        return ""
    candidates = []
    for part in srcset.split(","):
        part = part.strip()
        m = re.match(r"(\S+)\s+(\d+)w", part)
        if m:
            candidates.append((int(m.group(2)), m.group(1)))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def pick_best_image_from_tag(img_tag) -> str:
    """
    Given an <img> tag, prefer the largest srcset candidate, otherwise src.
    """
    if not img_tag:
        return ""

    # Prefer srcset
    srcset = img_tag.get("srcset", "") or img_tag.get("data-srcset", "") or ""
    best = _largest_from_srcset(srcset)
    if best:
        return absolute_url(best)

    # Then src-like attributes
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        src = img_tag.get(attr, "")
        if src:
            return absolute_url(src)

    return ""


def _looks_like_bad_image(url: str) -> bool:
    """
    Filter out icons/logos/sprites/spacers/etc.
    This is conservative but helps avoid picking header icons.
    """
    u = (url or "").strip().lower()
    if not u:
        return True

    # Ignore inline data images and SVGs (often icons)
    if u.startswith("data:image"):
        return True
    if u.endswith(".svg") or ".svg?" in u:
        return True

    bad_substrings = [
        "sprite", "icon", "icons", "logo", "favicon", "spinner", "loading",
        "placeholder", "transparent", "blank", "1x1", "pixel"
    ]
    if any(s in u for s in bad_substrings):
        return True

    return False


def _pick_from_picture_tag(picture_tag) -> str:
    """
    Given a <picture>, prefer the largest candidate from the first <source srcset>
    in DOM order; fall back to its <img>.
    """
    if not picture_tag:
        return ""

    # Prefer <source srcset> (often contains the real responsive image)
    for src in picture_tag.find_all("source"):
        ss = (src.get("srcset", "") or "").strip()
        if ss:
            best = _largest_from_srcset(ss)
            if best:
                return absolute_url(best)

            # If no width descriptors, take first URL in srcset list
            first = ss.split(",")[0].strip().split(" ")[0].strip()
            if first:
                return absolute_url(first)

    # Fallback: <img> inside picture
    img = picture_tag.find("img")
    return pick_best_image_from_tag(img)


def pick_top_image(soup: BeautifulSoup) -> str:
    """
    Find the image that is highest up on the page (DOM order),
    but skip "junk" images (icons/logos/svgs/etc.).
    Prefer images within main/article first.

    This is much more reliable than fixed IDs since the site changes.
    """
    # Prefer searching within main/article content first
    containers = soup.select("main, article")
    search_roots = containers if containers else [soup]

    seen = set()

    # Scan DOM order: picture first, then figure img, then any img
    selectors = ["picture", "figure img", "img"]

    for root in search_roots:
        for sel in selectors:
            for el in root.select(sel):
                if el.name == "picture":
                    url = _pick_from_picture_tag(el)
                elif el.name == "img":
                    url = pick_best_image_from_tag(el)
                else:
                    # In case BeautifulSoup returns other tags unexpectedly
                    url = ""

                url = (url or "").strip()
                if not url:
                    continue

                url = absolute_url(url)
                if not url or url in seen:
                    continue
                seen.add(url)

                if _looks_like_bad_image(url):
                    continue

                return url

    # If nothing found in main/article, try whole document (as last resort)
    for sel in selectors:
        for el in soup.select(sel):
            if el.name == "picture":
                url = _pick_from_picture_tag(el)
            elif el.name == "img":
                url = pick_best_image_from_tag(el)
            else:
                url = ""

            url = (url or "").strip()
            if not url:
                continue

            url = absolute_url(url)
            if not url or url in seen:
                continue
            seen.add(url)

            if _looks_like_bad_image(url):
                continue

            return url

    return ""


def scrape_week(week: int) -> dict:
    url = BASE + MANUAL_PATH.format(week=week)

    r = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; SnowCanyonWardBot/1.0; +https://github.com/kdidso)"
        },
    )
    r.raise_for_status()

    # Force correct decoding so curly quotes / dashes survive
    r.encoding = "utf-8"

    soup = BeautifulSoup(r.text, "html.parser")

    # Small heading is typically p.title-number
    small_heading_el = soup.select_one("p.title-number")
    # Big heading is typically the first h1
    big_heading_el = soup.select_one("h1")

    small_heading = get_text_or_empty(small_heading_el)
    big_heading = get_text_or_empty(big_heading_el)

    # Updated: choose the highest-up meaningful image on the page
    image_url = pick_top_image(soup)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "week_number": week,
        "source_url": url,
        "image_url": image_url,
        "small_heading": small_heading,
        "big_heading": big_heading,
    }


def main():
    today = date.today()
    week = iso_week_number(today)

    try:
        payload = scrape_week(week)
    except Exception as e:
        print(f"ERROR scraping week {week}: {e}", file=sys.stderr)
        raise

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote {OUT_JSON} for week {week}: {payload.get('big_heading','')}")


if __name__ == "__main__":
    main()
