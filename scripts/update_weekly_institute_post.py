from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


INSTAGRAM_PROFILE_URL = "https://www.instagram.com/stginstitute/"
OUTPUT_JSON = Path("data/weekly_institute_post.json")
FAILURE_SCREENSHOT = Path("debug_instagram_failure.png")
FAILURE_HTML = Path("debug_instagram_failure.html")

MAX_POSTS_TO_CHECK = 12
PAGE_LOAD_TIMEOUT_MS = 30_000
WAIT_BETWEEN_ACTIONS_MS = 1500


@dataclass
class MatchResult:
    page_url: str
    post_url: str
    embed_html: str
    mobile_image_url: str
    mobile_text: str
    fallback_text: str
    updated_at: str


def save_failure_html(html: str, path: Path) -> None:
    try:
        path.write_text(html, encoding="utf-8")
        print(f"Saved page source to {path}")
    except Exception as exc:
        print(f"Could not save page source: {exc}", file=sys.stderr)


def normalize_post_url(url: str) -> str:
    """
    Normalize Instagram post URL to:
    https://www.instagram.com/p/<code>/
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    m = re.match(r"^/(p|reel|tv)/([^/]+)$", path)
    if not m:
        return url
    post_type = m.group(1)
    code = m.group(2)
    return f"https://www.instagram.com/{post_type}/{code}/"


def build_embed_html(post_url: str) -> str:
    permalink = f"{post_url}?utm_source=ig_embed&amp;utm_campaign=loading"
    return (
        f'<blockquote class="instagram-media" '
        f'data-instgrm-captioned '
        f'data-instgrm-permalink="{permalink}" '
        f'data-instgrm-version="14" '
        f'style="background:#FFF; border:0; border-radius:3px; '
        f'box-shadow:0 0 1px 0 rgba(0,0,0,0.5),0 1px 10px 0 rgba(0,0,0,0.15); '
        f'margin:1px; max-width:540px; min-width:326px; width:99.375%;">'
        f"</blockquote>"
    )


def sentence_contains_keywords(sentence: str, keywords: Iterable[str]) -> bool:
    s = sentence.lower()
    return all(word.lower() in s for word in keywords)


def split_into_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    parts = re.split(r"[.!?\n]+", cleaned)
    return [p.strip() for p in parts if p.strip()]


def extract_og_image_from_html(html: str) -> str:
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def extract_meta_content(html: str, attr_name: str, attr_value: str) -> str:
    patterns = [
        rf'<meta[^>]+{attr_name}=["\']{re.escape(attr_value)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+{attr_name}=["\']{re.escape(attr_value)}["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def extract_title_text(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def strip_html_text(html: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_caption_text_from_html(html: str) -> str:
    texts: list[str] = []

    desc = extract_meta_content(html, "name", "description")
    if desc:
        texts.append(desc)

    og_desc = extract_meta_content(html, "property", "og:description")
    if og_desc and og_desc not in texts:
        texts.append(og_desc)

    title = extract_title_text(html)
    if title and title not in texts:
        texts.append(title)

    body_text = strip_html_text(html)
    if body_text and body_text not in texts:
        texts.append(body_text)

    return "\n".join(texts).strip()


def is_rate_limited_html(html: str) -> bool:
    lowered = html.lower()
    return "http error 429" in lowered or "too many requests" in lowered


def is_login_page_url(url: str) -> bool:
    return "/accounts/login" in url.lower()


def collect_post_links_from_profile(page) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()

    selectors = [
        "a[href*='/p/']",
        "a[href*='/reel/']",
        "a[href*='/tv/']",
    ]

    for attempt in range(8):
        print(f"Playwright collect attempt {attempt + 1}")

        for selector in selectors:
            hrefs = page.locator(selector).evaluate_all(
                "(els) => els.map(e => e.href).filter(Boolean)"
            )

            for href in hrefs:
                normalized = normalize_post_url(href.strip())
                if normalized not in seen:
                    seen.add(normalized)
                    links.append(normalized)
                    print(f"Found post link: {normalized}")
                    if len(links) >= MAX_POSTS_TO_CHECK:
                        return links[:MAX_POSTS_TO_CHECK]

        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(1200)

    return links[:MAX_POSTS_TO_CHECK]


def find_matching_post() -> MatchResult:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 2200},
            locale="en-US",
        )

        page = context.new_page()
        page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)

        try:
            print(f"Opening profile: {INSTAGRAM_PROFILE_URL}")
            page.goto(INSTAGRAM_PROFILE_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(WAIT_BETWEEN_ACTIONS_MS)

            current_url = page.url
            html = page.content()

            print(f"Initial URL: {current_url}")

            if is_rate_limited_html(html):
                page.screenshot(path=str(FAILURE_SCREENSHOT), full_page=True)
                save_failure_html(html, FAILURE_HTML)
                raise RuntimeError("Instagram returned HTTP 429 on the Playwright profile request.")

            if is_login_page_url(current_url):
                page.screenshot(path=str(FAILURE_SCREENSHOT), full_page=True)
                save_failure_html(html, FAILURE_HTML)
                raise RuntimeError(
                    "Instagram redirected the headless Playwright browser to the login page."
                )

            post_links = collect_post_links_from_profile(page)
            if not post_links:
                page.screenshot(path=str(FAILURE_SCREENSHOT), full_page=True)
                save_failure_html(page.content(), FAILURE_HTML)
                raise RuntimeError("No Instagram post links were found on the public profile page.")

            keywords = ("activities", "week")

            for index, post_url in enumerate(post_links, start=1):
                print(f"Checking post {index}/{len(post_links)}: {post_url}")

                page.goto(post_url, wait_until="domcontentloaded")
                page.wait_for_timeout(WAIT_BETWEEN_ACTIONS_MS)

                current_url = normalize_post_url(page.url)
                html = page.content()

                if is_rate_limited_html(html):
                    page.screenshot(path=str(FAILURE_SCREENSHOT), full_page=True)
                    save_failure_html(html, FAILURE_HTML)
                    raise RuntimeError(f"Instagram returned HTTP 429 while opening post: {post_url}")

                if is_login_page_url(page.url):
                    page.screenshot(path=str(FAILURE_SCREENSHOT), full_page=True)
                    save_failure_html(html, FAILURE_HTML)
                    raise RuntimeError(
                        f"Instagram redirected to login while opening post: {post_url}"
                    )

                caption_text = extract_caption_text_from_html(html)
                sentences = split_into_sentences(caption_text)

                matched_sentence = None
                for sentence in sentences:
                    if sentence_contains_keywords(sentence, keywords):
                        matched_sentence = sentence
                        break

                if matched_sentence:
                    og_image = extract_og_image_from_html(html)
                    updated_at = datetime.now(timezone.utc).isoformat()

                    return MatchResult(
                        page_url=INSTAGRAM_PROFILE_URL,
                        post_url=current_url,
                        embed_html=build_embed_html(current_url),
                        mobile_image_url=og_image,
                        mobile_text=matched_sentence,
                        fallback_text="Open this week's institute activities post.",
                        updated_at=updated_at,
                    )

            page.screenshot(path=str(FAILURE_SCREENSHOT), full_page=True)
            save_failure_html(page.content(), FAILURE_HTML)
            raise RuntimeError(
                f"No matching post found in the first {len(post_links)} posts. "
                f"Expected a sentence containing both 'activities' and 'week'."
            )

        except PlaywrightTimeoutError as exc:
            try:
                page.screenshot(path=str(FAILURE_SCREENSHOT), full_page=True)
                save_failure_html(page.content(), FAILURE_HTML)
            except Exception:
                pass
            raise RuntimeError(f"Playwright timed out: {exc}") from exc

        finally:
            context.close()
            browser.close()


def write_output(result: MatchResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "page_url": result.page_url,
        "post_url": result.post_url,
        "embed_html": result.embed_html,
        "mobile_image_url": result.mobile_image_url,
        "mobile_text": result.mobile_text,
        "fallback_text": result.fallback_text,
        "updated_at": result.updated_at,
    }

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {output_path}")


def main() -> int:
    try:
        result = find_matching_post()
        write_output(result, OUTPUT_JSON)

        print("Success.")
        print(f"Post URL: {result.post_url}")
        print(f"Matched text: {result.mobile_text}")
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
