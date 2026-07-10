# keep current 
import requests
import sqlite3
from flask import Flask, render_template, send_file, send_from_directory, request, jsonify, session
from urllib.parse import quote, urljoin
from datetime import datetime
import os
import random
import socket
import time
import ipaddress
import hmac
import secrets
import qrcode
import io
import json
import sys
import re
import subprocess
import csv
import uuid
from datetime import datetime
from pathlib import Path
from werkzeug.utils import secure_filename
import os
from flask import jsonify, request, abort
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from functools import wraps
from PIL import Image, UnidentifiedImageError
from avicommons import read_species_metadata
from image_cache_utils import (
    ALLOWED_IMAGE_EXTENSIONS,
    ALLOWED_IMAGE_FORMATS,
    IMAGE_FORMAT_EXTENSIONS,
    is_allowed_image_filename,
    validate_cached_image,
)
from path_config import PATHS

app = Flask(
    __name__,
    template_folder=str(PATHS.static_dir),
    static_folder=str(PATHS.static_dir),
)
app.config['TEMPLATES_AUTO_RELOAD'] = True


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[WARN] Ignoring invalid integer for {name}: {raw}")
        return default
    return value if value > 0 else default


BIRDNET_UPLOAD_MAX_BYTES = _env_int("BIRDNET_UPLOAD_MAX_BYTES", 10 * 1024 * 1024)
BIRDNET_UPLOAD_MAX_DIMENSION = _env_int("BIRDNET_UPLOAD_MAX_DIMENSION", 4096)
BIRDNET_UPLOAD_MAX_PIXELS = _env_int("BIRDNET_UPLOAD_MAX_PIXELS", 8_847_360)
app.config["MAX_CONTENT_LENGTH"] = BIRDNET_UPLOAD_MAX_BYTES


@app.errorhandler(RequestEntityTooLarge)
def handle_request_too_large(_exc):
    return jsonify({"ok": False, "error": "Uploaded image is too large"}), 413

# Admin protection:
# Set BIRDNET_DISPLAY_ADMIN_SECRET to a long random value before enabling any
# admin controls, for example:
#   export BIRDNET_DISPLAY_ADMIN_SECRET="replace-with-a-long-random-value"
# Do not commit, print, or log the real secret.
ADMIN_SECRET = ""
ADMIN_REQUIRE_LOCAL_NETWORK = False
ADMIN_SECRET_FILE = Path(
    os.environ.get(
        "BIRDNET_DISPLAY_ADMIN_SECRET_FILE",
        PATHS.display_home / ".admin_secret",
    )
)
TRUSTED_ADMIN_NETWORKS = (
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,"
    "192.168.0.0/16,169.254.0.0/16,fe80::/10"
)

app.config["ADMIN_SECRET"] = ADMIN_SECRET
app.secret_key = (
    os.environ.get("BIRDNET_DISPLAY_FLASK_SECRET_KEY")
    or os.environ.get("BIRDNET_DISPLAY_ADMIN_SECRET")
    or secrets.token_bytes(32)
)

CSRF_SESSION_KEY = "admin_csrf_token"
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_admin_secret_file() -> str:
    try:
        if ADMIN_SECRET_FILE.exists():
            return ADMIN_SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"[ADMIN] Could not read admin secret file: {exc}")
    return ""


def get_admin_secret() -> str:
    return (
        os.environ.get("BIRDNET_DISPLAY_ADMIN_SECRET")
        or app.config.get("ADMIN_SECRET")
        or _read_admin_secret_file()
        or ""
    ).strip()


def admin_secret_configured() -> bool:
    return bool(get_admin_secret())


def validate_new_admin_secret(secret: str) -> tuple[bool, str]:
    if not isinstance(secret, str):
        return False, "System password is required"
    secret = secret.strip()
    if len(secret) < 8:
        return False, "System password must be at least 8 characters"
    if "\n" in secret or "\r" in secret:
        return False, "System password cannot contain line breaks"
    return True, ""


def save_admin_secret(secret: str) -> tuple[bool, str]:
    is_valid, message = validate_new_admin_secret(secret)
    if not is_valid:
        return False, message
    try:
        ADMIN_SECRET_FILE.write_text(secret.strip() + "\n", encoding="utf-8")
        ADMIN_SECRET_FILE.chmod(0o600)
    except OSError as exc:
        print(f"[ADMIN] Could not save admin secret file: {exc}")
        return False, "Could not save system password"
    return True, ""


def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _json_error(message: str, status_code: int):
    return jsonify({"ok": False, "error": message}), status_code


def _json_validation_error(message: str):
    return _json_error(message, 400)


def _json_server_error(message: str = "Request failed. Check the server logs for details."):
    return _json_error(message, 500)


def get_json_payload_or_error():
    if not request.is_json:
        return None, _json_validation_error("Expected a JSON request body.")
    data = request.get_json(silent=True)
    if data is None:
        return None, _json_validation_error("Malformed JSON request body.")
    if not isinstance(data, dict):
        return None, _json_validation_error("Expected a JSON object request body.")
    return data, None


