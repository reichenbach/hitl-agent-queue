"""Shared platform paths for the desktop board and tray."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def user_data_dir() -> Path:
    """Return the platform's conventional per-user application data directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HITL Queue"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / "HITL Queue"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "hitl-queue"
