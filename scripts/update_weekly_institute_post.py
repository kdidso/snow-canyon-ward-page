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
FAILURE_SCREENSHOT = Path("debug_instagram_failure.png")
FAILURE_HTML = Path("debug_instagram_failure.html")

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

def save_failure_screenshot(driver: webdriver.Chrome, path: Path) -> None:
    try:
        driver.save_screenshot(str(path))
        print(f"Saved screenshot to {path}")
    except Exception as exc:
        print(f"Could not save screenshot: {exc}", file=sys.stderr)

def save_failure_html(driver: webdriver.Chrome, path: Path) -> None:
    try:
        path.write_text(driver.page_source, encoding="utf-8")
        print(f"Saved page source to {path}")
    except Exception as exc:
        print(f"Could not save page source: {exc}", file=sys.stderr)

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
    Dismiss Instagram sign-up/login modal if it appears.
    """
    xpaths = [
        "//div[@role='dialog']//button//*[name()='svg']/ancestor::button[1]",
        "//div[@role='dialog']//button",
        "//button//*[name()='svg']/ancestor::button[1]",
    ]

    for xpath in xpaths:
        try:
            buttons = driver.find_elements(By.XPATH, xpath)
            for btn in buttons:
                try:
                    aria = (btn.get_attribute("aria-label") or "").strip().lower()
                    text = (btn.text or "").strip().lower()

                    # Prefer obvious close buttons
                    if aria in {"close", "dismiss"} or text in {"", "close"}:
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1)
                        return
                except Exception:
                    continue
        except Exception:
            continue

    # Last resort: ESC key
    try:
        from selenium.webdriver.common.keys import Keys
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.ESCAPE)
        time.sleep(1)
    except Exception:
        pass


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
    Collect Instagram post/reel links from the profile page.
    More tolerant than the first version.
    """
    links: list[str] = []
    seen: set[str] = set()

    # Give the page a moment and try to dismiss popup again
    time.sleep(2)
    try_dismiss_login_popup(driver)

    end_time = time.time() + 35
    while time.time() < end_time and len(links) < max_posts:
        anchors = driver.find_elements(By.TAG_NAME, "a")

        for a in anchors:
            try:
                href = (a.get_attribute("href") or "").strip()
            except StaleElementReferenceException:
                continue

            if not href:
                continue

            if "/p/" not in href and "/reel/" not in href and "/tv/" not in href:
                continue

            href = normalize_post_url(href)

            if href not in seen:
                seen.add(href)
                links.append(href)
                if len(links) >= max_posts:
                    break

        if len(links) >= max_posts:
            break

        driver.execute_script("window.scrollBy(0, 900);")
        time.sleep(1.5)
        try_dismiss_login_popup(driver)

    return links[:max_posts]


def find_matching_post(driver: webdriver.Chrome) -> MatchResult:
    driver.get(INSTAGRAM_PROFILE_URL)
    time.sleep(3)
    print("Current URL after load:", driver.current_url)
    print("Page title:", driver.title)
    print("Page source snippet:", driver.page_source[:1000])
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
    if driver is not None:
        save_failure_screenshot(driver, FAILURE_SCREENSHOT)
        save_failure_html(driver, FAILURE_HTML)
    return 1

    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