def _trusted_admin_networks():
    raw = os.environ.get("TRUSTED_ADMIN_NETWORKS", TRUSTED_ADMIN_NETWORKS)
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            print(f"[ADMIN] Ignoring invalid trusted network: {item}")
    return networks


def _remote_addr_is_trusted() -> bool:
    try:
        remote_ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    return any(remote_ip in network for network in _trusted_admin_networks())


def trusted_network_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        error = _trusted_network_error()
        if error:
            return error
        return func(*args, **kwargs)

    return wrapper


def _trusted_network_error():
    require_local = _env_bool("ADMIN_REQUIRE_LOCAL_NETWORK", ADMIN_REQUIRE_LOCAL_NETWORK)
    if require_local and not _remote_addr_is_trusted():
        return _json_error("System Controls are not allowed from this network", 403)
    return None


def _admin_access_error():
    trusted_error = _trusted_network_error()
    if trusted_error:
        return trusted_error
    if not admin_secret_configured():
        return _json_error("System Controls are locked until a system password is created", 503)
    if not session.get("admin_authenticated"):
        return _json_error("Unlock System Controls first", 401)
    return None


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        error = _admin_access_error()
        if error:
            return error
        return func(*args, **kwargs)

    return wrapper


def _csrf_error():
    if request.method not in STATE_CHANGING_METHODS:
        return None
    expected = session.get(CSRF_SESSION_KEY)
    provided = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    if not provided and request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            provided = data.get("csrf_token")
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        return _json_error("Missing or invalid CSRF token", 403)
    return None


def csrf_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        error = _csrf_error()
        if error:
            return error
        return func(*args, **kwargs)

    return wrapper


@app.get("/admin/csrf")
def admin_csrf():
    return jsonify({"ok": True, "csrf_token": get_csrf_token()})


@app.get("/admin/status")
def admin_status():
    return jsonify(
        {
            "ok": True,
            "configured": admin_secret_configured(),
            "authenticated": bool(session.get("admin_authenticated")),
            "local_network_required": _env_bool("ADMIN_REQUIRE_LOCAL_NETWORK", ADMIN_REQUIRE_LOCAL_NETWORK),
        }
    )


@app.post("/admin/setup")
@trusted_network_required
@csrf_required
def admin_setup():
    if admin_secret_configured():
        return _json_error("System password is already configured", 409)

    data, payload_error = get_json_payload_or_error()
    if payload_error:
        return payload_error
    provided = str(data.get("secret", "")).strip()
    saved, message = save_admin_secret(provided)
    if not saved:
        return _json_error(message, 400)

    session["admin_authenticated"] = True
    return jsonify({"ok": True, "configured": True, "authenticated": True})


@app.post("/admin/login")
@trusted_network_required
@csrf_required
def admin_login():
    secret = get_admin_secret()
    if not secret:
        return _json_error("System Controls are locked until a system password is created", 503)

    data, payload_error = get_json_payload_or_error()
    if payload_error:
        return payload_error
    provided = str(data.get("secret", ""))
    if not hmac.compare_digest(secret, provided):
        return _json_error("Incorrect password", 401)

    session["admin_authenticated"] = True
    return jsonify({"ok": True})


@app.post("/admin/logout")
@admin_required
@csrf_required
def admin_logout():
    session.pop("admin_authenticated", None)
    return jsonify({"ok": True})

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
    for name in _valid_cached_image_names(
        os.path.basename(folder_path), folder_path, context="api_bird_images"
    ):
        p = os.path.join(folder_path, name)
        st = os.stat(p)
        metadata = _image_metadata(folder_path, name)
        items.append({
            "name": name,
            "url": _bird_image_url(os.path.basename(folder_path), name),
            "bytes": st.st_size,
            "mtime": int(st.st_mtime),
            "attribution": _image_attribution(folder_path, name),
            "source": metadata.get("source", ""),
            "license": metadata.get("license", ""),
            "by": metadata.get("by", ""),
        })

    return jsonify({"ok": True, "folder_exists": True, "images": items})


@app.delete("/api/bird_images")
@admin_required
@csrf_required
def api_bird_images_delete():
    """
    JSON body:
      { species_folder: "...", filename: "..." }
    """
    data, payload_error = get_json_payload_or_error()
    if payload_error:
        return payload_error
    species_folder = data.get("species_folder", "")
    filename = data.get("filename", "")

    folder_path = _safe_species_folder(species_folder)

    safe_file = _safe_image_filename(filename)
    folder_base = Path(folder_path).resolve()
    target = (folder_base / safe_file).resolve()
    if target.parent != folder_base:
        abort(400, description="Invalid path")

    if not target.is_file():
        return jsonify({"ok": False, "error": "File not found"}), 404

    target.unlink()
    return jsonify({"ok": True})


