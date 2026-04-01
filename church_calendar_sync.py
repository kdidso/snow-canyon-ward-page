from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from selenium import webdriver
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ============================================================
# CONFIG
# ============================================================

LCR_BASE = "https://lcr.churchofjesuschrist.org"
CHURCH_CALENDAR_BASE = "https://www.churchofjesuschrist.org"
CHURCH_CALENDAR_PAGE = f"{CHURCH_CALENDAR_BASE}/calendar/month?lang=eng"
CHURCH_TIMEZONE = ZoneInfo("America/Denver")

USERNAME = os.getenv("LDS_USERNAME", "").strip()
PASSWORD = os.getenv("LDS_PASSWORD", "").strip()

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

SYNC_DAYS_BACK = int(os.getenv("SYNC_DAYS_BACK", "14"))
SYNC_DAYS_FORWARD = int(os.getenv("SYNC_DAYS_FORWARD", "180"))
INCLUDE_HIDDEN_CALENDARS = os.getenv("CHURCH_INCLUDE_HIDDEN", "false").strip().lower() == "true"
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() != "false"
LONG_WAIT = 60

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ============================================================
# HELPERS
# ============================================================

def log(msg: str) -> None:
    print(f"[INFO] {msg}")


def err(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def now_local() -> datetime:
    return datetime.now(CHURCH_TIMEZONE)


def to_epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CHURCH_TIMEZONE)


def nonempty(values: List[Optional[str]]) -> List[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def build_date_range(days_back: int, days_forward: int) -> Tuple[int, int]:
    today = now_local()
    start = (today - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (today + timedelta(days=days_forward)).replace(hour=23, minute=59, second=59, microsecond=999000)
    return to_epoch_ms(start), to_epoch_ms(end)


# ============================================================
# SELENIUM LOGIN
# ============================================================

def make_driver() -> webdriver.Chrome:
    opts = ChromeOptions()
    if HEADLESS or os.getenv("CI", "").lower() == "true":
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1600,2200")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US")
    return webdriver.Chrome(options=opts)


def login(driver: webdriver.Chrome) -> None:
    if not USERNAME or not PASSWORD:
        err("Missing env vars LDS_USERNAME and/or LDS_PASSWORD")
        sys.exit(1)

    log("Opening LCR login page")
    driver.get(LCR_BASE)

    try:
        user_input = WebDriverWait(driver, LONG_WAIT).until(
            EC.presence_of_element_located((By.ID, "username-input"))
        )
        user_input.clear()
        user_input.send_keys(USERNAME)
        user_input.send_keys(Keys.ENTER)

        pwd_input = WebDriverWait(driver, LONG_WAIT).until(
            EC.presence_of_element_located((By.ID, "password-input"))
        )
        pwd_input.clear()
        pwd_input.send_keys(PASSWORD)
        pwd_input.send_keys(Keys.ENTER)

        WebDriverWait(driver, LONG_WAIT).until(
            EC.url_contains("churchofjesuschrist.org")
        )
        log("Login submitted successfully")
    except Exception as ex:
        raise RuntimeError("Automated login failed with the known username/password flow.") from ex


def build_requests_session_from_driver(driver: webdriver.Chrome) -> requests.Session:
    session = requests.Session()

    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )

    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Referer": CHURCH_CALENDAR_PAGE,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


# ============================================================
# CHURCH EVENT FETCH
# ============================================================

def church_events_api_url(start_ms: int, end_ms: int, include_hidden_calendars: bool) -> str:
    hidden_value = "true" if include_hidden_calendars else "false"
    return (
        f"{CHURCH_CALENDAR_BASE}/church-calendar/services/v3.0/evt/calendar/"
        f"{start_ms}-{end_ms}?includeLocationEvents=true&includeHiddenCalendars={hidden_value}"
    )


def fetch_json(session: requests.Session, url: str) -> dict | list:
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_church_events(
    session: requests.Session,
    days_back: int,
    days_forward: int,
    include_hidden_calendars: bool,
) -> List[Dict[str, Any]]:
    start_ms, end_ms = build_date_range(days_back, days_forward)
    url = church_events_api_url(start_ms, end_ms, include_hidden_calendars)
    log(f"Fetching church events: {url}")
    payload = fetch_json(session, url)

    if not isinstance(payload, list):
        raise RuntimeError("Church calendar API did not return a list.")

    log(f"Events fetched: {len(payload)}")
    return payload


# ============================================================
# GOOGLE CALENDAR API
# ============================================================

def build_google_service(service_account_json: str):
    if not service_account_json:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")

    info = json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=GOOGLE_SCOPES,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def build_google_location(evt: Dict[str, Any]) -> str:
    return ", ".join(
        nonempty([
            evt.get("address1"),
            evt.get("address2"),
            evt.get("city"),
            evt.get("stateProvince"),
            evt.get("postalCode"),
        ])
    )


def build_google_description(evt: Dict[str, Any]) -> str:
    parts: List[str] = []

    description = (evt.get("description") or "").strip()
    if description:
        parts.append(f"Description-\n{description}")

    event_contact = evt.get("eventContact") or {}
    contact_lines = nonempty([
        event_contact.get("name"),
        event_contact.get("phoneNbr"),
        event_contact.get("emailAddress"),
    ])
    if contact_lines:
        parts.append("Event contact-\n" + "\n".join(contact_lines))

    hoster = (evt.get("owningUnitName") or evt.get("calendarName") or "").strip()
    if hoster:
        parts.append(f"Event Hoster-\n{hoster}")

    source_id = str(evt.get("id") or "").strip()
    if source_id:
        parts.append(f"Source-\n{source_id}")

    return "\n\n".join(parts).strip()


