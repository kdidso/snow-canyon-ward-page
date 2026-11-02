#!/usr/bin/env python3
import os
import re
import json
import time
import zipfile
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE = "https://unithistory.churchofjesuschrist.org"
START_URL = "https://unithistory.churchofjesuschrist.org/"

# Your confirmed story-card class (two classes on same element)
STORY_CARD_SELECTOR = ".sc-1m4vew7-0.bYMucc"

# Env-configurable outputs
OUT_DIR = os.getenv("OUT_DIR", "unit-history/events").strip() or "unit-history/events"
ZIP_NAME = os.getenv("ZIP_NAME", "unit-history/unit_history_export.zip").strip() or "unit-history/unit_history_export.zip"
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "unit-history/manifest.json").strip() or "unit-history/manifest.json"

HEADLESS = os.getenv("HEADLESS", "1").strip().lower() not in ("0", "false", "no", "")
SKIP_EXISTING_FOLDERS = os.getenv("SKIP_EXISTING_FOLDERS", "1").strip().lower() not in ("0", "false", "no", "")

# Auth env (username/password only)
LDS_USERNAME = os.getenv("LDS_USERNAME", "").strip()
LDS_PASSWORD = os.getenv("LDS_PASSWORD", "").strip()


# ---------------------------
# Utilities
# ---------------------------
def safe_name(name: str, max_len: int = 90) -> str:
    name = (name or "").strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)  # Windows-safe
    name = name.strip(" .")
    if not name:
        name = "Untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def save_debug(page, tag="debug"):
    ts = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs("debug", exist_ok=True)
    shot = f"debug/{tag}_{ts}.png"
    html = f"debug/{tag}_{ts}.html"
    try:
        page.screenshot(path=shot, full_page=True)
    except Exception:
        pass
    try:
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass
    print(f"🧪 Saved debug screenshot: {shot}")
    print(f"🧪 Saved debug HTML:       {html}")
    print(f"🧭 Current URL:            {getattr(page, 'url', '')}")


def absolutize(href: str) -> str:
    if not href:
        return ""
    return urljoin(BASE, href)


def pick_largest_from_srcset(srcset: str) -> str:
    """
    srcset: "url1 480w, url2 800w, url3 1200w"
    return url with the largest width. If no widths, return first URL.
    """
    if not srcset:
        return ""
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    candidates = []
    for p in parts:
        m = re.match(r"(\S+)\s+(\d+)w", p)
        if m:
            candidates.append((int(m.group(2)), m.group(1)))
        else:
            tok = p.split()
            if tok:
                candidates.append((0, tok[0]))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def normalize_img_url(u: str) -> str:
    if not u:
        return ""
