import os
from dataclasses import dataclass
from pathlib import Path


def _resolve_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def _env_path(name: str, default: str | Path) -> Path:
    return _resolve_path(os.environ.get(name) or default)


@dataclass(frozen=True)
class PathConfig:
    display_home: Path
    static_dir: Path
    image_cache_dir: Path
    birdnet_pi_home: Path
    db_path: Path
    audio_dir: Path
    species_file: Path
    placeholder_dir: Path
    spectrogram_cache_dir: Path
    fan_state_file: Path


def load_path_config() -> PathConfig:
    display_home = _env_path("BIRDNET_DISPLAY_HOME", Path(__file__).resolve().parent)
    static_dir = _env_path("BIRDNET_DISPLAY_STATIC_DIR", display_home / "static")

    image_cache_default = static_dir / "bird_images_cache"
    image_cache_override = (
        os.environ.get("BIRDNET_IMAGE_CACHE_DIR")
        or os.environ.get("BIRDNET_IMAGE_DIR")
        or image_cache_default
    )
    image_cache_dir = _resolve_path(image_cache_override)

    birdnet_pi_home = _env_path("BIRDNET_PI_HOME", Path.home() / "BirdNET-Pi")
    db_path = _env_path("BIRDNET_DB_PATH", birdnet_pi_home / "scripts" / "birds.db")
    audio_dir = _env_path("BIRDNET_AUDIO_DIR", Path.home() / "BirdSongs" / "Extracted" / "By_Date")

    return PathConfig(
        display_home=display_home,
        static_dir=static_dir,
        image_cache_dir=image_cache_dir,
        birdnet_pi_home=birdnet_pi_home,
        db_path=db_path,
        audio_dir=audio_dir,
        species_file=display_home / "species_list.csv",
        placeholder_dir=image_cache_dir / "placeholders",
        spectrogram_cache_dir=static_dir / "spectrogram_cache",
        fan_state_file=display_home / "fan_state.json",
    )


PATHS = load_path_config()
