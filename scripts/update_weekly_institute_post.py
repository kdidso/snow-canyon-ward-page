from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


INSTAGRAM_PROFILE_URL = "https://www.instagram.com/stginstitute/"
OUTPUT_JSON = Path("data/weekly_institute_post.json")
FAILURE_SCREENSHOT = Path("debug_instagram_failure.png")
FAILURE_HTML = Path("debug_instagram_failure.html")

MAX_POSTS_TO_CHECK = 20
PAGE_LOAD_TIMEOUT = 30
MEDIUM_WAIT = 10
SLEEP_BETWEEN_ACTIONS = 1.0

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class MatchResult:
    page_url: str
    post_url: str
    embed_html: str
    mobile_image_url: str
    mobile_text: str
    fallback_text: str
    updated_at: str


def save_failure_screenshot(driver: webdriver.Chrome, path: Path) -> None:
    try:
        driver.save_screenshot(str(path))
        print(f"Saved screenshot to {path}")
    except Exception as exc:
        print(f"Could not save screenshot: {exc}", file=sys.stderr)


def save_failure_html_text(html: str, path: Path) -> None:
    try:
        path.write_text(html, encoding="utf-8")
        print(f"Saved page source to {path}")
    except Exception as exc:
        print(f"Could not save page source: {exc}", file=sys.stderr)


def save_failure_html(driver: webdriver.Chrome, path: Path) -> None:
    save_failure_html_text(driver.page_source, path)