EXTRACTED_DIR = PATHS.audio_dir


def get_latest_clip_for_species(com_name: str):
    """Return the most recent DB detection row (with File_Name) whose audio file still exists on disk."""
    db_path = _get_db_path()
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
    db_path = _get_db_path()
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
    base = EXTRACTED_DIR / date / species_folder
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

PLACEHOLDER_DIRECTORY = PATHS.placeholder_dir

BASE_URL = "http://localhost:5000/"
API_ENDPOINT = "api/v2/detections/recent"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}
PROXIES = {"http": None, "https": None}
SERVER_PORT = 5000
BIRDNET_DB_PATH = PATHS.db_path


DETECTION_CACHE = {"id": None, "raw_data": []}

BIRD_IMAGE_BASE = PATHS.image_cache_dir

SPECTROGRAM_DIR = PATHS.spectrogram_cache_dir

# Only allow these image types for browsing/deleting
ALLOWED_EXTS = ALLOWED_IMAGE_EXTENSIONS
_INVALID_CACHED_IMAGE_LOGGED = set()

def _bird_image_base_path() -> Path:
    return BIRD_IMAGE_BASE.resolve()


def _safe_species_folder_name(species_folder: str) -> str:
    raw = (species_folder or "").strip()
    safe = secure_filename(raw)
    if not raw or not safe or safe != raw or "/" in raw or "\\" in raw:
        abort(400, description="Invalid species folder")
    if not re.match(r"^[A-Za-z0-9_.-]+$", safe):
        abort(400, description="Invalid species folder")
    return safe


def _safe_species_folder(species_folder: str) -> str:
    """
    Resolve a user-provided species folder under BIRD_IMAGE_BASE.
    Reject traversal and path-like input instead of silently rewriting it.
    """
    safe = _safe_species_folder_name(species_folder)
    base = _bird_image_base_path()
    full = (base / safe).resolve()
    if base not in full.parents:
        abort(400, description="Invalid path")
    return str(full)


def _image_cache_static_prefix() -> str | None:
    try:
        rel = PATHS.image_cache_dir.relative_to(PATHS.static_dir)
    except ValueError:
        return None
    return "/static/" + rel.as_posix().strip("/")


def _bird_image_url(species_folder: str, filename: str) -> str:
    prefix = _image_cache_static_prefix()
    safe_folder = quote(species_folder, safe="")
    safe_filename = quote(filename, safe="")
    if prefix:
        return f"{prefix}/{safe_folder}/{safe_filename}"
    return f"/bird-image-cache/{safe_folder}/{safe_filename}"


def _display_image_url(output_relative: str) -> str:
    relative = Path(output_relative)
    if len(relative.parts) < 2:
        return ""
    return f"/bird-image-cache/{quote(relative.parts[0], safe='')}/{quote(relative.name, safe='')}"


def _image_metadata(folder_path: str | Path, filename: str) -> dict:
    metadata = read_species_metadata(folder_path).get(filename, {})
    return metadata if isinstance(metadata, dict) else {}


