"""Small append-only UTF-8 JSONL audit log."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage_safety import InterProcessFileLock, atomic_write_bytes

VALID_RESULTS = {"SESSION_RESUME", "APPROVED", "STARTED", "SUCCESS", "FAILED", "STOPPED", "CLOSED"}


class AuditLogger:
    def __init__(self, path: str | Path, session_id: str, executor: str = "product-core",
                 default_metadata: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.executor = executor
        self.default_metadata = dict(default_metadata or {})

    def append(self, action: str, command_or_operation: Any, result: str,
               working_directory: str | Path | None = None,
               files_changed: list[str] | None = None,
               metadata: dict[str, Any] | None = None, pid: int | None = None) -> dict[str, Any]:
        if result not in VALID_RESULTS:
            raise ValueError(f"Invalid audit result: {result}")
        combined_metadata = dict(self.default_metadata)
        combined_metadata.update(metadata or {})
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id, "executor": self.executor,
            "pid": os.getpid() if pid is None else pid, "action": action,
            "command_or_operation": command_or_operation,
            "working_directory": str(working_directory or Path.cwd()),
            "result": result, "files_changed": list(files_changed or []),
            "metadata": combined_metadata,
        }
        # Opening in append mode is the append-only guarantee; errors deliberately propagate.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with InterProcessFileLock(lock_path, blocking=True, timeout=30.0):
            existing = self.path.read_bytes() if self.path.exists() else b""
            if existing and not existing.endswith(b"\n"):
                raise ValueError(f"Refusing to append to a partial audit log: {self.path}")
            for line_number, raw_line in enumerate(existing.splitlines(), start=1):
                try:
                    json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"Refusing to append to a corrupt audit log at line {line_number}: {self.path}"
                    ) from exc
            encoded_entry = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            atomic_write_bytes(self.path, existing + encoded_entry)
        return entry


def read_tail(path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    item = Path(path)
    if not item.exists():
        return []
    lines = item.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-limit:] if line.strip()]
