"""Temporary JSON-lines trace for the Normal UI install handoff."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_paths import runtime_path

DEBUG_LOG = runtime_path("logs", "install_handoff.jsonl")
_LOCK = threading.Lock()


def trace(event: str, **fields: Any) -> None:
    """Append diagnostics without changing the application path if logging fails."""
    try:
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str, sort_keys=True)
        with _LOCK:
            DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DEBUG_LOG.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        pass
