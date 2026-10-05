"""Filesystem locations used by Tsuyaku (config, models, binaries, logs)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

APP_NAME = "Tsuyaku"

_ENV_HOME = "TSUYAKU_HOME"


def _base_override() -> Path | None:
    value = os.environ.get(_ENV_HOME)
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    base = _base_override()
    path = base / "config" if base else Path(user_config_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    base = _base_override()
    path = base / "data" if base else Path(user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    base = _base_override()
    path = base / "logs" if base else Path(user_log_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    path = data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bin_dir() -> Path:
    path = data_dir() / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exe_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def config_file() -> Path:
    return config_dir() / "config.toml"


def glossary_file() -> Path:
    return config_dir() / "glossary.json"
