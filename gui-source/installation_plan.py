"""Deterministic, evidence-bound installation plan construction."""
from __future__ import annotations

import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

PLAN_FILE = "installation_plan.json"
EXECUTABLE_TYPES = {"CREATE_PYTHON_ENV", "INSTALL_PYTHON_PACKAGES", "INSTALL_PYTORCH_RUNTIME",
                    "DOWNLOAD_MODEL_ASSET", "RUN_FIXED_VERIFICATION_COMMAND", "INSTALL_LOCAL_EDITABLE_PROJECT",
                    "INSTALL_LOCAL_PYTHON_PROJECT"}
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_EXTRA_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_SPECIFIER = re.compile(r"(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*(?:,(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*)*")
_UNSAFE_PYTHON_PACKAGES = {"python", "torch", "torchvision", "torchaudio", "pytorch", "flashinfer-python"}
DEPENDENCY_SCOPES = ("CORE", "OPTIONAL", "DEMO", "BENCHMARK", "DEV_TEST", "UNKNOWN_SCOPE")
_PYTORCH_INDEX = re.compile(r"https://download\.pytorch\.org/whl/(cu\d{3})")
_PYTORCH_SPEC = re.compile(r"(torch|torchvision|torchaudio)==([A-Za-z0-9][A-Za-z0-9.+_-]*)")


def execution_step_status(step: dict[str, Any]) -> str:
    """Keep guard stops distinct from a command or verification failure."""
    status = str(step.get("status") or "PENDING").upper()
    execution = step.get("execution") or {}
    verification = execution.get("verification_result") or {}
    if status == "STOPPED" and (
        verification.get("status") == "FAILED"
        or execution.get("exit_code") not in (None, 0)
    ):
        return "FAILED"
    return status


def execution_status(state: dict[str, Any]) -> str:
    """Read new and legacy execution records, never acquisition results."""
    steps = state.get("steps") or []
    statuses = [execution_step_status(step) for step in steps]
    if "FAILED" in statuses:
        return "FAILED"
    if statuses and all(status == "SUCCESS" for status in statuses):
        return "VERIFIED"
    if state.get("stop_reason") or state.get("cancel_requested") or any(
        status in {"STOPPED", "WAITING_FOR_USER"} for status in statuses
    ):
        return "STOPPED"
    # Partial records must not inherit a stale final status from a previous attempt.
    if statuses:
        return "RUNNING" if all(status in {"PENDING", "RUNNING", "SUCCESS"}
                                for status in statuses) else ""
    status = str(state.get("status") or "").upper()
    return status if status in {"NEEDS_APPROVAL", "STOPPED", "FAILED", "RUNNING",
                               "APPROVED", "WAITING_FOR_APPROVAL", "VERIFIED"} else ""


def _evidence(row: dict[str, Any]) -> list[Any]:
    value = row.get("evidence") or []
    return value if isinstance(value, list) else [value]


def _step(index: int, kind: str, action: str, target: str, inputs: dict[str, Any], evidence: list[Any]) -> dict[str, Any]:
    return {"step_id": f"install-{index:03d}", "type": kind, "action": action, "target": target,
            "inputs": inputs, "evidence": evidence, "expected_changes": [target],
            "rollback": "Remove only artifacts created by this step.", "restart_required": False,
            "approval_required": True, "approved": False, "timeout": 1800, "status": "PENDING",
            "expected_result": {"completed": True}}


def _model_asset_step(row: dict[str, Any], source: Path, index: int) -> tuple[dict[str, Any] | None, str]:
    """Map only fully evidenced assets to the fixed HTTPS download operation."""
    url = str(row.get("url") or "").strip()
    if not url or url.upper() == "UNKNOWN":
        name = str(row.get("filename") or row.get("name") or "").strip()
        if name and "/" not in name and "\\" not in name:
            evidenced_urls = {
                candidate.rstrip(".,);]")
                for item in _evidence(row) if isinstance(item, dict)
                for candidate in re.findall(r"https://[^\s<>\"']+", str(item.get("exact_text") or ""))
                if unquote(urlparse(candidate.rstrip(".,);]")).path.rsplit("/", 1)[-1]) == name
            }
            if len(evidenced_urls) == 1:
                url = evidenced_urls.pop()
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        return None, "Model asset lacks an exact supported HTTPS URL"

    raw_name = parsed.path.rsplit("/", 1)[-1]
    derived_name = unquote(raw_name)
    documented_name = str(row.get("filename") or "").strip()
    filename = documented_name or derived_name
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        return None, "Model asset filename is missing or cannot be derived safely"

    category = str(row.get("destination_category") or "").strip()
    relative_text = str(row.get("relative_destination") or row.get("destination_path") or "").strip()
    relative = PurePosixPath(relative_text.replace("\\", "/"))
    if category != "PROJECT_MODEL_ASSET" or not relative_text:
        return None, "Model asset lacks an approved project asset destination"
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None, "Model asset destination is unsafe"
    if relative.name != filename:
        return None, "Model asset destination filename does not match the evidenced asset"

    evidence = _evidence(row)
    if not evidence:
        return None, "Model asset lacks an original evidence location"

    sha256_text = str(row.get("sha256") or "").strip()
    sha256 = "" if sha256_text.upper() == "UNKNOWN" else sha256_text.lower()
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return None, "Model asset has malformed documented SHA-256 evidence"
    expected_hash: str | dict[str, str] = "UNKNOWN"
    if sha256:
        expected_hash = {"algorithm": "SHA-256", "value": sha256, "source": "DOCUMENTED"}

    size_value = row.get("size_bytes", row.get("expected_size_bytes"))
    if (
        size_value is None
        or size_value == ""
        or (isinstance(size_value, str) and size_value.strip().upper() == "UNKNOWN")
    ):
        expected_size: str | int = "UNKNOWN"
    elif isinstance(size_value, bool) or not isinstance(size_value, int) or size_value < 0:
        return None, "Model asset has malformed documented size evidence"
    else:
        expected_size = size_value

    revision = str(row.get("revision") or row.get("commit") or "").strip() or "UNKNOWN"
    revision_kind = str(row.get("revision_kind") or "unknown").strip().lower() or "unknown"
    if revision_kind not in {"immutable", "symbolic", "unknown"}:
        return None, "Model asset has unsupported revision-kind evidence"

    has_immutable_identity = revision_kind == "immutable" and revision != "UNKNOWN"
    has_publisher_hash = expected_hash != "UNKNOWN"
    if not has_immutable_identity and not has_publisher_hash:
        return None, (
            "Model asset lacks an immutable documented revision or "
            "publisher-documented SHA-256"
        )
    root = source.resolve()
    destination = root.joinpath(*relative.parts).resolve()
    try:
        destination.relative_to(root)
    except ValueError:
        return None, "Model asset destination escapes the approved asset root"

    inputs = {
        "url": url,
        "filename": filename,
        "filename_source": "DOCUMENTED" if documented_name else "DERIVED_FROM_URL",
        "destination_category": category,
        "approved_asset_root": str(root),
        "relative_destination": relative.as_posix(),
        "destination": str(destination),
        "expected_hash": expected_hash,
        "expected_size_bytes": expected_size,
        "expected_revision": revision,
        "expected_revision_kind": revision_kind,
        "evidence_location": evidence,
    }
    return _step(index, "DOWNLOAD_MODEL_ASSET", "Download documented model asset",
                 str(destination), inputs, evidence), ""


