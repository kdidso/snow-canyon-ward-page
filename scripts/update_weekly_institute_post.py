from __future__ import annotations

import json
import os
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
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


INSTAGRAM_PROFILE_URL = "https://www.instagram.com/stginstitute/"
OUTPUT_JSON = Path("data/weekly_institute_post.json")
FAILURE_SCREENSHOT = Path("debug_instagram_failure.png")
FAILURE_HTML = Path("debug_instagram_failure.html")
LOGIN_FILLED_SCREENSHOT = Path("debug_instagram_login_filled.png")
LOGIN_CLICKED_SCREENSHOT = Path("debug_instagram_login_clicked.png")

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


def find_first_present(
    driver: webdriver.Chrome,
    selectors: list[tuple[str, str]],
    timeout: int = 10,
) -> WebElement:
    last_exc = None
    for by, selector in selectors:
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, selector))
            )
            print(f"Found element using selector: {by} | {selector}")
            return element
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc or RuntimeError("No matching element found.")


def debug_selector_matches(
    driver: webdriver.Chrome,
    selectors: list[tuple[str, str]],
    label: str,
) -> None:
    print(f"Debugging selector matches for: {label}")
    for by, selector in selectors:
        try:
            elements = driver.find_elements(by, selector)
            print(f"  Selector {by} | {selector} -> {len(elements)} match(es)")
            for i, el in enumerate(elements[:3], start=1):
                try:
                    text = (el.text or "").strip()
                    aria = el.get_attribute("aria-label")
                    role = el.get_attribute("role")
                    tag = el.tag_name
                    enabled = el.is_enabled()
                    displayed = el.is_displayed()
                    print(
                        f"    [{i}] tag={tag} role={role} aria={aria} "
                        f"displayed={displayed} enabled={enabled} text={text!r}"
                    )
                except Exception as inner_exc:
                    print(f"    [{i}] Could not inspect element: {inner_exc}")
        except Exception as exc:
            print(f"  Selector {by} | {selector} raised: {exc}")


def find_first_clickable(
    driver: webdriver.Chrome,
    selectors: list[tuple[str, str]],
    timeout: int = 10,
) -> WebElement:
    last_exc = None
    for by, selector in selectors:
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, selector))
            )
            print(f"Found clickable element using selector: {by} | {selector}")
            return element
        except Exception as exc:
            print(f"Clickable lookup failed for selector: {by} | {selector} | {exc}")
            last_exc = exc
            continue

    debug_selector_matches(driver, selectors, "clickable target")
    raise last_exc or RuntimeError("No matching clickable element found.")


