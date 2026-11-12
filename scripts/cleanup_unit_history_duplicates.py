#!/usr/bin/env python3
"""Audit and safely clean duplicate ward Unit History event folders.

Run from a checked-out snow-canyon-ward-page repository (or use --repo).

  python scripts/cleanup_unit_history_duplicates.py
  python scripts/cleanup_unit_history_duplicates.py --apply

Default is a dry run: writes a JSON report without changing events or the index.
--apply moves verified duplicate folders to a backup outside the repository,
removes duplicate index entries, and redirects manifest references to the
retained original folders. Original website image files are untouched.
Optional --upgrade-photos replaces thumbnail-size originals with their larger
counterparts, backing up the original files first. Beware repository/site size.
Changes must be committed by the user.

Rules: normalized full title + calendar date AND same Church story UUID AND
same ordered source photo filenames AND matching downloaded photo inventory.
The existing, unpadded-day folder is favored to protect website references.
A failed/ambiguous match is reported, never automatically removed.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, unquote

MONTHS = {month.lower(): i for i, month in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
MONTH_PATTERN = '|'.join((
    'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December',
    'Jan', 'Feb', 'Mar', 'Apr', 'Jun', 'Jul', 'Aug', 'Sep', 'Sept', 'Oct', 'Nov', 'Dec'))
EVENT_SUFFIX = re.compile(rf'^(.*?)\s+-\s+({MONTH_PATTERN})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})$', re.I)
STORY_UUID = re.compile(r'/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)', re.I)
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.heic'}


def norm_title(value: str) -> str:
    t = unicodedata.normalize('NFKD', str(value or '')).casefold()
    t = ''.join(c for c in t if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', t.replace('&', ' and ')).split())


def folder_key(name: str):
    m = EVENT_SUFFIX.fullmatch(name)
    if not m:
        return None
    title, month_text, day, year = m.groups()
    month = MONTHS.get(month_text[:3].lower())
    try:
        iso = datetime(int(year), month, int(day)).date().isoformat()
    except (ValueError, TypeError):
        return None
    return (iso, norm_title(title))


def story_id(metadata: dict):
    for key in ('final_url', 'url', 'story_url'):
        m = STORY_UUID.search(urlsplit(str(metadata.get(key) or '')).path)
        if m:
            return m.group(1).lower()
    return None


def source_photo_names(metadata: dict):
    images = metadata.get('images')
    if not isinstance(images, list) or not images:
        return None
    names = []
    for image in images:
        if not isinstance(image, dict):
            return None
        url = image.get('url')
        if not url:
            return None
        names.append(unquote(urlsplit(url).path.rsplit('/', 1)[-1]).casefold())
    return names


def physical_photos(folder: Path):
    return sorted(p.name.casefold() for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def read_json(path: Path):
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def atomic_json(path: Path, obj):
    temp = path.with_name(path.name + '.cleanup-tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write('\n')
    temp.replace(path)


def preferred_order(folder: Path, index_names: set):
    # Existing original folders use "Nov 6"; newer dupes use "Nov 06".
    m = EVENT_SUFFIX.fullmatch(folder.name)
    is_zero_padded = bool(m and len(m.group(3)) == 2 and m.group(3).startswith('0'))
    return (is_zero_padded, folder.name not in index_names, folder.name.casefold())


def replace_path_refs(node, mapping: dict[str, str]):
    """Only replace path substrings; do not alter story titles, dates or URLs to Church."""
    if isinstance(node, list):
        return [replace_path_refs(item, mapping) for item in node]
    if isinstance(node, dict):
        return {key: replace_path_refs(value, mapping) for key, value in node.items()}
    if isinstance(node, str):
        for old, new in mapping.items():
            # Prefix paths with final slash to prevent collision with similar names.
            node = node.replace('unit-history/events/' + old + '/',
                                'unit-history/events/' + new + '/')
            if node == 'unit-history/events/' + old:
                node = 'unit-history/events/' + new
        return node
    return node


def validate_pair(keep: Path, duplicate: Path, index_by_name: dict):
    errors = []
    try:
        original = read_json(keep / 'story.json')
        extra = read_json(duplicate / 'story.json')
    except (ValueError, OSError) as e:
        return False, [f'Cannot read story.json: {e}'], {}
    aid, bid = story_id(original), story_id(extra)
    if not aid or aid != bid:
        errors.append(f'Church story ID mismatch or absent ({aid!r} vs {bid!r})')
    asrc, bsrc = source_photo_names(original), source_photo_names(extra)
    if asrc is None or asrc != bsrc:
        errors.append('Original source photo filename/order differs or is missing')
    aphys, bphys = physical_photos(keep), physical_photos(duplicate)
    if not aphys or aphys != bphys:
        errors.append(f'Downloaded photo filenames differ ({len(aphys)} vs {len(bphys)})')
    if asrc is not None and (len(aphys) != len(asrc) or len(bphys) != len(bsrc or [])):
        errors.append('Downloaded photo count differs from story metadata')
    if keep.name not in index_by_name or duplicate.name not in index_by_name:
        errors.append('Both folders must have index entries before automatic removal')
    else:
        keep_row = index_by_name[keep.name]
        dup_row = index_by_name[duplicate.name]
        if keep_row.get('photo_count') != dup_row.get('photo_count'):
            errors.append('Index photo_count differs')
        for label, record, photo_list in [('original', keep_row, aphys), ('duplicate', dup_row, bphys)]:
            images = record.get('images') or []
            indexed = sorted(str(x.get('name', '')).casefold() for x in images if isinstance(x, dict))
            if indexed != photo_list:
                errors.append(f'{label} index photo list differs from physical folder')
    upgrades = [f for f in physical_photos(keep)
                if (duplicate / f).stat().st_size > (keep / f).stat().st_size] if not errors else []
    info = {'story_id': aid, 'photos_original': len(aphys), 'photos_duplicate': len(bphys),
            'same_original_source_photo_names': asrc is not None and asrc == bsrc,
            'higher_resolution_candidates': len(upgrades),
            'original_photo_bytes': sum((keep / f).stat().st_size for f in aphys),
            'duplicate_photo_bytes': sum((duplicate / f).stat().st_size for f in bphys)}
    return not errors, errors, info


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent.parent,
                        help='Repository root (default: parent of scripts/)')
    parser.add_argument('--apply', action='store_true', help='Back up duplicates and remove duplicate index entries')
    parser.add_argument('--upgrade-photos', action='store_true', help='With --apply, replace retained thumbnail images with higher-resolution photos')
    parser.add_argument('--backup-dir', type=Path, help='Backup destination OUTSIDE repository (apply only)')
    parser.add_argument('--report', type=Path, help='Report filename (default: unit-history/duplicate_cleanup_report.json)')
    args = parser.parse_args()
    if args.upgrade_photos and not args.apply:
        parser.error('--upgrade-photos requires --apply')

    repo = args.repo.resolve()
    events = repo / 'unit-history' / 'events'
    index_path = repo / 'data' / 'unit_history_index.json'
    manifest_path = repo / 'unit-history' / 'manifest.json'
    if not events.is_dir() or not index_path.is_file():
        parser.error(f'Expected {events} and {index_path}; specify --repo /path/to/repo')

    index = read_json(index_path)
    if not isinstance(index, dict) or not isinstance(index.get('folders'), list):
        parser.error('Unexpected index format: expected {"folders": [...]}')
    rows = index['folders']
    indexed = {r['folder_name']: r for r in rows if isinstance(r, dict) and isinstance(r.get('folder_name'), str)}
    buckets = defaultdict(list)
    for d in events.iterdir():
        if d.is_dir():
            key = folder_key(d.name)
            if key:
                buckets[key].append(d)

    pairs = []
    for key, folders in sorted(buckets.items()):
        if len(folders) < 2:
            continue
        folders.sort(key=lambda p: preferred_order(p, set(indexed)))
        keep = folders[0]
        for extra in folders[1:]:
            ok, problems, evidence = validate_pair(keep, extra, indexed)
            pairs.append({'title': key[1], 'event_date': key[0], 'preserve': keep.name,
                          'duplicate': extra.name, 'verified': ok, 'blockers': problems,
                          **evidence})

    verified = [p for p in pairs if p['verified']]
    ambiguous = [p for p in pairs if not p['verified']]
    report = {'mode': 'apply' if args.apply else 'dry-run',
              'event_folders_scanned': len([p for p in events.iterdir() if p.is_dir()]),
              'index_entries_before': len(rows), 'confirmed_duplicate_folders': len(verified),
              'needs_manual_review': len(ambiguous), 'pairs': pairs}

    print(f"Found {len(verified)} verified duplicates, {len(ambiguous)} needing review.")
    for p in pairs:
        print(f"{'SAFE' if p['verified'] else 'REVIEW'}: {p['duplicate']} -> {p['preserve']}")
        if p['blockers']:
            for problem in p['blockers']:
                print(f'    - {problem}')

    report_path = (args.report or (repo / 'unit-history' / 'duplicate_cleanup_report.json')).resolve()
    if args.apply:
        if ambiguous:
            print('REFUSING --apply: ambiguous duplicates exist; inspect report first.', file=sys.stderr)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(report_path, report)
            return 2
        if not verified:
            print('Nothing to clean.')
        else:
            backup_root = (args.backup_dir or (repo.parent / (repo.name + '-unit-history-duplicate-backup'))).resolve()
            if backup_root == repo or repo in backup_root.parents or backup_root in repo.parents:
                parser.error('Backup destination must be outside the Git repository and not an ancestor of it')
            backup_root.mkdir(parents=True, exist_ok=True)
            conflicts = [p['duplicate'] for p in verified if (backup_root / 'duplicate-folders' / p['duplicate']).exists()]
            if args.upgrade_photos:
                for p in verified:
                    keep = events / p['preserve']
                    extra = events / p['duplicate']
                    for photo in physical_photos(keep):
                        if (extra / photo).stat().st_size > (keep / photo).stat().st_size:
                            prior = backup_root / 'replaced-original-photos' / p['preserve'] / photo
                            if prior.exists():
                                conflicts.append(str(prior))
            if conflicts:
                parser.error('Backup already exists for: ' + ', '.join(conflicts))
            # Back up the duplicate folder FIRST. Copy its higher-resolution photos
            # to the existing original paths, backing up thumbnails before replacement.
            # Both variants are saved outside the repository for local recovery.
            photo_upgrades = 0
            for p in verified:
                keep = events / p['preserve']
                extra = events / p['duplicate']
                saved = backup_root / 'duplicate-folders' / p['duplicate']
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(extra), str(saved))
                if args.upgrade_photos:
                    for photo in physical_photos(keep):
                        current = keep / photo
                        improved = saved / photo
                        if improved.stat().st_size > current.stat().st_size:
                            prior = backup_root / 'replaced-original-photos' / p['preserve'] / photo
                            prior.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(current, prior)
                            shutil.copy2(improved, current)
                            photo_upgrades += 1
            removed = {p['duplicate'] for p in verified}
            mapping = {p['duplicate']: p['preserve'] for p in verified}
            index['folders'] = [r for r in rows if r.get('folder_name') not in removed]
            # Preserve the kept index entries and their existing image URLs as-is.
            atomic_json(index_path, index)
            if manifest_path.exists():
                manifest = read_json(manifest_path)
                manifest = replace_path_refs(manifest, mapping)
                atomic_json(manifest_path, manifest)
            report['backup_directory'] = str(backup_root)
            report['index_entries_after'] = len(index['folders'])
            report['original_photos_upgraded'] = photo_upgrades
            report['upgrades_requested'] = args.upgrade_photos
            print(f"Backed up {len(verified)} duplicate folders to {backup_root}")
            if args.upgrade_photos:
                print(f'Upgraded {photo_upgrades} original image files, keeping their existing paths')
            else:
                print('Existing website image files and image quality left unchanged')
            print(f"Updated index: {len(rows)} -> {len(index['folders'])} entries")
            print('To publish deletions in GitHub, run:')
            print('  git add -u unit-history/events data/unit_history_index.json unit-history/manifest.json')
            print('  git commit -m "Remove verified Unit History duplicate events"')
            print('  git push')
    else:
        print('DRY RUN ONLY: no event folder or index has been changed.')
        print('When ready, run again with --apply to back up duplicates and preserve existing photos.')
        print('Optional: --apply --upgrade-photos replaces old thumbnail images with larger copies (may exceed Pages size limits).')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(report_path, report)
    print(f'Full report: {report_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
