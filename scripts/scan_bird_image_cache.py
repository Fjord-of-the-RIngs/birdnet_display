#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from image_cache_utils import ALLOWED_IMAGE_EXTENSIONS, validate_cached_image
from path_config import PATHS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan the BirdNET display image cache for broken or unsupported images."
    )
    parser.add_argument(
        "--cache-dir",
        default=str(PATHS.image_cache_dir),
        help="Image cache directory to scan. Defaults to BIRDNET_IMAGE_CACHE_DIR.",
    )
    parser.add_argument(
        "--include-valid",
        action="store_true",
        help="Print valid image files as well as invalid ones.",
    )
    parser.add_argument(
        "--delete-invalid",
        action="store_true",
        help="Delete invalid image files. Without this flag, the scan is report-only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cache_dir = Path(args.cache_dir).resolve()

    if not cache_dir.is_dir():
        print(f"Cache directory does not exist: {cache_dir}", file=sys.stderr)
        return 2

    scanned = 0
    valid = 0
    invalid = 0
    skipped = 0
    deleted = 0

    for image_path in sorted(cache_dir.rglob("*")):
        if not image_path.is_file():
            continue
        if image_path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            skipped += 1
            continue

        scanned += 1
        result = validate_cached_image(image_path, cache_dir)
        rel_path = image_path.relative_to(cache_dir)
        if result.ok:
            valid += 1
            if args.include_valid:
                print(f"VALID   {rel_path} {result.image_format} {result.width}x{result.height}")
            continue

        invalid += 1
        print(f"INVALID {rel_path} reason={result.reason}")
        if args.delete_invalid:
            try:
                image_path.unlink()
                deleted += 1
                print(f"DELETED {rel_path}")
            except OSError as exc:
                print(f"ERROR   {rel_path} delete-failed:{exc}", file=sys.stderr)

    print(
        "SUMMARY "
        f"cache_dir={cache_dir} scanned_images={scanned} valid={valid} "
        f"invalid={invalid} skipped_non_images={skipped} deleted={deleted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
