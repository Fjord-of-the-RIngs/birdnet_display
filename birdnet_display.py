# keep current 
import requests
import sqlite3
from flask import Flask, render_template, send_file, request, jsonify
from urllib.parse import urljoin
from datetime import datetime
import os
import random
import socket
import qrcode
import io
import json
import sys
import re
import subprocess
import csv
from datetime import datetime
from pathlib import Path
from werkzeug.utils import secure_filename
import os
from flask import jsonify, request, abort
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder="static")
app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.get("/api/bird_images")
def api_bird_images_list():
    """
    Query params:
      species_folder=<folder>
    Returns:
      { ok: true, folder_exists: bool, images: [{name, url, bytes, mtime}] }
    """
    species_folder = request.args.get("species_folder", "")
    folder_path = _safe_species_folder(species_folder)

    if not os.path.isdir(folder_path):
        return jsonify({"ok": True, "folder_exists": False, "images": []})

    items = []
    for name in sorted(os.listdir(folder_path)):
        p = os.path.join(folder_path, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTS:
            continue

        st = os.stat(p)
        # This URL assumes static files are served under /static/...
        url = f"/static/bird_images_cache/{os.path.basename(folder_path)}/{name}"
        items.append({
            "name": name,
            "url": url,
            "bytes": st.st_size,
            "mtime": int(st.st_mtime),
        })

    return jsonify({"ok": True, "folder_exists": True, "images": items})


@app.delete("/api/bird_images")
def api_bird_images_delete():
    """
    JSON body:
      { species_folder: "...", filename: "..." }
    """
    data = request.get_json(silent=True) or {}
    species_folder = data.get("species_folder", "")
    filename = data.get("filename", "")

    folder_path = _safe_species_folder(species_folder)

    # File name should not include slashes
    safe_file = os.path.basename(filename)
    if not safe_file or safe_file != filename:
        abort(400, description="Invalid filename")

    ext = os.path.splitext(safe_file)[1].lower()
    if ext not in ALLOWED_EXTS:
        abort(400, description="Not an allowed image type")

    target = os.path.abspath(os.path.join(folder_path, safe_file))
    if not target.startswith(os.path.abspath(folder_path) + os.sep):
        abort(400, description="Invalid path")

    if not os.path.isfile(target):
        return jsonify({"ok": False, "error": "File not found"}), 404

    os.remove(target)
    return jsonify({"ok": True})


EXTRACTED_DIR = "/home/birdpi/BirdSongs/Extracted/By_Date"


def get_latest_clip_for_species(com_name: str):
    """Return the most recent DB detection row (with File_Name) whose audio file still exists on disk."""
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT Date, Time, Confidence, File_Name FROM detections "
            "WHERE Com_Name = ? AND File_Name IS NOT NULL AND File_Name != '' "
            "ORDER BY Date DESC, Time DESC LIMIT 50",
            (com_name,)
        )
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            audio_path, _ = find_clip_paths(com_name, row[0], row[3])
            if audio_path:
                return {"Date": row[0], "Time": row[1], "Confidence": row[2], "File_Name": row[3]}
    except Exception:
        pass
    return None


def get_best_clip_for_species(com_name: str):
    """Return the highest-confidence detection row whose audio file still exists on disk.
    Ties broken by most recent date/time."""
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT Date, Time, Confidence, File_Name FROM detections "
            "WHERE Com_Name = ? AND File_Name IS NOT NULL AND File_Name != '' "
            "ORDER BY CAST(Confidence AS REAL) DESC, Date DESC, Time DESC LIMIT 50",
            (com_name,)
        )
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            audio_path, _ = find_clip_paths(com_name, row[0], row[3])
            if audio_path:
                return {"Date": row[0], "Time": row[1], "Confidence": row[2], "File_Name": row[3]}
    except Exception:
        pass
    return None


def find_clip_paths(com_name: str, date: str, file_name: str):
    """Return (audio_path, spectrogram_path) for a detection, either may be None."""
    species_folder = com_name.replace("'", "").replace(" ", "_")
    base = Path(EXTRACTED_DIR) / date / species_folder
    audio = base / file_name
    spectrogram = base / (file_name + ".png")
    return (
        audio if audio.is_file() else None,
        spectrogram if spectrogram.is_file() else None,
    )


from functools import lru_cache

# Import variables/functions from cache_builder
from cache_builder import CACHE_DIRECTORY, SPECIES_FILE, load_species_from_file

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

PLACEHOLDER_DIRECTORY = "/home/birdpi/birdnet_display/static/bird_images_cache/placeholders"

BASE_URL = "http://localhost:5000/"
API_ENDPOINT = "api/v2/detections/recent"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}
PROXIES = {"http": None, "https": None}
SERVER_PORT = 5000


DETECTION_CACHE = {"id": None, "raw_data": []}

# Adjust this to your real base folder:
BIRD_IMAGE_BASE = "/home/birdpi/birdnet_display/static/bird_images_cache"

SPECTROGRAM_DIR = "/home/birdpi/birdnet_display/static/spectrogram_cache"

# Only allow these image types for browsing/deleting
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

def _safe_species_folder(species_folder: str) -> str:
    """
    Convert a user-provided folder name into a safe on-disk path
    under BIRD_IMAGE_BASE, and ensure it can't escape via ../
    """
    # Keep it simple: treat folder name as a single path component
    safe = secure_filename(species_folder or "")
    if not safe:
        abort(400, description="Invalid species folder")

    full = os.path.abspath(os.path.join(BIRD_IMAGE_BASE, safe))
    base = os.path.abspath(BIRD_IMAGE_BASE)

    # Prevent path traversal
    if not full.startswith(base + os.sep):
        abort(400, description="Invalid path")

    return full

# ----------------------------------------------------------------------
# IP + QR code helpers
# ----------------------------------------------------------------------
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@app.route("/qr_code.png")
def qr_code():
    ip = get_local_ip()
    url = f"http://{ip}:{SERVER_PORT}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ----------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------
def parse_absolute_time_to_seconds_ago(time_str: str) -> float:
    if not time_str:
        return 0.0
    try:
        t = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        delta = datetime.now() - t
        return max(0.0, delta.total_seconds())
    except Exception:
        return 0.0


def format_seconds_ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60.0
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24.0
    return f"{int(days)}d ago"


# ----------------------------------------------------------------------
# Cached image helpers
# ----------------------------------------------------------------------
@lru_cache(maxsize=1)
def _known_species_dirs():
    if not CACHE_DIRECTORY or not os.path.isdir(CACHE_DIRECTORY):
        return set()
    return {
        d
        for d in os.listdir(CACHE_DIRECTORY)
        if os.path.isdir(os.path.join(CACHE_DIRECTORY, d))
    }


def _slug_for_cache_name(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in " _-")
    safe = safe.replace("-", "")
    return safe.strip().replace(" ", "_")


_QUALIFIER_PATTERN = re.compile(
    r"\s*\(.*?\)" r"|\s+-\s+.*$" r"|\s+–\s+.*$" r"|:[^:]*$"
)


def _strip_qualifiers(name: str) -> str:
    stripped = _QUALIFIER_PATTERN.sub("", name)
    stripped = re.sub(r"\s+(sp|spp)\.?$", "", stripped, flags=re.IGNORECASE)
    return stripped.strip()


SPECIES_ALIASES = {
    # "Fish Crow": "American Crow",
}

# Base path for your existing cache – adjust if your project uses a different path
BIRD_IMAGE_CACHE_BASE = "/home/birdpi/birdnet_display/static/bird_images_cache"

# Snapshot of all existing species folders
EXISTING_BIRD_IMAGE_FOLDERS = {
    name
    for name in os.listdir(BIRD_IMAGE_CACHE_BASE)
    if os.path.isdir(os.path.join(BIRD_IMAGE_CACHE_BASE, name))
}


