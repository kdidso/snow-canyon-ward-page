from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[1]
EVENTS_DIR = REPO_ROOT / "unit-history" / "events"
OUTPUT_FILE = REPO_ROOT / "data" / "unit_history_index.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTS


def parse_manifest_date_text(date_text: str) -> str | None:
    """
    Convert strings like 'Apr 6, 2026' into ISO YYYY-MM-DD for sorting.
    Returns None if parsing fails.
    """
    raw = (date_text or "").strip()
    if not raw:
        return None

    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

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
    sort_date = parse_manifest_date_text(date_text)

    # Use manifest thumbnail if present; otherwise first image
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


def main() -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not EVENTS_DIR.exists():
        data = {"folders": []}
        OUTPUT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return

    folders = [p for p in EVENTS_DIR.iterdir() if p.is_dir()]
    records = [build_folder_record(folder) for folder in folders]

    # Dated folders first, newest to oldest. Undated folders last, alphabetical.
    records.sort(
        key=lambda r: (
            0 if r["sort_date"] else 1,
            -(int(r["sort_date"].replace("-", "")) if r["sort_date"] else 0),
            r["folder_name"].lower(),
        )
    )

    data = {"folders": records}
    OUTPUT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
