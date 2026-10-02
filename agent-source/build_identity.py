"""Read the identity bundled with the currently running build."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def build_info_path() -> Path:
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return root / "BUILD_INFO.json"


def read_build_info() -> dict:
    path = build_info_path()
    if not path.is_file():
        if getattr(sys, "frozen", False):
            raise FileNotFoundError(f"Packaged build identity missing: {path}")
        return {"build_id": "dev", "build_timestamp_utc": "", "source_fingerprint": ""}
    return json.loads(path.read_text(encoding="utf-8"))


def short_build_id() -> str:
    build_id = str(read_build_info().get("build_id") or "unknown")
    return build_id[-8:] if build_id != "dev" else build_id
