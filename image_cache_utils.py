from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from PIL import Image, UnidentifiedImageError


ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_IMAGE_FORMATS = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "GIF": {".gif"},
    "WEBP": {".webp"},
}
IMAGE_FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
}


@dataclass(frozen=True)
class ImageValidationResult:
    ok: bool
    path: Path
    reason: str = ""
    image_format: str = ""
    width: int = 0
    height: int = 0


def validate_cached_image(
    image_path: str | Path,
    cache_base: str | Path,
    *,
    require_format_extension_match: bool = True,
) -> ImageValidationResult:
    """Validate an existing cached image before exposing it to the UI."""
    base = Path(cache_base).resolve()
    path = Path(image_path)

    try:
        resolved = path.resolve()
    except OSError as exc:
        return ImageValidationResult(False, path, f"resolve-error:{exc}")

    if resolved != base and base not in resolved.parents:
        return ImageValidationResult(False, resolved, "outside-cache-dir")
    if not resolved.exists():
        return ImageValidationResult(False, resolved, "missing-file")
    if not resolved.is_file():
        return ImageValidationResult(False, resolved, "not-a-file")
    if not os.access(resolved, os.R_OK):
        return ImageValidationResult(False, resolved, "not-readable")

    ext = resolved.suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return ImageValidationResult(False, resolved, f"unsupported-extension:{ext or '<none>'}")

    try:
        with Image.open(resolved) as img:
            image_format = (img.format or "").upper()
            width, height = img.size
            img.verify()
    except (UnidentifiedImageError, OSError) as exc:
        return ImageValidationResult(False, resolved, f"invalid-image:{type(exc).__name__}:{exc}")

    if image_format not in ALLOWED_IMAGE_FORMATS:
        return ImageValidationResult(False, resolved, f"unsupported-format:{image_format or '<unknown>'}")
    if require_format_extension_match and ext not in ALLOWED_IMAGE_FORMATS[image_format]:
        return ImageValidationResult(False, resolved, f"extension-format-mismatch:{ext}:{image_format}")
    if width <= 0 or height <= 0:
        return ImageValidationResult(False, resolved, f"invalid-dimensions:{width}x{height}")

    return ImageValidationResult(True, resolved, "", image_format, width, height)


def is_allowed_image_filename(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in ALLOWED_IMAGE_EXTENSIONS