def is_login_page(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    if "/accounts/login" in url:
        return True

    try:
        has_user = bool(
            driver.find_elements(By.NAME, "username")
            or driver.find_elements(By.NAME, "email")
        )
        has_pass = bool(
            driver.find_elements(By.NAME, "password")
            or driver.find_elements(By.NAME, "pass")
        )
        return has_user and has_pass
    except Exception:
        return False


def login_to_instagram(driver: webdriver.Chrome) -> None:
    username = os.environ.get("INSTAGRAM_USERNAME", "").strip()
    password = os.environ.get("INSTAGRAM_PASSWORD", "").strip()

    if not username or not password:
        raise RuntimeError(
            "Missing INSTAGRAM_USERNAME or INSTAGRAM_PASSWORD environment variables."
        )

    print("Login page detected. Beginning Instagram login flow.")
    print("Current URL before login:", driver.current_url)

    username_selectors = [
        (By.NAME, "username"),
        (By.NAME, "email"),
        (By.XPATH, "//input[@name='username']"),
        (By.XPATH, "//input[@name='email']"),
        (By.CSS_SELECTOR, "input[aria-label='Phone number, username, or email']"),
        (By.CSS_SELECTOR, "input[type='text']"),
    ]

    password_selectors = [
        (By.NAME, "password"),
        (By.NAME, "pass"),
        (By.XPATH, "//input[@name='password']"),
        (By.XPATH, "//input[@name='pass']"),
        (By.CSS_SELECTOR, "input[type='password']"),
    ]

    login_button_selectors = [
        (By.XPATH, "//button[@type='submit']"),
        (By.XPATH, "//*[@id='login_form']//button[@type='submit']"),
        (By.XPATH, "//button[normalize-space()='Log in']"),
        (By.XPATH, "//button[normalize-space()='Log In']"),
        (By.XPATH, "//button[.//div[normalize-space()='Log in']]"),
        (By.XPATH, "//button[.//div[normalize-space()='Log In']]"),
        (By.XPATH, "//*[normalize-space()='Log in']/ancestor::button[1]"),
        (By.XPATH, "//*[normalize-space()='Log In']/ancestor::button[1]"),
        (By.XPATH, "//*[@id='login_form']//*[@role='button' and @aria-label='Log In']"),
        (By.XPATH, "//*[@role='button' and @aria-label='Log In']"),
        (By.XPATH, "//*[@role='button' and @tabindex='0' and @aria-label='Log In']"),
        (By.XPATH, "//div[@role='button'][.//span[normalize-space()='Log In']]"),
        (By.XPATH, "//div[@role='button'][.//div[normalize-space()='Log In']]"),
        (By.XPATH, "//*[@id='login_form']//*[@role='button']"),
    ]

    username_input = find_first_present(driver, username_selectors, timeout=20)
    password_input = find_first_present(driver, password_selectors, timeout=20)

    username_input.clear()
    username_input.send_keys(username)

    password_input.clear()
    password_input.send_keys(password)

    entered_username = username_input.get_attribute("value") or ""
    entered_password = password_input.get_attribute("value") or ""

    print(f"Username entered? {bool(entered_username)}")
    print(f"Username length entered: {len(entered_username)}")
    print(f"Password entered? {bool(entered_password)}")
    print(f"Password length entered: {len(entered_password)}")

    save_failure_screenshot(driver, LOGIN_FILLED_SCREENSHOT)

    print("Attempting to identify Log in button...")
    debug_selector_matches(driver, login_button_selectors, "login button before click")

    login_button = find_first_clickable(driver, login_button_selectors, timeout=20)
    print("About to click Log in button.")
    safe_click(driver, login_button)

    print("Clicked Log in button.")
    print("URL immediately after click:", driver.current_url)

    save_failure_screenshot(driver, LOGIN_CLICKED_SCREENSHOT)

    time.sleep(5)

    try:
        WebDriverWait(driver, 25).until(
            lambda d: "/accounts/login" not in d.current_url.lower()
        )
    except TimeoutException:
        print("Still appears to be on login page after click.")
        print("URL after login wait:", driver.current_url)
        save_failure_screenshot(driver, FAILURE_SCREENSHOT)
        save_failure_html(driver, FAILURE_HTML)
        raise RuntimeError(
            "Instagram login did not complete successfully. "
            "Still on login page or redirected to a challenge page."
        )

    print("Login appears successful.")
    print("URL after login:", driver.current_url)

    time.sleep(3)


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


def extract_caption_text(driver: webdriver.Chrome) -> str:
    """
    Try several selectors for caption text on an Instagram post page.
    """
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

    return "\n".join(texts).strip()


def extract_og_image(driver: webdriver.Chrome) -> str:
    try:
        meta = driver.find_element(By.XPATH, "//meta[@property='og:image']")
        content = meta.get_attribute("content") or ""
        return content.strip()
    except NoSuchElementException:
        return ""


def collect_post_links(driver: webdriver.Chrome, max_posts: int) -> list[str]:
    """
    Collect post links by manually searching the visible Instagram profile tiles.
    """
    links: list[str] = []
    seen: set[str] = set()

    time.sleep(2)

    for attempt in range(8):
        print(f"Collect attempt {attempt + 1}")

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
                    print(f"Found post link: {href}")

                    if len(links) >= max_posts:
                        return links[:max_posts]

        driver.execute_script("window.scrollBy(0, 1200);")
        time.sleep(1.5)

    return links[:max_posts]


def find_matching_post(driver: webdriver.Chrome) -> MatchResult:
    driver.get(INSTAGRAM_PROFILE_URL)
    time.sleep(3)

    print("URL before page-ready wait:", driver.current_url)

    wait_for_page_ready(driver)
    time.sleep(SLEEP_BETWEEN_ACTIONS * 2)

    if is_login_page(driver):
        login_to_instagram(driver)

        driver.get(INSTAGRAM_PROFILE_URL)
        wait_for_page_ready(driver)
        time.sleep(3)

        print("URL after returning to profile post-login:", driver.current_url)

        if is_login_page(driver):
            raise RuntimeError(
                "Still on Instagram login page after attempting login."
            )

    post_links = collect_post_links(driver, MAX_POSTS_TO_CHECK)
    if not post_links:
        raise RuntimeError("No Instagram post links were found on the profile page.")

    keywords = ("activities", "week")

    for index, post_url in enumerate(post_links, start=1):
        print(f"Checking post {index}/{len(post_links)}: {post_url}")
        driver.get(post_url)
        wait_for_page_ready(driver)
        time.sleep(SLEEP_BETWEEN_ACTIONS)

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

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
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