def church_event_to_google_event(evt: Dict[str, Any]) -> Dict[str, Any]:
    source_id = str(evt.get("id") or "").strip()
    title = (evt.get("name") or "").strip() or f"Church Event {source_id}"
    description = build_google_description(evt)
    location = build_google_location(evt)

    body: Dict[str, Any] = {
        "summary": title,
        "description": description,
        "extendedProperties": {
            "private": {
                "churchSourceId": source_id,
                "churchUpdatedDate": str(evt.get("updatedDate") or ""),
                "churchCalendarId": str(evt.get("calendarId") or ""),
                "churchCalendarName": str(evt.get("calendarName") or ""),
            }
        },
    }

    if location:
        body["location"] = location

    all_day = bool(evt.get("allDayEvent"))
    start_dt = from_epoch_ms(int(evt["startTime"]))
    end_dt = from_epoch_ms(int(evt["endTime"]))

    if all_day:
        start_date = start_dt.date().isoformat()
        end_date = (end_dt.date() + timedelta(days=1)).isoformat()
        body["start"] = {"date": start_date, "timeZone": "America/Denver"}
        body["end"] = {"date": end_date, "timeZone": "America/Denver"}
    else:
        body["start"] = {
            "dateTime": start_dt.isoformat(),
            "timeZone": "America/Denver",
        }
        body["end"] = {
            "dateTime": end_dt.isoformat(),
            "timeZone": "America/Denver",
        }

    return body


def get_existing_google_events(
    service,
    calendar_id: str,
    days_back: int,
    days_forward: int,
) -> Dict[str, Dict[str, Any]]:
    now_dt = now_local()
    time_min = (now_dt - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = (now_dt + timedelta(days=days_forward)).replace(hour=23, minute=59, second=59, microsecond=999000)

    page_token = None
    existing: Dict[str, Dict[str, Any]] = {}

    while True:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.astimezone(timezone.utc).isoformat(),
                timeMax=time_max.astimezone(timezone.utc).isoformat(),
                singleEvents=True,
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )

        for item in response.get("items", []):
            source_id = (
                (((item.get("extendedProperties") or {}).get("private") or {}).get("churchSourceId"))
                or ""
            ).strip()
            if source_id:
                existing[source_id] = item

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return existing


def sync_events_to_google(service, calendar_id: str, church_events: List[Dict[str, Any]]) -> None:
    if not calendar_id:
        raise RuntimeError("Missing GOOGLE_CALENDAR_ID")

    existing = get_existing_google_events(service, calendar_id, SYNC_DAYS_BACK, SYNC_DAYS_FORWARD)
    created = 0
    updated = 0
    skipped = 0

    for evt in church_events:
        source_id = str(evt.get("id") or "").strip()
        if not source_id:
            skipped += 1
            continue

        body = church_event_to_google_event(evt)
        existing_item = existing.get(source_id)

        try:
            if existing_item:
                service.events().update(
                    calendarId=calendar_id,
                    eventId=existing_item["id"],
                    body=body,
                ).execute()
                updated += 1
            else:
                service.events().insert(
                    calendarId=calendar_id,
                    body=body,
                ).execute()
                created += 1
        except HttpError as ex:
            err(f"Google API error for source {source_id}: {ex}")

    log(f"Created: {created}")
    log(f"Updated: {updated}")
    log(f"Skipped: {skipped}")


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    if not USERNAME or not PASSWORD:
        err("Missing LDS_USERNAME and/or LDS_PASSWORD environment variables.")
        return 1

    driver = make_driver()
    try:
        login(driver)

        # Warm the church domain first, then the calendar page.
        driver.get(CHURCH_CALENDAR_BASE)
        log(f"After CHURCH_CALENDAR_BASE, current URL: {driver.current_url}")
        log(f"Page title: {driver.title}")

        WebDriverWait(driver, LONG_WAIT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        driver.get(CHURCH_CALENDAR_PAGE)
        log(f"After CHURCH_CALENDAR_PAGE, current URL: {driver.current_url}")
        log(f"Page title: {driver.title}")

        WebDriverWait(driver, LONG_WAIT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        for c in driver.get_cookies():
            domain = c.get("domain", "")
            if "churchofjesuschrist.org" in domain:
                log(f"Cookie loaded: {c.get('name')} | domain: {domain}")

        session = build_requests_session_from_driver(driver)

        church_events = fetch_church_events(
            session=session,
            days_back=SYNC_DAYS_BACK,
            days_forward=SYNC_DAYS_FORWARD,
            include_hidden_calendars=INCLUDE_HIDDEN_CALENDARS,
        )

        google_service = build_google_service(GOOGLE_SERVICE_ACCOUNT_JSON)
        sync_events_to_google(
            service=google_service,
            calendar_id=GOOGLE_CALENDAR_ID,
            church_events=church_events,
        )

        log("Church calendar sync completed successfully")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