def canonical_folder_name(common_name: str) -> str:
    """
    Convert a BirdNET common name into our standard folder name.

    Rules:
    - Remove hyphens: 'Black-crowned Night-Heron' -> 'Blackcrowned NightHeron'
    - Turn spaces into underscores: -> 'Blackcrowned_NightHeron'
    - Strip apostrophes and periods: "Wilson's" -> "Wilsons"
    """
    name = common_name.strip()

    # Remove apostrophes and periods
    name = name.replace("'", "").replace(".", "")

    # Remove hyphens entirely
    name = name.replace("-", "")

    # Collapse multiple spaces to a single space just in case
    name = re.sub(r"\s+", " ", name)

    # Replace spaces with underscores
    name = name.replace(" ", "_")

    return name


def get_bird_folder_name(common_name: str) -> tuple[str, bool]:
    """
    Decide which folder name to use for this bird.

    More forgiving than strict canonical matching:
    - Treat hyphens/spaces/underscores as equivalent
    - Ignore apostrophes/periods
    - Case-insensitive
    """
    canonical = canonical_folder_name(common_name)

    def norm(s: str) -> str:
        s = s.strip().lower()
        s = s.replace("'", "").replace(".", "")
        # treat separators as equivalent by removing them
        s = s.replace("_", "").replace(" ", "").replace("-", "")
        return s

    want = norm(canonical)

    # Build a lookup from normalized -> real folder name
    for existing in EXISTING_BIRD_IMAGE_FOLDERS:
        if norm(existing) == want:
            return existing, True

    # Not found: this is what we'd create
    return canonical, False