def _canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _package_specifier(value: Any) -> str | None:
    """Normalize the extras/version/marker suffix of a PEP 508 requirement."""
    text = str(value or "").strip()
    if not text or text.upper() == "UNKNOWN":
        return ""
    requirement, separator, marker = text.partition(";")
    if separator:
        marker = marker.strip()
        if not marker or "\n" in marker or "\r" in marker:
            return None
    extras = ""
    requirement = requirement.strip()
    if requirement.startswith("["):
        closing = requirement.find("]")
        if closing < 0:
            return None
        values = [item.strip() for item in requirement[1:closing].split(",")]
        if not values or any(not _EXTRA_NAME.fullmatch(item) for item in values):
            return None
        extras = "[" + ",".join(values) + "]"
        requirement = requirement[closing + 1:].strip()
    if re.fullmatch(r"[0-9][A-Za-z0-9.*+!_-]*", requirement):
        requirement = "==" + requirement
    requirement = re.sub(r"\s*(===|==|~=|!=|<=|>=|<|>)\s*", r"\1", requirement)
    requirement = re.sub(r"\s*,\s*", ",", requirement)
    if requirement and not _SPECIFIER.fullmatch(requirement):
        return None
    return extras + requirement + (f"; {marker}" if separator else "")


def _requirement_parts(name_value: Any, suffix_value: Any) -> dict[str, str] | None:
    """Parse a requirement whether its suffix is attached to or split from its name."""
    text = str(name_value or "").strip()
    match = re.fullmatch(rf"(?P<name>{_PACKAGE_NAME.pattern})(?P<suffix>.*)", text)
    if not match:
        return None
    inline_suffix = _package_specifier(match.group("suffix"))
    stored_suffix = _package_specifier(suffix_value)
    if inline_suffix is None or stored_suffix is None:
        return None
    if inline_suffix and stored_suffix and inline_suffix != stored_suffix:
        return None
    suffix = inline_suffix or stored_suffix
    requirement, separator, marker = suffix.partition(";")
    extras = ""
    if requirement.startswith("["):
        closing = requirement.index("]")
        extras = requirement[:closing + 1]
        requirement = requirement[closing + 1:]
    return {
        "name": match.group("name"),
        "extras": extras,
        "specifier": requirement,
        "marker": marker.strip() if separator else "",
        "suffix": suffix,
    }


