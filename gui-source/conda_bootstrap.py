"""Fixed Windows Miniforge prerequisite for isolated Python environments."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from audit import AuditLogger


MISSING_CONDA = "PRECONDITION_MISSING_CONDA"
MINIFORGE_VERSION = "26.7.2-0"
MINIFORGE_FILENAME = f"Miniforge3-{MINIFORGE_VERSION}-Windows-x86_64.exe"
MINIFORGE_SOURCE = (
    f"https://github.com/conda-forge/miniforge/releases/download/{MINIFORGE_VERSION}/"
    f"{MINIFORGE_FILENAME}"
)
MINIFORGE_CHECKSUM_SOURCE = MINIFORGE_SOURCE + ".sha256"
PRIVATE_TOOLS_ROOT = Path(r"E:\AI-Apps") / ".ornith-tools"
PRIVATE_INSTALL_ROOT = PRIVATE_TOOLS_ROOT / "miniforge"
MAX_INSTALLER_BYTES = 500 * 1024 * 1024


class CondaBootstrapError(RuntimeError):
    pass


def _audit(audit: AuditLogger | None, action: str, result: str, **metadata: object) -> None:
    if audit:
        audit.append(action, action, result, metadata=metadata)


def detect_conda(audit: AuditLogger | None = None,
                 install_root: Path = PRIVATE_INSTALL_ROOT) -> dict[str, str | None]:
    """Prefer an existing PATH Conda, then the application-managed executable."""
    system = shutil.which("conda")
    private = install_root / "Scripts" / "conda.exe"
    system_candidates: list[Path] = []
    if system:
        located = Path(system)
        if located.suffix.lower() == ".exe":
            system_candidates.append(located)
        else:
            # A PATH shim may be a .bat file. Invoke only the native executable.
            system_candidates.extend((located.parent / "conda.exe",
                                      located.parent.parent / "Scripts" / "conda.exe"))
    candidates = [(path, "SYSTEM") for path in system_candidates]
    candidates.append((private, "APP_MANAGED"))
    for candidate, source in candidates:
        path = Path(candidate).resolve()
        if not path.is_file():
            continue
        try:
            completed = subprocess.run([str(path), "--version"], shell=False, check=False,
                                       capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        version = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode or not re.fullmatch(r"conda \d+\.\d+(?:\.\d+)?(?:[\w.+-]*)?", version):
            continue
        found = {"status": "AVAILABLE", "executable": str(path),
                 "version": version, "source": source}
        _audit(audit, "CONDA_DETECTION", "SUCCESS", conda_detected=True,
               chosen_conda_path=str(path), version=version, install_source=found["source"],
               checksum=None, install_result="NOT_REQUIRED")
        return found
    _audit(audit, "CONDA_DETECTION", "STOPPED", conda_detected=False,
           chosen_conda_path=None, version=None, install_source=None,
           checksum=None, install_result="NOT_STARTED")
    return {"status": MISSING_CONDA, "executable": None, "version": None, "source": None}


def _official_checksum() -> str:
    with urllib.request.urlopen(MINIFORGE_CHECKSUM_SOURCE, timeout=30) as response:
        content = response.read(256).decode("ascii")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?" + re.escape(MINIFORGE_FILENAME)
                         + r"\s*", content)
    if not match:
        raise CondaBootstrapError("Official Miniforge checksum metadata is malformed")
    return match.group(1).lower()


def install_miniforge(audit: AuditLogger | None = None,
                      install_root: Path = PRIVATE_INSTALL_ROOT) -> dict[str, str | None]:
    """Install a pinned, checksum-verified Miniforge without modifying global PATH."""
    existing = detect_conda(audit, install_root)
    if existing["status"] == "AVAILABLE":
        _audit(audit, "MINIFORGE_INSTALL", "SUCCESS", conda_detected=True,
               chosen_conda_path=existing["executable"], version=existing["version"],
               install_source="EXISTING_CONDA", checksum=None, install_result="SKIPPED_EXISTING")
        return existing
    installer = install_root.parent / (MINIFORGE_FILENAME + ".partial.exe")
    checksum: str | None = None
    created_installer = False
    try:
        if sys.platform != "win32":
            raise CondaBootstrapError("The fixed Miniforge installer supports Windows only")
        if not install_root.is_absolute():
            raise CondaBootstrapError("Managed Miniforge destination must be absolute")
        if install_root.exists():
            raise CondaBootstrapError("Managed Miniforge destination already exists; overwrite is forbidden")
        if installer.exists():
            raise CondaBootstrapError("A Miniforge installer download already exists; overwrite is forbidden")
        checksum = _official_checksum()
        install_root.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        count = 0
        with urllib.request.urlopen(MINIFORGE_SOURCE, timeout=60) as response, installer.open("xb") as output:
            created_installer = True
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_INSTALLER_BYTES:
                    raise CondaBootstrapError("Miniforge installer exceeds fixed size limit")
                output.write(chunk)
                digest.update(chunk)
        if not count or digest.hexdigest() != checksum:
            raise CondaBootstrapError("Miniforge installer SHA-256 mismatch")
        argv = [str(installer), "/InstallationType=JustMe", "/AddToPath=0",
                "/RegisterPython=0", "/S", f"/D={install_root}"]
        result = subprocess.run(argv, shell=False, check=False, capture_output=True,
                                text=True, timeout=900)
        if result.returncode:
            raise CondaBootstrapError(f"Miniforge installer exited with code {result.returncode}")
        found = detect_conda(audit, install_root)
        if found["status"] != "AVAILABLE" or Path(str(found["executable"])).resolve() != \
                (install_root / "Scripts" / "conda.exe").resolve():
            raise CondaBootstrapError("Installed Miniforge executable could not be verified")
        _audit(audit, "MINIFORGE_INSTALL", "SUCCESS", conda_detected=True,
               chosen_conda_path=found["executable"], version=found["version"],
               install_source=MINIFORGE_SOURCE, checksum=checksum, install_result="INSTALLED",
               installer_argv=argv)
        return found
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError,
            CondaBootstrapError) as exc:
        _audit(audit, "MINIFORGE_INSTALL", "FAILED", conda_detected=False,
               chosen_conda_path=None, version=MINIFORGE_VERSION,
               install_source=MINIFORGE_SOURCE, checksum=checksum,
               install_result="FAILED", reason=str(exc))
        raise CondaBootstrapError(str(exc)) from exc
    finally:
        if created_installer and installer.exists():
            installer.unlink()