def _image_attribution(folder_path: str | Path, filename: str) -> str:
    copyright_file = os.path.splitext(filename)[0] + ".txt"
    copyright_path = Path(folder_path) / copyright_file
    if copyright_path.is_file():
        with copyright_path.open("r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read().strip()
        if text:
            return text
    metadata = _image_metadata(folder_path, filename)
    return str(metadata.get("attribution") or "")


def _log_invalid_cached_image(
    context: str,
    *,
    species_name: str,
    species_folder: str,
    image_path: str | Path,
    url: str,
    reason: str,
) -> None:
    key = (context, str(image_path), reason)
    if key in _INVALID_CACHED_IMAGE_LOGGED:
        return
    _INVALID_CACHED_IMAGE_LOGGED.add(key)
    print(
        "[IMAGE_CACHE] Skipping invalid cached image "
        f"context={context} species={species_name!r} "
        f"folder={species_folder!r} path={image_path} url={url!r} "
        f"reason={reason}"
    )


def _valid_cached_image_names(
    species_folder: str,
    folder_path: str | Path,
    *,
    species_name: str = "",
    context: str = "image_cache",
) -> list[str]:
    if not os.path.isdir(folder_path):
        return []

    valid_names = []
    for name in sorted(os.listdir(folder_path)):
        if not is_allowed_image_filename(name):
            continue
        image_path = Path(folder_path) / name
        url = _bird_image_url(species_folder, name)
        validation = validate_cached_image(image_path, BIRD_IMAGE_BASE)
        if validation.ok:
            valid_names.append(name)
            continue
        _log_invalid_cached_image(
            context,
            species_name=species_name,
            species_folder=species_folder,
            image_path=image_path,
            url=url,
            reason=validation.reason,
        )
    return valid_names


def safe_join_under_base(base_dir: Path, filename: str) -> Path:
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if target.parent != base:
        raise ValueError("Unsafe upload path")
    return target


def _safe_image_filename(filename: str) -> str:
    raw = (filename or "").strip()
    safe = secure_filename(raw)
    if not raw or not safe or safe != raw or "/" in raw or "\\" in raw:
        abort(400, description="Invalid filename")
    ext = Path(safe).suffix.lower()
    if ext not in ALLOWED_EXTS:
        abort(400, description="Not an allowed image type")
    return safe


def _uploaded_file_size(file_storage) -> int:
    try:
        current_pos = file_storage.stream.tell()
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(current_pos)
        return size
    except Exception:
        return int(file_storage.content_length or 0)


def validate_uploaded_image(file_storage) -> dict:
    raw_name = (file_storage.filename or "").strip()
    if not raw_name:
        raise ValueError("Missing file")
    if "/" in raw_name or "\\" in raw_name:
        raise ValueError("Unsafe filename")

    original_name = secure_filename(raw_name)
    if not original_name:
        raise ValueError("Unsafe filename")

    original_ext = Path(original_name).suffix.lower()
    if original_ext not in ALLOWED_EXTS:
        raise ValueError("Unsupported file type")

    file_size = _uploaded_file_size(file_storage)
    if file_size <= 0:
        raise ValueError("Missing file")
    if file_size > BIRDNET_UPLOAD_MAX_BYTES:
        raise ValueError("Uploaded image is too large")

    try:
        file_storage.stream.seek(0)
        with Image.open(file_storage.stream) as img:
            image_format = (img.format or "").upper()
            width, height = img.size
            img.verify()

        if image_format not in ALLOWED_IMAGE_FORMATS:
            raise ValueError("Unsupported file type")
        if original_ext not in ALLOWED_IMAGE_FORMATS[image_format]:
            raise ValueError("File extension does not match image content")
        if width <= 0 or height <= 0:
            raise ValueError("Invalid image content")
        if width > BIRDNET_UPLOAD_MAX_DIMENSION or height > BIRDNET_UPLOAD_MAX_DIMENSION:
            raise ValueError("Image dimensions are too large")
        if width * height > BIRDNET_UPLOAD_MAX_PIXELS:
            raise ValueError("Image dimensions are too large")

        file_storage.stream.seek(0)
        return {
            "format": image_format,
            "extension": IMAGE_FORMAT_EXTENSIONS[image_format],
            "width": width,
            "height": height,
            "original_name": original_name,
            "size": file_size,
        }
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Invalid image content") from exc
    finally:
        try:
            file_storage.stream.seek(0)
        except Exception:
            pass


def generate_safe_image_filename(image_format: str, target_dir: Path) -> str:
    extension = IMAGE_FORMAT_EXTENSIONS[image_format]
    for _ in range(20):
        filename = f"{uuid.uuid4().hex}{extension}"
        if not safe_join_under_base(target_dir, filename).exists():
            return filename
    raise ValueError("Could not generate a safe image filename")


@app.route("/bird-image-cache/<species_folder>/<filename>")
def serve_bird_image_cache(species_folder, filename):
    folder_path = _safe_species_folder(species_folder)

    # Optimized display copies share this existing allowlisted URL route. Allow
    # legacy generated names with punctuation only when they resolve to an
    # existing file inside the dedicated optimized cache.
    display_base = DISPLAY_IMAGE_CACHE_BASE.resolve()
    display_path = (display_base / species_folder / filename).resolve()
    if (
        Path(filename).name == filename
        and display_base in display_path.parents
        and display_path.is_file()
        and display_path.suffix.lower() == ".webp"
    ):
        validation = validate_cached_image(display_path, display_base)
        if validation.ok:
            return send_file(display_path)

    safe_file = _safe_image_filename(filename)
    image_path = Path(folder_path) / safe_file
    validation = validate_cached_image(image_path, BIRD_IMAGE_BASE)
    if not validation.ok:
        _log_invalid_cached_image(
            "serve_bird_image_cache",
            species_name="",
            species_folder=species_folder,
            image_path=image_path,
            url=_bird_image_url(species_folder, safe_file),
            reason=validation.reason,
        )
        abort(404, description="Image not found")
    return send_from_directory(folder_path, safe_file)

# ----------------------------------------------------------------------
# IP + QR code helpers
# ----------------------------------------------------------------------
def get_local_ip():
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        if s is not None:
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

BIRD_IMAGE_CACHE_BASE = PATHS.image_cache_dir
BIRD_IMAGE_CACHE_BASE.mkdir(parents=True, exist_ok=True)
DISPLAY_IMAGE_CACHE_BASE = PATHS.display_image_cache_dir
DISPLAY_IMAGE_MANIFEST = DISPLAY_IMAGE_CACHE_BASE / "manifest.json"
SPECTROGRAM_DIR.mkdir(parents=True, exist_ok=True)

# Snapshot of all existing species folders
EXISTING_BIRD_IMAGE_FOLDERS = {
    name
    for name in os.listdir(BIRD_IMAGE_CACHE_BASE)
    if os.path.isdir(BIRD_IMAGE_CACHE_BASE / name)
}
_NORMALIZED_BIRD_IMAGE_FOLDERS: dict[str, str] = {}
_IMAGE_FOLDER_REFRESH_INTERVAL_SECONDS = 2.0
_last_image_folder_refresh = 0.0
_DISPLAY_IMAGE_MANIFEST_REFRESH_INTERVAL_SECONDS = 2.0
_last_display_image_manifest_refresh = 0.0
_display_images_by_folder: dict[str, list[dict[str, str]]] = {}


def refresh_display_image_manifest(*, force: bool = False) -> dict[str, list[dict[str, str]]]:
    """Load the optimized-image index without opening original image files."""
    global _last_display_image_manifest_refresh, _display_images_by_folder
    now = time.monotonic()
    if not force and now - _last_display_image_manifest_refresh < _DISPLAY_IMAGE_MANIFEST_REFRESH_INTERVAL_SECONDS:
        return _display_images_by_folder

    try:
        with DISPLAY_IMAGE_MANIFEST.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data.get("entries", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        entries = {}

    by_folder: dict[str, list[dict[str, str]]] = {}
    if isinstance(entries, dict):
        for source_relative, item in entries.items():
            if not isinstance(source_relative, str) or not isinstance(item, dict):
                continue
            output_relative = item.get("output")
            source_path = Path(source_relative)
            if not isinstance(output_relative, str) or len(source_path.parts) < 2:
                continue
            output_path = DISPLAY_IMAGE_CACHE_BASE / output_relative
            if not output_path.is_file():
                continue
            folder = source_path.parts[0]
            by_folder.setdefault(folder, []).append(
                {"source_name": source_path.name, "output": output_relative}
            )

    for images in by_folder.values():
        images.sort(key=lambda item: item["source_name"])
    _display_images_by_folder = by_folder
    _last_display_image_manifest_refresh = now
    return _display_images_by_folder


def _display_images_for_folder(folder_name: str) -> list[dict[str, str]]:
    return refresh_display_image_manifest().get(folder_name, [])


def refresh_bird_image_folders(*, force: bool = False) -> set[str]:
    """Keep the folder lookup current when imports add images after startup."""
    global _last_image_folder_refresh, _NORMALIZED_BIRD_IMAGE_FOLDERS
    now = time.monotonic()
    if not force and now - _last_image_folder_refresh < _IMAGE_FOLDER_REFRESH_INTERVAL_SECONDS:
        return set(EXISTING_BIRD_IMAGE_FOLDERS)

    if not BIRD_IMAGE_CACHE_BASE.is_dir():
        folders: set[str] = set()
    else:
        folders = {path.name for path in BIRD_IMAGE_CACHE_BASE.iterdir() if path.is_dir()}

    EXISTING_BIRD_IMAGE_FOLDERS.clear()
    EXISTING_BIRD_IMAGE_FOLDERS.update(folders)
    _NORMALIZED_BIRD_IMAGE_FOLDERS = {
        _normalize_species_folder_name(folder): folder for folder in folders
    }
    _known_species_dirs.cache_clear()
    _last_image_folder_refresh = now
    return folders


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


def _normalize_species_folder_name(value: str) -> str:
    value = value.strip().lower().replace("'", "").replace(".", "")
    return value.replace("_", "").replace(" ", "").replace("-", "")


def get_bird_folder_name(common_name: str) -> tuple[str, bool]:
    """
    Decide which folder name to use for this bird.

    More forgiving than strict canonical matching:
    - Treat hyphens/spaces/underscores as equivalent
    - Ignore apostrophes/periods
    - Case-insensitive
    """
    refresh_bird_image_folders()
    canonical = canonical_folder_name(common_name)

    want = _normalize_species_folder_name(canonical)
    existing = _NORMALIZED_BIRD_IMAGE_FOLDERS.get(want)
    if existing:
        return existing, True

    # Not found: this is what we'd create
    return canonical, False

def get_cached_image(species_name: str):
    if not species_name:
        return None
    refresh_bird_image_folders()
    base = species_name.strip()

    if base in SPECIES_ALIASES:
        base = SPECIES_ALIASES[base]

    candidates = [base]
    stripped = _strip_qualifiers(base)
    if stripped and stripped not in candidates:
        candidates.append(stripped)

        # Use the live in-memory set of known folders, which is updated whenever
    species_folder = None
    images: list[dict[str, str]] = []

    for cand in candidates:
        candidate_folder, folder_exists = get_bird_folder_name(cand)
        candidate_images = _display_images_for_folder(candidate_folder) if folder_exists else []
        if candidate_images:
            species_folder = candidate_folder
            images = candidate_images
            break

    # Fallback: generic "Common_Name" folder if it has optimized display copies.
    if species_folder is None:
        generic_images = _display_images_for_folder("Common_Name")
        if generic_images:
            images = generic_images
        species_folder = "Common_Name"

    if not images:
        return None

    # Pick a random optimized image each time this function is called.
    chosen_image = random.choice(images)
    source_name = chosen_image["source_name"]
    image_url = _display_image_url(chosen_image["output"])
    if not image_url:
        return None

    return {
        "image_url": image_url,
        "copyright": _image_attribution(BIRD_IMAGE_CACHE_BASE / species_folder, source_name),
    }


def has_cached_display_image(species_name: str) -> bool:
    """Whether a species has a ready-to-serve optimized display photo."""
    if not species_name:
        return False
    base = species_name.strip()
    if base in SPECIES_ALIASES:
        base = SPECIES_ALIASES[base]
    candidates = [base]
    stripped = _strip_qualifiers(base)
    if stripped and stripped not in candidates:
        candidates.append(stripped)
    for candidate in candidates:
        folder_name, folder_exists = get_bird_folder_name(candidate)
        if folder_exists and _display_images_for_folder(folder_name):
            return True
    return bool(_display_images_for_folder("Common_Name"))


def get_fallback_image() -> dict | None:
    """Return a local, attribution-free image for a species without a photo."""
    images = _display_images_for_folder("placeholders")
    if images:
        image_url = _display_image_url(images[0]["output"])
        return {"image_url": image_url, "copyright": ""} if image_url else None
    if not PLACEHOLDER_DIRECTORY or not PLACEHOLDER_DIRECTORY.is_dir():
        return None
    images = _valid_cached_image_names("placeholders", PLACEHOLDER_DIRECTORY, context="species_photo_fallback")
    if not images:
        return None
    return {"image_url": _bird_image_url("placeholders", sorted(images)[0]), "copyright": ""}


def get_display_image(species_name: str) -> dict | None:
    """Prefer a species photo and otherwise provide the friendly local fallback."""
    return get_cached_image(species_name) or get_fallback_image()


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
        image_files = _valid_cached_image_names(
            "placeholders",
            PLACEHOLDER_DIRECTORY,
            species_name="",
            context="offline_placeholders",
        )
        image_files.sort()

        if image_files:
            fallback_data = []
            max_cards = 4

            for i in range(max_cards):
                filename = image_files[i % len(image_files)]
                image_url = _bird_image_url("placeholders", filename)

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
    return BIRDNET_DB_PATH


def log_path_configuration():
    print("[PATHS] BirdNET Display resolved paths:")
    print(f"[PATHS]   BIRDNET_DISPLAY_HOME={PATHS.display_home}")
    print(f"[PATHS]   BIRDNET_DISPLAY_STATIC_DIR={PATHS.static_dir}")
    print(f"[PATHS]   BIRDNET_IMAGE_CACHE_DIR={PATHS.image_cache_dir}")
    print(f"[PATHS]   BIRDNET_PI_HOME={PATHS.birdnet_pi_home}")
    print(f"[PATHS]   BIRDNET_DB_PATH={PATHS.db_path}")
    print(f"[PATHS]   BIRDNET_AUDIO_DIR={PATHS.audio_dir}")

    warnings = [
        ("BIRDNET_DISPLAY_STATIC_DIR", PATHS.static_dir, "Display static files"),
        ("BIRDNET_IMAGE_CACHE_DIR", PATHS.image_cache_dir, "Bird image cache"),
        ("BIRDNET_DB_PATH", PATHS.db_path, "BirdNET detections database"),
        ("BIRDNET_AUDIO_DIR", PATHS.audio_dir, "BirdNET extracted audio directory"),
    ]
    for env_name, path, label in warnings:
        if not path.exists():
            print(f"[WARN] {label} not found at {path}. Set {env_name} to override.")


def get_bird_data_from_local_db():
    """Load bird data from BirdNET-Pi detections SQLite DB."""
    detections = []
    db_path = _get_db_path()

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

            cached = get_display_image(common_name)
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
            print(f"[WARN] Local DB not found at {db_path}")
            return None

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT Com_Name, Date, Time, Confidence
            FROM detections
            WHERE Date >= date('now', '-1 day')
              AND datetime(Date || ' ' || Time) >= datetime('now', '-1 day')
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
        species_image_state: dict[str, tuple[dict | None, str, bool, str, bool]] = {}
        for common_name, date_str, time_str, conf in rows:
            if not common_name:
                continue

            try:
                conf_val = int(float(conf) * 100)
            except Exception:
                conf_val = 0

            state = species_image_state.get(common_name)
            if state is None:
                # Resolve each species once per request; a 2,000-row detection
                # window often contains many repeated detections of the same bird.
                cached = get_display_image(common_name)
                folder_name, folder_exists = get_bird_folder_name(common_name)
                has_images = has_cached_display_image(common_name)
                state = (cached, folder_name, folder_exists, str(BIRD_IMAGE_CACHE_BASE / folder_name), has_images)
                species_image_state[common_name] = state

            cached, folder_name, folder_exists, folder_path, has_images = state
            img_url = cached["image_url"] if cached else ""
            copyright_info = cached["copyright"] if cached else ""

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
    db_path = _get_db_path()

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
            WHERE Date >= date('now', '-1 day')
              AND datetime(Date || ' ' || Time) >= datetime('now', '-1 day')
            GROUP BY Com_Name
            ORDER BY last_seen DESC;
        """

        rows = cursor.execute(query).fetchall()
        conn.close()

        results = []
        for name, sci_name, last_seen, conf, det_count in rows:
            if not name:
                continue

            # Prefer a species image, then use a friendly local fallback.
            cached = get_display_image(name)
            img_url = cached["image_url"] if cached else ""
            copyright_info = cached["copyright"] if cached else ""

            # Folder info
            folder_name, folder_exists = get_bird_folder_name(name)
            folder_path = str(BIRD_IMAGE_CACHE_BASE / folder_name)

            # Only ready-to-serve optimized copies count as display photos.
            has_images = has_cached_display_image(name)

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
    db_path = _get_db_path()
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

            cached = get_display_image(name)
            img_url = cached["image_url"] if cached else ""
            copyright_info = cached["copyright"] if cached else ""

            folder_name, folder_exists = get_bird_folder_name(name)
            folder_path = str(BIRD_IMAGE_CACHE_BASE / folder_name)

            has_images = has_cached_display_image(name)

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

    db_path = _get_db_path()
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
        return _json_server_error()


@app.route("/")
def index():
    bird_data, api_is_down = get_bird_data()

    template_path = "index.html"
    full_template_path = PATHS.static_dir / template_path
    if not full_template_path.exists():
        print(f"[ERROR] Display template not found: {full_template_path}")
        abort(500, description="Display template is missing")

    refresh_interval = 30 if api_is_down else 5
    server_url = f"http://{get_local_ip()}:{SERVER_PORT}"

    return render_template(
        template_path,
        birds=bird_data,
        refresh_interval=refresh_interval,
        api_is_down=api_is_down,
        server_url=server_url,
        csrf_token=get_csrf_token(),
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
        print(f"[ERROR] /temp: {e}")
        return _json_server_error("Could not read temperature")


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
    db_path = _get_db_path()

    if not os.path.exists(db_path):
        return jsonify({
            "ok": False,
            "error": "Detections database not found",
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
            "error": "Could not load Bird Day Index data",
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
    db_path = _get_db_path()

    if not os.path.exists(db_path):
        return jsonify({
            "ok": False,
            "error": "Detections database not found",
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
            "error": "Could not load Bird Day Index data",
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
    db_path = _get_db_path()
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
        print(f"[ERROR] /api/first_detections_today: {e}")
        return jsonify({"ok": False, "error": "Could not load first detections", "species": []}), 500


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
    db_path = _get_db_path()
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
        return _json_server_error("Could not load today's best clip")

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
                    cached = get_display_image(com_name)
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
        return _json_server_error("Could not load today's best clip")

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
@admin_required
@csrf_required
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
        data, payload_error = get_json_payload_or_error()
        if payload_error:
            return payload_error

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

        folder_name = _safe_species_folder_name(folder_name)
        folder_path = Path(_safe_species_folder(folder_name))
        already_exists = folder_path.is_dir()

        if not already_exists:
            folder_path.mkdir(parents=True, exist_ok=True)
            # Keep our in-memory folder set in sync
            EXISTING_BIRD_IMAGE_FOLDERS.add(folder_name)

        return jsonify(
            {
                "ok": True,
                "folder_name": folder_name,
                "folder_path": str(folder_path),
                "already_exists": already_exists,
                "error": None,
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating image folder: {e}")
        return _json_server_error("Could not create image folder")
        
        
@app.route("/upload_bird_images", methods=["POST"])
@admin_required
@csrf_required
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

        folder_name = _safe_species_folder_name(folder_name)
        folder_path = Path(_safe_species_folder(folder_name))
        folder_path.mkdir(parents=True, exist_ok=True)
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

        saved_files = []
        upload_errors = []

        for f in files:
            if not f or f.filename == "":
                upload_errors.append("Missing file")
                continue

            try:
                image_info = validate_uploaded_image(f)
                filename = generate_safe_image_filename(image_info["format"], folder_path)
                dest_path = safe_join_under_base(folder_path, filename)
                with open(dest_path, "xb") as out:
                    f.save(out)
                saved_files.append(filename)
            except ValueError as e:
                safe_original = secure_filename(f.filename or "") or "unnamed"
                message = str(e) or "Invalid image upload"
                print(f"Rejected uploaded image '{safe_original}': {message}")
                upload_errors.append(message)
                continue
            except FileExistsError:
                safe_original = secure_filename(f.filename or "") or "unnamed"
                print(f"Generated upload filename collision for '{safe_original}'")
                upload_errors.append("Could not generate a safe image filename")
                continue

        if not saved_files:
            error_message = upload_errors[0] if upload_errors else "No valid image files were uploaded"
            return jsonify(
                {
                    "ok": False,
                    "folder_name": folder_name,
                    "saved_files": [],
                    "error": error_message,
                }
            ), 400

        return jsonify(
            {
                "ok": True,
                "folder_name": folder_name,
                "folder_path": str(folder_path),
                "saved_files": saved_files,
                "error": None,
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error uploading bird images: {e}")
        return _json_server_error("Could not upload images")



@app.route("/shutdown", methods=["POST"])
@admin_required
@csrf_required
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

FAN_STATE_FILE = PATHS.fan_state_file
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


VALID_FAN_MODES = {"Off", "Auto", "On"}
BRIGHTNESS_MIN = 0
BRIGHTNESS_MAX = 255


def validate_fan_mode(value):
    if not isinstance(value, str) or not value:
        return None, "Missing or invalid fan mode. Expected Off, Auto, or On."
    if value not in VALID_FAN_MODES:
        return None, "Invalid fan mode. Expected Off, Auto, or On."
    return value, None


def _parse_int_like(value, field_name):
    if isinstance(value, bool) or value is None:
        return None, f"Invalid {field_name}. Expected an integer value."
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        raw = value.strip()
        if raw and re.fullmatch(r"[+-]?\d+", raw):
            return int(raw), None
        return None, f"Invalid {field_name}. Expected an integer value."
    return None, f"Invalid {field_name}. Expected an integer value."


def validate_fan_speed(value):
    parsed, error = _parse_int_like(value, "fan speed")
    if error:
        return None, error
    return max(0, min(100, parsed)), None


def validate_brightness(value):
    parsed, error = _parse_int_like(value, "brightness")
    if error:
        return None, error
    if parsed < BRIGHTNESS_MIN or parsed > BRIGHTNESS_MAX:
        return None, f"Invalid brightness. Expected {BRIGHTNESS_MIN} to {BRIGHTNESS_MAX}."
    return parsed, None


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

        # If UI says Off but hardware is spinning -> kernel override -> Auto
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
    admin_error = _admin_access_error()
    if admin_error:
        return admin_error
    csrf_error = _csrf_error()
    if csrf_error:
        return csrf_error

    data, payload_error = get_json_payload_or_error()
    if payload_error:
        return payload_error

    if "mode" not in data:
        return _json_validation_error("Missing fan mode. Expected Off, Auto, or On.")
    mode, mode_error = validate_fan_mode(data.get("mode"))
    if mode_error:
        return _json_validation_error(mode_error)

    if "speed" in data:
        speed, speed_error = validate_fan_speed(data.get("speed"))
        if speed_error:
            return _json_validation_error(speed_error)
    else:
        speed, speed_error = validate_fan_speed(state.get("speed", 100))
        if speed_error:
            speed = 100

    new_state = {"mode": mode, "speed": speed}
    save_fan_state(new_state)
    apply_fan_state_to_hardware(mode, speed)

    return jsonify(new_state)


@app.route("/brightness", methods=["POST"])
@admin_required
@csrf_required
def set_brightness():
    data, payload_error = get_json_payload_or_error()
    if payload_error:
        return payload_error

    if "brightness" not in data:
        return _json_validation_error("Missing brightness. Expected 0 to 255.")
    val, brightness_error = validate_brightness(data.get("brightness"))
    if brightness_error:
        return _json_validation_error(brightness_error)

    try:
        cmd = f"echo {val} | sudo tee /sys/class/backlight/10-0045/brightness"
        print(f"Executing brightness command: {cmd}")
        os.system(cmd)
    except Exception as e:
        print(f"Error setting brightness: {e}")
        return jsonify({"status": "error", "message": "Failed to set brightness"}), 500

    return jsonify({"status": "success", "brightness": val})


@app.route("/reboot", methods=["POST"])
@admin_required
@csrf_required
def reboot_system():
    print("Executing reboot command...")
    os.system("sudo reboot")
    return jsonify({"status": "rebooting"})


@app.route("/poweroff", methods=["POST"])
@admin_required
@csrf_required
def poweroff_system():
    print("Executing power off command...")
    os.system("sudo poweroff")
    return jsonify({"status": "shutting down"})


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if "--build-cache" in sys.argv:
        from cache_builder import ensure_cache_is_built, resize_cached_images

        os.chdir(PATHS.display_home)
        log_path_configuration()
        print("--- Starting Offline Image Cache Builder ---")
        ensure_cache_is_built()
        resize_cached_images()
        print("--- Cache building process complete. ---")
        sys.exit(0)

    log_path_configuration()
    print(f"Starting Flask server on http://0.0.0.0:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT)