def _evidence_keys(row: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    keys: set[tuple[str, str, str, str]] = set()
    for item in _evidence(row):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").casefold()
        exact = str(item.get("exact_text") or item.get("text") or "").strip()
        heading = str(item.get("heading") or "").strip().casefold()
        line = str(item.get("line") or item.get("line_start") or "")
        if path and (exact or heading or line):
            keys.add((path, exact, heading, line))
    return keys


def _is_evidenced_system_dependency(row: dict[str, Any], system_rows: list[Any]) -> bool:
    name = str(row.get("package") or row.get("name") or row.get("dependency") or "").strip()
    identity = re.sub(r"[-_.\s]+", "-", name).casefold()
    evidence = _evidence_keys(row)
    if not identity or not evidence:
        return False
    for system_row in system_rows:
        if not isinstance(system_row, dict):
            continue
        system_name = str(system_row.get("package") or system_row.get("name")
                          or system_row.get("dependency") or "").strip()
        system_identity = re.sub(r"[-_.\s]+", "-", system_name).casefold()
        if system_identity == identity and evidence & _evidence_keys(system_row):
            return True
    return False


def _compatible_exact_specifier(values: list[str]) -> str | None:
    """Prove compatibility only for an exact numeric version and simple numeric bounds."""
    clauses = [part for value in values for part in value.split(",") if part]
    parsed = [re.fullmatch(r"(==|!=|<=|>=|<|>)(\d+(?:\.\d+)*)", part) for part in clauses]
    if not clauses or any(item is None for item in parsed):
        return None
    exact = [item.group(2) for item in parsed if item and item.group(1) == "=="]
    if not exact:
        return None
    width = max(len(item.group(2).split(".")) for item in parsed if item)
    def version_tuple(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split(".")) + (0,) * (width - len(value.split(".")))
    target = version_tuple(exact[0])
    for item in parsed:
        assert item is not None
        other = version_tuple(item.group(2))
        if not {"==": target == other, "!=": target != other, "<": target < other,
                "<=": target <= other, ">": target > other, ">=": target >= other}[item.group(1)]:
            return None
    return "==" + exact[0]


def _build_extension_output(name: str, evidence: list[Any], commands: list[str]) -> bool:
    if not any(re.search(r"\bsetup\.py\s+build_ext\s+--inplace\b", command) for command in commands):
        return False
    return any(
        isinstance(item, dict) and re.search(
            rf"\bbuilds?\b[^\n]*`?{re.escape(name)}`?[^\n]*\bin place\b",
            str(item.get("exact_text") or ""), re.I)
        for item in evidence
    )


def _manifest_paths(research: dict[str, Any]) -> set[str]:
    rows = (research.get("diagnostics") or {}).get("deterministic_manifests") or []
    paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("path"):
            continue
        path = str(row["path"])
        name = Path(path).name.lower()
        if ((name.startswith("requirements") and name.endswith(".txt"))
                or name in {"pyproject.toml", "setup.cfg", "environment.yml", "environment.yaml"}):
            paths.add(path)
    return paths


def _primary_install_pip_evidence(name: str, evidence: list[Any]) -> list[dict[str, Any]]:
    """Return exact package-list evidence from a primary README install section."""
    canonical = _canonical_package_name(name)
    accepted: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/")
        heading = str(item.get("heading") or "").strip().lower()
        if not Path(path).name.lower().startswith("readme"):
            continue
        if not any(word in heading for word in ("install", "setup", "quick start", "getting started")):
            continue
        if any(word in heading for word in ("demo", "render", "benchmark", "test", "develop", "optional")):
            continue
        exclusive_os = re.search(r"\b(linux|windows|macos|mac|osx)\s+only\b", heading)
        if exclusive_os and exclusive_os.group(1) != {
            "Windows": "windows", "Linux": "linux", "Darwin": "macos",
        }.get(platform.system()):
            continue
        text = str(item.get("exact_text") or item.get("text") or "").strip()
        commands = re.findall(
            r"(?im)^(?:python\s+-m\s+)?pip\s+install\s+[^\r\n]+$", text,
        )
        if len(commands) != 1:
            continue
        arguments = re.sub(r"^(?:python\s+-m\s+)?pip\s+install\s+", "", commands[0],
                           flags=re.IGNORECASE).split()
        if (not arguments or any(arg.startswith("-") or any(mark in arg for mark in
                ("/", "\\", ":", "@", ";", "&", "|", "`", "$", "[", "]")) for arg in arguments)):
            continue
        for argument in arguments:
            match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:===|==|~=|!=|<=|>=|<|>|$)", argument)
            if match and _canonical_package_name(match.group(1)) == canonical:
                accepted.append(item)
                break
    return accepted


def _manifest_scope(path: str) -> str:
    """Classify a declaration from its repository-relative manifest context."""
    normalized = path.replace("\\", "/").strip("./")
    parts = [part.lower() for part in normalized.split("/") if part]
    directories = parts[:-1]
    filename = parts[-1] if parts else ""
    if any(part == "benchmark" or part == "benchmarks" or part.startswith("benchmark")
           for part in directories):
        return "BENCHMARK"
    if any(part in {"test", "tests", "dev", "development"} or part.startswith("test")
           for part in directories):
        return "DEV_TEST"
    if filename.startswith(("requirements-dev.", "requirements-test.", "requirements-tests.")):
        return "DEV_TEST"
    if filename.startswith(("requirements-doc.", "requirements-docs.")):
        return "UNKNOWN_SCOPE"
    if re.fullmatch(r"requirements[.-][^.]+\.txt", filename) and filename != "requirements.txt":
        return "UNKNOWN_SCOPE"
    if any(part in {"demo", "demos", "example", "examples"}
           or part.startswith("demo") or part.startswith("example") for part in directories):
        return "DEMO"
    if not directories:
        return "CORE"
    return "UNKNOWN_SCOPE"


def _dependency_safety_reason(row: dict[str, Any], canonical: str, has_authority: bool,
                              parsed_suffix: str | None,
                              conflict_resolved: bool = False) -> str:
    source = str(row.get("source") or "").strip().lower()
    if (row.get("conflict") or row.get("conflicting_versions")) and not conflict_resolved:
        return "conflicting dependency declarations"
    if canonical in _UNSAFE_PYTHON_PACKAGES or canonical.startswith("flashinfer"):
        return "runtime-sensitive package requires manual review"
    if canonical.endswith("-gpu"):
        return "GPU-specific package requires manual review"
    if source not in {"", "index", "pip", "pypi", "https://pypi.org/simple", "https://pypi.org/simple/"}:
        return "custom or unsupported package source"
    if not has_authority:
        return "no authoritative manifest or exact safe primary-install evidence"
    if parsed_suffix is None:
        return "unsupported version specifier"
    return ""


