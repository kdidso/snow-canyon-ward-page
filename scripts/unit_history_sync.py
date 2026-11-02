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
    u = str(u).strip().strip(' "\'')
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("/"):
        u = absolutize(u)
    return u


def scroll_to_load(page, max_scrolls=18, pause_ms=350):
    for _ in range(max_scrolls):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(pause_ms)
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(250)


def _has_any(page, selector: str) -> bool:
    try:
        return page.locator(selector).count() > 0
    except Exception:
        return False


def is_login_page(page) -> bool:
    """
    LDS auth flows sometimes land back on START_URL while still unauthenticated.
    So: treat as 'login page' if the login inputs exist, OR the URL looks like auth.
    """
    u = (getattr(page, "url", "") or "").lower()
    if "signin" in u or "login" in u or "okta" in u:
        return True
    if ("auth" in u and "churchofjesuschrist.org" in u):
        return True

    # DOM-based detection (more reliable than URL alone)
    if _has_any(page, "#username-input") or _has_any(page, "#password-input"):
        return True

    # Sometimes an interstitial challenge doesn't show those IDs; look for common sign-in headings
    try:
        if page.locator("text=Sign In").count() > 0 and page.locator("input[type='password']").count() > 0:
            return True
    except Exception:
        pass

    return False


def file_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"]:
        return ext
    return ".jpg"


def strip_downscaling_params(url: str) -> str:
    """
    Some CDNs/thumb services add params like w/h/width/height/fit/quality etc.
    Removing them can yield a larger image.
    """
    try:
        pu = urlparse(url)
        qs = parse_qs(pu.query, keep_blank_values=True)

        remove_keys = {
            "w", "h", "width", "height",
            "fit", "crop", "rect",
            "q", "quality",
            "auto", "format",
            "dpr"
        }
        changed = False
        for k in list(qs.keys()):
            if k.lower() in remove_keys:
                qs.pop(k, None)
                changed = True

        if not changed:
            return url

        new_query = urlencode(qs, doseq=True)
        return urlunparse((pu.scheme, pu.netloc, pu.path, pu.params, new_query, pu.fragment))
    except Exception:
        return url


def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)


def zip_folder(root: pathlib.Path, zip_name: str):
    ensure_dir(pathlib.Path(zip_name).parent)
    with zipfile.ZipFile(zip_name, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in root.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(root))
    print(f"📦 Wrote zip: {zip_name}")


