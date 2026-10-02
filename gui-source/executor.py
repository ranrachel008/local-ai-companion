"""Atomic executor for explicitly approved Product Core plan steps."""
from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement

from audit import AuditLogger
from editable_project_install import (EditableSourceError, documented_project_installs,
                                      verified_editable_source, verified_project_source,
                                      verify_editable_command_evidence, verify_project_command_evidence)

STEP_TYPES = {"DOWNLOAD", "VERIFY_FILE", "COPY_FILE", "CREATE_DIRECTORY", "INSTALL_SOURCE",
              "VERIFY_PACKAGE", "CHECK_API", "CHECK_PORT", "CHECK_MODEL", "USER_ACTION_REQUIRED",
              "ACQUIRE_GITHUB_SNAPSHOT", "CREATE_PYTHON_ENV", "INSTALL_PYTHON_PACKAGES",
              "INSTALL_PYTORCH_RUNTIME", "INSTALL_LOCAL_EDITABLE_PROJECT", "INSTALL_LOCAL_PYTHON_PROJECT", "DOWNLOAD_MODEL_ASSET",
              "RUN_FIXED_VERIFICATION_COMMAND", "REVIEW_DOCUMENTED_ACTION"}
STEP_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILED", "STOPPED", "WAITING_FOR_USER"}
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_SPECIFIER = re.compile(r"(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*(?:,(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*)*")
_UNSAFE_PYTHON_PACKAGES = {"python", "torch", "torchvision", "torchaudio", "pytorch", "flashinfer-python"}
EXTRACTED_TREE_MANIFEST_SCHEMA = "ornith.extracted-tree-manifest"
EXTRACTED_TREE_MANIFEST_VERSION = 1
INTEGRITY_STATES = {"VERIFIED", "MODIFIED", "MISSING", "UNVERIFIABLE"}
# Solution Scout safety-policy ceiling for assets without publisher-documented size.
# This is an operational bound, not publisher integrity evidence.
MAX_MODEL_ASSET_BYTES = 20 * 1024 * 1024 * 1024
MAX_MODEL_ASSET_REDIRECTS = 5


class StopExecution(RuntimeError):
    pass


def _validate_model_asset_network_target(url: str) -> None:
    parsed = urlparse(url)
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment):
        raise StopExecution("Model assets require an exact HTTPS URL")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                                       type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise StopExecution(f"Model asset hostname could not be resolved safely: {exc}") from exc
    if not addresses:
        raise StopExecution("Model asset hostname did not resolve to an address")
    for entry in addresses:
        raw_address = entry[4][0]
        try:
            address = ipaddress.ip_address(raw_address.split("%", 1)[0])
        except ValueError as exc:
            raise StopExecution("Model asset hostname resolved to an invalid address") from exc
        if not address.is_global:
            raise StopExecution("Model asset network target is local, private, or reserved")


