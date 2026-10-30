"""Startup configuration. Never populate this module from HTTP request data.

ROOT_PATH is granted only by a local TOML file or the startup CLI. With no
explicit grant, filesystem access is disabled. No environment/.env root grant
is supported.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Sequence


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_DIR / "config.local.toml"


@dataclass(frozen=True, slots=True)
class Settings:
    DATA_DIR: Path = PROJECT_DIR / "data"
    ROOT_PATH: Path | None = None
    CLOUD_ENABLED: bool = False
    MODEL_PROVIDER: str = "mock"
    WRITE_ENABLED: bool = False
    MAX_FILES: int = 500

    def authorized_path(self, path: str | Path) -> Path:
        """Resolve a requested path and reject traversal and symlink escapes."""
        if self.ROOT_PATH is None:
            raise PermissionError("ROOT_PATH has not been authorized locally")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.ROOT_PATH / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.ROOT_PATH):
            raise PermissionError("Path is outside the authorized ROOT_PATH")
        return candidate


def _directory(value: object, base: Path, name: str, *, required: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty directory path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if required and not path.is_dir():
        raise ValueError(f"{name} must be an existing directory: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{name} is not a directory: {path}")
    return path


def load_settings(argv: Sequence[str] = ()) -> Settings:
    """Load once at process startup, using actual CLI args, never web input.

    CLI --root-path overrides the local file. File-relative paths are relative
    to the TOML file; CLI-relative paths are relative to the working directory.
    This function has no filesystem write side effects.
    """
    parser = argparse.ArgumentParser(description="FileMindDemo configuration")
    parser.add_argument("--config", type=Path, help="Local TOML configuration")
    parser.add_argument("--root-path", help="Locally authorize a filesystem root")
    args = parser.parse_args(argv)
    config_file = (args.config or DEFAULT_CONFIG_FILE).expanduser().resolve()
    values = {}
    if args.config is not None or config_file.exists():
        with config_file.open("rb") as source:
            values = tomllib.load(source)

    allowed = set(Settings.__dataclass_fields__)
    unknown = values.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    for name in ("CLOUD_ENABLED", "WRITE_ENABLED"):
        if name in values and type(values[name]) is not bool:
            raise ValueError(f"{name} must be a boolean")
    maximum = values.get("MAX_FILES", 500)
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("MAX_FILES must be a positive integer")
    provider = values.get("MODEL_PROVIDER", "mock")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("MODEL_PROVIDER must be a non-empty string")
    if not values.get("CLOUD_ENABLED", False) and provider != "mock":
        raise ValueError("Only MODEL_PROVIDER=mock is supported when cloud is disabled")
    if "DATA_DIR" in values:
        values["DATA_DIR"] = _directory(values["DATA_DIR"], config_file.parent, "DATA_DIR", required=False)
    if "ROOT_PATH" in values:
        values["ROOT_PATH"] = _directory(values["ROOT_PATH"], config_file.parent, "ROOT_PATH", required=True)
    if args.root_path is not None:
        values["ROOT_PATH"] = _directory(args.root_path, Path.cwd(), "ROOT_PATH", required=True)
    return Settings(**values)


# Immutable process snapshot. Web handlers must use this instance and must not
# call load_settings with query parameters, JSON bodies, or uploaded files.
settings = load_settings()
DATA_DIR = settings.DATA_DIR
ROOT_PATH = settings.ROOT_PATH
CLOUD_ENABLED = settings.CLOUD_ENABLED
MODEL_PROVIDER = settings.MODEL_PROVIDER
WRITE_ENABLED = settings.WRITE_ENABLED
MAX_FILES = settings.MAX_FILES