def get_cached_image(species_name: str):
    if not species_name:
        return None
    if not CACHE_DIRECTORY or not os.path.isdir(CACHE_DIRECTORY):
        return None

    base = species_name.strip()

    if base in SPECIES_ALIASES:
        base = SPECIES_ALIASES[base]

    candidates = [base]
    stripped = _strip_qualifiers(base)
    if stripped and stripped not in candidates:
        candidates.append(stripped)

        # Use the live in-memory set of known folders, which is updated whenever
    # new folders are created or images are uploaded.
    known_dirs = set(EXISTING_BIRD_IMAGE_FOLDERS)
    species_folder = None

    for cand in candidates:
        slug = _slug_for_cache_name(cand)
        if slug in known_dirs:
            species_folder = slug
            break

    # Fallback: generic "Common_Name" folder if it exists
    if species_folder is None and "Common_Name" in known_dirs:
        species_folder = "Common_Name"


    if species_folder is None:
        return None

    species_dir = os.path.join(CACHE_DIRECTORY, species_folder)
    # Allow multiple common image formats
    images = [
        f for f in os.listdir(species_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
    ]
    if not images:
        return None

    # Pick a random image each time this function is called
    chosen_image = random.choice(images)

    copyright_file = os.path.splitext(chosen_image)[0] + ".txt"
    copyright_path = os.path.join(species_dir, copyright_file)
    copyright_text = ""
    if os.path.isfile(copyright_path):
        with open(
            copyright_path, "r", encoding="utf-8", errors="ignore"
        ) as fh:
            copyright_text = fh.read().strip()

    return {
        "image_url": f"/static/bird_images_cache/{species_folder}/{chosen_image}",
        "copyright": copyright_text,
    }


# ----------------------------------------------------------------------
# Offline fallback
# ----------------------------------------------------------------------
def get_offline_fallback_data():
    """
    Fallback data when there are no detections available from the BirdNET-Pi DB.
    First try to use local placeholder images; if none are found, fall back to
    the old species-based offline cache.
    """
    print("[INFO] Loading data from offline fallback (placeholders/species cache).")

    # --- 1) Preferred: custom placeholder images ---
    if PLACEHOLDER_DIRECTORY and os.path.isdir(PLACEHOLDER_DIRECTORY):
        image_files = [
            f
            for f in os.listdir(PLACEHOLDER_DIRECTORY)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
        ]
        image_files.sort()

        if image_files:
            fallback_data = []
            max_cards = 4

            for i in range(max_cards):
                filename = image_files[i % len(image_files)]
                image_url = f"/static/bird_images_cache/placeholders/{filename}"

                fallback_data.append(
                    {
                        "name": "Waiting for birds to visit your BirdNET",
                        "time_display": "No recent detections yet.",
                        "confidence": "",
                        "confidence_value": 0,
                        "image_url": image_url,
                        "copyright": "",
                        "time_raw": "",
                    }
                )

            # api_is_down stays True to indicate we're in a fallback state
            return fallback_data, True

    # --- 2) Legacy behavior: random species from cached images ---
    species_list = load_species_from_file(SPECIES_FILE)
    if not species_list:
        return [], True

    fallback_data = []
    num = min(len(species_list), 4)
    sampled = random.sample(species_list, num)

    for common_name, scientific_name in sampled:
        cached = get_cached_image(common_name)
        if cached:
            fallback_data.append(
                {
                    "name": common_name,
                    "time_display": "Offline",
                    "confidence": "0%",
                    "confidence_value": 0,
                    "image_url": cached["image_url"],
                    "copyright": cached["copyright"],
                    "time_raw": "",
                }
            )

    return fallback_data, True

# ----------------------------------------------------------------------
# Local BirdNET-Pi DB helpers
# ----------------------------------------------------------------------
def _get_db_path():
    user_dir = os.path.expanduser("~")
    return os.path.join(user_dir, "BirdNET-Pi", "scripts", "birds.db")


def get_bird_data_from_local_db():
    """Load bird data from BirdNET-Pi detections SQLite DB."""
    detections = []
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"

    if not os.path.exists(db_path):
        print(f"[WARN] Local DB not found at {db_path}")
        return None

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT Com_Name, Date, Time, Confidence, File_Name
            FROM detections
            ORDER BY Date DESC, Time DESC
            """
        )

        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            common_name, date_str, time_str, conf, filename = row

            try:
                confidence_value = int(float(conf) * 100)
            except Exception:
                confidence_value = 0

            cached = get_cached_image(common_name)
            image_url = cached["image_url"] if cached else ""
            copyright_info = cached["copyright"] if cached else ""

            detections.append(
                {
                    "name": common_name,
                    "time_raw": f"{date_str} {time_str}",
                    "confidence_value": confidence_value,
                    "image_url": image_url,
                    "copyright": copyright_info,
                }
            )

        return detections

    except Exception as e:
        print(f"[ERROR] Could not read from local DB: {e}")
        return None


def get_detections_last_24h(limit_rows=2000):
    try:
        db_path = _get_db_path()
        if not os.path.exists(db_path):
            return None

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT Com_Name, Date, Time, Confidence
            FROM detections
            WHERE datetime(Date || ' ' || Time) >= datetime('now', '-1 day')
            ORDER BY Date DESC, Time DESC
            LIMIT ?
            """,
            (limit_rows,),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return None

        detections = []
        for common_name, date_str, time_str, conf in rows:
            if not common_name:
                continue

            try:
                conf_val = int(float(conf) * 100)
            except Exception:
                conf_val = 0

            # Existing image cache lookup (may still provide a fallback image)
            cached = get_cached_image(common_name)
            img_url = cached["image_url"] if cached else ""
            copyright_info = cached["copyright"] if cached else ""

            # Figure out the correct folder name + whether it exists
            folder_name, folder_exists = get_bird_folder_name(common_name)
            folder_path = os.path.join(BIRD_IMAGE_CACHE_BASE, folder_name)

            # Does this folder actually contain any image files?
            has_images = False
            if folder_exists:
                try:
                    for fname in os.listdir(folder_path):
                        if fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                            has_images = True
                            break
                except Exception as e:
                    print(f"Error checking image folder for {common_name}: {e}")

            detections.append(
                {
                    "name": common_name,
                    "time_raw": f"{date_str} {time_str}",
                    "confidence_value": conf_val,
                    "image_url": img_url,
                    "copyright": copyright_info,
                    "folder_name": folder_name,
                    "folder_exists": folder_exists,
                    "folder_path": folder_path,
                    "has_images": has_images,
                }
            )

        return detections

    except Exception as e:
        print(f"Error reading last-24h BirdNET database window: {e}")
        return None

def get_unique_species_last_24h():
    """
    Returns one record per species detected in the last 24 hours,
    sorted by most recent detection time.
    Includes folder + image metadata so the UI photo browser works.
    """
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        query = """
            SELECT
                Com_Name,
                MAX(Sci_Name) AS Sci_Name,
                MAX(Date || ' ' || Time) AS last_seen,
                MAX(Confidence) AS Confidence,
                COUNT(*) AS detection_count
            FROM detections
            WHERE datetime(Date || ' ' || Time) >= datetime('now', '-1 day')
            GROUP BY Com_Name
            ORDER BY last_seen DESC;
        """

        rows = cursor.execute(query).fetchall()
        conn.close()

        results = []
        for name, sci_name, last_seen, conf, det_count in rows:
            if not name:
                continue

            # Cached image (if available)
            cached = get_cached_image(name)
            img_url = cached["image_url"] if cached else ""
            copyright_info = cached["copyright"] if cached else ""

            # Folder info
            folder_name, folder_exists = get_bird_folder_name(name)
            folder_path = os.path.join(BIRD_IMAGE_CACHE_BASE, folder_name)

            # Check whether folder contains any images
            has_images = False
            if folder_exists:
                try:
                    for fname in os.listdir(folder_path):
                        if fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                            has_images = True
                            break
                except Exception as e:
                    print(f"Error checking image folder for {name}: {e}")

            try:
                conf_val = int(float(conf) * 100) if conf is not None else 0
            except Exception:
                conf_val = 0

            results.append(
                {
                    "name": name,
                    "sci_name": sci_name or "",
                    "time_raw": last_seen,
                    "confidence_value": conf_val,
                    "detection_count": det_count,
                    "image_url": img_url,
                    "copyright": copyright_info,
                    "folder_name": folder_name,
                    "folder_exists": folder_exists,
                    "folder_path": folder_path,
                    "has_images": has_images,
                }
            )

        return results

    except Exception as e:
        print(f"Error in get_unique_species_last_24h: {e}")
        return []


def get_all_species_ever():
    """
    Returns one record per species ever detected, sorted by most recent detection.
    Formats time_display as a human-readable date rather than seconds-ago.
    """
    from datetime import datetime as _dt
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        query = """
            SELECT
                Com_Name,
                MAX(Sci_Name)              AS Sci_Name,
                MAX(Date || ' ' || Time)   AS last_seen,
                MAX(Confidence)            AS Confidence,
                COUNT(*)                   AS detection_count
            FROM detections
            GROUP BY Com_Name
            ORDER BY last_seen DESC;
        """
        rows = cursor.execute(query).fetchall()
        conn.close()

        now = _dt.now()
        results = []
        for name, sci_name, last_seen, conf, det_count in rows:
            if not name:
                continue

            cached = get_cached_image(name)
            img_url = cached["image_url"] if cached else ""
            copyright_info = cached["copyright"] if cached else ""

            folder_name, folder_exists = get_bird_folder_name(name)
            folder_path = os.path.join(BIRD_IMAGE_CACHE_BASE, folder_name)

            has_images = False
            if folder_exists:
                try:
                    for fname in os.listdir(folder_path):
                        if fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                            has_images = True
                            break
                except Exception:
                    pass

            try:
                conf_val = int(float(conf) * 100) if conf is not None else 0
            except Exception:
                conf_val = 0

            time_display = ""
            if last_seen:
                try:
                    dt = _dt.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
                    delta = now - dt
                    if delta.days == 0:
                        time_display = "Today"
                    elif delta.days == 1:
                        time_display = "Yesterday"
                    elif delta.days < 365:
                        time_display = dt.strftime("%-d %b")
                    else:
                        time_display = dt.strftime("%-d %b %Y")
                except Exception:
                    time_display = last_seen[:10] if last_seen else ""

            species_escaped = name.replace(" ", "%20")
            results.append({
                "name": name,
                "sci_name": sci_name or "",
                "time_raw": last_seen,
                "time_display": time_display,
                "confidence_value": conf_val,
                "confidence": f"{conf_val}%",
                "detection_count": det_count,
                "image_url": img_url,
                "copyright": copyright_info,
                "folder_name": folder_name,
                "folder_exists": folder_exists,
                "folder_path": folder_path,
                "has_images": has_images,
                "recording_url": f"/recording_by_name?species={species_escaped}",
            })

        return results
    except Exception as e:
        print(f"Error in get_all_species_ever: {e}")
        return []


def _build_display_from_selection(selected):
    display_data = []
    for bird in selected:
        copy = bird.copy()

        if bird.get("time_raw"):
            secs = parse_absolute_time_to_seconds_ago(bird["time_raw"])
            copy["time_display"] = format_seconds_ago(secs)
        else:
            copy["time_display"] = "Offline"

        copy["confidence"] = f"{bird.get('confidence_value', 0)}%"

        species_escaped = bird["name"].replace(" ", "%20")
        copy["recording_url"] = f"/recording_by_name?species={species_escaped}"

        display_data.append(copy)

    return display_data


def get_rarest_birds_last_24h(rare_days=3):
    """
    Return up to 4 birds seen in the last 24 h, ranked by how infrequently
    they appear over the rolling `rare_days`-day window.  Fewer detections
    in that window = rarer.  Tie-break: most recently detected comes first.
    """
    rare_days = max(1, min(365, int(rare_days)))
    db_path = _get_db_path()
    if not db_path or not os.path.exists(db_path):
        return get_bird_data()

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"""
            WITH candidates AS (
                SELECT Com_Name,
                       MAX(Date || ' ' || Time) AS latest_dt
                FROM   detections
                WHERE  datetime(Date || ' ' || Time) >= datetime('now', '-1 day')
                GROUP  BY Com_Name
            ),
            history AS (
                SELECT Com_Name, COUNT(*) AS count_nd
                FROM   detections
                WHERE  datetime(Date || ' ' || Time) >= datetime('now', '-{rare_days} days')
                GROUP  BY Com_Name
            )
            SELECT c.Com_Name, h.count_nd, c.latest_dt
            FROM   candidates c
            JOIN   history h ON h.Com_Name = c.Com_Name
            ORDER  BY h.count_nd ASC, c.latest_dt DESC
            LIMIT  4
            """
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print(f"[ERROR] get_rarest_birds_last_24h: {e}")
        return get_bird_data()

    if not rows:
        return get_bird_data()

    # Use the GROUP BY query (no row-limit) so rare birds detected earlier
    # in the day aren't crowded out by high-volume common-species detections.
    unique = {d["name"]: d for d in (get_unique_species_last_24h() or [])}
    chosen = [unique[r["Com_Name"]] for r in rows if r["Com_Name"] in unique]
    if not chosen:
        return get_bird_data()

    return _build_display_from_selection(chosen), False


def get_random_birds_last_24h():
    """
    Shuffle from ALL unique species detected in the last 24 hours,
    not just from cached/filtered rows.
    """

    unique_species = get_unique_species_last_24h()
    if not unique_species:
        return get_bird_data()

    k = min(4, len(unique_species))
    chosen = random.sample(unique_species, k)

    # Re-attach image + folder metadata from the main 24h window
    enriched = []
    full_24h = get_detections_last_24h() or []

    latest_by_name = {}
    for d in full_24h:
        name = d.get("name")
        if name and name not in latest_by_name:
            latest_by_name[name] = d

    for entry in chosen:
        name = entry["name"]
        if name in latest_by_name:
            enriched.append(latest_by_name[name])
        else:
            enriched.append(entry)

    display_data = _build_display_from_selection(enriched)
    return display_data, False


# ----------------------------------------------------------------------
# Bird data: NOW **LOCAL ONLY**, NO BirdNET-Go
# ----------------------------------------------------------------------
def get_bird_data():
    """
    Get the main set of birds to display, using ONLY the local BirdNET-Pi
    SQLite database. If that fails, fall back to offline cache.
    Returns (display_data, api_is_down_flag)
    """
    local_data = get_detections_last_24h()
    if local_data:
        # Pick up to 4 unique species, most recent first
        by_name = {}
        for d in local_data:
            name = d.get("name")
            if not name:
                continue
            if name not in by_name:
                by_name[name] = d
            if len(by_name) >= 4:
                break

        selected = list(by_name.values())
        display_data = _build_display_from_selection(selected)
        return display_data, False

    # No DB or no rows — use offline images if we have them
    return get_offline_fallback_data()


# ----------------------------------------------------------------------
# Flask routes
# ----------------------------------------------------------------------
@app.route("/recording_by_name")
def recording_by_name():
    """Stream the latest extracted clip for a species (used by legacy callers)."""
    species = request.args.get("species", "").strip()
    if not species:
        return jsonify({"error": "missing species parameter"}), 400
    row = get_latest_clip_for_species(species)
    if not row:
        return jsonify({"error": f"No clip found for '{species}'"}), 404
    audio_path, _ = find_clip_paths(species, row["Date"], row["File_Name"])
    if not audio_path:
        return jsonify({"error": f"Audio file missing for '{species}'"}), 404
    return send_file(str(audio_path), mimetype="audio/mpeg")


@app.route("/api/latest_clip")
def api_latest_clip():
    """Return metadata (audio URL + spectrogram URL) for the latest clip of a species."""
    species = request.args.get("species", "").strip()
    if not species:
        return jsonify({"ok": False, "error": "Missing 'species' parameter"}), 400

    row = get_latest_clip_for_species(species)
    if not row:
        return jsonify({"ok": False, "error": f"No clip found for '{species}'"}), 404

    audio_path, spec_path = find_clip_paths(species, row["Date"], row["File_Name"])
    if not audio_path:
        return jsonify({"ok": False, "error": f"Audio file missing for '{species}'"}), 404

    species_folder = species.replace(" ", "_")
    relpath = f"{row['Date']}/{species_folder}/{row['File_Name']}"
    audio_url = f"/clip/{relpath}"
    spectrogram_url = f"/clip/{relpath}.png" if spec_path else None
    clip_time = f"{row['Date']} {row['Time']}".strip()

    return jsonify({
        "ok": True,
        "species": species,
        "clip_id": row["File_Name"],
        "clip_time": clip_time,
        "audio_url": audio_url,
        "spectrogram_url": spectrogram_url,
    })


@app.route("/api/best_clip")
def api_best_clip():
    """Return metadata for the highest-confidence clip of a species that still exists on disk."""
    species = request.args.get("species", "").strip()
    if not species:
        return jsonify({"ok": False, "error": "Missing 'species' parameter"}), 400

    row = get_best_clip_for_species(species)
    if not row:
        return jsonify({"ok": False, "error": f"No clip found for '{species}'"}), 404

    audio_path, spec_path = find_clip_paths(species, row["Date"], row["File_Name"])
    if not audio_path:
        return jsonify({"ok": False, "error": f"Audio file missing for '{species}'"}), 404

    species_folder = species.replace("'", "").replace(" ", "_")
    relpath = f"{row['Date']}/{species_folder}/{row['File_Name']}"
    clip_time = f"{row['Date']} {row['Time']}".strip()

    return jsonify({
        "ok": True,
        "species": species,
        "clip_id": row["File_Name"],
        "clip_time": clip_time,
        "confidence": round(float(row["Confidence"]) * 100),
        "audio_url": f"/clip/{relpath}",
        "spectrogram_url": f"/clip/{relpath}.png" if spec_path else None,
    })


@app.route("/api/species_stats")
def api_species_stats():
    """Return detection statistics for a single species."""
    species = request.args.get("species", "").strip()
    if not species:
        return jsonify({"ok": False, "error": "Missing 'species' parameter"}), 400

    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
    if not os.path.exists(db_path):
        return jsonify({"ok": False, "error": "Database not found"}), 500

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        summary = conn.execute("""
            SELECT
                COUNT(*)                                                          AS total_detections,
                COUNT(DISTINCT Date)                                              AS days_detected,
                MIN(Date)                                                         AS first_detected,
                MAX(Date || ' ' || Time)                                          AS last_detected_dt,
                MAX(Confidence)                                                   AS best_confidence,
                AVG(Confidence)                                                   AS avg_confidence,
                SUM(CASE WHEN datetime(Date||' '||Time) >= datetime('now','-7 days')  THEN 1 ELSE 0 END) AS det_7d,
                SUM(CASE WHEN datetime(Date||' '||Time) >= datetime('now','-14 days')
                          AND datetime(Date||' '||Time) <  datetime('now','-7 days')  THEN 1 ELSE 0 END) AS det_prev_7d,
                SUM(CASE WHEN datetime(Date||' '||Time) >= datetime('now','-30 days') THEN 1 ELSE 0 END) AS det_30d,
                SUM(CASE WHEN datetime(Date||' '||Time) >= datetime('now','-60 days')
                          AND datetime(Date||' '||Time) <  datetime('now','-30 days') THEN 1 ELSE 0 END) AS det_prev_30d
            FROM detections
            WHERE Com_Name = ?
        """, (species,)).fetchone()

        if not summary or summary["total_detections"] == 0:
            conn.close()
            return jsonify({"ok": False, "error": f"No detections found for '{species}'"}), 404

        # Consecutive-day streak ending on the most recent detection date
        date_rows = conn.execute(
            "SELECT DISTINCT Date FROM detections WHERE Com_Name = ? ORDER BY Date DESC",
            (species,)
        ).fetchall()
        streak = 0
        if date_rows:
            from datetime import date as _date, timedelta
            today = _date.today()
            most_recent = _date.fromisoformat(date_rows[0]["Date"])
            if (today - most_recent).days <= 1:
                for i, r in enumerate(date_rows):
                    if _date.fromisoformat(r["Date"]) == most_recent - timedelta(days=i):
                        streak += 1
                    else:
                        break

        # Hourly distribution (hour 0-23)
        hourly_rows = conn.execute("""
            SELECT CAST(strftime('%H', Time) AS INTEGER) AS hr, COUNT(*) AS cnt
            FROM detections WHERE Com_Name = ?
            GROUP BY hr ORDER BY hr
        """, (species,)).fetchall()
        hourly = [0] * 24
        for r in hourly_rows:
            if 0 <= r["hr"] <= 23:
                hourly[r["hr"]] = r["cnt"]

        # Monthly distribution — last 18 months
        monthly_rows = conn.execute("""
            SELECT strftime('%Y-%m', Date) AS month, COUNT(*) AS cnt
            FROM detections
            WHERE Com_Name = ? AND Date >= date('now', '-18 months')
            GROUP BY month ORDER BY month
        """, (species,)).fetchall()
        monthly_map = {r["month"]: r["cnt"] for r in monthly_rows}

        from datetime import date as _date
        today = _date.today()
        monthly = []
        for i in range(17, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0:
                m += 12; y -= 1
            key = f"{y}-{m:02d}"
            monthly.append({"month": key, "count": monthly_map.get(key, 0)})

        # Rank by total detections
        rank_row = conn.execute("""
            SELECT COUNT(*) + 1 AS rank FROM (
                SELECT Com_Name, COUNT(*) AS cnt FROM detections
                GROUP BY Com_Name
                HAVING cnt > (SELECT COUNT(*) FROM detections WHERE Com_Name = ?)
            )
        """, (species,)).fetchone()
        total_species = conn.execute(
            "SELECT COUNT(DISTINCT Com_Name) AS cnt FROM detections"
        ).fetchone()

        conn.close()

        last_dt = summary["last_detected_dt"] or ""
        days_since_last = 0
        if last_dt:
            try:
                days_since_last = (datetime.now() - datetime.strptime(last_dt, "%Y-%m-%d %H:%M:%S")).days
            except Exception:
                pass

        return jsonify({
            "ok": True,
            "species": species,
            "summary": {
                "total_detections":  summary["total_detections"],
                "days_detected":     summary["days_detected"],
                "first_detected":    summary["first_detected"],
                "last_detected":     last_dt,
                "days_since_last":   max(0, days_since_last),
                "best_confidence":   round(float(summary["best_confidence"] or 0) * 100),
                "avg_confidence":    round(float(summary["avg_confidence"]  or 0) * 100),
                "streak_days":       streak,
                "detections_7d":     summary["det_7d"]      or 0,
                "detections_prev_7d":summary["det_prev_7d"] or 0,
                "detections_30d":    summary["det_30d"]     or 0,
                "detections_prev_30d":summary["det_prev_30d"] or 0,
            },
            "hourly":  [{"hour": i, "count": hourly[i]} for i in range(24)],
            "monthly": monthly,
            "rank": {
                "by_total":     rank_row["rank"] if rank_row else None,
                "total_species": total_species["cnt"] if total_species else None,
            },
        })

    except Exception as e:
        print(f"[ERROR] /api/species_stats: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/")
def index():
    bird_data, api_is_down = get_bird_data()

    if not os.path.exists("static"):
        os.makedirs("static")
    template_path = "index.html"
    full_template_path = os.path.join("static", template_path)
    if not os.path.exists(full_template_path):
        with open(full_template_path, "w", encoding="utf-8") as f:
            f.write(
                "<h1>Template file not found. Please create a static/index.html template.</h1>"
            )

    refresh_interval = 30 if api_is_down else 5
    server_url = f"http://{get_local_ip()}:{SERVER_PORT}"

    return render_template(
        template_path,
        birds=bird_data,
        refresh_interval=refresh_interval,
        api_is_down=api_is_down,
        server_url=server_url,
    )

import os

@app.route("/temp")
def get_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            raw = f.read().strip()
            celsius = int(raw) / 1000.0
        return jsonify({"temperature_c": round(celsius, 1)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/data")
def data():
    mode = request.args.get("mode", "recent")
    if mode == "rare":
        try:
            rare_days = int(request.args.get("rare_days", "3"))
        except ValueError:
            rare_days = 3
        bird_data, api_is_down = get_rarest_birds_last_24h(rare_days)
    elif mode == "shuffle":
        bird_data, api_is_down = get_random_birds_last_24h()
    elif mode == "all":
        all_species = get_unique_species_last_24h()
        if all_species:
            bird_data = _build_display_from_selection(all_species)
            api_is_down = False
        else:
            bird_data, api_is_down = get_offline_fallback_data()
    elif mode == "all_time":
        bird_data = get_all_species_ever()
        api_is_down = not bool(bird_data)
    else:
        bird_data, api_is_down = get_bird_data()

    return jsonify({"birds": bird_data, "api_is_down": api_is_down})


# ----------------------------------------------------------------------
# Bird Day Index V1 — daily activity scoring query
#
# Additive, read-only analytics endpoint. Does NOT modify any existing
# route or database. Returns one row per date that has detections,
# newest first. The client uses this data to render three views:
# Single Day, 10-Day, and a GitHub-style Heatmap Calendar Year.
# ----------------------------------------------------------------------
BIRD_DAY_INDEX_V1_SQL = """
WITH daily_counts AS (
    SELECT
        Date AS detection_date,
        COUNT(*) AS daily_detections
    FROM detections
    GROUP BY Date
),
all_time_avg AS (
    SELECT AVG(daily_detections * 1.0) AS avg_daily_detections
    FROM daily_counts
),
first_seen AS (
    SELECT
        Sci_Name,
        MIN(Date) AS first_seen_date
    FROM detections
    GROUP BY Sci_Name
),
daily_new_species AS (
    SELECT
        d.Date AS detection_date,
        COUNT(DISTINCT d.Sci_Name) AS new_species_count
    FROM detections d
    INNER JOIN first_seen f
        ON d.Sci_Name = f.Sci_Name
       AND d.Date = f.first_seen_date
    GROUP BY d.Date
),
daily_rare_species AS (
    WITH species_days AS (
        SELECT DISTINCT
            Date AS detection_date,
            Sci_Name
        FROM detections
    ),
    species_history AS (
        SELECT
            detection_date,
            Sci_Name,
            LAG(detection_date) OVER (
                PARTITION BY Sci_Name
                ORDER BY detection_date
            ) AS previous_detection_date
        FROM species_days
    )
    SELECT
        detection_date,
        COUNT(*) AS rare_species_count
    FROM species_history
    WHERE previous_detection_date IS NOT NULL
      AND (julianday(detection_date) - julianday(previous_detection_date)) > ?
    GROUP BY detection_date
),
scored AS (
    SELECT
        dc.detection_date,
        dc.daily_detections,
        ROUND(a.avg_daily_detections, 2) AS all_time_daily_avg,
        ROUND(dc.daily_detections * 1.0 / a.avg_daily_detections, 3) AS activity_ratio,
        COALESCE(drs.rare_species_count, 0) AS rare_species_count,
        COALESCE(dns.new_species_count, 0) AS new_species_count,
        CASE
            WHEN (50 + (20 * ln(dc.daily_detections * 1.0 / a.avg_daily_detections))) < 0 THEN 0
            WHEN (50 + (20 * ln(dc.daily_detections * 1.0 / a.avg_daily_detections))) > 95 THEN 95
            ELSE ROUND(50 + (20 * ln(dc.daily_detections * 1.0 / a.avg_daily_detections)), 1)
        END AS base_score,
        MIN(
            (COALESCE(drs.rare_species_count, 0) * 6) +
            (COALESCE(dns.new_species_count, 0) * 10),
            20
        ) AS bonus_score
    FROM daily_counts dc
    CROSS JOIN all_time_avg a
    LEFT JOIN daily_new_species dns
        ON dc.detection_date = dns.detection_date
    LEFT JOIN daily_rare_species drs
        ON dc.detection_date = drs.detection_date
)
SELECT
    detection_date,
    daily_detections,
    all_time_daily_avg,
    activity_ratio,
    rare_species_count,
    new_species_count,
    base_score,
    bonus_score,
    CASE
        WHEN (base_score + bonus_score) > 100 THEN 100
        ELSE ROUND(base_score + bonus_score, 1)
    END AS final_score,
    CASE
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 24 THEN 'Dead Quiet'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 39 THEN 'Slow Bird Day'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 49 THEN 'Below Average'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 59 THEN 'Average Bird Day'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 74 THEN 'Good Bird Day'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 89 THEN 'Great Bird Day'
        ELSE 'Banner Bird Day'
    END AS bird_day_rating
FROM scored
ORDER BY detection_date DESC
"""


@app.route("/api/bird_day_index_v1")
def api_bird_day_index_v1():
    """
    Read-only analytics endpoint. Runs the Bird Day Index V1 query
    against the existing BirdNET-Pi detections DB and returns JSON.

    Response shape (success):
        {
            "ok": true,
            "today": "YYYY-MM-DD",
            "rows": [
                {
                    "detection_date": "YYYY-MM-DD",
                    "daily_detections": int,
                    "all_time_daily_avg": float,
                    "activity_ratio": float,
                    "rare_species_count": int,
                    "new_species_count": int,
                    "base_score": float,
                    "bonus_score": int,
                    "final_score": float,
                    "bird_day_rating": str
                },
                ...
            ]
        }

    Response shape (error):
        { "ok": false, "error": "..." }  (HTTP 200 — UI handles gracefully)
    """
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"

    if not os.path.exists(db_path):
        return jsonify({
            "ok": False,
            "error": f"Detections database not found at {db_path}",
            "rows": [],
            "today": datetime.now().strftime("%Y-%m-%d"),
        })

    try:
        rare_days = max(1, min(365, int(request.args.get("rare_days", "3"))))
    except (ValueError, TypeError):
        rare_days = 3

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(BIRD_DAY_INDEX_V1_SQL, (rare_days,))
        raw_rows = cursor.fetchall()
        conn.close()

        rows = []
        for r in raw_rows:
            rows.append({
                "detection_date": r["detection_date"],
                "daily_detections": r["daily_detections"],
                "all_time_daily_avg": r["all_time_daily_avg"],
                "activity_ratio": r["activity_ratio"],
                "rare_species_count": r["rare_species_count"],
                "new_species_count": r["new_species_count"],
                "base_score": r["base_score"],
                "bonus_score": r["bonus_score"],
                "final_score": r["final_score"],
                "bird_day_rating": r["bird_day_rating"],
            })

        return jsonify({
            "ok": True,
            "today": datetime.now().strftime("%Y-%m-%d"),
            "rare_days": rare_days,
            "rows": rows,
        })

    except Exception as e:
        print(f"[ERROR] /api/bird_day_index_v1 failed: {e}")
        return jsonify({
            "ok": False,
            "error": str(e),
            "rows": [],
            "today": datetime.now().strftime("%Y-%m-%d"),
        })


# ----------------------------------------------------------------------
# Bird Day Index V2 — same scoring logic as V1, but baseline is the
# last 30 days instead of all-time. Falls back to all-time if the
# 30-day window is empty (e.g. fresh install / offline gap).
# Additive and read-only: does NOT modify V1 or any existing route.
# ----------------------------------------------------------------------
BIRD_DAY_INDEX_V2_SQL = """
WITH daily_counts AS (
    SELECT
        Date AS detection_date,
        COUNT(*) AS daily_detections
    FROM detections
    GROUP BY Date
),
recent_window AS (
    SELECT AVG(daily_detections * 1.0) AS recent_avg
    FROM daily_counts
    WHERE julianday('now') - julianday(detection_date) BETWEEN 0 AND 30
),
all_time_avg AS (
    SELECT AVG(daily_detections * 1.0) AS all_time_avg
    FROM daily_counts
),
baseline AS (
    SELECT
        CASE
            WHEN rw.recent_avg IS NULL OR rw.recent_avg = 0
                THEN ata.all_time_avg
            ELSE rw.recent_avg
        END AS effective_avg
    FROM recent_window rw
    CROSS JOIN all_time_avg ata
),
first_seen AS (
    SELECT
        Sci_Name,
        MIN(Date) AS first_seen_date
    FROM detections
    GROUP BY Sci_Name
),
daily_new_species AS (
    SELECT
        d.Date AS detection_date,
        COUNT(DISTINCT d.Sci_Name) AS new_species_count
    FROM detections d
    INNER JOIN first_seen f
        ON d.Sci_Name = f.Sci_Name
       AND d.Date = f.first_seen_date
    GROUP BY d.Date
),
daily_rare_species AS (
    WITH species_days AS (
        SELECT DISTINCT
            Date AS detection_date,
            Sci_Name
        FROM detections
    ),
    species_history AS (
        SELECT
            detection_date,
            Sci_Name,
            LAG(detection_date) OVER (
                PARTITION BY Sci_Name
                ORDER BY detection_date
            ) AS previous_detection_date
        FROM species_days
    )
    SELECT
        detection_date,
        COUNT(*) AS rare_species_count
    FROM species_history
    WHERE previous_detection_date IS NOT NULL
      AND (julianday(detection_date) - julianday(previous_detection_date)) > ?
    GROUP BY detection_date
),
scored AS (
    SELECT
        dc.detection_date,
        dc.daily_detections,
        ROUND(b.effective_avg, 2) AS all_time_daily_avg,
        ROUND(dc.daily_detections * 1.0 / b.effective_avg, 3) AS activity_ratio,
        COALESCE(drs.rare_species_count, 0) AS rare_species_count,
        COALESCE(dns.new_species_count, 0) AS new_species_count,
        CASE
            WHEN (50 + (20 * ln(dc.daily_detections * 1.0 / b.effective_avg))) < 0 THEN 0
            WHEN (50 + (20 * ln(dc.daily_detections * 1.0 / b.effective_avg))) > 95 THEN 95
            ELSE ROUND(50 + (20 * ln(dc.daily_detections * 1.0 / b.effective_avg)), 1)
        END AS base_score,
        MIN(
            (COALESCE(drs.rare_species_count, 0) * 6) +
            (COALESCE(dns.new_species_count, 0) * 10),
            20
        ) AS bonus_score
    FROM daily_counts dc
    CROSS JOIN baseline b
    LEFT JOIN daily_new_species dns
        ON dc.detection_date = dns.detection_date
    LEFT JOIN daily_rare_species drs
        ON dc.detection_date = drs.detection_date
)
SELECT
    detection_date,
    daily_detections,
    all_time_daily_avg,
    activity_ratio,
    rare_species_count,
    new_species_count,
    base_score,
    bonus_score,
    CASE
        WHEN (base_score + bonus_score) > 100 THEN 100
        ELSE ROUND(base_score + bonus_score, 1)
    END AS final_score,
    CASE
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 24 THEN 'Dead Quiet'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 39 THEN 'Slow Bird Day'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 49 THEN 'Below Average'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 59 THEN 'Average Bird Day'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 74 THEN 'Good Bird Day'
        WHEN (CASE WHEN (base_score + bonus_score) > 100 THEN 100 ELSE (base_score + bonus_score) END) <= 89 THEN 'Great Bird Day'
        ELSE 'Banner Bird Day'
    END AS bird_day_rating
FROM scored
ORDER BY detection_date DESC
"""


@app.route("/api/bird_day_index_v2")
def api_bird_day_index_v2():
    """
    V2 analytics endpoint. Same response shape as V1, but the baseline
    uses detections from the last 30 days. Falls back to V1's all-time
    baseline if the recent window has no data. Identical columns so
    the UI renders both datasets with the same code.
    """
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"

    if not os.path.exists(db_path):
        return jsonify({
            "ok": False,
            "error": f"Detections database not found at {db_path}",
            "rows": [],
            "today": datetime.now().strftime("%Y-%m-%d"),
        })

    try:
        rare_days = max(1, min(365, int(request.args.get("rare_days", "3"))))
    except (ValueError, TypeError):
        rare_days = 3

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(BIRD_DAY_INDEX_V2_SQL, (rare_days,))
        raw_rows = cursor.fetchall()
        conn.close()

        rows = []
        for r in raw_rows:
            rows.append({
                "detection_date": r["detection_date"],
                "daily_detections": r["daily_detections"],
                "all_time_daily_avg": r["all_time_daily_avg"],
                "activity_ratio": r["activity_ratio"],
                "rare_species_count": r["rare_species_count"],
                "new_species_count": r["new_species_count"],
                "base_score": r["base_score"],
                "bonus_score": r["bonus_score"],
                "final_score": r["final_score"],
                "bird_day_rating": r["bird_day_rating"],
            })

        return jsonify({
            "ok": True,
            "today": datetime.now().strftime("%Y-%m-%d"),
            "rare_days": rare_days,
            "rows": rows,
        })

    except Exception as e:
        print(f"[ERROR] /api/bird_day_index_v2 failed: {e}")
        return jsonify({
            "ok": False,
            "error": str(e),
            "rows": [],
            "today": datetime.now().strftime("%Y-%m-%d"),
        })


@app.route("/audio_status")
def audio_status():
    """
    Report whether BirdNET's local audio/recording services are running.
    Used by the QR UI mic icon.
    """
    try:
        def is_active(unit: str) -> bool:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return result.returncode == 0

        recording_ok = is_active("birdnet_recording.service")
        analysis_ok = is_active("birdnet_analysis.service")

        connected = recording_ok and analysis_ok
        return jsonify({"connected": connected})
    except Exception as e:
        print(f"Error checking audio status: {e}")
        return jsonify({"connected": False})


from flask import abort

@app.route("/api/first_detections_today")
def api_first_detections_today():
    """Return all unique species detected today, ordered by most recently detected."""
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
    if not os.path.exists(db_path):
        return jsonify({"ok": False, "error": "Database not found", "species": []}), 500
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT Com_Name, MAX(Date || ' ' || Time) AS last_seen
            FROM detections
            WHERE Date = DATE('now', 'localtime')
            GROUP BY Com_Name
            ORDER BY last_seen DESC
        """).fetchall()
        conn.close()
        species = [{"name": r["Com_Name"], "last_seen": r["last_seen"]} for r in rows]
        return jsonify({"ok": True, "today": datetime.now().strftime("%Y-%m-%d"), "species": species})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "species": []}), 500


# Tracks species -> Unix timestamp when their 6h chirp cooldown expires.
# Resets on server restart (intentional — daily listening sessions are ephemeral).
_chirp_cooldown: dict = {}
_CHIRP_COOLDOWN_SECS = 6 * 3600
_CHIRP_TOP_N = 15  # candidate pool size to randomize from


@app.route("/api/best_clip_today")
def api_best_clip_today():
    """Return a randomized high-confidence clip from today's detections.

    Selection rules:
    1. Pool: top-N species by best confidence recorded today with a valid audio file.
    2. Species used in the last 6 hours are excluded (cooldown) unless all are on cooldown.
    3. Pick randomly from the remaining eligible pool.
    4. After a species is selected, start its 6-hour cooldown.
    """
    db_path = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
    if not os.path.exists(db_path):
        return jsonify({"ok": False, "error": "Database not found"}), 500

    now_ts = datetime.now().timestamp()

    # Prune expired cooldowns
    for k in [k for k, v in _chirp_cooldown.items() if v <= now_ts]:
        del _chirp_cooldown[k]

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Top-N species by highest confidence today (recordings with a File_Name only)
        top_species = conn.execute("""
            SELECT Com_Name,
                   MAX(CAST(Confidence AS REAL)) AS best_conf
            FROM   detections
            WHERE  Date = DATE('now', 'localtime')
              AND  File_Name IS NOT NULL AND File_Name != ''
            GROUP  BY Com_Name
            ORDER  BY best_conf DESC
            LIMIT  ?
        """, (_CHIRP_TOP_N,)).fetchall()
        conn.close()
    except Exception as e:
        print(f"[ERROR] /api/best_clip_today: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    if not top_species:
        return jsonify({"ok": False, "error": "No detections today"}), 404

    # Separate eligible (not on cooldown) from ineligible; fall back to full pool if all cooling
    eligible = [s for s in top_species if s["Com_Name"] not in _chirp_cooldown]
    pool = eligible if eligible else list(top_species)

    random.shuffle(pool)

    try:
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row

        for candidate in pool:
            com_name = candidate["Com_Name"]
            rows = conn2.execute("""
                SELECT Date, Time, Confidence, File_Name
                FROM   detections
                WHERE  Date = DATE('now', 'localtime')
                  AND  Com_Name = ?
                  AND  File_Name IS NOT NULL AND File_Name != ''
                ORDER  BY CAST(Confidence AS REAL) DESC, Time DESC
                LIMIT  50
            """, (com_name,)).fetchall()

            for row in rows:
                audio_path, spec_path = find_clip_paths(com_name, row["Date"], row["File_Name"])
                if audio_path:
                    conn2.close()
                    _chirp_cooldown[com_name] = now_ts + _CHIRP_COOLDOWN_SECS
                    cached = get_cached_image(com_name)
                    species_folder = com_name.replace("'", "").replace(" ", "_")
                    relpath = f"{row['Date']}/{species_folder}/{row['File_Name']}"
                    return jsonify({
                        "ok":              True,
                        "species":         com_name,
                        "confidence":      round(float(row["Confidence"]) * 100),
                        "audio_url":       f"/clip/{relpath}",
                        "spectrogram_url": f"/clip/{relpath}.png" if spec_path else None,
                        "clip_time":       f"{row['Date']} {row['Time']}",
                        "image_url":       cached["image_url"] if cached else None,
                    })

        conn2.close()
    except Exception as e:
        print(f"[ERROR] /api/best_clip_today search: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": False, "error": "No audio files on disk for today's detections"}), 404


@app.route('/clip/<path:relpath>')
def serve_clip(relpath):
    """Serve an extracted audio clip or its spectrogram PNG from the BirdSongs directory."""
    canonical_base = os.path.realpath(EXTRACTED_DIR)
    full_path = os.path.realpath(os.path.join(EXTRACTED_DIR, relpath))
    if not full_path.startswith(canonical_base + os.sep):
        abort(403)
    if not os.path.isfile(full_path):
        abort(404)
    mimetype = "image/png" if full_path.endswith(".png") else "audio/mpeg"
    return send_file(full_path, mimetype=mimetype)


@app.route("/create_image_folder", methods=["POST"])
def create_image_folder():
    """
    Create an image folder for a given bird species under BIRD_IMAGE_CACHE_BASE.

    Expected JSON:
        {
            "folder_name": "Tree_Swallow"
            // or:
            // "name": "Tree Swallow"
            // "common_name": "Tree Swallow"
        }

    Returns JSON:
        {
            "ok": true/false,
            "folder_name": "...",
            "folder_path": "...",
            "already_exists": true/false,
            "error": null or string
        }
    """
    try:
        data = request.get_json(silent=True) or {}

        folder_name = data.get("folder_name")
        common_name = data.get("name") or data.get("common_name")

        if not folder_name and not common_name:
            return jsonify(
                {
                    "ok": False,
                    "error": "Missing folder_name or common_name",
                }
            ), 400

        # If only common_name is given, derive the canonical folder name
        if not folder_name and common_name:
            folder_name, _ = get_bird_folder_name(common_name)

        # Basic safety: only allow simple folder names
        if not re.match(r"^[A-Za-z0-9_\-]+$", folder_name):
            return jsonify(
                {
                    "ok": False,
                    "error": "Invalid folder name",
                }
            ), 400

        folder_path = os.path.join(BIRD_IMAGE_CACHE_BASE, folder_name)
        already_exists = os.path.isdir(folder_path)

        if not already_exists:
            os.makedirs(folder_path, exist_ok=True)
            # Keep our in-memory folder set in sync
            EXISTING_BIRD_IMAGE_FOLDERS.add(folder_name)

        return jsonify(
            {
                "ok": True,
                "folder_name": folder_name,
                "folder_path": folder_path,
                "already_exists": already_exists,
                "error": None,
            }
        )

    except Exception as e:
        print(f"Error creating image folder: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
        
        
@app.route("/upload_bird_images", methods=["POST"])
def upload_bird_images():
    """
    Upload one or more image files for a given bird species.

    Expects multipart/form-data with fields:
        - folder_name (optional)
        - name or common_name (optional, used if folder_name is not provided)
        - files: one or more image files

    Returns JSON:
        {
            "ok": true/false,
            "folder_name": "...",
            "folder_path": "...",
            "saved_files": ["file1.jpg", "file2.png"],
            "error": null or string
        }
    """
    try:
        # folder_name or common_name to figure out target folder
        folder_name = request.form.get("folder_name") or request.form.get("species_folder")
        common_name = request.form.get("name") or request.form.get("common_name")

        if not folder_name and not common_name:
            return jsonify(
                {
                    "ok": False,
                    "error": "Missing folder_name or common_name",
                }
            ), 400

        if not folder_name and common_name:
            folder_name, _ = get_bird_folder_name(common_name)

        # Basic safety: only allow simple folder names
        if not re.match(r"^[A-Za-z0-9_\-]+$", folder_name):
            return jsonify(
                {
                    "ok": False,
                    "error": "Invalid folder name",
                }
            ), 400

        folder_path = os.path.join(BIRD_IMAGE_CACHE_BASE, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        EXISTING_BIRD_IMAGE_FOLDERS.add(folder_name)

        # Get files list
        files = request.files.getlist("files")
        if not files:
            # support single file field "file" as well
            single = request.files.get("file")
            if single:
                files = [single]

        if not files:
            return jsonify(
                {
                    "ok": False,
                    "error": "No files provided",
                }
            ), 400

        allowed_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif")
        saved_files = []

        for f in files:
            if not f or f.filename == "":
                continue

            filename = secure_filename(f.filename)
            if not filename:
                continue

            ext = os.path.splitext(filename)[1].lower()
            if ext not in allowed_exts:
                print(f"Skipping unsupported file type: {filename}")
                continue

            dest_path = os.path.join(folder_path, filename)
            f.save(dest_path)
            saved_files.append(filename)

        if not saved_files:
            return jsonify(
                {
                    "ok": False,
                    "folder_name": folder_name,
                    "folder_path": folder_path,
                    "saved_files": [],
                    "error": "No valid image files were uploaded",
                }
            ), 400

        return jsonify(
            {
                "ok": True,
                "folder_name": folder_name,
                "folder_path": folder_path,
                "saved_files": saved_files,
                "error": None,
            }
        )

    except Exception as e:
        print(f"Error uploading bird images: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/shutdown", methods=["POST"])
def shutdown():
    shutdown_func = request.environ.get("werkzeug.server.shutdown")
    if shutdown_func:
        print("Shutdown request received. Shutting down server...")
        shutdown_func()
        return "Server is shutting down..."
    else:
        print("Error: Not running with Werkzeug server; cannot shut down.")
        return "Server not running with Werkzeug.", 500

# ----------------------------------------------------------------------
# Fan state + hardware control + /fan API
# ----------------------------------------------------------------------

from pathlib import Path  # (safe even if already imported above)

FAN_STATE_FILE = Path("/home/birdpi/birdnet_display/fan_state.json")
FAN_COOLING_PATH = Path("/sys/class/thermal/cooling_device0/cur_state")
OFF_AUTO_TEMP_C = 75.0  # temperature at which Off fails over to Auto


def load_fan_state():
    """
    Load fan state from disk.
    Example: {"mode": "Auto", "speed": 100}
    """
    if FAN_STATE_FILE.exists():
        try:
            return json.loads(FAN_STATE_FILE.read_text())
        except Exception:
            pass
    # Default
    return {"mode": "Auto", "speed": 100}


def save_fan_state(state: dict):
    """
    Save fan state to disk.
    """
    try:
        FAN_STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


def apply_fan_state_to_hardware(mode: str, speed: int):
    """
    Map logical mode+speed to kernel pwm-fan cooling_device0 cur_state.

    mode: "Off", "On", "Auto"
    speed: 0–100 (used only when mode == "On")
    """
    try:
        if not FAN_COOLING_PATH.exists():
            return

        mode_lower = (mode or "").lower()

        # Auto: let kernel thermal governor handle it
        if mode_lower == "auto":
            return

        # Off: force state 0
        if mode_lower == "off":
            state = 0
        else:
            # On: map 0–100% to discrete states 0–4
            pct = max(0, min(100, int(speed)))
            if pct == 0:
                state = 0
            elif pct <= 25:
                state = 1
            elif pct <= 50:
                state = 2
            elif pct <= 75:
                state = 3
            else:
                state = 4

        cmd = f"echo {state} | sudo tee {FAN_COOLING_PATH}"
        print(f"[FAN] Applying hardware state: mode={mode}, speed={speed}, state={state}")
        os.system(cmd)
    except Exception as e:
        print(f"[FAN] Error applying hardware state: {e}")


@app.route("/fan", methods=["GET", "POST"])
def fan_control():
    """
    GET  -> return fan state (+ temp, hw_state) if available
    POST -> update fan state and apply to hardware
    """
    state = load_fan_state()

    if request.method == "GET":
        # ----------------------------------------------------
        # Read CPU temp
        # ----------------------------------------------------
        temp_c = None
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                raw = f.read().strip()
                temp_c = int(raw) / 1000.0
        except Exception:
            pass

        # ----------------------------------------------------
        # Read actual hardware fan state
        # ----------------------------------------------------
        hw_state = None
        try:
            if FAN_COOLING_PATH.exists():
                hw_state = int(FAN_COOLING_PATH.read_text().strip())
        except Exception:
            hw_state = None

        # ----------------------------------------------------
        # Determine "off-like" mode
        # ----------------------------------------------------
        mode_val = state.get("mode", "Auto")
        speed_val = state.get("speed", 100)
        is_off_like = (mode_val == "Off") or (mode_val == "On" and speed_val <= 0)

        # If UI says Off but hardware is spinning → kernel override → Auto
        if is_off_like and hw_state not in (None, 0):
            state["mode"] = "Auto"
            save_fan_state(state)

        # Automatic failover to Auto if temperature exceeds threshold
        if (
            is_off_like
            and temp_c is not None
            and temp_c >= OFF_AUTO_TEMP_C
        ):
            state["mode"] = "Auto"
            save_fan_state(state)

        # Response payload
        resp = {
            "mode": state.get("mode", "Auto"),
            "speed": state.get("speed", 100),
        }
        if temp_c is not None:
            resp["temperature_c"] = round(temp_c, 1)
        if hw_state is not None:
            resp["hw_state"] = hw_state

        return jsonify(resp)

    # ==========================================================
    # POST (Update fan state)
    # ==========================================================
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", state.get("mode", "Auto"))
    speed = int(data.get("speed", state.get("speed", 100)))
    speed = max(0, min(100, speed))

    new_state = {"mode": mode, "speed": speed}
    save_fan_state(new_state)
    apply_fan_state_to_hardware(mode, speed)

    return jsonify(new_state)


@app.route("/brightness", methods=["POST"])
def set_brightness():
    try:
        data = request.get_json(force=True, silent=True) or {}
        brightness = data.get("brightness")
        if brightness is not None:
            val = int(brightness)
            if 0 <= val <= 255:
                cmd = (
                    f"echo {val} | sudo tee /sys/class/backlight/10-0045/brightness"
                )
                print(f"Executing brightness command: {cmd}")
                os.system(cmd)
                return jsonify({"status": "success", "brightness": val})
        return jsonify({"status": "error", "message": "Invalid brightness value"}), 400
    except Exception as e:
        print(f"Error setting brightness: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/reboot", methods=["POST"])
def reboot_system():
    print("Executing reboot command...")
    os.system("sudo reboot")
    return jsonify({"status": "rebooting"})


@app.route("/poweroff", methods=["POST"])
def poweroff_system():
    print("Executing power off command...")
    os.system("sudo poweroff")
    return jsonify({"status": "shutting down"})


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if "--build-cache" in sys.argv:
        print("To build the cache, please run 'python cache_builder.py' directly.")
        sys.exit(0)

    print(f"Starting Flask server on http://0.0.0.0:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT)