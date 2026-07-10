#!/usr/bin/env python3
"""Build lightweight display-ready copies of BirdNET Display source photos."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from image_cache_utils import is_allowed_image_filename, validate_cached_image
from path_config import PATHS

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
DEFAULT_MAX_EDGE = 900
DEFAULT_WEBP_QUALITY = 82


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create optimized WebP display copies while preserving original bird photos."
    )
    parser.add_argument("--source-dir", default=str(PATHS.image_cache_dir))
    parser.add_argument("--display-cache-dir", default=str(PATHS.display_image_cache_dir))
    parser.add_argument("--max-edge", type=int, default=DEFAULT_MAX_EDGE)
    parser.add_argument("--quality", type=int, default=DEFAULT_WEBP_QUALITY)
    parser.add_argument("--force", action="store_true", help="Rebuild copies even when the source is unchanged.")
    parser.add_argument("--dry-run", action="store_true", help="Report work without writing copies or a manifest.")
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries", {}) if isinstance(data, dict) else {}
    return entries if isinstance(entries, dict) else {}


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    temp_path.replace(path)


def manifest_payload(entries: dict[str, dict], args: argparse.Namespace) -> dict:
    return {
        "version": MANIFEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_edge": args.max_edge,
        "quality": args.quality,
        "entries": entries,
    }


def output_relative_path(source_relative: Path) -> Path:
    digest = hashlib.sha256(source_relative.as_posix().encode("utf-8")).hexdigest()[:12]
    return source_relative.parent / f"image-{digest}.webp"


def source_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def build_copy(source: Path, destination: Path, max_edge: int, quality: int) -> tuple[int, int]:
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".webp", dir=destination.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            image.save(temp_path, format="WEBP", quality=quality, method=4)
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)
        return image.size


def main() -> int:
    args = parse_args()
    source_dir = Path(args.source_dir).resolve()
    display_dir = Path(args.display_cache_dir).resolve()
    if args.max_edge < 1 or not 1 <= args.quality <= 100:
        print("--max-edge must be positive and --quality must be between 1 and 100.", file=sys.stderr)
        return 2
    if not source_dir.is_dir():
        print(f"Source image cache does not exist: {source_dir}", file=sys.stderr)
        return 2
    if display_dir == source_dir or source_dir in display_dir.parents:
        print("Display cache must not be inside the source image cache.", file=sys.stderr)
        return 2

    manifest_path = display_dir / MANIFEST_NAME
    previous_entries = load_manifest(manifest_path)
    entries: dict[str, dict] = {}
    scanned = converted = skipped = invalid = errors = planned = 0

    for source in sorted(source_dir.rglob("*")):
        if not source.is_file() or not is_allowed_image_filename(source.name):
            continue
        scanned += 1
        relative = source.relative_to(source_dir)
        relative_key = relative.as_posix()
        validation = validate_cached_image(source, source_dir)
        if not validation.ok:
            invalid += 1
            print(f"[DISPLAY-CACHE] INVALID source={relative_key!r} reason={validation.reason}")
            continue

        mtime_ns, source_size = source_signature(source)
        output_relative = output_relative_path(relative)
        destination = display_dir / output_relative
        previous = previous_entries.get(relative_key, {})
        unchanged = (
            not args.force
            and previous.get("source_mtime_ns") == mtime_ns
            and previous.get("source_size") == source_size
            and previous.get("output") == output_relative.as_posix()
            and destination.is_file()
        )
        if unchanged:
            entries[relative_key] = previous
            skipped += 1
            continue

        # Rename existing derivatives without recompressing when only the
        # route-safe output filename scheme changed.
        previous_output = previous.get("output") if isinstance(previous, dict) else None
        previous_path = display_dir / previous_output if isinstance(previous_output, str) else None
        if (
            not args.force
            and previous.get("source_mtime_ns") == mtime_ns
            and previous.get("source_size") == source_size
            and previous_path is not None
            and previous_path.is_file()
        ):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.link(previous_path, destination)
            entries[relative_key] = {**previous, "output": output_relative.as_posix()}
            skipped += 1
            continue

        # A prior interrupted run may have finished the atomic image write before
        # it reached its next manifest checkpoint. Adopt that safe output instead
        # of doing expensive work again.
        if (
            not args.force
            and not previous
            and destination.is_file()
            and destination.stat().st_mtime_ns >= mtime_ns
        ):
            entries[relative_key] = {
                "output": output_relative.as_posix(),
                "source_mtime_ns": mtime_ns,
                "source_size": source_size,
                "width": 0,
                "height": 0,
            }
            skipped += 1
            continue

        if args.dry_run:
            planned += 1
            print(f"[DISPLAY-CACHE] DRY-RUN source={relative_key!r} output={output_relative.as_posix()!r}")
            continue

        try:
            width, height = build_copy(source, destination, args.max_edge, args.quality)
            entries[relative_key] = {
                "output": output_relative.as_posix(),
                "source_mtime_ns": mtime_ns,
                "source_size": source_size,
                "width": width,
                "height": height,
            }
            converted += 1
            print(f"[DISPLAY-CACHE] SAVED source={relative_key!r} output={output_relative.as_posix()!r} size={width}x{height}")
            if converted % 25 == 0:
                atomic_write_json(manifest_path, manifest_payload(entries, args))
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            errors += 1
            print(f"[DISPLAY-CACHE] ERROR source={relative_key!r} error={exc}")

    if not args.dry_run:
        atomic_write_json(manifest_path, manifest_payload(entries, args))

    print(
        "[DISPLAY-CACHE] SUMMARY "
        f"scanned={scanned} converted={converted} skipped={skipped} planned={planned} "
        f"invalid={invalid} errors={errors} dry_run={args.dry_run}"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