def _python_package_steps(
        research: dict[str, Any], environment: Path,
        superseded_requirements: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, list[dict[str, Any]]]]:
    superseded_requirements = superseded_requirements or set()
    manifests = _manifest_paths(research)
    candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    manual: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {scope: [] for scope in DEPENDENCY_SCOPES}
    documented_commands = [command for command, _ in _documented_commands(research)]
    for row in research.get("dependencies") or []:
        if not isinstance(row, dict):
            continue
        if _is_evidenced_system_dependency(row, research.get("system_dependencies") or []):
            continue
        evidence = _evidence(row)
        if evidence and all(
            isinstance(item, dict) and item.get("evidence_type") == "deterministic_lockfile"
            for item in evidence
        ):
            continue
        raw_name = str(row.get("package") or row.get("name") or "").strip()
        parsed = _requirement_parts(raw_name, row.get("version") or row.get("specifier"))
        if parsed is None:
            if raw_name:
                manual.append(f"Python dependency has an unsupported requirement: {raw_name}")
            continue
        name = parsed["name"]
        canonical = _canonical_package_name(name)
        if _build_extension_output(name, evidence, documented_commands):
            continue
        evidence_paths = {str(item.get("path")) for item in evidence if isinstance(item, dict) and item.get("path")}
        manifest_paths = sorted(evidence_paths & manifests)
        primary_pip_evidence = _primary_install_pip_evidence(name, evidence)
        scopes = {_manifest_scope(path) for path in manifest_paths}
        promoted = bool(primary_pip_evidence and any(scope != "CORE" for scope in scopes or {"UNKNOWN_SCOPE"}))
        if primary_pip_evidence:
            scopes.add("CORE")
        if not scopes:
            scopes.add("UNKNOWN_SCOPE")
        specifier = parsed["specifier"]
        conflict_values = [specifier or ""] + [
            _package_specifier(value) or str(value)
            for value in row.get("conflicting_versions") or []
        ]
        resolved = _compatible_exact_specifier(conflict_values) if len(set(conflict_values)) > 1 else None
        safety_reason = _dependency_safety_reason(row, canonical, bool(manifest_paths or primary_pip_evidence),
                                                   parsed["suffix"], bool(resolved))
        superseded = (
            (not row.get("conflict") and not row.get("conflicting_versions") or bool(resolved))
            and (canonical, specifier or "") in superseded_requirements
        )
        for declared_scope in sorted(scopes):
            scope = "OPTIONAL" if declared_scope == "CORE" and row.get("mandatory") is not True else declared_scope
            reason = safety_reason
            if row.get("mandatory") is not True:
                reason = reason or "optional or non-mandatory dependency"
            elif scope != "CORE":
                reason = f"{scope} dependency is not a core installation dependency"
            if superseded:
                reason = ""
            executable = scope == "CORE" and row.get("mandatory") is True and not reason and not superseded
            scope_paths = [path for path in manifest_paths if _manifest_scope(path) == declared_scope]
            record = {
                "name": name, "canonical_name": canonical, "extras": parsed["extras"],
                "specifier": specifier, "marker": parsed["marker"],
                "requirement": name + parsed["suffix"], "scope": scope,
                "mandatory": row.get("mandatory") is True, "optional": row.get("mandatory") is not True,
                "executable": executable, "source_manifests": scope_paths,
                "evidence": evidence, "classification_evidence": {
                    "manifest_paths": scope_paths,
                    "primary_install_evidence": primary_pip_evidence,
                    "promoted_to_core": promoted and scope == "CORE",
                },
                "review_reason": reason,
            }
            if superseded:
                record["superseded_by"] = "INSTALL_PYTORCH_RUNTIME"
            groups[scope].append(record)
            if executable:
                candidates.setdefault((canonical, parsed["extras"], parsed["marker"]), []).append(record)
            elif not superseded:
                manual.append(f"{record['requirement']}: {reason}")

    packages: list[dict[str, Any]] = []
    for (canonical, extras, marker), rows in sorted(candidates.items()):
        declared = sorted({row["specifier"] for row in rows if row["specifier"]})
        compatible = _compatible_exact_specifier(declared)
        if len(declared) > 1 and not compatible:
            manual.append(f"{rows[0]['name']}: conflicting dependency declarations ({', '.join(declared)})")
            for record in groups["CORE"]:
                if record["canonical_name"] == canonical:
                    record["executable"] = False
                    record["review_reason"] = "conflicting dependency declarations"
            continue
        specifier = compatible or (declared[0] if declared else "")
        evidence: list[Any] = []
        source_manifests: set[str] = set()
        for row in rows:
            source_manifests.update(row["source_manifests"])
            for item in row["evidence"]:
                if item not in evidence:
                    evidence.append(item)
        packages.append({
            "name": rows[0]["name"], "canonical_name": canonical, "extras": extras,
            "specifier": specifier, "marker": marker,
            "requirement": rows[0]["name"] + extras + specifier + (f"; {marker}" if marker else ""),
            "mandatory": True,
            "optional": False, "scope": "CORE", "executable": True,
            "source_manifests": sorted(source_manifests), "evidence": evidence,
        })

    if not packages:
        return [], list(dict.fromkeys(manual)), groups
    python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
    combined_evidence = [item for package in packages for item in package["evidence"]]
    step = _step(0, "INSTALL_PYTHON_PACKAGES",
                 "Install evidence-backed ordinary Python packages into the isolated environment",
                 str(environment), {
                     "target_environment": str(environment), "environment_path": str(environment),
                     "python_executable": str(python), "packages": packages,
                     "source_manifests": sorted({path for package in packages
                                                 for path in package["source_manifests"]}),
                 }, combined_evidence)
    step["expected_changes"] = [f"Python packages installed only inside {environment}"]
    step["rollback"] = f"Remove only the isolated environment directory: {environment}"
    return [step], list(dict.fromkeys(manual)), groups


