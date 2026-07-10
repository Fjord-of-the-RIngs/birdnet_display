from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from typing import Iterable

import requests

from image_cache_utils import validate_cached_image
from path_config import PATHS


AVICOMMONS_JSON_URL = "https://avicommons.org/latest.json"
AVICOMMONS_STATIC_BASE = "https://static.avicommons.org"
AVICOMMONS_SIZES = {160, 240, 320, 480, 900}
DEFAULT_SIZE = 480
DEFAULT_JSON_CACHE = PATHS.display_home / "avicommons_latest.json"
METADATA_FILENAME = ".avicommons.json"
HEADERS = {"User-Agent": "BirdNET-Display/1.0"}

# Add narrow fixes here when BirdNET and Avicommons taxonomy names diverge.
SPECIES_OVERRIDES: dict[str, str] = {}


@dataclass(frozen=True)
class SpeciesRecord:
    common_name: str
    scientific_name: str = ""


def normalize_name(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.replace("&", " and ")
    value = re.sub(r"['.]", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def canonical_folder_name(common_name: str) -> str:
    name = (common_name or "").strip()
    name = name.replace("'", "").replace(".", "").replace("-", "")
    name = re.sub(r"\s+", " ", name)
    return name.replace(" ", "_")


def valid_image_count(folder_path: str | Path, cache_dir: str | Path = PATHS.image_cache_dir) -> int:
    folder = Path(folder_path)
    if not folder.is_dir():
        return 0
    total = 0
    for image_path in folder.iterdir():
        if not image_path.is_file():
            continue
        result = validate_cached_image(image_path, cache_dir)
        if result.ok:
            total += 1
    return total


def load_species_from_db(db_path: str | Path = PATHS.db_path) -> list[SpeciesRecord]:
    db = Path(db_path)
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            """
            SELECT Com_Name, MAX(Sci_Name) AS Sci_Name
            FROM detections
            WHERE Com_Name IS NOT NULL AND Com_Name != ''
            GROUP BY Com_Name
            ORDER BY Com_Name
            """
        ).fetchall()
    finally:
        conn.close()
    return [SpeciesRecord(row[0], row[1] or "") for row in rows if row[0]]


def load_species_from_csv(species_file: str | Path = PATHS.species_file) -> list[SpeciesRecord]:
    path = Path(species_file)
    if not path.is_file():
        return []
    records: list[SpeciesRecord] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            if len(row) >= 1 and row[0].strip():
                records.append(SpeciesRecord(row[0].strip(), row[1].strip() if len(row) > 1 else ""))
    return records


def load_default_species() -> list[SpeciesRecord]:
    records = load_species_from_db()
    if records:
        return records
    return load_species_from_csv()


def fetch_avicommons_json(
    *,
    json_cache: str | Path = DEFAULT_JSON_CACHE,
    refresh: bool = False,
    timeout: int = 30,
) -> list[dict]:
    cache_path = Path(json_cache)
    if cache_path.is_file() and not refresh:
        with cache_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        response = requests.get(AVICOMMONS_JSON_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache_path.parent, delete=False) as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            tmp.write("\n")
            temp_name = tmp.name
        os.replace(temp_name, cache_path)
    if not isinstance(data, list):
        raise ValueError("Avicommons JSON must be a list")
    return [entry for entry in data if isinstance(entry, dict)]


def index_avicommons_entries(entries: Iterable[dict]) -> dict[str, dict[str, list[dict]]]:
    """Index every usable Avicommons photo by the supported taxonomy names."""
    by_sci: dict[str, list[dict]] = {}
    by_common: dict[str, list[dict]] = {}
    by_norm: dict[str, list[dict]] = {}
    for entry in entries:
        if not all(entry.get(key) for key in ("code", "key", "name", "sciName", "license", "by")):
            continue
        by_sci.setdefault(normalize_name(str(entry["sciName"])), []).append(entry)
        by_common.setdefault(normalize_name(str(entry["name"])), []).append(entry)
        by_norm.setdefault(normalize_name(str(entry["name"])), []).append(entry)
    return {"sci": by_sci, "common": by_common, "normalized": by_norm}


def match_avicommons_entries(
    species: SpeciesRecord,
    index: dict[str, dict[str, list[dict]]],
) -> tuple[list[dict], str]:
    """Return all matching Avicommons photos for a species, in source order."""
    override_key = SPECIES_OVERRIDES.get(species.scientific_name) or SPECIES_OVERRIDES.get(species.common_name)
    if override_key:
        key = normalize_name(override_key)
        for bucket_name in ("sci", "common", "normalized"):
            matches = index[bucket_name].get(key, [])
            if matches:
                return matches, f"override:{bucket_name}"

    if species.scientific_name:
        matches = index["sci"].get(normalize_name(species.scientific_name), [])
        if matches:
            return matches, "sciName"

    matches = index["common"].get(normalize_name(species.common_name), [])
    if matches:
        return matches, "name"

    matches = index["normalized"].get(normalize_name(species.common_name), [])
    if matches:
        return matches, "normalized"

    return [], "missing"


def match_avicommons_entry(
    species: SpeciesRecord,
    index: dict[str, dict[str, list[dict]]],
) -> tuple[dict | None, str]:
    """Backward-compatible single-photo matcher for callers that need one entry."""
    matches, reason = match_avicommons_entries(species, index)
    return (matches[0] if matches else None), reason


def image_url(entry: dict, size: int = DEFAULT_SIZE) -> str:
    if size not in AVICOMMONS_SIZES:
        raise ValueError(f"Unsupported Avicommons image size: {size}")
    return f"{AVICOMMONS_STATIC_BASE}/{entry['code']}-{entry['key']}-{size}.jpg"


def _safe_filename_component(value: object) -> str:
    """Make an external Avicommons identifier safe without losing uniqueness."""
    raw = str(value or "")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-_")
    if not safe:
        safe = "id"
    if safe != raw:
        safe = f"{safe}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]}"
    return safe


def image_filename(entry: dict, size: int = DEFAULT_SIZE) -> str:
    if size not in AVICOMMONS_SIZES:
        raise ValueError(f"Unsupported Avicommons image size: {size}")
    code = _safe_filename_component(entry.get("code"))
    key = _safe_filename_component(entry.get("key"))
    return f"avicommons_{code}_{key}_{size}.jpg"


def attribution_text(entry: dict) -> str:
    return f"© {entry.get('by', 'Unknown')} / Avicommons / {entry.get('license', 'unknown')}"


def read_species_metadata(folder_path: str | Path) -> dict:
    path = Path(folder_path) / METADATA_FILENAME
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_species_metadata(folder_path: str | Path, metadata: dict) -> None:
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / METADATA_FILENAME
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False) as tmp:
        json.dump(metadata, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write("\n")
        temp_name = tmp.name
    os.replace(temp_name, path)


def build_metadata_entry(entry: dict, filename: str, source_url: str, size: int) -> dict:
    return {
        "source": "avicommons",
        "code": entry.get("code", ""),
        "name": entry.get("name", ""),
        "sciName": entry.get("sciName", ""),
        "license": entry.get("license", ""),
        "key": entry.get("key", ""),
        "by": entry.get("by", ""),
        "family": entry.get("family", ""),
        "source_url": source_url,
        "cached_filename": filename,
        "size": size,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "attribution": attribution_text(entry),
    }
