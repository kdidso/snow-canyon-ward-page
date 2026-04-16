from __future__ import annotations

import json
import re
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[1]
EVENTS_DIR = REPO_ROOT / "unit-history" / "events"
OUTPUT_FILE = REPO_ROOT / "data" / "unit_history_index.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTS


def parse_date_string(raw: str) -> str | None:
    """
    Convert strings like:
      'Apr 6, 2026'
      'March 1, 2025'
    into ISO YYYY-MM-DD.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def parse_folder_date(folder_name: str) -> str | None:
    """
    Try to find a date inside the folder name.

    Examples:
      '1 year anniversary of being a ward - Mar 1, 2026'
      'Fall scavenger hunt FHE - Nov 10, 2025'

    Special case:
      'March stake conference Saturday evening - March stake conference Saturday evening'
      => 2025-03-01
    """
    raw = (folder_name or "").strip()

    # Special case first
    if raw.lower() == "march stake conference saturday evening - march stake conference saturday evening":
        return "2025-03-01"

    # Look for trailing or embedded month-day-year text
    m = re.search(
        r"\b("
        r"Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|"
        r"Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December"
        r")\s+\d{1,2},\s+\d{4}\b",
        raw,
        re.IGNORECASE,
    )
    if m:
        return parse_date_string(m.group(0))

    return None


def build_folder_record(folder: Path) -> dict:
    manifest_path = folder / "manifest.json"
    manifest = {}

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    images = sorted(
        [p for p in folder.iterdir() if is_image_file(p)],
        key=lambda p: p.name.lower()
    )

    title = manifest.get("title") or folder.name
    date_text = (manifest.get("date_text") or "").strip()

    # First prefer manifest date_text; if blank, parse from folder name
    sort_date = parse_date_string(date_text) or parse_folder_date(folder.name)

    # If manifest is blank, you can still show the folder-name date on the page later if desired
    thumb_name = (manifest.get("thumbnail") or "").strip()
    thumb_path = None

    if thumb_name:
        candidate = folder / thumb_name
        if candidate.exists() and is_image_file(candidate):
            thumb_path = candidate

    if thumb_path is None and images:
        thumb_path = images[0]

    thumbnail_url = ""
    if thumb_path is not None:
        rel_thumb = thumb_path.relative_to(REPO_ROOT).as_posix()
        thumbnail_url = f"./{rel_thumb}"

    folder_rel = folder.relative_to(REPO_ROOT).as_posix()

    image_records = []
    for img in images:
        rel_img = img.relative_to(REPO_ROOT).as_posix()
        image_records.append({
            "name": img.name,
            "url": f"./{rel_img}",
            "path": rel_img,
        })

    return {
        "folder_name": folder.name,
        "folder": folder_rel,
        "title": title,
        "date_text": date_text,
        "sort_date": sort_date,
        "thumbnail_url": thumbnail_url,
        "photo_count": len(images),
        "images": image_records,
    }


def sort_key(record: dict):
    sort_date = record.get("sort_date")

    # Dated folders first, newest to oldest
    if sort_date:
        return (0, -int(sort_date.replace("-", "")), record["folder_name"].lower())

    # Undated folders last, alphabetical
    return (1, 0, record["folder_name"].lower())


def main() -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not EVENTS_DIR.exists():
        data = {"folders": []}
        OUTPUT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return

    folders = [p for p in EVENTS_DIR.iterdir() if p.is_dir()]
    records = [build_folder_record(folder) for folder in folders]
    records.sort(key=sort_key)

    data = {"folders": records}
    OUTPUT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