def _documented_commands(research: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    rows: list[tuple[str, list[Any]]] = []
    for method in research.get("install_methods") or []:
        if not isinstance(method, dict) or str(method.get("classification") or "official") != "official":
            continue
        commands = method.get("exact_commands")
        if not isinstance(commands, list):
            commands = [method.get("command") or method.get("value")]
        evidence = _evidence(method)
        joined: list[str] = []
        for command in commands:
            part = str(command or "").rstrip()
            if joined and joined[-1].endswith("\\"):
                joined[-1] += "\n" + part
            else:
                joined.append(part.strip())
        for text in joined:
            if text:
                exact = [item for item in evidence if isinstance(item, dict)
                         and str(item.get("exact_text") or "").strip() == text]
                rows.append((text, exact or evidence))
    return rows


def _python_310_supported(py: dict[str, Any]) -> bool:
    """Accept only an explicit compatible Python constraint in root package metadata."""
    constraint = str(py.get("version") or "").strip()
    if not re.fullmatch(r"(?:>=|>|<=|<|==)\d+(?:\.\d+){1,2}(?:,(?:>=|>|<=|<|==)\d+(?:\.\d+){1,2})*", constraint):
        return False
    for clause in constraint.split(","):
        match = re.fullmatch(r"(>=|>|<=|<|==)(\d+(?:\.\d+){1,2})", clause)
        if not match:
            return False
        operator, version = match.groups()
        parts = tuple(int(part) for part in version.split("."))
        target = (3, 10, 0)
        parts += (0,) * (3 - len(parts))
        if not {">=": target >= parts, ">": target > parts, "<=": target <= parts,
                "<": target < parts, "==": target == parts}[operator]:
            return False
    return any(
        isinstance(item, dict)
        and str(item.get("path") or "").replace("\\", "/") in {"pyproject.toml", "setup.cfg", "setup.py"}
        and (str(item.get("path") or "").replace("\\", "/") != "setup.py"
             or item.get("evidence_type") == "deterministic_ast")
        and constraint in str(item.get("exact_text") or "")
        for item in _evidence(py)
    )


def _documented_project_pip_rows(research: dict[str, Any]) -> list[dict[str, Any]]:
    """Use an exact primary pip command for the named project, without inventing a version."""
    identity = research.get("source_identity") or {}
    repository = str(identity.get("repository") or identity.get("repo") or "").strip("/")
    if "/" not in repository:
        return []
    project_name = repository.rsplit("/", 1)[-1]
    canonical = _canonical_package_name(project_name)
    for row in research.get("dependencies") or []:
        if not isinstance(row, dict) or _canonical_package_name(str(row.get("package") or row.get("name") or "")) != canonical:
            continue
        if any(item.get("evidence_type") != "deterministic_lockfile"
               for item in _evidence(row) if isinstance(item, dict)):
            return []
    rows: list[dict[str, Any]] = []
    for command, evidence in _documented_commands(research):
        match = re.fullmatch(r"(?:python\s+-m\s+)?pip\s+install\s+([A-Za-z0-9][A-Za-z0-9._-]*)", command, re.I)
        if not match or _canonical_package_name(match.group(1)) != canonical:
            continue
        primary_evidence = _primary_install_pip_evidence(project_name, evidence)
        if not primary_evidence:
            continue
        rows.append({"package": match.group(1), "version": "UNKNOWN", "mandatory": True,
                     "source": "pypi", "evidence": primary_evidence})
    return rows[:1]


def _represented_pip_commands(research: dict[str, Any], package_steps: list[dict[str, Any]]) -> set[str]:
    packages = [package for step in package_steps for package in step["inputs"]["packages"]]
    represented: set[str] = set()
    for command, evidence in _documented_commands(research):
        match = re.fullmatch(r"(?:python\s+-m\s+)?pip\s+install\s+([A-Za-z0-9][A-Za-z0-9._-]*)", command, re.I)
        if not match:
            continue
        name = _canonical_package_name(match.group(1))
        if any(package["canonical_name"] == name and not package["specifier"]
               and _primary_install_pip_evidence(name, evidence) for package in packages):
            represented.add(command)
    return represented


def _represented_environment_commands(research: dict[str, Any]) -> set[str]:
    """Return only exact conda-create evidence represented by CREATE_PYTHON_ENV."""
    exact_environment_evidence = {
        str(item.get("exact_text") or "").strip()
        for item in _evidence(research.get("python_environment") or {})
        if isinstance(item, dict) and item.get("exact_text")
    }
    represented: set[str] = set()
    for command, _ in _documented_commands(research):
        if command not in exact_environment_evidence:
            continue
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            continue
        if tokens[:2] == ["conda", "create"] and "python=3.10" in tokens:
            represented.add(command)
    return represented


def _represented_activation_commands(research: dict[str, Any], environment: Path,
                                     steps: list[dict[str, Any]]) -> set[str]:
    """Interactive activation is redundant when fixed installs use the isolated Python."""
    if not any(step["type"] == "CREATE_PYTHON_ENV" for step in steps):
        return set()
    installs = [step for step in steps if step["type"] in
                {"INSTALL_PYTHON_PACKAGES", "INSTALL_PYTORCH_RUNTIME"}]
    python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
    if not installs or any(step["inputs"].get("python_executable") != str(python) for step in installs):
        return set()
    names: set[str] = set()
    for command in _represented_environment_commands(research):
        tokens = shlex.split(command, posix=True)
        for flag in ("-n", "--name"):
            if flag in tokens and tokens.index(flag) + 1 < len(tokens):
                names.add(tokens[tokens.index(flag) + 1])
    represented: set[str] = set()
    for command, _ in _documented_commands(research):
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            continue
        if len(tokens) == 3 and tokens[:2] == ["conda", "activate"] and tokens[2] in names:
            represented.add(command)
    return represented


def _runtime_preflight(python_version: str, requirements: dict[str, Any]) -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    visible = False
    if nvidia_smi:
        try:
            result = subprocess.run([nvidia_smi, "-L"], shell=False, check=False,
                                    capture_output=True, text=True, timeout=5)
            visible = result.returncode == 0 and bool((result.stdout or "").strip())
        except (OSError, subprocess.SubprocessError):
            visible = False
    return {
        "os": platform.system(),
        "gpu_presence": "DETECTED" if visible else "NOT_DETECTED",
        "nvidia_driver_visibility": "VISIBLE" if visible else "NOT_VISIBLE",
        "target_python_version": python_version,
        "documented_runtime_requirements": requirements,
        "system_cuda_toolkit_required": False,
        "nvcc_checked": False,
    }


def _pytorch_runtime_step(research: dict[str, Any], environment: Path) -> tuple[dict[str, Any] | None, list[str], set[str]]:
    """Build one exact, evidence-bound PyTorch wheel install or leave it review-only."""
    documented_commands = _documented_commands(research)
    if not any(re.search(r"\b(?:torch|torchvision|torchaudio)==", command)
               for command, _ in documented_commands):
        return None, [], set()
    gpu = research.get("gpu_runtime") or {}
    cuda_candidates: set[str] = set()
    embedded_tags: set[str] = set()
    version = str(gpu.get("version") or "").strip()
    if re.fullmatch(r"\d+\.\d+", version):
        cuda_candidates.add(version)
    cuda_fact = str(gpu.get("cuda") or "").strip()
    cuda_match = re.fullmatch(r"(\d+\.\d+)(?:\s*/\s*(cu\d{3}))?", cuda_fact, re.I)
    if cuda_match:
        cuda_candidates.add(cuda_match.group(1))
        if cuda_match.group(2):
            embedded_tags.add(cuda_match.group(2).lower())
    persisted_tag = str(gpu.get("wheel_tag") or "").strip().lower()
    indexed_gpu_evidence = [item for item in _evidence(gpu) if isinstance(item, dict)
                            and _PYTORCH_INDEX.search(str(item.get("exact_text") or ""))]
    if not cuda_candidates and re.fullmatch(r"cu\d{3}", persisted_tag) and any(
            _PYTORCH_INDEX.search(str(item.get("exact_text") or "")).group(1) == persisted_tag
            for item in indexed_gpu_evidence):
        cuda_candidates.add(f"{int(persisted_tag[2:-1])}.{persisted_tag[-1]}")
    if len(cuda_candidates) != 1:
        return None, ["PyTorch runtime lacks exact documented CUDA evidence"], set()
    cuda = next(iter(cuda_candidates))
    cuda_evidence = [item for item in _evidence(gpu) if isinstance(item, dict)
                     and re.search(rf"CUDA\s*{re.escape(cuda)}\b", str(item.get("exact_text") or ""), re.I)]
    if not cuda_evidence:
        cuda_evidence = [item for item in indexed_gpu_evidence
                         if _PYTORCH_INDEX.search(str(item.get("exact_text") or "")).group(1)
                         == "cu" + cuda.replace(".", "")]
    if not cuda_evidence:
        return None, ["PyTorch runtime lacks exact documented CUDA evidence"], set()
    wheel_tag = "cu" + cuda.replace(".", "")
    if ((persisted_tag and persisted_tag != "unknown" and persisted_tag != wheel_tag)
            or (embedded_tags and embedded_tags != {wheel_tag})):
        return None, ["PyTorch runtime evidence is ambiguous or conflicting"], set()
    candidates: list[tuple[str, list[str], str, list[Any]]] = []
    for command, evidence in documented_commands:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            continue
        if len(tokens) < 6 or tokens[:2] != ["pip", "install"] or tokens[-2] != "--index-url":
            continue
        index = tokens[-1]
        match = _PYTORCH_INDEX.fullmatch(index)
        specs = tokens[2:-2]
        parsed = [_PYTORCH_SPEC.fullmatch(spec) for spec in specs]
        if not match or not parsed or any(item is None for item in parsed):
            continue
        names = [item.group(1) for item in parsed if item]
        if len(names) != len(set(names)) or "torch" not in names or "torchvision" not in names:
            continue
        if match.group(1) != wheel_tag:
            continue
        candidates.append((command, specs, index, evidence))
    unique = {(tuple(specs), index) for _, specs, index, _ in candidates}
    if len(unique) != 1:
        reason = "PyTorch runtime evidence is missing" if not unique else "PyTorch runtime evidence is ambiguous or conflicting"
        return None, [reason], set()
    command, specs, index, command_evidence = candidates[0]
    specs_by_name = {spec.split("==", 1)[0]: spec for spec in specs}
    for name in ("torch", "torchvision", "torchaudio"):
        documented_version = str(gpu.get(name) or "").strip()
        if documented_version and documented_version.upper() != "UNKNOWN":
            if specs_by_name.get(name) != f"{name}=={documented_version}":
                return None, ["PyTorch runtime evidence is ambiguous or conflicting"], set()
    python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
    evidence = command_evidence + [item for item in cuda_evidence if item not in command_evidence]
    source_item = next((item for item in command_evidence if isinstance(item, dict)), {})
    requirements = {"torch": specs_by_name["torch"], "torchvision": specs_by_name["torchvision"],
                    "torchaudio": specs_by_name.get("torchaudio"), "cuda": cuda, "wheel_tag": wheel_tag}
    inputs = {
        "target_environment": str(environment), "environment_path": str(environment),
        "python_executable": str(python), "torch_spec": specs_by_name["torch"],
        "torchvision_spec": specs_by_name["torchvision"],
        "torchaudio_spec": specs_by_name.get("torchaudio"),
        "cuda_runtime": {"version": cuda, "wheel_tag": wheel_tag,
                         "distribution": "pytorch_wheel", "system_cuda_toolkit_required": False,
                         "nvcc_required": False},
        "package_index": index,
        "source": {"path": source_item.get("path"), "heading": source_item.get("heading")},
        "evidence": evidence, "original_documented_command": command,
        "normalized_argv": [str(python), "-m", "pip", "install", *specs, "--index-url", index],
        "compatibility_preflight": _runtime_preflight("3.10", requirements),
    }
    step = _step(0, "INSTALL_PYTORCH_RUNTIME", "Install the exact documented PyTorch CUDA wheel runtime",
                 str(environment), inputs, evidence)
    step["expected_changes"] = [f"PyTorch runtime packages installed only inside {environment}"]
    step["rollback"] = f"Remove only the isolated environment directory: {environment}"
    return step, [], {command}


def build_plan(research: dict[str, Any], job_id: str = "", job_context: Any = None) -> dict[str, Any]:
    if research.get("status") not in {"COMPLETE", "REVIEW_REQUIRED"}:
        raise ValueError("InstallResearch must be COMPLETE or REVIEW_REQUIRED before planning")
    identity = dict(research.get("source_identity") or {})
    source = Path(str(identity.get("local_path") or ""))
    commit = str(identity.get("exact_commit") or identity.get("commit") or "")
    env = source.parent / ".ornith-envs" / f"{source.name}-{commit[:12]}"
    steps: list[dict[str, Any]] = []
    blockers: list[str] = []
    manual: list[str] = []
    py = research.get("python_environment") or {}
    evidence = _evidence(py)
    text = json.dumps(evidence, ensure_ascii=False)
    consumed_commands: set[str] = set()
    documented_conda = "conda create" in text and "python=3.10" in text
    from editable_project_install import (EditableSourceError, documented_project_installs,
                                          verified_project_source, verify_project_command_evidence)
    project_installs = documented_project_installs(research, identity)
    project_bound = None
    if project_installs and job_context is not None:
        try:
            project_bound = verified_project_source(job_context, identity)
            for item in project_installs:
                verify_project_command_evidence(project_bound["source_path"], item["author_command"],
                                                item["evidence"], identity)
        except EditableSourceError as exc:
            project_bound = None
            blockers.append(f"VERIFIED_PROJECT_SOURCE_INVALID: {exc}")
    package_research = dict(research)
    package_research["dependencies"] = [row for row in research.get("dependencies") or []
        if not (project_bound and documented_project_installs({"dependencies": [row]}, identity))]
    package_research["dependencies"] += _documented_project_pip_rows(research)
    package_steps, package_manual, dependency_groups = _python_package_steps(package_research, env)
    represented_pip = _represented_pip_commands(research, package_steps)
    managed_isolation = bool((package_steps or project_bound) and _python_310_supported(py))
    if documented_conda or managed_isolation:
        env_evidence = evidence if documented_conda else evidence + [
            {"policy": "PRODUCT_MANAGED_ISOLATION",
             "reason": "Product-selected isolation for supported Python installation steps, not an author environment requirement",
             "author_environment_manager": py.get("manager") or "UNKNOWN",
             "supported_install_step_types": sorted({step["type"] for step in package_steps}
                 | ({"INSTALL_LOCAL_PYTHON_PROJECT"} if project_bound else set())),
             "documented_pip_commands": sorted(represented_pip)}]
        steps.append(_step(len(steps)+1, "CREATE_PYTHON_ENV", "Create isolated Python environment", str(env),
                           {"manager": "conda", "environment_path": str(env), "python_version": "3.10"}, env_evidence))
        if documented_conda:
            consumed_commands.update(_represented_environment_commands(research))
    else:
        blockers.append("DOCUMENTED_ISOLATED_ENVIRONMENT_UNKNOWN")
    runtime_step, runtime_manual, runtime_commands = _pytorch_runtime_step(research, env)
    consumed_commands.update(runtime_commands)
    manual.extend(runtime_manual)
    superseded_requirements: set[tuple[str, str]] = set()
    if runtime_step:
        runtime_inputs = runtime_step.get("inputs") or {}
        for key in ("torch_spec", "torchvision_spec", "torchaudio_spec"):
            spec = str(runtime_inputs.get(key) or "")
            match = _PYTORCH_SPEC.fullmatch(spec)
            if match:
                superseded_requirements.add((_canonical_package_name(match.group(1)), f"=={match.group(2)}"))
    if superseded_requirements:
        package_steps, package_manual, dependency_groups = _python_package_steps(
            package_research, env, superseded_requirements)
        represented_pip = _represented_pip_commands(research, package_steps)
    consumed_commands.update(represented_pip)
    for package_step in package_steps:
        package_step["step_id"] = f"install-{len(steps)+1:03d}"
        steps.append(package_step)
    manual.extend(package_manual)
    if runtime_step:
        runtime_step["step_id"] = f"install-{len(steps)+1:03d}"
        steps.append(runtime_step)
    if project_bound and any(s["type"] == "CREATE_PYTHON_ENV" for s in steps):
        python = env / ("python.exe" if sys.platform == "win32" else "bin/python")
        item = project_installs[0]
        strategy = {"policy": "VERIFIED_FIXED_SNAPSHOT",
                    "reason": "Product execution strategy: ordinary install of the verified fixed snapshot; not an author command"}
        project_step = _step(len(steps)+1, "INSTALL_LOCAL_PYTHON_PROJECT",
            "Install the verified local Python project snapshot", project_bound["source_path"], {
                "environment_path": str(env), "python_executable": str(python),
                "repository": project_bound["repository"], "canonical_url": project_bound["canonical_url"],
                "exact_commit": project_bound["exact_commit"], "source_path": project_bound["source_path"],
                "source_tree_sha256": project_bound["tree_sha256"],
                "source_tree_manifest": project_bound["tree_manifest"],
                "author_command": item["author_command"], "execution_strategy": "VERIFIED_FIXED_SNAPSHOT",
            }, [candidate["evidence"] for candidate in project_installs] + [strategy])
        project_step["expected_changes"] = [f"Project installed only inside {env}"]
        project_step["rollback"] = f"Remove only the isolated environment directory: {env}"
        steps.append(project_step)
        consumed_commands.update(candidate["author_command"] for candidate in project_installs)
    else:
        manual.extend(candidate["author_command"] for candidate in project_installs)
    system_prerequisites = list(research.get("system_dependencies") or [])
    for row in research.get("dependencies") or []:
        if _is_evidenced_system_dependency(row, system_prerequisites) and row not in system_prerequisites:
            system_prerequisites.append(row)
    if system_prerequisites:
        manual.append("System prerequisites require independent manual review; no automatic installation")
    editable_rows = [(command, evidence) for command, evidence in _documented_commands(research)
                     if command == "pip install -e ." and any(
                         isinstance(item, dict) and item.get("exact_text") == command
                         and item.get("evidence_type") == "verbatim_command"
                         for item in evidence)]
    if editable_rows and not project_bound and job_context is not None and any(s["type"] == "CREATE_PYTHON_ENV" for s in steps):
        from editable_project_install import EditableSourceError, verified_editable_source, verify_editable_command_evidence
        try:
            bound = verified_editable_source(job_context, identity)
            verify_editable_command_evidence(bound["source_path"], editable_rows[0][1])
        except EditableSourceError:
            pass  # Keep the documented command in review when its source cannot be verified.
        else:
            python = env / ("python.exe" if sys.platform == "win32" else "bin/python")
            editable = _step(len(steps)+1, "INSTALL_LOCAL_EDITABLE_PROJECT",
                             "Install the verified local Python project in editable mode",
                             bound["source_path"], {
                                 "environment_path": str(env), "python_executable": str(python),
                                 "repository": bound["repository"], "exact_commit": bound["exact_commit"],
                                 "source_tree_sha256": bound["tree_sha256"],
                             }, editable_rows[0][1])
            editable["expected_changes"] = [f"Editable project installed only inside {env}"]
            editable["rollback"] = f"Remove only the isolated environment directory: {env}"
            steps.append(editable)
            consumed_commands.add("pip install -e .")
    consumed_commands.update(_represented_activation_commands(research, env, steps))
    for command, _ in _documented_commands(research):
        if command not in consumed_commands and not any(command in x for x in manual):
            manual.append(command)
    has_model_download = False
    for row in research.get("models") or []:
        if not isinstance(row, dict) or not row:
            continue
        step, reason = _model_asset_step(row, source, len(steps) + 1)
        if step is None:
            manual.append(reason)
        else:
            steps.append(step)
            has_model_download = True
    if manual:
        steps.append(_step(len(steps)+1, "REVIEW_DOCUMENTED_ACTION", "Review unsupported documented actions",
                    str(source), {"documented_actions": manual,
                                  "system_prerequisites": system_prerequisites,
                                  "dependency_groups": {scope: rows for scope, rows in dependency_groups.items()
                                                        if scope != "CORE" and rows}},
                    [item for row in system_prerequisites for item in _evidence(row)]))
    executable = [s for s in steps if s["type"] in EXECUTABLE_TYPES]
    status = "BLOCKED" if blockers or not executable else (
        "REVIEW_REQUIRED" if manual or has_model_download else "READY_FOR_APPROVAL")
    now = datetime.now(timezone.utc).isoformat()
    return {"schema_version": 1, "kind": "INSTALLATION_PLAN", "job_id": job_id, "status": status,
            "source_acquisition_job_id": research.get("source_acquisition_job_id"),
            "source_identity": identity, "environment": {"manager": "conda", "path": str(env), "python": "3.10"},
            "steps": steps, "block_reasons": blockers, "unsupported_actions": manual,
            "dependency_scope_groups": dependency_groups,
            "system_prerequisites": system_prerequisites,
            "estimated_download_bytes": None, "created_at": now, "updated_at": now}


def build_job_plan(jobs_root: Path, job_id: str) -> dict[str, Any]:
    root = Path(jobs_root) / job_id
    research = json.loads((root / "install_research.json").read_text(encoding="utf-8"))
    context = None
    from editable_project_install import documented_project_installs, load_source_job_context
    if documented_project_installs(research, research.get("source_identity") or {}):
        context = load_source_job_context(root, research)
    elif any(command == "pip install -e ." for command, _ in _documented_commands(research)):
        from job_context import JobContext
        context = JobContext.load(root)
    plan = build_plan(research, job_id, context)
    path = root / PLAN_FILE
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return plan


def load_job_plan(jobs_root: Path, job_id: str) -> dict[str, Any]:
    """Load the persisted plan that is the sole input to the review screen."""
    return json.loads((Path(jobs_root) / job_id / PLAN_FILE).read_text(encoding="utf-8"))


def review_text(plan: dict[str, Any]) -> str:
    """Render every approved-plan field without turning review text into execution."""
    environment = plan.get("environment") or {}
    lines = [
        f"Plan status: {plan.get('status', 'UNKNOWN')}",
        f"Environment target: {environment.get('path', 'UNKNOWN')}",
        f"Environment manager: {environment.get('manager', 'UNKNOWN')}",
        f"Python version: {environment.get('python', 'UNKNOWN')}",
        "",
        "Steps:",
    ]
    steps = plan.get("steps") or []
    if not steps:
        lines.append("- None")
    for step in steps:
        executable = step.get("type") in EXECUTABLE_TYPES
        lines.extend([
            "",
            f"{step.get('step_id', 'UNKNOWN')} — {step.get('type', 'UNKNOWN')}",
            f"Documented action: {step.get('action', 'UNKNOWN')}",
            f"Persisted inputs: {json.dumps(step.get('inputs') or {}, ensure_ascii=False, indent=2)}",
            f"Evidence: {json.dumps(step.get('evidence') or [], ensure_ascii=False, indent=2)}",
            f"Mode: {'Executable fixed operation' if executable else 'Review only — never executable'}",
            f"Expected changes: {json.dumps(step.get('expected_changes') or [], ensure_ascii=False)}",
            f"Rollback: {step.get('rollback') or 'UNKNOWN'}",
            f"Approval required: {'YES' if step.get('approval_required') else 'NO'}",
            f"Approved: {'YES' if step.get('approved') else 'NO'}",
        ])
    lines.extend([
        "",
        f"Blockers: {json.dumps(plan.get('block_reasons') or [], ensure_ascii=False)}",
        f"Unsupported actions: {json.dumps(plan.get('unsupported_actions') or [], ensure_ascii=False, indent=2)}",
        "Approval: This screen is review-only. Nothing is executed here.",
    ])
    return "\n".join(lines)


def console_review_text(plan: dict[str, Any], encoding: str | None = None) -> str:
    """Render safely for a legacy console without changing the persisted Unicode plan."""
    target_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    return review_text(plan).encode(target_encoding, errors="backslashreplace").decode(target_encoding)


def write_console_review(plan: dict[str, Any], stream: Any = None) -> None:
    target = stream or sys.stdout
    target.write(console_review_text(plan, getattr(target, "encoding", None)))