# ---------------------------
# Auth helpers (UPDATED)
# ---------------------------
def _click_first_that_exists(page, selectors, timeout_ms=10_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


def _fill_like_human(page, selector: str, value: str, timeout_ms: int = 60_000):
    """
    Some auth forms don't enable the button unless real input events fire.
    Playwright's fill() usually works, but type() is more "human".
    We'll do: click -> fill -> small type tweak (or type directly if needed).
    """
    page.wait_for_selector(selector, timeout=timeout_ms)
    loc = page.locator(selector).first
    loc.click(timeout=10_000)
    try:
        loc.fill(value, timeout=10_000)
        # Ensure input event chain
        page.wait_for_timeout(100)
    except Exception:
        # fallback to typing
        loc.type(value, delay=35, timeout=20_000)


def wait_for_history_home(page, timeout_ms=90000):
    """Recognize the actual authenticated application, independent of CSS hashes."""
    try:
        page.get_by_text("Unit History", exact=True).first.wait_for(state="visible", timeout=timeout_ms)
        page.get_by_text("Choose Unit", exact=False).first.wait_for(state="visible", timeout=30000)
        return True
    except Exception:
        return False


def attempt_headless_login(page, story_selector: str = STORY_CARD_SELECTOR):
    if not LDS_USERNAME or not LDS_PASSWORD:
        raise RuntimeError("LDS_USERNAME/LDS_PASSWORD are not set (GitHub Secrets).")
    print("🔐 Opening Unit History and checking authentication...")
    page.goto(START_URL, wait_until="domcontentloaded", timeout=120_000)
    if wait_for_history_home(page, timeout_ms=15000):
        print("✅ Already authenticated (Unit History application visible).")
        return
    if not is_login_page(page):
        page.wait_for_timeout(3000)
        if wait_for_history_home(page, timeout_ms=15000):
            print("✅ Authenticated (Unit History application visible).")
            return
    if not is_login_page(page):
        save_debug(page, tag="login_unknown_state")
        raise RuntimeError("Neither the Unit History application nor the login form appeared.")

    print("🔐 Signing in with LDS_USERNAME/LDS_PASSWORD...")
    try:
        page.wait_for_selector("#username-input", timeout=60000)
    except PWTimeoutError:
        save_debug(page, tag="login_no_username_input")
        raise RuntimeError("Could not find the username field.")
    _fill_like_human(page, "#username-input", LDS_USERNAME)
    if not _click_first_that_exists(page, ["#button-primary", "button#button-primary"], timeout_ms=20000):
        page.locator("#username-input").first.press("Enter")
    try:
        page.wait_for_selector("#password-input", timeout=60000)
    except PWTimeoutError:
        save_debug(page, tag="login_no_password_input_after_next")
        raise RuntimeError("Password field not found after username submission; check for verification.")
    _fill_like_human(page, "#password-input", LDS_PASSWORD)
    if not _click_first_that_exists(page, ["#button-primary", "button#button-primary"], timeout_ms=20000):
        page.locator("#password-input").first.press("Enter")
    if wait_for_history_home(page, timeout_ms=90000):
        print("✅ Signed in; Unit History application is visible.")
        return
    save_debug(page, tag="login_post_submit_no_home")
    raise RuntimeError("Sign-in submitted, but the Unit History application was not detected.")


# ---------------------------
# Grid interactions
# ---------------------------
# Cards are marked after examining the rendered page, rather than relying on
# generated styled-components CSS class names which change between deployments.
CARD_MARKER = "data-uh-export-card"


def mark_story_cards(page):
    """Find cards by their visible Ward badge, image, and date/title content."""
    return page.evaluate(r"""() => {
      document.querySelectorAll('[data-uh-export-card]').forEach(e => e.removeAttribute('data-uh-export-card'));
      const badges = [...document.querySelectorAll('body *')].filter(e =>
        e.children.length === 0 && (e.textContent || '').trim() === 'Ward');
      const cards = [];
      for (const badge of badges) {
        let node = badge;
        for (let i = 0; i < 8 && node && node !== document.body; i++, node = node.parentElement) {
          const text = (node.innerText || '').trim();
          const imgs = node.querySelectorAll('img');
          const wardBadges = [...node.querySelectorAll('*')].filter(x =>
            x.children.length === 0 && (x.textContent || '').trim() === 'Ward');
          if (imgs.length >= 1 && wardBadges.length === 1 && text.length > 15 && text.length < 1000) {
            if (!cards.includes(node)) cards.push(node);
            break;
          }
        }
      }
      cards.forEach((e,i) => e.setAttribute('data-uh-export-card', String(i)));
      return cards.length;
    }""")


def get_story_card_count(page) -> int:
    count = mark_story_cards(page)
    if count:
        return count
    # Legacy fallback in case the page layout has changed differently.
    return page.locator(STORY_CARD_SELECTOR).count()


def story_cards(page):
    count = mark_story_cards(page)
    if count:
        return page.locator(f"[{CARD_MARKER}]")
    return page.locator(STORY_CARD_SELECTOR)


def open_story_grid(page):
    print(f"➡️  Opening: {START_URL}")
    page.goto(START_URL, wait_until="domcontentloaded", timeout=120000)
    if not wait_for_history_home(page, timeout_ms=60000):
        save_debug(page, tag="grid_home_not_found")
        raise RuntimeError("Unit History home screen not detected; login may have expired.")
    # Cards load asynchronously after the application shell appears.
    for _ in range(40):
        if get_story_card_count(page):
            return
        page.wait_for_timeout(1000)
    save_debug(page, tag="story_cards_not_found")
    raise RuntimeError("Authenticated Unit History page detected, but no story cards were recognized.")


def choose_results_page(page, number):
    """Click the numbered paginator without selecting other numeric text."""
    if number == 1:
        return True
    candidates = page.get_by_role("button", name=str(number), exact=True)
    if candidates.count() == 0:
        candidates = page.get_by_text(str(number), exact=True)
    for i in range(candidates.count()):
        target = candidates.nth(i)
        try:
            if target.is_visible():
                target.click(timeout=5000)
                for _ in range(20):
                    page.wait_for_timeout(500)
                    if get_story_card_count(page):
                        return True
        except Exception:
            continue
    return False


def get_card_title(card_locator) -> str:
    for sel in ["h2", "h3", "[role='heading']"]:
        try:
            t = card_locator.locator(sel).first.inner_text(timeout=500).strip()
            if t:
                return t
        except Exception:
            pass

    for sel in ["strong", "b"]:
        try:
            t = card_locator.locator(sel).first.inner_text(timeout=500).strip()
            if t:
                return t
        except Exception:
            pass

    try:
        a = card_locator.get_attribute("aria-label") or ""
        if a.strip():
            return a.strip()
    except Exception:
        pass

    try:
        txt = card_locator.inner_text(timeout=1000).strip()
        txt = re.sub(r"\s+", " ", txt)
        if txt:
            return txt[:80]
    except Exception:
        pass

    return "Untitled"


# ---------------------------
# Story page: extract images + metadata
# ---------------------------
def extract_image_urls_from_dom(page) -> list[str]:
    raw = page.evaluate("""
      () => {
        const urls = new Set();

        for (const img of Array.from(document.querySelectorAll('img'))) {
          const src = img.getAttribute('src') || '';
          const dataSrc = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
          const srcset = img.getAttribute('srcset') || '';
          if (src) urls.add(src);
          if (dataSrc) urls.add(dataSrc);
          if (srcset) urls.add(srcset);
        }

        for (const s of Array.from(document.querySelectorAll('picture source'))) {
          const srcset = s.getAttribute('srcset') || '';
          if (srcset) urls.add(srcset);
        }

        return Array.from(urls);
      }
    """)

    out = set()
    for item in raw:
        if not item:
            continue
        item = str(item).strip()

        if "," in item and ("w" in item or "x" in item):
            best = pick_largest_from_srcset(item)
            best = normalize_img_url(best)
            if best:
                out.add(best)
        else:
            u = normalize_img_url(item)
            if u:
                out.add(u)

    cleaned = []
    for u in out:
        low = u.lower()
        if low.startswith("data:") or low.startswith("blob:"):
            continue
        cleaned.append(u)

    cleaned.sort()
    return cleaned


def guess_story_title_date(page) -> tuple[str, str]:
    title = "Untitled"
    date_str = ""

    try:
        title = page.locator("h1").first.inner_text(timeout=8000).strip() or "Untitled"
    except Exception:
        pass

    try:
        date_candidate = page.locator(
            "text=/\\b(Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\\b/i"
        ).first
        date_str = date_candidate.inner_text(timeout=3000).strip()
        if len(date_str) > 40:
            date_str = date_str[:40].strip()
    except Exception:
        date_str = ""

    return title, date_str


def download_file_via_context(context, url: str, dest_path: pathlib.Path) -> bool:
    try:
        resp = context.request.get(url, timeout=120_000)
        if not resp.ok:
            return False
        dest_path.write_bytes(resp.body())
        return True
    except Exception:
        return False


def _try_close_lightbox(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass

    for sel in [
        "button[aria-label*='Close']",
        "button[title*='Close']",
        "[role='button'][aria-label*='Close']",
        "button:has-text('Close')",
        "button:has-text('close')",
        "button:has-text('×')",
        "button:has-text('X')",
        "svg[aria-label*='Close']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=800)
                page.wait_for_timeout(250)
                break
        except Exception:
            pass


def collect_fullsize_urls_via_lightbox(page) -> list[str]:
    thumb_selectors = [
        "main img",
        "article img",
        "img",
    ]

    lightbox_img_selectors = [
        "[role='dialog'] img",
        ".modal img",
        ".lightbox img",
        "div[aria-modal='true'] img",
        "img[style*='max-width']",
        "img[style*='maxHeight']",
    ]

    thumbs = None
    for sel in thumb_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            thumbs = loc
            break

    if thumbs is None or thumbs.count() == 0:
        return []

    candidate_indices = []
    count = thumbs.count()
    for i in range(count):
        try:
            el = thumbs.nth(i)
            box = el.bounding_box()
            if not box:
                continue
            if box["width"] < 120 or box["height"] < 90:
                continue
            src = (el.get_attribute("src") or "") + " " + (el.get_attribute("srcset") or "")
            src = src.lower()
            if "unithistory" in src or "blob.core.windows.net" in src or "image" in src:
                candidate_indices.append(i)
            else:
                candidate_indices.append(i)
        except Exception:
            continue

    seen = set()
    ordered = []
    for i in candidate_indices:
        if i not in seen:
            seen.add(i)
            ordered.append(i)

    full_urls = []
    for idx in ordered:
        try:
            thumb = thumbs.nth(idx)
            thumb.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(150)

            try:
                thumb.click(timeout=5000)
            except Exception:
                try:
                    thumb.locator("xpath=..").click(timeout=5000)
                except Exception:
                    continue

            page.wait_for_timeout(350)

            best = ""
            for sel in lightbox_img_selectors:
                try:
                    img = page.locator(sel).first
                    if img.count() == 0:
                        continue
                    srcset = img.get_attribute("srcset") or ""
                    if srcset.strip():
                        best = pick_largest_from_srcset(srcset)
                        best = normalize_img_url(best)
                    if not best:
                        src = img.get_attribute("src") or ""
                        best = normalize_img_url(src)
                    if best:
                        break
                except Exception:
                    continue

            if best:
                full_urls.append(best)

            _try_close_lightbox(page)
            page.wait_for_timeout(150)

        except Exception:
            _try_close_lightbox(page)
            continue

    upgraded = [strip_downscaling_params(u) for u in full_urls]

    seen = set()
    out = []
    for u in upgraded:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def download_current_story(page, context, out_root: pathlib.Path) -> dict:
    try:
        page.wait_for_selector("h1", timeout=45_000)
    except PWTimeoutError:
        save_debug(page, tag="story_no_h1")

    scroll_to_load(page, max_scrolls=18, pause_ms=350)

    title, date_str = guess_story_title_date(page)
    folder_base = safe_name(title)
    if date_str:
        folder_base = safe_name(f"{folder_base} - {date_str}")

    story_folder = out_root / folder_base

    # Skip rule: if already exists, skip entirely
    if SKIP_EXISTING_FOLDERS and story_folder.exists():
        print(f"⏭️  Skipping (folder already exists): {story_folder}")
        return {
            "final_url": page.url,
            "title": title,
            "date_text": date_str,
            "skipped_existing_folder": True,
            "folder": str(story_folder),
            "image_count_found": 0,
            "images": [],
        }

    ensure_dir(story_folder)

    # 1) Best attempt: fullsize via lightbox
    image_urls = collect_fullsize_urls_via_lightbox(page)

    # 2) Fallback: DOM scrape
    if not image_urls:
        dom_urls = extract_image_urls_from_dom(page)
        dom_urls = [strip_downscaling_params(u) for u in dom_urls]
        seen = set()
        image_urls = []
        for u in dom_urls:
            if u and u not in seen:
                seen.add(u)
                image_urls.append(u)

    meta = {
        "final_url": page.url,
        "title": title,
        "date_text": date_str,
        "folder": str(story_folder),
        "image_count_found": len(image_urls),
        "images": [],
    }

    print(f"🗂️  Story: {title} ({len(image_urls)} images)")

    for idx, img_url in enumerate(image_urls, start=1):
        ext = file_ext_from_url(img_url)
        fname = f"{idx:03d}{ext}"
        dest = story_folder / fname

        ok = download_file_via_context(context, img_url, dest)
        if not ok:
            page.wait_for_timeout(600)
            ok = download_file_via_context(context, img_url, dest)

        meta["images"].append({
            "index": idx,
            "url": img_url,
            "file": str(dest),
            "downloaded": bool(ok),
        })

        if ok:
            print(f"  ✅ {fname}")
        else:
            print(f"  ⚠️  FAILED {fname}  ({img_url})")

    with open(story_folder / "story.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return meta


# ---------------------------
# Main
# ---------------------------
def main():
    out_root = pathlib.Path(OUT_DIR)
    ensure_dir(out_root)

    # Temp auth state path (in repo workspace)
    storage_state_path = "storage_state.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)

        # Username/password only: login headless each run, then use saved state for remainder of this run
        context = browser.new_context()
        page = context.new_page()

        attempt_headless_login(page)

        # Save state for the rest of this run (avoids re-login mid-run)
        context.storage_state(path=storage_state_path)
        print("🔐 Saved storage_state.json for this workflow run.")

        # Rebuild context using the stored state (cleaner + consistent)
        context.close()
        context = browser.new_context(storage_state=storage_state_path)
        page = context.new_page()

        # Open grid and discover numbered result pages.
        open_story_grid(page)
        first_page_count = get_story_card_count(page)
        print(f"✅ Found {first_page_count} cards on the first results page")
        all_meta = []
        processed = 0
        skipped = 0
        seen = set()
        page_number = 1
        max_pages = int(os.getenv("MAX_HISTORY_PAGES", "20"))
        total = 0

        while page_number <= max_pages:
            open_story_grid(page)
            if not choose_results_page(page, page_number):
                print(f"ℹ️ No numbered results page {page_number}; pagination complete.")
                break
            count = get_story_card_count(page)
            print(f"📄 Results page {page_number}: {count} cards")
            if count == 0:
                break
            page_new = 0
            for i in range(count):
                open_story_grid(page)
                if not choose_results_page(page, page_number):
                    raise RuntimeError(f"Could not return to results page {page_number}")
                cards = story_cards(page)
                if i >= cards.count():
                    save_debug(page, tag="card_count_changed")
                    break
                card = cards.nth(i)
                card_title = get_card_title(card)
                card_text = card.inner_text(timeout=5000).strip()
                # Use full card text to distinguish entries with similar titles.
                identifier = re.sub(r"\s+", " ", card_text).strip()
                if identifier in seen:
                    continue
                seen.add(identifier)
                page_new += 1
                total += 1
                print(f"📌 [{page_number}:{i+1}] {card_title}")
                clicked = False
                for target in [card, card.locator("a").first]:
                    try:
                        target.click(timeout=10000)
                        clicked = True
                        break
                    except Exception:
                        pass
                if not clicked:
                    save_debug(page, tag=f"cannot_click_card_{page_number}_{i+1}")
                    print("⚠️ Could not open this card; skipping.")
                    continue
                if is_login_page(page):
                    save_debug(page, tag="login_bounce_story")
                    raise RuntimeError("Authentication expired while opening a story.")
                meta = download_current_story(page, context, out_root)
                meta["grid_title_guess"] = card_title
                meta["results_page"] = page_number
                all_meta.append(meta)
                if meta.get("skipped_existing_folder"):
                    skipped += 1
                else:
                    processed += 1
            if page_new == 0:
                print("ℹ️ No new stories on this page; stopping.")
                break
            page_number += 1

        manifest = {
            "exported_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "base": BASE,
            "start_url": START_URL,
            "headless": bool(HEADLESS),
            "skip_existing_folders": bool(SKIP_EXISTING_FOLDERS),
            "story_count_found_on_grid": total,
            "results_pages_visited": page_number if page_number <= max_pages else max_pages,
            "story_count_downloaded_new": processed,
            "story_count_skipped_existing": skipped,
            "stories": all_meta,
        }

        ensure_dir(pathlib.Path(MANIFEST_PATH).parent)
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # Optional zip (handy for manual download too)
        zip_folder(out_root, ZIP_NAME)

        context.close()
        browser.close()

    print("\n✅ Done.")


if __name__ == "__main__":
    main()
