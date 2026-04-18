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

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


INSTAGRAM_PROFILE_URL = "https://www.instagram.com/stginstitute/"
OUTPUT_JSON = Path("data/weekly_institute_post.json")

MAX_POSTS_TO_CHECK = 20
PAGE_LOAD_TIMEOUT = 30
SHORT_WAIT = 5
MEDIUM_WAIT = 10
SLEEP_BETWEEN_ACTIONS = 1.0


@dataclass
class MatchResult:
    page_url: str
    post_url: str
    embed_html: str
    mobile_image_url: str
    mobile_text: str
    fallback_text: str
    updated_at: str


def build_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,2200")
    options.add_argument("--lang=en-US")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def wait_for_page_ready(driver: webdriver.Chrome, timeout: int = MEDIUM_WAIT) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def safe_click(driver: webdriver.Chrome, element: WebElement) -> None:
    try:
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)


def try_dismiss_login_popup(driver: webdriver.Chrome) -> None:
    """
    Instagram sometimes shows a sign-up / log-in modal immediately.
    We try a few selectors and continue if it is not present.
    """
    selectors = [
        # Generic dialog close buttons
        (By.XPATH, "//div[@role='dialog']//button//*[name()='svg' and @aria-label='Close']/ancestor::button[1]"),
        (By.XPATH, "//div[@role='dialog']//button[@aria-label='Close']"),
        # Top-right "X" button variants
        (By.XPATH, "//button//*[name()='svg' and @aria-label='Close']/ancestor::button[1]"),
        (By.XPATH, "//div[@role='dialog']//button"),
    ]

    for by, selector in selectors:
        try:
            btn = WebDriverWait(driver, SHORT_WAIT).until(
                EC.element_to_be_clickable((by, selector))
            )
            safe_click(driver, btn)
            time.sleep(SLEEP_BETWEEN_ACTIONS)
            return
        except TimeoutException:
            continue
        except Exception:
            continue


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
    # Split on punctuation/newlines while keeping it simple and robust.
    parts = re.split(r"[.!?\n]+", cleaned)
    return [p.strip() for p in parts if p.strip()]


def extract_caption_text(driver: webdriver.Chrome) -> str:
    """
    Try several selectors for caption text on an Instagram post page.
    """
    candidate_selectors = [
        # Main article text blocks
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

    # Join distinct text fragments and let sentence matching sort it out.
    joined = "\n".join(texts).strip()
    return joined


def extract_og_image(driver: webdriver.Chrome) -> str:
    try:
        meta = driver.find_element(By.XPATH, "//meta[@property='og:image']")
        content = meta.get_attribute("content") or ""
        return content.strip()
    except NoSuchElementException:
        return ""


def collect_post_links(driver: webdriver.Chrome, max_posts: int) -> list[str]:
    """
    Collect post links from the profile grid.
    """
    links: list[str] = []
    seen: set[str] = set()

    end_time = time.time() + 25
    while time.time() < end_time and len(links) < max_posts:
        anchors = driver.find_elements(By.XPATH, "//a[contains(@href,'/p/') or contains(@href,'/reel/')]")
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
                if len(links) >= max_posts:
                    break

        if len(links) >= max_posts:
            break

        # Small scroll in case the initial page did not expose enough anchors.
        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(1.25)

    return links[:max_posts]


def find_matching_post(driver: webdriver.Chrome) -> MatchResult:
    driver.get(INSTAGRAM_PROFILE_URL)
    wait_for_page_ready(driver)
    time.sleep(SLEEP_BETWEEN_ACTIONS * 2)
    try_dismiss_login_popup(driver)

    post_links = collect_post_links(driver, MAX_POSTS_TO_CHECK)
    if not post_links:
        raise RuntimeError("No Instagram post links were found on the profile page.")

    keywords = ("activities", "week")

    for index, post_url in enumerate(post_links, start=1):
        print(f"Checking post {index}/{len(post_links)}: {post_url}")
        driver.get(post_url)
        wait_for_page_ready(driver)
        time.sleep(SLEEP_BETWEEN_ACTIONS)
        try_dismiss_login_popup(driver)

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

    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {output_path}")


def main() -> int:
    driver = None
    try:
        driver = build_driver()
        result = find_matching_post(driver)
        write_output(result, OUTPUT_JSON)

        print("Success.")
        print(f"Post URL: {result.post_url}")
        print(f"Matched text: {result.mobile_text}")
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