class _ModelAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        redirect_count = int(getattr(req, "_ornith_redirect_count", 0))
        if redirect_count >= MAX_MODEL_ASSET_REDIRECTS:
            raise StopExecution("Model asset redirect limit exceeded")
        _validate_model_asset_network_target(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            raise StopExecution("Model asset redirect could not be handled safely")
        setattr(redirected, "_ornith_redirect_count", redirect_count + 1)
        return redirected


def _open_model_asset_url(request: urllib.request.Request, timeout: float) -> Any:
    opener = urllib.request.build_opener(_ModelAssetRedirectHandler())
    return opener.open(request, timeout=timeout)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_manifest_bytes(files: list[dict[str, Any]]) -> bytes:
    payload = {
        "schema": EXTRACTED_TREE_MANIFEST_SCHEMA,
        "version": EXTRACTED_TREE_MANIFEST_VERSION,
        "files": files,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _scan_extracted_tree(root: Path) -> list[dict[str, Any]]:
    """Return a destination-independent manifest without following links."""
    root = Path(root)
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError as error:
        raise StopExecution("Extracted source root is missing") from error
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise StopExecution("Extracted source root is not a regular directory")

    files: list[dict[str, Any]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            entry_stat = entry.lstat()
            mode = entry_stat.st_mode
            if stat.S_ISLNK(mode):
                raise StopExecution("Extracted source tree contains a symlink")
            if stat.S_ISDIR(mode):
                pending.append(entry)
                continue
            if not stat.S_ISREG(mode):
                raise StopExecution("Extracted source tree contains a special file")
            relative = PurePosixPath(*entry.relative_to(root).parts).as_posix()
            files.append({"path": relative, "size": entry_stat.st_size,
                          "sha256": _sha256(entry)})
    files.sort(key=lambda item: item["path"])
    return files


def build_extracted_tree_manifest(root: Path, repository: str, exact_commit: str,
                                  archive_sha256: str) -> dict[str, Any]:
    """Build acquisition-time evidence for the exact extracted regular-file tree."""
    files = _scan_extracted_tree(Path(root))
    digest = hashlib.sha256(_canonical_manifest_bytes(files)).hexdigest()
    return {
        "schema": EXTRACTED_TREE_MANIFEST_SCHEMA,
        "version": EXTRACTED_TREE_MANIFEST_VERSION,
        "repository": repository,
        "exact_commit": exact_commit,
        "archive_sha256": archive_sha256,
        "file_count": len(files),
        "tree_sha256": digest,
        "files": files,
    }


def _valid_extracted_tree_manifest(manifest: Any) -> bool:
    if not isinstance(manifest, dict):
        return False
    files = manifest.get("files")
    if (manifest.get("schema") != EXTRACTED_TREE_MANIFEST_SCHEMA
            or manifest.get("version") != EXTRACTED_TREE_MANIFEST_VERSION
            or not isinstance(files, list)
            or manifest.get("file_count") != len(files)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                                str(manifest.get("repository") or ""))
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("exact_commit") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("archive_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("tree_sha256") or ""))):
        return False
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            return False
        path = item.get("path")
        if (not isinstance(path, str) or not path or "\\" in path
                or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or PurePosixPath(path).as_posix() != path
                or not isinstance(item.get("size"), int) or item["size"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))):
            return False
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        return False
    expected_digest = hashlib.sha256(_canonical_manifest_bytes(files)).hexdigest()
    return expected_digest == manifest["tree_sha256"]


def verify_extracted_tree(root: Path, acquisition_evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Read-only comparison against acquisition-time tree evidence."""
    manifest = (acquisition_evidence or {}).get("extracted_tree_manifest")
    base = {"missing_files": [], "added_files": [], "modified_files": []}
    if manifest is None:
        return {"state": "UNVERIFIABLE", "reason": "MISSING_EXTRACTED_TREE_MANIFEST", **base}
    if not _valid_extracted_tree_manifest(manifest):
        return {"state": "UNVERIFIABLE", "reason": "INVALID_EXTRACTED_TREE_MANIFEST", **base}
    root = Path(root)
    if not root.exists():
        return {"state": "MISSING", "reason": "EXTRACTED_SOURCE_ROOT_MISSING", **base}
    try:
        current_files = _scan_extracted_tree(root)
    except StopExecution as error:
        return {"state": "MODIFIED", "reason": str(error), **base}
    except OSError as error:
        return {"state": "UNVERIFIABLE", "reason": f"TREE_READ_FAILED: {error}", **base}

    expected = {item["path"]: item for item in manifest["files"]}
    current = {item["path"]: item for item in current_files}
    missing = sorted(expected.keys() - current.keys())
    added = sorted(current.keys() - expected.keys())
    modified = sorted(path for path in expected.keys() & current.keys()
                      if expected[path]["size"] != current[path]["size"]
                      or expected[path]["sha256"] != current[path]["sha256"])
    current_digest = hashlib.sha256(_canonical_manifest_bytes(current_files)).hexdigest()
    if missing or added or modified or current_digest != manifest["tree_sha256"]:
        return {"state": "MODIFIED", "reason": "EXTRACTED_TREE_MISMATCH",
                "missing_files": missing, "added_files": added, "modified_files": modified,
                "expected_tree_sha256": manifest["tree_sha256"],
                "actual_tree_sha256": current_digest}
    return {"state": "VERIFIED", "reason": "EXACT_EXTRACTED_TREE_MATCH", **base,
            "tree_sha256": current_digest, "file_count": len(current_files)}


def _canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


class PlanExecutor:
    def __init__(self, plan: dict[str, Any], audit: AuditLogger | None = None,
                 handlers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
                 cancellation_requested: Callable[[], bool] | None = None,
                 job_context: Any = None, review_risk_accepted: bool = False,
                 conda_executable: str | None = None) -> None:
        self.plan = plan
        self.audit = audit
        self.handlers = dict(handlers or {})
        self.cancellation_requested = cancellation_requested or (lambda: False)
        self.job_context = job_context
        self.review_risk_accepted = review_risk_accepted
        self.conda_executable = conda_executable
        self.cancelled = False
        self.cancel_phase: str | None = None
        self.cancel_step: str | None = None

    def _log(self, step: dict[str, Any], result: str, metadata: dict[str, Any] | None = None) -> None:
        if self.audit:
            self.audit.append(step["step_id"], step["type"], result,
                              files_changed=step.get("files_changed", []), metadata=metadata)

    def _validate(self, step: dict[str, Any]) -> None:
        required = {"step_id", "type", "inputs", "expected_result", "approved", "timeout", "status"}
        missing = required - step.keys()
        if missing:
            raise StopExecution(f"Malformed step; missing: {sorted(missing)}")
        if step["type"] not in STEP_TYPES or step["status"] not in STEP_STATUSES:
            raise StopExecution("Unsupported step type or status")
        if step["approved"] is not True:
            raise StopExecution("Step is not approved")
        inputs = step["inputs"]
        if inputs.get("new_dependency"):
            raise StopExecution("New dependency requires a revised approved plan")
        if inputs.get("persistent_environment_modification") and not inputs.get("persistent_environment_approved"):
            raise StopExecution("Persistent environment modification is not approved")
        if inputs.get("source_url") and inputs["source_url"] not in {
                self.plan.get("source_url"), *[x.get("url") for x in self.plan.get("planned_downloads", [])]}:
            raise StopExecution("New or substituted source is forbidden")
        if step["type"] == "ACQUIRE_GITHUB_SNAPSHOT":
            self._validate_github_snapshot(inputs)
        if step["type"] in {"CREATE_PYTHON_ENV", "INSTALL_PYTHON_PACKAGES", "INSTALL_PYTORCH_RUNTIME",
                            "INSTALL_LOCAL_EDITABLE_PROJECT", "INSTALL_LOCAL_PYTHON_PROJECT",
                            "DOWNLOAD_MODEL_ASSET", "RUN_FIXED_VERIFICATION_COMMAND"}:
            if any(key in inputs for key in ("command", "argv", "shell")):
                raise StopExecution("Arbitrary command execution is forbidden")
            if step["type"] != "DOWNLOAD_MODEL_ASSET" and not inputs.get("environment_path"):
                raise StopExecution("An isolated environment is required")
        if step["type"] == "CREATE_PYTHON_ENV":
            environment = Path(str(inputs.get("environment_path") or ""))
            if (inputs.get("manager") != "conda" or not environment.is_absolute()
                    or not re.fullmatch(r"\d+\.\d+", str(inputs.get("python_version") or ""))):
                raise StopExecution("CREATE_PYTHON_ENV requires conda, an absolute isolated path, and an exact Python version")
            if environment.resolve() == Path(sys.prefix).resolve():
                raise StopExecution("The active or global Python environment cannot be modified")
        if step["type"] == "INSTALL_PYTHON_PACKAGES":
            self._validate_python_packages(inputs)
        if step["type"] == "INSTALL_PYTORCH_RUNTIME":
            self._validate_pytorch_runtime(inputs)
        if step["type"] == "INSTALL_LOCAL_EDITABLE_PROJECT":
            self._validate_local_editable_project(step)
        if step["type"] == "INSTALL_LOCAL_PYTHON_PROJECT":
            self._validate_local_python_project(step)
        if step["type"] == "DOWNLOAD_MODEL_ASSET":
            self._validate_model_asset(step)
        if step["type"] == "REVIEW_DOCUMENTED_ACTION":
            raise StopExecution("Documented action requires manual review and is not executable")

    def _validate_local_editable_project(self, step: dict[str, Any]) -> None:
        values = step["inputs"]
        if set(values) != {"environment_path", "python_executable", "repository", "exact_commit",
                           "source_tree_sha256"}:
            raise StopExecution("INSTALL_LOCAL_EDITABLE_PROJECT has an unsupported fixed schema")
        if self.plan.get("review_required") and not self.review_risk_accepted:
            raise StopExecution("USER_ACCEPTED_UNRESOLVED_RISK is required for reviewed editable install")
        evidence = step.get("evidence")
        if (not isinstance(evidence, list) or not any(
                isinstance(item, dict) and item.get("exact_text") == "pip install -e ."
                and item.get("evidence_type") == "verbatim_command" for item in evidence)):
            raise StopExecution("Editable install requires the exact documented upstream command")
        try:
            bound = verified_editable_source(self.job_context, self.plan.get("source_identity") or {},
                                              values["source_tree_sha256"])
            verify_editable_command_evidence(bound["source_path"], evidence)
        except (EditableSourceError, AttributeError, TypeError) as exc:
            raise StopExecution(str(exc)) from exc
        source = Path(bound["source_path"])
        environment = Path(str(values["environment_path"]))
        python = Path(str(values["python_executable"]))
        expected_environment = source.parent / ".ornith-envs" / f"{source.name}-{bound['exact_commit'][:12]}"
        expected_python = expected_environment / ("python.exe" if sys.platform == "win32" else "bin/python")
        if (not environment.is_absolute() or environment.resolve() != expected_environment.resolve()
                or environment.resolve() == Path(sys.prefix).resolve()
                or python.resolve() != expected_python.resolve()
                or str((self.plan.get("environment") or {}).get("path") or "") != str(environment)
                or Path(str(step.get("target") or "")).resolve() != source.resolve()
                or str(values["repository"]).casefold() != bound["repository"].casefold()
                or str(values["exact_commit"]).casefold() != bound["exact_commit"].casefold()):
            raise StopExecution("Editable install target differs from the verified job source or isolated environment")
        environment_steps = [candidate for candidate in self.plan.get("steps", [])
                             if candidate.get("type") == "CREATE_PYTHON_ENV"
                             and candidate.get("inputs", {}).get("environment_path") == str(environment)]
        if (len(environment_steps) != 1 or environment_steps[0].get("approved") is not True
                or environment_steps[0].get("status") != "SUCCESS"
                or self.plan["steps"].index(environment_steps[0]) >= self.plan["steps"].index(step)):
            raise StopExecution("The current job's approved isolated Python environment has not been created")

    def _validate_local_python_project(self, step: dict[str, Any]) -> None:
        values = step["inputs"]
        if set(values) != {"environment_path", "python_executable", "repository", "canonical_url",
                           "exact_commit", "source_path", "source_tree_sha256", "source_tree_manifest",
                           "author_command", "execution_strategy"}:
            raise StopExecution("INSTALL_LOCAL_PYTHON_PROJECT has an unsupported fixed schema")
        if self.plan.get("review_required") and not self.review_risk_accepted:
            raise StopExecution("USER_ACCEPTED_UNRESOLVED_RISK is required for reviewed project install")
        identity = self.plan.get("source_identity") or {}
        evidence = step.get("evidence")
        documented = documented_project_installs({"dependencies": [{"evidence": evidence}]}, identity)
        matching = [item for item in documented if item["author_command"] == values["author_command"]]
        if not matching:
            raise StopExecution("Project install requires the documented upstream repository command")
        try:
            bound = verified_project_source(self.job_context, identity, values["source_tree_sha256"])
            for item in matching:
                verify_project_command_evidence(bound["source_path"], item["author_command"],
                                                item["evidence"], identity)
        except (EditableSourceError, AttributeError, TypeError) as exc:
            raise StopExecution(str(exc)) from exc
        source = Path(bound["source_path"])
        environment = Path(str(values["environment_path"]))
        python = Path(str(values["python_executable"]))
        expected_environment = source.parent / ".ornith-envs" / f"{source.name}-{bound['exact_commit'][:12]}"
        expected_python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
        if (not environment.is_absolute() or not python.is_absolute()
                or not Path(str(values["source_path"])).is_absolute()
                or _resolved(environment) != _resolved(expected_environment)
                or _resolved(environment) == _resolved(Path(sys.prefix))
                or _resolved(python) != _resolved(expected_python)
                or _resolved(environment) != _resolved(Path(str((self.plan.get("environment") or {}).get("path") or "")))
                or _resolved(Path(str(values["source_path"]))) != _resolved(source)
                or _resolved(Path(str(step["target"]))) != _resolved(source)
                or str(values["repository"]).casefold() != bound["repository"].casefold()
                or str(values["canonical_url"]).rstrip("/").casefold() != bound["canonical_url"].rstrip("/").casefold()
                or values["exact_commit"] != bound["exact_commit"]
                or values["source_tree_manifest"] != bound["tree_manifest"]
                or values["execution_strategy"] != "VERIFIED_FIXED_SNAPSHOT"):
            raise StopExecution("Project install source identity, fingerprint, or isolated environment mismatch")
        environment_steps = [item for item in self.plan["steps"]
                             if item.get("type") == "CREATE_PYTHON_ENV"
                             and _resolved(Path(str(item.get("inputs", {}).get("environment_path") or ""))) == _resolved(environment)]
        if (len(environment_steps) != 1 or environment_steps[0].get("approved") is not True
                or environment_steps[0].get("status") != "SUCCESS"
                or self.plan["steps"].index(environment_steps[0]) >= self.plan["steps"].index(step)):
            raise StopExecution("The current job's approved isolated Python environment has not been created")

    def _validate_model_asset(self, step: dict[str, Any]) -> None:
        values = step["inputs"]
        allowed = {"url", "filename", "filename_source", "destination_category",
                   "approved_asset_root", "relative_destination", "destination",
                   "expected_hash", "expected_size_bytes", "expected_revision",
                   "expected_revision_kind",
                   "evidence_location"}
        if set(values) != allowed:
            raise StopExecution("DOWNLOAD_MODEL_ASSET has an unsupported or incomplete fixed schema")
        parsed = urlparse(str(values["url"]))
        if (parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.fragment):
            raise StopExecution("Model assets require an exact HTTPS URL")
        raw_name = parsed.path.rsplit("/", 1)[-1]
        derived_name = unquote(raw_name)
        filename = str(values["filename"])
        if (not filename or filename in {".", ".."} or "/" in filename or "\\" in filename
                or values["filename_source"] not in {"DOCUMENTED", "DERIVED_FROM_URL"}
                or (values["filename_source"] == "DERIVED_FROM_URL" and filename != derived_name)):
            raise StopExecution("Model asset filename is not safely evidenced")
        if values["destination_category"] != "PROJECT_MODEL_ASSET":
            raise StopExecution("Model asset destination category is not approved")

        root = Path(str(values["approved_asset_root"]))
        plan_root = Path(str((self.plan.get("source_identity") or {}).get("local_path") or ""))
        relative = PurePosixPath(str(values["relative_destination"]))
        if (not root.is_absolute() or not plan_root.is_absolute()
                or _resolved(root) != _resolved(plan_root)
                or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.name != filename):
            raise StopExecution("Model asset destination is outside the approved asset root")
        destination = root.joinpath(*relative.parts)
        persisted_destination = Path(str(values["destination"]))
        if (not persisted_destination.is_absolute()
                or _resolved(destination) != _resolved(persisted_destination)
                or _resolved(destination) != _resolved(Path(str(step.get("target") or "")))):
            raise StopExecution("Model asset destination does not match the approved plan")

        expected_hash = values["expected_hash"]
        if expected_hash != "UNKNOWN":
            if (not isinstance(expected_hash, dict)
                    or set(expected_hash) != {"algorithm", "value", "source"}
                    or expected_hash.get("algorithm") != "SHA-256"
                    or expected_hash.get("source") != "DOCUMENTED"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash.get("value") or ""))):
                raise StopExecution("Documented model asset hash is malformed")
        expected_revision = values["expected_revision"]
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise StopExecution("Model asset expected revision is invalid")
        expected_revision_kind = values["expected_revision_kind"]
        if expected_revision_kind not in {"immutable", "symbolic", "unknown"}:
            raise StopExecution("Model asset expected revision kind is invalid")
        has_immutable_identity = (
            expected_revision_kind == "immutable" and expected_revision != "UNKNOWN"
        )
        has_publisher_hash = expected_hash != "UNKNOWN"
        if not has_immutable_identity and not has_publisher_hash:
            raise StopExecution(
                "Model asset requires an immutable documented revision or "
                "publisher-documented SHA-256"
            )
        expected_size = values["expected_size_bytes"]
        if (expected_size != "UNKNOWN"
                and (isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0)):
            raise StopExecution("Documented model asset size is malformed")
        if not isinstance(values["evidence_location"], list) or not values["evidence_location"]:
            raise StopExecution("Model asset requires its original evidence location")
        if step.get("evidence") != values["evidence_location"]:
            raise StopExecution("Model asset evidence does not match the approved plan")
        timeout = step.get("timeout")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout)) or float(timeout) <= 0):
            raise StopExecution("Model asset download requires a finite positive network timeout")

    def _download_model_asset(self, step: dict[str, Any], timeout: float) -> dict[str, bool]:
        values = step["inputs"]
        destination = Path(values["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_hash = values["expected_hash"]
        expected_size = values["expected_size_bytes"]
        byte_limit = expected_size if expected_size != "UNKNOWN" else MAX_MODEL_ASSET_BYTES

        if destination.exists():
            if not destination.is_file() or expected_hash == "UNKNOWN":
                raise StopExecution("Model asset destination already exists; overwrite is forbidden")
            observed_size = destination.stat().st_size
            observed_hash = _sha256(destination)
            if (observed_hash != expected_hash["value"]
                    or (expected_size != "UNKNOWN" and observed_size != expected_size)):
                raise StopExecution("Existing model asset does not match documented integrity evidence")
            step["execution"] = {
                "downloaded": False,
                "existing_asset_verified": True,
                "expected_integrity": expected_hash,
                "observed_integrity": {"algorithm": "SHA-256", "value": observed_hash,
                                       "source": "OBSERVED_AFTER_DOWNLOAD",
                                       "publisher_authenticated": False},
                "observed_size_bytes": observed_size,
            }
            return {"completed": True}

        temporary = destination.with_name(f".{destination.name}.ornith-download.partial")
        if temporary.exists():
            raise StopExecution("A model asset partial file already exists; refusing to overwrite it")
        digest = hashlib.sha256()
        observed_size = 0
        try:
            _validate_model_asset_network_target(values["url"])
            request = urllib.request.Request(values["url"], method="GET")
            with _open_model_asset_url(request, timeout) as response, temporary.open("xb") as output:
                status = int(getattr(response, "status", 200) or 200)
                if status < 200 or status >= 300:
                    raise StopExecution(f"Model asset download returned HTTP {status}")
                final_url = response.geturl() if hasattr(response, "geturl") else values["url"]
                if final_url != values["url"]:
                    raise StopExecution("Model asset URL redirected or was substituted")
                content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except (TypeError, ValueError) as exc:
                        raise StopExecution("Model asset response has an invalid Content-Length") from exc
                    if declared_size < 0 or declared_size > byte_limit:
                        raise StopExecution("Model asset response exceeds the approved download byte limit")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    if observed_size + len(chunk) > byte_limit:
                        raise StopExecution("Model asset response exceeds the approved download byte limit")
                    output.write(chunk)
                    digest.update(chunk)
                    observed_size += len(chunk)
            observed_hash = digest.hexdigest()
            if expected_size != "UNKNOWN" and observed_size != expected_size:
                raise StopExecution("Model asset size does not match documented evidence")
            if expected_hash != "UNKNOWN" and observed_hash != expected_hash["value"]:
                raise StopExecution("Model asset SHA-256 does not match documented evidence")
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise StopExecution("Model asset destination appeared during download; overwrite is forbidden") from exc
            except OSError as exc:
                raise StopExecution(f"Atomic model asset finalization failed: {exc}") from exc
            temporary.unlink()
        except StopExecution:
            if temporary.exists():
                temporary.unlink()
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if temporary.exists():
                temporary.unlink()
            raise StopExecution(f"Model asset download failed; partial data removed: {exc}") from exc

        step["files_changed"] = [str(destination)]
        step["execution"] = {
            "downloaded": True,
            "expected_integrity": expected_hash,
            "expected_size_bytes": expected_size,
            "observed_integrity": {"algorithm": "SHA-256", "value": observed_hash,
                                   "source": "OBSERVED_AFTER_DOWNLOAD",
                                   "publisher_authenticated": False},
            "observed_size_bytes": observed_size,
        }
        return {"completed": True}

    def _validate_python_packages(self, values: dict[str, Any]) -> None:
        environment = Path(str(values.get("environment_path") or ""))
        target_environment = Path(str(values.get("target_environment") or ""))
        python = Path(str(values.get("python_executable") or ""))
        plan_environment = Path(str((self.plan.get("environment") or {}).get("path") or ""))
        expected_python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
        if (not environment.is_absolute() or not target_environment.is_absolute()
                or not python.is_absolute() or not plan_environment.is_absolute()):
            raise StopExecution("INSTALL_PYTHON_PACKAGES requires an absolute approved isolated environment")
        if _resolved(target_environment) != _resolved(environment):
            raise StopExecution("Package target environment does not match its persisted environment path")
        if _resolved(environment) != _resolved(plan_environment):
            raise StopExecution("Package target does not match the approved plan environment")
        if _resolved(python) != _resolved(expected_python):
            raise StopExecution("Package installer must use the approved environment Python")
        application_root = _resolved(Path(__file__).parent)
        resolved_environment = _resolved(environment)
        forbidden = {_resolved(Path(sys.prefix)), _resolved(Path(sys.base_prefix)),
                     _resolved(Path(sys.executable).parent)}
        if resolved_environment in forbidden or resolved_environment == application_root \
                or application_root in resolved_environment.parents:
            raise StopExecution("The active, global, or Local AI Companion Python environment cannot be modified")
        packages = values.get("packages")
        if not isinstance(packages, list) or not packages:
            raise StopExecution("No approved Python package entries were provided")
        seen: set[tuple[str, str, str]] = set()
        package_manifests: set[str] = set()
        for package in packages:
            if not isinstance(package, dict):
                raise StopExecution("Malformed approved Python package entry")
            name = str(package.get("name") or "")
            canonical = str(package.get("canonical_name") or "")
            extras = str(package.get("extras") or "")
            specifier = str(package.get("specifier") or "")
            marker = str(package.get("marker") or "")
            requirement = str(package.get("requirement") or "")
            identity = (canonical, extras, marker)
            if (not _PACKAGE_NAME.fullmatch(name) or canonical != _canonical_package_name(name)
                    or identity in seen or canonical in _UNSAFE_PYTHON_PACKAGES
                    or canonical.startswith("flashinfer") or canonical.endswith("-gpu")):
                raise StopExecution("Unsafe or duplicate Python package entry")
            if specifier and not _SPECIFIER.fullmatch(specifier):
                raise StopExecution("Unsupported Python package version specifier")
            if (package.get("scope") != "CORE" or package.get("executable") is not True
                    or package.get("mandatory") is not True or package.get("optional") is not False):
                raise StopExecution("Only mandatory CORE dependencies may be installed automatically")
            reconstructed = name + extras + specifier + (f"; {marker}" if marker else "")
            try:
                parsed = Requirement(requirement)
                approved = Requirement(reconstructed)
            except InvalidRequirement as exc:
                raise StopExecution("Python package entry has an invalid PEP 508 requirement") from exc
            if parsed.url is not None or approved.url is not None:
                raise StopExecution("Python package sources must remain approved package indexes")
            if (_canonical_package_name(parsed.name) != canonical
                    or _canonical_package_name(approved.name) != canonical
                    or parsed.extras != approved.extras
                    or parsed.specifier != approved.specifier
                    or str(parsed.marker or "") != str(approved.marker or "")):
                raise StopExecution("Python package entry does not match its approved requirement")
            if not isinstance(package.get("source_manifests"), list) \
                    or not isinstance(package.get("evidence"), list) or not package["evidence"]:
                raise StopExecution("Python package entry is missing persisted evidence")
            package_manifests.update(str(path) for path in package["source_manifests"])
            seen.add(identity)
        source_manifests = values.get("source_manifests")
        if not isinstance(source_manifests, list) or set(map(str, source_manifests)) != package_manifests:
            raise StopExecution("Python package source manifests do not match approved package evidence")

    def _validate_pytorch_runtime(self, values: dict[str, Any]) -> None:
        allowed = {"target_environment", "environment_path", "python_executable", "torch_spec",
                   "torchvision_spec", "torchaudio_spec", "cuda_runtime", "package_index", "source",
                   "evidence", "original_documented_command", "normalized_argv", "compatibility_preflight"}
        if set(values) != allowed:
            raise StopExecution("PyTorch runtime inputs do not match the fixed schema")
        environment = Path(str(values["environment_path"])); target = Path(str(values["target_environment"]))
        python = Path(str(values["python_executable"])); plan_environment = Path(str((self.plan.get("environment") or {}).get("path") or ""))
        expected_python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
        if not all(path.is_absolute() for path in (environment, target, python, plan_environment)) \
                or _resolved(environment) != _resolved(target) or _resolved(environment) != _resolved(plan_environment) \
                or _resolved(python) != _resolved(expected_python):
            raise StopExecution("PyTorch runtime must target the approved isolated environment Python")
        application_root = _resolved(Path(__file__).parent); resolved_environment = _resolved(environment)
        if resolved_environment in {_resolved(Path(sys.prefix)), _resolved(Path(sys.base_prefix)), _resolved(Path(sys.executable).parent)} \
                or resolved_environment == application_root or application_root in resolved_environment.parents:
            raise StopExecution("The active, global, or Local AI Companion Python environment cannot be modified")
        specs = [values["torch_spec"], values["torchvision_spec"]]
        if values["torchaudio_spec"] is not None:
            specs.append(values["torchaudio_spec"])
        matches = [re.fullmatch(r"(torch|torchvision|torchaudio)==[A-Za-z0-9][A-Za-z0-9.+_-]*", str(spec)) for spec in specs]
        if any(match is None for match in matches) or [match.group(1) for match in matches if match][:2] != ["torch", "torchvision"] \
                or len({match.group(1) for match in matches if match}) != len(matches):
            raise StopExecution("PyTorch runtime package set or exact versions are invalid")
        index = str(values["package_index"]); index_match = re.fullmatch(r"https://download\.pytorch\.org/whl/(cu\d{3})", index)
        runtime = values["cuda_runtime"]
        if not isinstance(runtime, dict) or set(runtime) != {"version", "wheel_tag", "distribution", "system_cuda_toolkit_required", "nvcc_required"} \
                or not index_match or runtime["wheel_tag"] != index_match.group(1) \
                or runtime["wheel_tag"] != "cu" + str(runtime["version"]).replace(".", "") \
                or runtime["distribution"] != "pytorch_wheel" \
                or runtime["system_cuda_toolkit_required"] is not False or runtime["nvcc_required"] is not False:
            raise StopExecution("PyTorch CUDA runtime or official package index mismatch")
        expected_argv = [str(python), "-m", "pip", "install", *specs, "--index-url", index]
        if values["normalized_argv"] != expected_argv:
            raise StopExecution("PyTorch runtime argv differs from the fixed approved operation")
        try:
            documented = __import__("shlex").split(str(values["original_documented_command"]), posix=True)
        except ValueError as exc:
            raise StopExecution("Malformed documented PyTorch command") from exc
        if documented != ["pip", "install", *specs, "--index-url", index]:
            raise StopExecution("PyTorch runtime inputs do not exactly match documented evidence")
        if not isinstance(values["evidence"], list) or not values["evidence"]:
            raise StopExecution("PyTorch runtime is missing persisted evidence")
        preflight = values["compatibility_preflight"]
        if not isinstance(preflight, dict) or preflight.get("system_cuda_toolkit_required") is not False \
                or preflight.get("nvcc_checked") is not False:
            raise StopExecution("PyTorch compatibility preflight is missing or unsafe")

    def _validate_github_snapshot(self, values: dict[str, Any]) -> None:
        repository = str(values.get("repository") or "")
        commit = str(values.get("exact_commit") or "")
        canonical_url = str(values.get("canonical_url") or "").rstrip("/")
        parsed = urlparse(canonical_url)
        path_repository = parsed.path.strip("/")
        if path_repository.endswith(".git"):
            path_repository = path_repository[:-4]
        expected_archive = f"https://codeload.github.com/{repository}/zip/{commit}"
        if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or path_repository.lower() != repository.lower()
                or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or values.get("archive_url") != expected_archive):
            raise StopExecution("Verified GitHub repository, URL, commit, or archive URL mismatch")
        destination = values.get("destination")
        if not destination:
            raise StopExecution("Repository acquisition destination is missing")

    def execute_step(self, step: dict[str, Any]) -> dict[str, Any]:
        if step.get("status") == "SUCCESS":
            return step
        try:
            self._validate(step)
            if self.plan.get("status") not in ("READY_FOR_APPROVAL",):
                raise StopExecution("Plan is not executable")
            step["status"] = "RUNNING"
            audit_source = None
            if step["type"] == "ACQUIRE_GITHUB_SNAPSHOT":
                values = step["inputs"]
                audit_source = {key: values.get(key) for key in (
                    "repository", "canonical_url", "exact_commit", "archive_url", "destination", "provenance")}
            if step["type"] in {"INSTALL_LOCAL_EDITABLE_PROJECT", "INSTALL_LOCAL_PYTHON_PROJECT"}:
                values = step["inputs"]
                source = str(self.job_context.resolved_project_destination)
                audit_source = {"step_type": step["type"], "python_executable": values["python_executable"],
                                "source_path": source, "verified_repository": values["repository"],
                                "verified_commit": values["exact_commit"],
                                "argv": [values["python_executable"], "-m", "pip", "install"]
                                        + (["-e"] if step["type"] == "INSTALL_LOCAL_EDITABLE_PROJECT" else []) + [source]}
            self._log(step, "STARTED", audit_source)
            if step["type"] == "USER_ACTION_REQUIRED":
                step["status"] = "WAITING_FOR_USER"
                self._log(step, "STOPPED", {"reason": "user action required"})
                return step
            handler = self._builtin if step["type"] in {"INSTALL_LOCAL_EDITABLE_PROJECT", "INSTALL_LOCAL_PYTHON_PROJECT"} else self.handlers.get(step["type"], self._builtin)
            outcome = handler(step)
            if outcome != step["expected_result"]:
                raise StopExecution(f"Result mismatch: expected {step['expected_result']!r}, got {outcome!r}")
            step["status"] = "SUCCESS"
            step["result"] = outcome
            success_metadata = dict(audit_source or {})
            if step.get("acquisition_evidence"):
                success_metadata["acquisition_evidence"] = step["acquisition_evidence"]
            if step.get("execution"):
                success_metadata["execution"] = step["execution"]
            self._log(step, "SUCCESS", success_metadata or None)
        except Exception as exc:
            step["status"] = "STOPPED"
            step["reason"] = str(exc)
            metadata = {"reason": str(exc)}
            if step.get("execution"):
                metadata["execution"] = step["execution"]
            self._log(step, "STOPPED", metadata)
        return step

    def _builtin(self, step: dict[str, Any]) -> Any:
        kind, values = step["type"], step["inputs"]
        timeout = float(step["timeout"])
        if kind == "CREATE_DIRECTORY":
            path = Path(values["path"])
            path.mkdir(parents=values.get("parents", True), exist_ok=values.get("exist_ok", True))
            step["files_changed"] = [str(path)]
            return {"exists": path.is_dir()}
        if kind == "COPY_FILE":
            source, destination = Path(values["source"]), Path(values["destination"])
            if not source.is_file():
                raise StopExecution("Copy source is missing")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            step["files_changed"] = [str(destination)]
            return {"sha256": _sha256(destination), "size": destination.stat().st_size}
        if kind == "VERIFY_FILE":
            path = Path(values["path"])
            return {"exists": path.is_file(), "sha256": _sha256(path) if path.is_file() else None,
                    "size": path.stat().st_size if path.is_file() else None}
        if kind == "DOWNLOAD":
            destination = Path(values["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".partial")
            try:
                with urllib.request.urlopen(values["url"], timeout=timeout) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if temporary.exists():
                    temporary.unlink()
                raise StopExecution(f"Approved download failed; no substitution: {exc}") from exc
            temporary.replace(destination)
            step["files_changed"] = [str(destination)]
            return {"sha256": _sha256(destination), "size": destination.stat().st_size}
        if kind == "DOWNLOAD_MODEL_ASSET":
            return self._download_model_asset(step, timeout)
        if kind == "ACQUIRE_GITHUB_SNAPSHOT":
            return self._acquire_github_snapshot(step, timeout)
        if kind == "CREATE_PYTHON_ENV":
            environment = Path(values["environment_path"])
            if environment.exists():
                raise StopExecution("Python environment target already exists; nothing was overwritten")
            if not environment.parent.is_dir():
                if not environment.parent.parent.is_dir():
                    raise StopExecution("Python environment parent directory is missing")
                try:
                    environment.parent.mkdir()
                except OSError as exc:
                    raise StopExecution(f"Python environment parent directory could not be created: {exc}") from exc
            conda = self.conda_executable or shutil.which("conda")
            if not conda:
                raise StopExecution("The approved conda environment manager is unavailable")
            argv = [conda, "create", "--yes", "--prefix", str(environment),
                    f"python={values['python_version']}"]
            execution = {"argv": argv, "shell": False, "exit_code": None}
            step["execution"] = execution
            try:
                completed = subprocess.run(argv, shell=False, check=False, capture_output=True,
                                           text=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as exc:
                execution.update({"stderr": str(exc), "verification_result": {"status": "FAILED"}})
                raise StopExecution(f"Isolated Python environment creation failed: {exc}") from exc
            execution.update({"exit_code": completed.returncode, "stdout": completed.stdout or "",
                              "stderr": completed.stderr or ""})
            if completed.returncode != 0:
                execution["verification_result"] = {"status": "FAILED"}
                detail = (completed.stderr or completed.stdout or "unknown conda failure").strip()
                raise StopExecution(f"Isolated Python environment creation failed: {detail}")
            python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
            if not environment.is_dir() or not python.is_file():
                execution["verification_result"] = {"status": "FAILED"}
                raise StopExecution("Conda reported success but the isolated Python environment could not be verified")
            execution["verification_result"] = {"status": "VERIFIED"}
            step["files_changed"] = [str(environment)]
            step["rollback"] = f"Remove only the isolated environment directory: {environment}"
            return {"completed": True}
        if kind in {"INSTALL_LOCAL_EDITABLE_PROJECT", "INSTALL_LOCAL_PYTHON_PROJECT"}:
            source = Path(str(self.job_context.resolved_project_destination))
            environment = Path(values["environment_path"])
            python = Path(values["python_executable"])
            if not environment.is_dir() or not python.is_file():
                raise StopExecution("The approved isolated Python environment is missing")
            argv = [str(python), "-m", "pip", "install"] + (["-e"] if kind == "INSTALL_LOCAL_EDITABLE_PROJECT" else []) + [str(source)]
            execution = {"step_type": kind, "python_executable": str(python), "source_path": str(source),
                         "verified_repository": values["repository"], "verified_commit": values["exact_commit"],
                         "argv": argv, "shell": False, "exit_code": None}
            step["execution"] = execution
            try:
                completed = subprocess.run(argv, cwd=source, shell=False, check=False,
                                           capture_output=True, text=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as exc:
                execution.update({"stderr": str(exc), "verification_result": {"status": "FAILED"}})
                raise StopExecution(f"Approved local project installation failed: {exc}") from exc
            execution.update({"exit_code": completed.returncode, "stdout": completed.stdout or "",
                              "stderr": completed.stderr or "", "verification_result": {
                                  "status": "VERIFIED" if completed.returncode == 0 else "FAILED",
                                  "method": "pip_exit_code", "exit_code": completed.returncode}})
            if completed.returncode != 0:
                raise StopExecution(f"Approved local project installation failed with exit code {completed.returncode}")
            step["files_changed"] = [str(environment)]
            return {"completed": True}
        if kind == "INSTALL_PYTHON_PACKAGES":
            environment = Path(values["environment_path"])
            python = Path(values["python_executable"])
            if not environment.is_dir() or not python.is_file():
                raise StopExecution("The approved isolated Python environment is missing")
            packages = values["packages"]
            requirements = [str(package["requirement"]) for package in packages]
            argv = [str(python), "-m", "pip", "install", *requirements]
            execution = {
                "argv": argv, "shell": False, "target_environment": str(environment),
                "target_python": str(python), "requested_packages": packages,
                "evidence": step.get("evidence") or [],
            }
            step["execution"] = execution
            try:
                completed = subprocess.run(argv, shell=False, check=False, capture_output=True,
                                           text=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as exc:
                execution.update({"exit_code": None, "stdout": "", "stderr": str(exc),
                                  "installed_packages": [],
                                  "verification_result": {"status": "FAILED", "method": "pip_exit_code"},
                                  "changed_state": "UNKNOWN"})
                raise StopExecution(f"Approved isolated Python package installation failed: {exc}") from exc
            execution.update({"exit_code": completed.returncode, "stdout": completed.stdout or "",
                              "stderr": completed.stderr or "", "installed_packages": (
                                  packages if completed.returncode == 0 else []),
                              "verification_result": {"status": (
                                  "VERIFIED" if completed.returncode == 0 else "FAILED"),
                                  "method": "pip_exit_code", "exit_code": completed.returncode},
                              "changed_state": ("PACKAGES_INSTALLED" if completed.returncode == 0
                                                else "UNKNOWN")})
            if completed.returncode != 0:
                raise StopExecution("Approved isolated Python package installation failed with exit code "
                                    f"{completed.returncode}")
            step["files_changed"] = [str(environment)]
            step["rollback"] = f"Remove only the isolated environment directory: {environment}"
            return {"completed": True}
        if kind == "INSTALL_PYTORCH_RUNTIME":
            environment = Path(values["environment_path"]); python = Path(values["python_executable"])
            if not environment.is_dir() or not python.is_file():
                raise StopExecution("The approved isolated Python environment is missing")
            argv = list(values["normalized_argv"])
            execution = {"argv": argv, "shell": False, "target_environment": str(environment),
                         "target_python": str(python), "runtime": values["cuda_runtime"],
                         "source": values["source"], "evidence": values["evidence"]}
            step["execution"] = execution
            try:
                completed = subprocess.run(argv, shell=False, check=False, capture_output=True,
                                           text=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as exc:
                execution.update({"exit_code": None, "stdout": "", "stderr": str(exc),
                                  "verification_result": {"status": "FAILED", "method": "pip_exit_code"},
                                  "changed_state": "UNKNOWN"})
                raise StopExecution(f"Approved PyTorch runtime installation failed: {exc}") from exc
            execution.update({"exit_code": completed.returncode, "stdout": completed.stdout or "",
                              "stderr": completed.stderr or "", "verification_result": {
                                  "status": "VERIFIED" if completed.returncode == 0 else "FAILED",
                                  "method": "pip_exit_code", "exit_code": completed.returncode},
                              "changed_state": "RUNTIME_INSTALLED" if completed.returncode == 0 else "UNKNOWN"})
            if completed.returncode != 0:
                raise StopExecution("Approved PyTorch runtime installation failed with exit code "
                                    f"{completed.returncode}")
            step["files_changed"] = [str(environment)]
            return {"completed": True}
        if kind == "VERIFY_PACKAGE":
            try:
                version = importlib.metadata.version(values["name"])
            except importlib.metadata.PackageNotFoundError:
                version = None
            return {"name": values["name"], "version": version}
        if kind == "CHECK_PORT":
            with socket.socket() as sock:
                sock.settimeout(timeout)
                listening = sock.connect_ex((values.get("host", "127.0.0.1"), int(values["port"]))) == 0
            return {"listening": listening}
        if kind == "CHECK_MODEL":
            path = Path(values["path"])
            return {"exists": path.exists(), "size": path.stat().st_size if path.is_file() else None}
        if kind == "CHECK_API":
            with urllib.request.urlopen(values["url"], timeout=timeout) as response:
                body = response.read()
                return {"status": response.status, "json": json.loads(body) if values.get("json") else None}
        if kind == "INSTALL_SOURCE":
            source, destination = Path(values["source"]), Path(values["destination"])
            if not source.exists() or values.get("command"):
                raise StopExecution("INSTALL_SOURCE accepts only an approved local source copy")
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=False)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            step["files_changed"] = [str(destination)]
            return {"installed": destination.exists()}
        raise StopExecution(f"No handler for {kind}")

    def _acquire_github_snapshot(self, step: dict[str, Any], timeout: float) -> dict[str, Any]:
        values = step["inputs"]
        destination = Path(values["destination"])
        if destination.exists():
            raise StopExecution("Repository acquisition destination already exists; nothing was overwritten")
        if not destination.parent.is_dir():
            raise StopExecution("Repository acquisition destination parent is missing")
        archive = destination.with_name(f".{destination.name}.ornith-snapshot.partial.zip")
        staging = destination.with_name(f".{destination.name}.ornith-snapshot.partial")
        if archive.exists() or staging.exists():
            raise StopExecution("Repository acquisition temporary path already exists")
        repository_name = values["repository"].split("/", 1)[1]
        expected_root = f"{repository_name}-{values['exact_commit']}"
        member_count = 0
        skipped_symlinks: list[str] = []
        skipped_archive_members: list[dict[str, str]] = []
        try:
            with urllib.request.urlopen(values["archive_url"], timeout=timeout) as response, archive.open("xb") as output:
                final_url = getattr(response, "geturl", lambda: values["archive_url"])()
                if final_url != values["archive_url"]:
                    raise StopExecution("GitHub snapshot download was redirected to an unapproved source")
                shutil.copyfileobj(response, output)
            archive_sha256 = _sha256(archive)
            staging.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    path = PurePosixPath(member.filename)
                    parts = path.parts
                    if (not parts or parts[0] != expected_root or path.is_absolute()
                            or ".." in parts or "\\" in member.filename):
                        raise StopExecution("GitHub snapshot archive contains an unexpected or unsafe path")
                    mode = member.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if stat.S_ISLNK(mode):
                        skipped_symlinks.append(member.filename)
                        skipped_archive_members.append({
                            "archive_member_path": member.filename,
                            "source_relative_path": PurePosixPath(*parts[1:]).as_posix(),
                            "type": "SYMLINK", "mode": oct(mode),
                            "reason_skipped": "SYMLINK_NOT_MATERIALIZED",
                        })
                        continue
                    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise StopExecution("GitHub snapshot archive contains an unsupported file type")
                    relative = Path(*parts[1:]) if len(parts) > 1 else Path()
                    if not relative.parts:
                        continue
                    target = staging / relative
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.open(member) as source, target.open("xb") as output:
                            shutil.copyfileobj(source, output)
                        member_count += 1
            if not member_count:
                raise StopExecution("GitHub snapshot archive contains no files")
            extracted_tree_manifest = build_extracted_tree_manifest(
                staging, values["repository"], values["exact_commit"], archive_sha256)
            if extracted_tree_manifest["file_count"] != member_count:
                raise StopExecution("Extracted snapshot file count does not match the archive")
            staging.replace(destination)
            archive.unlink()
            step["files_changed"] = [str(destination)]
            step["acquisition_evidence"] = {
                "archive_sha256": archive_sha256, "archive_root": expected_root,
                "extracted_file_count": member_count,
                "skipped_symlinks": skipped_symlinks,
                "skipped_archive_members": skipped_archive_members,
                "source_fidelity": {
                    "archive_tree_equivalent": not skipped_archive_members,
                    "scope": "EXTRACTED_TREE_ONLY" if skipped_archive_members else "COMPLETE_ARCHIVE_MEMBERS",
                },
                "extracted_tree_manifest": extracted_tree_manifest,
            }
        except Exception:
            if archive.exists():
                archive.unlink()
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return {
            "acquired": True, "repository": values["repository"],
            "canonical_url": values["canonical_url"], "exact_commit": values["exact_commit"],
            "destination": str(destination),
        }

    def run(self) -> dict[str, Any]:
        for step in self.plan.get("steps", []):
            if self.cancellation_requested():
                self.cancelled, self.cancel_phase, self.cancel_step = True, "BEFORE_STEP", step.get("step_id")
                break
            if step.get("status") == "SUCCESS" and not step.get("repeatable", False):
                continue
            self.execute_step(step)
            if self.cancellation_requested():
                self.cancelled, self.cancel_phase, self.cancel_step = True, "AFTER_STEP", step.get("step_id")
                break
            if step["status"] != "SUCCESS":
                break
        return self.plan
