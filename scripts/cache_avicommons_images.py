#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import requests

from avicommons import (
    AVICOMMONS_SIZES,
    DEFAULT_JSON_CACHE,
    DEFAULT_SIZE,
    SpeciesRecord,
    attribution_text,
    build_metadata_entry,
    canonical_folder_name,
    fetch_avicommons_json,
    image_filename,
    image_url,
    index_avicommons_entries,
    load_default_species,
    load_species_from_csv,
    load_species_from_db,
    match_avicommons_entries,
    read_species_metadata,
    write_species_metadata,
)
from image_cache_utils import is_allowed_image_filename, validate_cached_image
from path_config import PATHS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add Avicommons photos to BirdNET Display species folders."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--from-db", action="store_true", help="Read species from the BirdNET SQLite DB.")
    source.add_argument("--from-species-list", action="store_true", help="Read species from species_list.csv.")
    parser.add_argument("--species", action="append", default=[], help="Limit to one common name. Repeatable.")
    parser.add_argument("--cache-dir", default=str(PATHS.image_cache_dir), help="Bird image cache directory.")
    parser.add_argument("--json-cache", default=str(DEFAULT_JSON_CACHE), help="Local Avicommons JSON cache path.")
    parser.add_argument("--refresh-json", action="store_true", help="Fetch a fresh Avicommons JSON file.")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE, choices=sorted(AVICOMMONS_SIZES))
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of Avicommons images to download. 0 means no limit.")
    parser.add_argument(
        "--max-per-species",
        type=int,
        default=1,
        help="Maximum valid Avicommons images per species folder. Default: 1.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report planned downloads without writing files.")
    parser.add_argument(
        "--licenses",
        default="",
        help="Comma-separated license allowlist. Empty accepts all Avicommons licenses.",
    )
    return parser.parse_args()


def selected_species(args: argparse.Namespace) -> list[SpeciesRecord]:
    if args.species:
        requested = {name.strip().lower() for name in args.species if name.strip()}
        all_records = load_default_species()
        selected = [record for record in all_records if record.common_name.lower() in requested]
        found = {record.common_name.lower() for record in selected}
        for name in requested - found:
            selected.append(SpeciesRecord(name))
        return selected
    if args.from_db:
        return load_species_from_db()
    if args.from_species_list:
        return load_species_from_csv()
    return load_default_species()


