"""Well-known directories (XDG based)."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_home() -> Path:
    override = os.environ.get("MYSYNC_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "mysync"


def conf_dir() -> Path:
    """Directory holding one TOML file per sync job."""
    return config_home() / "conf.d"


def global_config_file() -> Path:
    """Optional file with defaults applied to every job."""
    return config_home() / "mysync.toml"


def default_state_dir() -> Path:
    return config_home() / "db"


def log_dir() -> Path:
    override = os.environ.get("MYSYNC_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_STATE_HOME", ".local/state") / "mysync" / "logs"
