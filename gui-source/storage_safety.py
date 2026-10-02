from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class LockUnavailable(RuntimeError):
    """Raised when an inter-process lock cannot be acquired safely."""


_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
_ATOMIC_REPLACE_RETRY_DELAYS = (0.005, 0.01, 0.02, 0.04, 0.08, 0.16, 0.32)


class InterProcessFileLock:
    """Small cross-process advisory lock whose ownership is released by the OS."""

    def __init__(
        self,
        path: Path,
        *,
        blocking: bool = False,
        timeout: float = 0.0,
    ) -> None:
        self.path = Path(path)
        self.blocking = blocking
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> "InterProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())

        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except (OSError, PermissionError) as exc:
                if not self.blocking or time.monotonic() >= deadline:
                    handle.close()
                    raise LockUnavailable(f"Lock is already held: {self.path}") from exc
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, traceback) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace a file atomically using a unique same-directory temporary file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_atomic_file(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: Any) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, content)


def _replace_atomic_file(source: Path, destination: Path) -> None:
    """Retry only transient Windows replace contention, preserving atomicity."""

    for attempt in range(len(_ATOMIC_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            is_transient_windows_contention = (
                os.name == "nt"
                and getattr(exc, "winerror", None)
                in _WINDOWS_TRANSIENT_REPLACE_ERRORS
            )
            if not is_transient_windows_contention or attempt == len(
                _ATOMIC_REPLACE_RETRY_DELAYS
            ):
                raise
            time.sleep(_ATOMIC_REPLACE_RETRY_DELAYS[attempt])
