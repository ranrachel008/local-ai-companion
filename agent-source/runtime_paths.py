"""User-writable runtime paths kept separate from packaged resources."""
from __future__ import annotations

import os
from pathlib import Path


APP_DATA_DIRNAME = "Ornith-Solution-Scout"


def user_data_root() -> Path:
    """Return the per-user runtime-data root without creating it."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / APP_DATA_DIRNAME


def runtime_path(*parts: str) -> Path:
    return user_data_root().joinpath(*parts)