def write_download(response: requests.Response, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=dest_path.parent, delete=False) as tmp:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                tmp.write(chunk)
        temp_path = Path(tmp.name)
    try:
        # link() fails if a concurrent process created the destination, unlike
        # replace(), which could overwrite an existing photo.
        os.link(temp_path, dest_path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _existing_image_sets(folder_path: Path, cache_dir: Path) -> tuple[int, int, set[str], set[tuple[str, str, int]]]:
    """Return local and Avicommons valid counts plus known Avicommons identities."""
    metadata = read_species_metadata(folder_path)
    metadata_names = {
        name
        for name, item in metadata.items()
        if isinstance(item, dict) and item.get("source") == "avicommons"
    }
    identities: set[tuple[str, str, int]] = set()
    for item in metadata.values():
        if not isinstance(item, dict) or item.get("source") != "avicommons":
            continue
        try:
            size = int(item.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        identities.add((str(item.get("code", "")), str(item.get("key", "")), size))
    local_count = avicommons_count = 0
    filenames: set[str] = set()
    if not folder_path.is_dir():
        return local_count, avicommons_count, filenames, identities

    for image_path in folder_path.iterdir():
        if not image_path.is_file() or not is_allowed_image_filename(image_path.name):
            continue
        filenames.add(image_path.name)
        validation = validate_cached_image(image_path, cache_dir)
        if not validation.ok:
            continue
        if image_path.name in metadata_names or image_path.name.startswith("avicommons_"):
            avicommons_count += 1
        else:
            local_count += 1
    return local_count, avicommons_count, filenames, identities


def main() -> int:
    args = parse_args()
    cache_dir = Path(args.cache_dir).resolve()
    if args.limit < 0 or args.max_per_species < 1:
        print("--limit must be non-negative and --max-per-species must be at least 1.")
        return 2
    accepted_licenses = {
        item.strip().lower()
        for item in args.licenses.split(",")
        if item.strip()
    }

    species_records = selected_species(args)
    if not species_records:
        print("No species found from DB or species_list.csv.")
        return 1

    print(f"[AVICOMMONS] Loading JSON cache={args.json_cache} refresh={args.refresh_json}")
    entries = fetch_avicommons_json(json_cache=args.json_cache, refresh=args.refresh_json)
    index = index_avicommons_entries(entries)
    print(f"[AVICOMMONS] Loaded {len(entries)} entries; checking {len(species_records)} species.")

    matched = skipped_max = skipped_duplicate = skipped_license = missing = downloaded = invalid = errors = planned = 0

    for species in species_records:
        folder_name = canonical_folder_name(species.common_name)
        folder_path = cache_dir / folder_name

        local_count, avicommons_count, existing_filenames, existing_identities = _existing_image_sets(
            folder_path, cache_dir
        )
        entries_for_species, match_reason = match_avicommons_entries(species, index)
        if not entries_for_species:
            missing += 1
            print(
                f"[AVICOMMONS] species={species.common_name!r} match=none "
                f"local_images={local_count} avicommons_images={avicommons_count} "
                f"max_per_species={args.max_per_species} planned=0 skip=no-avicommons-match"
            )
            continue

        matched += 1
        remaining = args.max_per_species - avicommons_count
        if remaining <= 0:
            skipped_max += 1
            print(
                f"[AVICOMMONS] species={species.common_name!r} match={match_reason} "
                f"local_images={local_count} avicommons_images={avicommons_count} "
                f"max_per_species={args.max_per_species} planned=0 skip=max-per-species-reached"
            )
            continue

        candidates = []
        candidate_filenames = set(existing_filenames)
        candidate_identities = set(existing_identities)
        license_rejected = duplicate_rejected = 0
        for entry in entries_for_species:
            filename = image_filename(entry, args.size)
            identity = (str(entry.get("code", "")), str(entry.get("key", "")), args.size)
            license_name = str(entry.get("license", "")).lower()
            if accepted_licenses and license_name not in accepted_licenses:
                skipped_license += 1
                license_rejected += 1
                continue
            if filename in candidate_filenames or identity in candidate_identities:
                skipped_duplicate += 1
                duplicate_rejected += 1
                continue
            candidates.append((entry, filename, identity))
            candidate_filenames.add(filename)
            candidate_identities.add(identity)

        to_add = candidates[:remaining]
        if args.limit:
            to_add = to_add[: max(0, args.limit - downloaded - planned)]

        if not to_add:
            if candidates:
                skip_reason = "download-limit-reached"
            elif license_rejected and not duplicate_rejected:
                skip_reason = "license-not-allowed"
            else:
                skip_reason = "duplicate-avicommons-image"
            print(
                f"[AVICOMMONS] species={species.common_name!r} match={match_reason} "
                f"local_images={local_count} avicommons_images={avicommons_count} "
                f"max_per_species={args.max_per_species} planned=0 skip={skip_reason}"
            )
            if args.limit and (downloaded + planned) >= args.limit:
                break
            continue

        print(
            f"[AVICOMMONS] species={species.common_name!r} match={match_reason} "
            f"local_images={local_count} avicommons_images={avicommons_count} "
            f"max_per_species={args.max_per_species} planned={len(to_add)}"
        )
        for entry, filename, identity in to_add:
            source_url = image_url(entry, args.size)
            dest_path = folder_path / filename
            metadata = build_metadata_entry(entry, filename, source_url, args.size)
            if args.dry_run:
                planned += 1
                print(
                    f"[AVICOMMONS] DRY-RUN download species={species.common_name!r} "
                    f"file={filename!r} url={source_url} attribution={attribution_text(entry)!r}"
                )
                continue

            created_download = False
            validated_download = False
            try:
                response = requests.get(source_url, timeout=30, stream=True)
                response.raise_for_status()
                write_download(response, dest_path)
                created_download = True
                validation = validate_cached_image(dest_path, cache_dir)
                if not validation.ok:
                    invalid += 1
                    if created_download:
                        try:
                            dest_path.unlink()
                        except OSError:
                            pass
                    print(
                        f"[AVICOMMONS] INVALID species={species.common_name!r} "
                        f"path={dest_path} reason={validation.reason}"
                    )
                    continue
                validated_download = True

                txt_path = folder_path / (dest_path.stem + ".txt")
                if not txt_path.exists():
                    txt_path.write_text(attribution_text(entry), encoding="utf-8")
                species_metadata = read_species_metadata(folder_path)
                species_metadata[filename] = metadata
                write_species_metadata(folder_path, species_metadata)
                downloaded += 1
                print(
                    f"[AVICOMMONS] SAVED species={species.common_name!r} "
                    f"sci={species.scientific_name!r} match={match_reason} "
                    f"path={dest_path} license={entry.get('license', '')!r}"
                )
            except Exception as exc:
                errors += 1
                if created_download and not validated_download:
                    try:
                        dest_path.unlink()
                    except OSError:
                        pass
                print(f"[AVICOMMONS] ERROR species={species.common_name!r} url={source_url} error={exc}")

        if args.limit and (downloaded + planned) >= args.limit:
            break

    print(
        "[AVICOMMONS] SUMMARY "
        f"matched={matched} planned={planned} downloaded={downloaded} "
        f"skipped_max={skipped_max} skipped_duplicate={skipped_duplicate} skipped_license={skipped_license} "
        f"missing={missing} invalid={invalid} errors={errors} dry_run={args.dry_run}"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