def build_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,2200")
    options.add_argument("--lang=en-US")
    options.add_argument(f"--user-agent={REQUEST_HEADERS['User-Agent']}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def wait_for_page_ready(driver: webdriver.Chrome, timeout: int = MEDIUM_WAIT) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def is_rate_limited_html(html: str) -> bool:
    lowered = html.lower()
    return "http error 429" in lowered or "too many requests" in lowered


def normalize_post_url(url: str) -> str:
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
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", attrs={"property": "og:image"})
    if not tag:
        return ""
    return (tag.get("content") or "").strip()


def extract_caption_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    texts: list[str] = []

    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc:
        content = (meta_desc.get("content") or "").strip()
        if content:
            texts.append(content)

    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc:
        content = (og_desc.get("content") or "").strip()
        if content and content not in texts:
            texts.append(content)

    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(" ", strip=True)
        if title_text and title_text not in texts:
            texts.append(title_text)

    body_text = soup.get_text(" ", strip=True)
    if body_text:
        shortened = re.sub(r"\s+", " ", body_text).strip()
        if shortened and shortened not in texts:
            texts.append(shortened)

    return "\n".join(texts).strip()


def extract_caption_text(driver: webdriver.Chrome) -> str:
    candidate_selectors = [
        (By.XPATH, "//article//h1"),
        (By.XPATH, "//article//div[contains(@class,'_a9zs')]"),
        (By.XPATH, "//article//ul//span"),
        (By.XPATH, "//article//div[@role='button']/following::span[1]"),
    ]

    texts: list[str] = []

    for by, selector in candidate_selectors:
        try:
            elements = driver.find_elements(by, selector)
        except Exception:
            elements = []

        for el in elements:
            try:
                txt = el.text.strip()
            except StaleElementReferenceException:
                continue
            if txt and txt not in texts:
                texts.append(txt)

    if texts:
        return "\n".join(texts).strip()

    return extract_caption_text_from_html(driver.page_source)


def extract_og_image(driver: webdriver.Chrome) -> str:
    try:
        meta = driver.find_element(By.XPATH, "//meta[@property='og:image']")
        content = meta.get_attribute("content") or ""
        return content.strip()
    except NoSuchElementException:
        return extract_og_image_from_html(driver.page_source)


def collect_post_links_requests(session: requests.Session, max_posts: int) -> list[str]:
    print("Trying requests-based profile scrape...")
    resp = session.get(INSTAGRAM_PROFILE_URL, timeout=30)
    print(f"Requests profile status: {resp.status_code}")

    if resp.status_code == 429 or is_rate_limited_html(resp.text):
        raise RuntimeError("Instagram returned HTTP 429 on requests profile fetch.")

    soup = BeautifulSoup(resp.text, "html.parser")
    links: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if not ("/p/" in href or "/reel/" in href or "/tv/" in href):
            continue

        full_url = normalize_post_url(urljoin("https://www.instagram.com", href))
        if full_url not in seen:
            seen.add(full_url)
            links.append(full_url)
            print(f"Requests found post link: {full_url}")
            if len(links) >= max_posts:
                break

    return links[:max_posts]


def collect_post_links_selenium(driver: webdriver.Chrome, max_posts: int) -> list[str]:
    print("Trying Selenium-based public profile scrape...")
    links: list[str] = []
    seen: set[str] = set()

    for attempt in range(8):
        print(f"Selenium collect attempt {attempt + 1}")

        xpaths = [
            "//main//a[contains(@href, '/p/')]",
            "//main//a[contains(@href, '/reel/')]",
            "//article//a[contains(@href, '/p/')]",
            "//article//a[contains(@href, '/reel/')]",
        ]

        for xpath in xpaths:
            try:
                anchors = driver.find_elements(By.XPATH, xpath)
            except Exception:
                anchors = []

            for a in anchors:
                try:
                    href = (a.get_attribute("href") or "").strip()
                except StaleElementReferenceException:
                    continue

                if not href:
                    continue

                href = normalize_post_url(href)

                if href not in seen:
                    seen.add(href)
                    links.append(href)
                    print(f"Selenium found post link: {href}")

                    if len(links) >= max_posts:
                        return links[:max_posts]

        driver.execute_script("window.scrollBy(0, 1200);")
        time.sleep(1.5)

    return links[:max_posts]


def find_matching_post_requests(session: requests.Session, post_links: list[str]) -> MatchResult | None:
    keywords = ("activities", "week")

    for index, post_url in enumerate(post_links, start=1):
        print(f"Requests checking post {index}/{len(post_links)}: {post_url}")
        resp = session.get(post_url, timeout=30)
        print(f"Requests post status: {resp.status_code}")

        if resp.status_code == 429 or is_rate_limited_html(resp.text):
            raise RuntimeError(f"Instagram returned HTTP 429 while opening post: {post_url}")

        caption_text = extract_caption_text_from_html(resp.text)
        sentences = split_into_sentences(caption_text)

        matched_sentence = None
        for sentence in sentences:
            if sentence_contains_keywords(sentence, keywords):
                matched_sentence = sentence
                break

        if matched_sentence:
            og_image = extract_og_image_from_html(resp.text)
            updated_at = datetime.now(timezone.utc).isoformat()

            return MatchResult(
                page_url=INSTAGRAM_PROFILE_URL,
                post_url=normalize_post_url(post_url),
                embed_html=build_embed_html(normalize_post_url(post_url)),
                mobile_image_url=og_image,
                mobile_text=matched_sentence,
                fallback_text="Open this week's institute activities post.",
                updated_at=updated_at,
            )

    return None


def find_matching_post_selenium(driver: webdriver.Chrome) -> MatchResult:
    keywords = ("activities", "week")

    driver.get(INSTAGRAM_PROFILE_URL)
    time.sleep(3)
    wait_for_page_ready(driver)
    time.sleep(2)

    if is_rate_limited_html(driver.page_source):
        raise RuntimeError("Instagram returned HTTP 429 on the Selenium profile request.")

    post_links = collect_post_links_selenium(driver, MAX_POSTS_TO_CHECK)
    if not post_links:
        raise RuntimeError("No Instagram post links were found on the public profile page.")

    for index, post_url in enumerate(post_links, start=1):
        print(f"Selenium checking post {index}/{len(post_links)}: {post_url}")
        driver.get(post_url)
        wait_for_page_ready(driver)
        time.sleep(SLEEP_BETWEEN_ACTIONS)

        if is_rate_limited_html(driver.page_source):
            raise RuntimeError(f"Instagram returned HTTP 429 while opening post: {post_url}")

        current_url = normalize_post_url(driver.current_url)
        caption_text = extract_caption_text(driver)
        sentences = split_into_sentences(caption_text)

        matched_sentence = None
        for sentence in sentences:
            if sentence_contains_keywords(sentence, keywords):
                matched_sentence = sentence
                break

        if matched_sentence:
            og_image = extract_og_image(driver)
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

    raise RuntimeError(
        f"No matching post found in the first {len(post_links)} posts. "
        f"Expected a sentence containing both 'activities' and 'week'."
    )


def find_matching_post() -> MatchResult:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    try:
        request_links = collect_post_links_requests(session, MAX_POSTS_TO_CHECK)
        if request_links:
            result = find_matching_post_requests(session, request_links)
            if result is not None:
                print("Success via requests-only scrape.")
                return result
            print("Requests found links but did not find a matching post.")
        else:
            print("Requests did not find any post links on the profile page.")
    except Exception as exc:
        print(f"Requests path failed: {exc}")

    print("Falling back to Selenium public scrape...")
    driver = None
    try:
        driver = build_driver()
        return find_matching_post_selenium(driver)
    finally:
        if driver is not None:
            driver.quit()


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
