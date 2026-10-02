"""Evidence-bound, local-only installation research for acquired source trees.

This module never executes repository content.  It reads a bounded allow-list of
documentation and manifests, applies deterministic parsers, then asks the
configured Local AI Companion model to extract only claims supported by supplied text.
"""
from __future__ import annotations

import ast
import copy
import configparser
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit

import product_cli
from local_only_test import (LocalOnlyError, LocalModelHTTPError,
                             LocalModelInvalidResponseError,
                             LocalModelNotFoundError,
                             LocalModelTimeoutError,
                             LocalModelTruncatedResponseError,
                             LocalModelUnavailableError, MODEL_ENDPOINT,
                             MODEL_NAME, invoke_local_model)
from runtime_paths import runtime_path


RESEARCH_FILE = "install_research.json"
MAX_FILE_BYTES = 512_000
MAX_FILES = 80
TARGET_CHUNK_CHARS = 12_000
MAX_CHUNK_CHARS = 24_000
MAX_MODEL_CHUNKS = 8
MAX_EVIDENCE_SUBCHUNKS = 4
INSTALL_RESEARCH_MAX_OUTPUT_TOKENS = 4096
CONFIDENCE_CHOICES = ("HIGH", "MEDIUM", "LOW")
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
FIXED_PLAN_TYPE = "REVIEW_DOCUMENTED_ACTION"
AI_DEBUG_FILE = runtime_path("logs", "install_research_ai_debug.json")


class InstallResearchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence(path: str, exact_text: str, *, heading: str = "UNKNOWN",
              kind: str = "deterministic_manifest", confidence: str = "HIGH") -> dict[str, str]:
    return {
        "path": path, "heading": heading or "UNKNOWN", "exact_text": exact_text,
        "confidence": confidence, "evidence_type": kind,
    }


def _empty_result(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "status": "ANALYZING",
        "source_identity": identity,
        "install_methods": [],
        "python_environment": {
            "version": "UNKNOWN", "environment_manager": "UNKNOWN",
            "package_installer": "UNKNOWN", "evidence": [],
        },
        "python_environments": [],
        "dependencies": [],
        "gpu_runtime": {
            "pytorch": "UNKNOWN", "torchvision": "UNKNOWN", "cuda": "UNKNOWN",
            "version": "UNKNOWN", "wheel_tag": "UNKNOWN", "flashinfer": "UNKNOWN",
            "triton": "UNKNOWN", "xformers": "UNKNOWN", "compiler": "UNKNOWN", "evidence": [],
        },
        "models": [], "system_dependencies": [],
        "launch": {"exact_command": "UNKNOWN", "port": "UNKNOWN", "ui": "UNKNOWN",
                   "verification": "UNKNOWN", "evidence": []},
        "risks": [], "unknowns": [], "proposed_plan": [],
        "diagnostics": {"files_inspected": [], "model_chunks": 0, "rejected_claims": []},
        "created_at": _now(), "updated_at": _now(),
    }


_PYTHON_ENVIRONMENT_SCOPES = ("CORE", "BENCHMARK", "DEMO", "DEV_TEST", "UNKNOWN_SCOPE")


def _python_environment_scope(path: str) -> str:
    parts = [part.casefold() for part in PurePosixPath(path.replace("\\", "/")).parts]
    filename = parts[-1] if parts else ""
    if any(part in {"benchmark", "benchmarks", "bench"} for part in parts[:-1]):
        return "BENCHMARK"
    if any(part in {"demo", "demos", "example", "examples"} for part in parts[:-1]):
        return "DEMO"
    if any(part in {"test", "tests", "testing", "dev", "development"} for part in parts[:-1]):
        return "DEV_TEST"
    if filename.startswith(("requirements-dev.", "requirements-test.", "requirements-tests.")):
        return "DEV_TEST"
    if filename.startswith(("requirements-doc.", "requirements-docs.")):
        return "UNKNOWN_SCOPE"
    if re.fullmatch(r"requirements[.-][^.]+\.txt", filename) and filename != "requirements.txt":
        return "UNKNOWN_SCOPE"
    if len(parts) == 1:
        return "CORE"
    return "UNKNOWN_SCOPE"


def _wanted(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part.lower() in SKIP_DIRS for part in rel.parts[:-1]):
        return False
    name = path.name.lower()
    suffix = path.suffix.lower()
    at_root = len(rel.parts) == 1
    in_docs = bool(rel.parts and rel.parts[0].lower() in {"docs", "doc", "documentation"})
    manifest = (
        name.startswith(("readme", "install", "requirements", "dockerfile"))
        or name in {"pyproject.toml", "setup.py", "setup.cfg", "environment.yml", "environment.yaml",
                    "uv.lock", "poetry.lock"}
    )
    script_or_example = (
        suffix in {".sh", ".ps1", ".bat", ".cmd"}
        or any(token in name for token in ("setup", "install", "launch", "start", "download", "model", "config.example", "example.config"))
    )
    return manifest or (in_docs and suffix in {".md", ".rst", ".txt", ".toml", ".yml", ".yaml"}) or (at_root and script_or_example)


def discover_research_files(root: Path) -> list[Path]:
    root = root.resolve()
    found: list[Path] = []
    for path in root.rglob("*"):
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
            if path.is_file() and not path.is_symlink() and _wanted(path, root) and path.stat().st_size <= MAX_FILE_BYTES:
                found.append(path)
        except (OSError, ValueError):
            continue
    def priority(path: Path) -> tuple[int, str]:
        rel = path.relative_to(root)
        name = path.name.casefold()
        if len(rel.parts) == 1 and name in {"readme.md", "readme.rst", "readme.txt"}:
            rank = 0
        elif len(rel.parts) == 1 and (name.startswith("requirements") or name in {
                "pyproject.toml", "setup.py", "setup.cfg", "environment.yml", "environment.yaml",
                "uv.lock", "poetry.lock"}):
            rank = 1
        elif len(rel.parts) == 1:
            rank = 2
        else:
            rank = 3
        return rank, rel.as_posix().casefold()
    return sorted(found, key=priority)[:MAX_FILES]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _source_fidelity(completed: dict[str, Any], root: Path,
                     files: list[Path]) -> dict[str, Any]:
    """Audit omitted archive members against installation evidence before model use."""
    acquisition = completed.get("acquisition_evidence") or {}
    membership_known = ("skipped_archive_members" in acquisition
                        or "skipped_symlinks" in acquisition)
    members = acquisition.get("skipped_archive_members") or []
    if not members:
        archive_root = str(acquisition.get("archive_root") or "")
        members = [{
            "archive_member_path": path,
            "source_relative_path": path[len(archive_root) + 1:] if archive_root and path.startswith(archive_root + "/") else path,
            "type": "SYMLINK", "mode": "UNKNOWN", "reason_skipped": "SYMLINK_NOT_MATERIALIZED",
        } for path in acquisition.get("skipped_symlinks") or []]
    texts = {path.relative_to(root).as_posix(): _read(path) for path in files}
    build_metadata = [name for name in ("MANIFEST.in", "pyproject.toml", "setup.py", "setup.cfg")
                      if (root / name).is_file()]
    audited = []
    for member in members:
        relative = str(member.get("source_relative_path") or "").replace("\\", "/")
        parts = PurePosixPath(relative).parts
        name = parts[-1].casefold() if parts else ""
        relevant_name = (name.startswith(("readme", "install", "requirements", "dockerfile"))
                         or name in {"pyproject.toml", "setup.py", "setup.cfg", "environment.yml",
                                     "environment.yaml", "uv.lock", "poetry.lock"}
                         or name.endswith((".sh", ".ps1", ".bat", ".cmd")))
        basename_reference = (re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(parts[-1])}(?![A-Za-z0-9_.-])", re.I)
                              if len(parts) > 1 and len(parts[-1]) >= 4 else None)
        references = [path for path, content in texts.items()
                      if relative and (relative.casefold() in content.casefold()
                                       or (basename_reference is not None
                                           and basename_reference.search(content)))]
        benign_area = bool(parts and parts[0].casefold() in {"test", "tests", "testing"})
        relevant = bool(relevant_name or references or not benign_area or build_metadata)
        audited.append({**member, "installation_relevant_or_uncertain": relevant,
                        "referenced_by": references,
                        "impact_reason": ("INSTALLATION_FILE_OR_SCRIPT" if relevant_name else
                                          "REFERENCED_BY_INSTALLATION_EVIDENCE" if references else
                                          "BUILD_PACKAGE_CONTENT_UNCERTAIN" if build_metadata else
                                          "PACKAGE_OR_RUNTIME_CONTENT_UNCERTAIN" if relevant else
                                          "NO_INSTALLATION_REFERENCE_IN_BOUNDED_EVIDENCE")})
    return {
        "archive_tree_equivalent": not bool(audited) if membership_known else None,
        "scope": ("EXTRACTED_TREE_ONLY" if audited else
                  "COMPLETE_ARCHIVE_MEMBERS" if membership_known else "UNKNOWN"),
        "skipped_members": audited,
        "review_required": any(row["installation_relevant_or_uncertain"] for row in audited),
        "files_checked_for_references": list(texts),
        "build_metadata_present": build_metadata,
    }


def _requirement_rows(path: Path, rel: str, text: str) -> list[dict[str, Any]]:
    rows = []
    scope = _python_environment_scope(rel)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(("-r", "--requirement", "-c", "--constraint")):
            continue
        if line.startswith(("--", "-f", "--find-links", "-e ")):
            continue
        source = "index"
        package = re.split(r"\s*(?:===|==|=|~=|!=|<=|>=|<|>|@|\[|;)", line, maxsplit=1)[0].strip()
        version = line[len(package):].strip() or "UNKNOWN"
        if line.startswith(("git+", "http://", "https://")) or " @ " in line:
            source = "direct_reference"
            package = line.split(" @ ", 1)[0].strip() if " @ " in line else "UNKNOWN"
        rows.append({
            "package": package or "UNKNOWN", "version": version, "source": source,
            "mandatory": scope == "CORE", "scope": scope,
            "evidence": [_evidence(rel, raw)],
        })
    return rows


def _poetry_dependency_rows(rel: str, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read only the direct Poetry runtime table, leaving complex entries for review."""
    block = re.search(r"(?ms)^\[tool\.poetry\.dependencies\][ \t]*\r?\n(.*?)(?=^\[|\Z)", text)
    if not block:
        return [], []
    dependencies: list[dict[str, Any]] = []
    environments: list[dict[str, Any]] = []
    for raw in block.group(1).splitlines():
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(.*?)\s*$", raw)
        if not match:
            continue
        package, value = match.groups()
        simple = re.fullmatch(r"['\"]([^'\"]+)['\"]", value)
        inline = value.startswith("{") and value.endswith("}")
        version_match = re.search(r"\bversion\s*=\s*['\"]([^'\"]+)['\"]", value) if inline else None
        if not simple and not version_match:
            continue
        version = simple.group(1) if simple else version_match.group(1)
        evidence = [_evidence(rel, raw)]
        if package.casefold() == "python":
            environments.append({
                "version": version, "environment_manager": "UNKNOWN",
                "package_installer": "poetry", "evidence": evidence,
            })
            continue
        optional = bool(re.search(r"\boptional\s*=\s*true\b", value, re.I)) if inline else False
        direct = bool(re.search(r"\b(?:git|url|path)\s*=", value)) if inline else False
        dependencies.append({
            "package": package, "version": version,
            "source": "direct_reference" if direct else "index",
            "mandatory": not optional, "evidence": evidence,
        })
    return dependencies, environments


def _quoted_list(tree: ast.AST, name: str) -> tuple[list[str], str | None]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setup":
            for keyword in node.keywords:
                if keyword.arg == name:
                    try:
                        value = ast.literal_eval(keyword.value)
                        if isinstance(value, (list, tuple)):
                            return [str(x) for x in value], name
                        if isinstance(value, str):
                            return [value], name
                    except (ValueError, TypeError):
                        return [], None
    return [], None


def _pyproject_project_dependencies(text: str) -> list[str]:
    """Read the string-only PEP 621 dependency array without treating comments as data."""
    section = re.search(r"(?ms)^\[project\][ \t]*(?:#.*)?\r?\n(.*?)(?=^\[|\Z)", text)
    if not section:
        return []
    declaration = re.search(r"(?m)^[ \t]*dependencies[ \t]*=[ \t]*\[", section.group(1))
    if not declaration:
        return []
    source = section.group(1)[declaration.end():]
    dependencies: list[str] = []
    index = 0
    needs_separator = False
    while index < len(source):
        char = source[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == "#":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline + 1
            continue
        if char == ",":
            if not needs_separator:
                return []
            needs_separator = False
            index += 1
            continue
        if char == "]":
            return dependencies
        if char not in "\"'" or needs_separator:
            return []
        quote = char
        start = index + 1
        index = start
        while index < len(source):
            if quote == '"' and source[index] == "\\":
                index += 2
            elif source[index] == quote:
                break
            else:
                index += 1
        if index >= len(source):
            return []
        raw = source[start:index]
        if quote == '"':
            try:
                value = json.loads(source[start - 1:index + 1])
            except json.JSONDecodeError:
                return []
        else:
            value = raw
        dependencies.append(value)
        index += 1
        needs_separator = True
    return []


def parse_deterministic(files: Iterable[Path], root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dependencies": [], "python_environment": [], "gpu_runtime": {}, "manifests": [],
    }
    seen: set[tuple[str, str]] = set()
    runtime_values: dict[str, dict[str, list[dict[str, str]]]] = {
        "pytorch": {}, "torchvision": {}, "cuda_version": {}, "wheel_tag": {},
    }

    def remember_runtime(field: str, value: str, evidence: dict[str, str]) -> None:
        runtime_values[field].setdefault(value, []).append(evidence)

    for path in files:
        rel = path.relative_to(root).as_posix()
        text = _read(path)
        lower = path.name.lower()
        for raw in text.splitlines():
            for package, field in (("torch", "pytorch"), ("torchvision", "torchvision")):
                match = re.search(rf"(?<![\w-]){package}\s*==\s*([A-Za-z0-9.+_-]+)", raw, re.I)
                if match:
                    remember_runtime(field, match.group(1), _evidence(rel, raw))
            for match in re.finditer(r"\bCUDA(?:\s+runtime)?\s*(?:version\s*)?[:=]?\s*([0-9]+\.[0-9]+)\b", raw, re.I):
                remember_runtime("cuda_version", match.group(1), _evidence(rel, raw))
            for match in re.finditer(r"\bcu([0-9]{2})([0-9])\b", raw, re.I):
                tag = f"cu{match.group(1)}{match.group(2)}".lower()
                remember_runtime("wheel_tag", tag, _evidence(rel, raw))
                remember_runtime("cuda_version", f"{int(match.group(1))}.{match.group(2)}", _evidence(rel, raw))
            if re.search(r"\b(?:conda|mamba|micromamba)\s+(?:create|env\s+create)\b", raw, re.I):
                manager = re.search(r"\b(conda|mamba|micromamba)\b", raw, re.I)
                python = re.search(r"\bpython\s*=\s*([^\s`]+)", raw, re.I)
                result["python_environment"].append({
                    "version": python.group(1) if python else "UNKNOWN",
                    "environment_manager": manager.group(1).lower() if manager else "UNKNOWN",
                    "package_installer": "UNKNOWN", "evidence": [_evidence(rel, raw)],
                })
            if re.search(r"\b(?:(?:python|py)\s+-m\s+)?pip\s+install\b", raw, re.I):
                result["python_environment"].append({
                    "version": "UNKNOWN", "environment_manager": "UNKNOWN",
                    "package_installer": "pip", "evidence": [_evidence(rel, raw)],
                })
        if lower.startswith("requirements") and path.suffix.lower() == ".txt":
            result["dependencies"].extend(_requirement_rows(path, rel, text))
            result["manifests"].append({"path": rel, "type": "requirements"})
        elif lower == "setup.cfg":
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(text)
                python = parser.get("options", "python_requires", fallback="").strip()
                if python:
                    python_line = re.search(r"(?mi)^[ \t]*python_requires[ \t]*=[^\r\n]+", text)
                    result["python_environment"].append({"version": python,
                                                         "environment_manager": "UNKNOWN",
                                                         "package_installer": "UNKNOWN",
                                                         "evidence": [_evidence(
                                                             rel,
                                                             python_line.group(0).strip() if python_line else python,
                                                         )]})
                raw = parser.get("options", "install_requires", fallback="")
                result["dependencies"].extend(_requirement_rows(path, rel, raw))
                result["manifests"].append({"path": rel, "type": "setup_cfg"})
            except configparser.Error:
                pass
        elif lower == "setup.py":
            try:
                tree = ast.parse(text)
                requirements, marker = _quoted_list(tree, "install_requires")
                python, _ = _quoted_list(tree, "python_requires")
                result["dependencies"].extend(_requirement_rows(path, rel, "\n".join(requirements)))
                if python:
                    result["python_environment"].append({"version": python[0],
                                                         "environment_manager": "UNKNOWN",
                                                         "package_installer": "UNKNOWN",
                                                         "evidence": [_evidence(rel, python[0], kind="deterministic_ast")]})
                if marker:
                    result["manifests"].append({"path": rel, "type": "setup_py_ast"})
            except SyntaxError:
                pass
        elif lower == "pyproject.toml":
            result["manifests"].append({"path": rel, "type": "pyproject"})
            python_match = re.search(r"(?m)^\s*requires-python\s*=\s*['\"]([^'\"]+)['\"]", text)
            if python_match:
                exact = python_match.group(0).strip()
                installer = "poetry" if "[tool.poetry" in text else ("uv" if "[tool.uv" in text else "UNKNOWN")
                result["python_environment"].append({"version": python_match.group(1),
                                                     "environment_manager": "UNKNOWN",
                                                     "package_installer": installer,
                                                     "evidence": [_evidence(rel, exact)]})
            result["dependencies"].extend(_requirement_rows(
                path, rel, "\n".join(_pyproject_project_dependencies(text))))
            poetry_dependencies, poetry_environments = _poetry_dependency_rows(rel, text)
            result["dependencies"].extend(poetry_dependencies)
            result["python_environment"].extend(poetry_environments)
        elif lower in {"environment.yml", "environment.yaml"}:
            result["manifests"].append({"path": rel, "type": "conda_environment"})
            match = re.search(
                r"(?ms)^[ \t]*dependencies[ \t]*:[ \t]*\n(.*?)(?=^[^\s#][^:\n]*:|\Z)", text)
            if match:
                lines = []
                for raw in match.group(1).splitlines():
                    item = re.sub(r"^\s*-\s*", "", raw).strip()
                    if item and item != "pip:" and not item.startswith("#"):
                        lines.append(item)
                result["dependencies"].extend(_requirement_rows(path, rel, "\n".join(lines)))
            py = re.search(r"(?m)^\s*-\s*python\s*([=<>!~].+)$", text)
            if py:
                result["python_environment"].append({"version": py.group(1).strip(),
                                                     "environment_manager": "conda",
                                                     "package_installer": "UNKNOWN",
                                                     "evidence": [_evidence(rel, py.group(0).strip())]})
        elif lower in {"uv.lock", "poetry.lock"}:
            result["manifests"].append({"path": rel, "type": lower.replace(".", "_")})
            manager = "uv" if lower == "uv.lock" else "poetry"
            packages = re.finditer(
                r"(?ms)^name\s*=\s*['\"]([^'\"]+)['\"]\s*$.*?^version\s*=\s*['\"]([^'\"]+)['\"]\s*$",
                text,
            )
            for match in packages:
                package, version = match.group(1), match.group(2)
                result["dependencies"].append({"package": package, "version": f"=={version}", "source": manager,
                                               "mandatory": "UNKNOWN", "evidence": [_evidence(
                                                   rel, match.group(0), kind="deterministic_lockfile")]} )
    for row in result["python_environment"]:
        evidence = row.get("evidence", [])
        path = next((str(item.get("path", "")) for item in evidence if isinstance(item, dict)), "")
        row["scope"] = _python_environment_scope(path)

    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in result["dependencies"]:
        key = (str(row.get("package", "")).lower(), str(row.get("version", "")),
               str(row.get("scope") or "CORE"))
        if key in unique:
            existing = unique[key].setdefault("evidence", [])
            for evidence in row.get("evidence", []):
                if evidence not in existing:
                    existing.append(evidence)
        else:
            seen.add(key)
            unique[key] = row
    result["dependencies"] = list(unique.values())
    runtime: dict[str, Any] = {"evidence": []}
    for source_field, target_field in (("pytorch", "pytorch"), ("torchvision", "torchvision"),
                                       ("cuda_version", "version"), ("wheel_tag", "wheel_tag")):
        values = runtime_values[source_field]
        if len(values) == 1:
            value, evidence = next(iter(values.items()))
            runtime[target_field] = value
            for item in evidence:
                if item not in runtime["evidence"]:
                    runtime["evidence"].append(item)
    if runtime.get("version") and runtime.get("wheel_tag"):
        runtime["cuda"] = f'{runtime["version"]} / {runtime["wheel_tag"]}'
    elif runtime.get("version"):
        runtime["cuda"] = runtime["version"]
    elif runtime.get("wheel_tag"):
        runtime["cuda"] = runtime["wheel_tag"]
    result["gpu_runtime"] = runtime
    return result


_MODEL_ASSET_SUFFIXES = {
    ".bin", ".ckpt", ".engine", ".gguf", ".onnx", ".pb", ".pt", ".pth",
    ".safetensors", ".tflite",
}
_DOCUMENTED_HTTPS_URL = re.compile(r"https://[^\s<>\"'`\]\)]+", re.IGNORECASE)


def _documented_model_asset(url: str) -> dict[str, str] | None:
    """Classify only an explicit HTTPS URL whose path names a concrete model file."""
    clean_url = url.rstrip(".,;:")
    parsed = urlsplit(clean_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    filename = unquote(PurePosixPath(parsed.path).name)
    if Path(filename).suffix.lower() not in _MODEL_ASSET_SUFFIXES:
        return None

    repository = "UNKNOWN"
    revision = "UNKNOWN"
    revision_kind = "unknown"
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.hostname.lower() in {"huggingface.co", "www.huggingface.co"} and len(parts) >= 5:
        if parts[2] == "resolve":
            repository = f"{parts[0]}/{parts[1]}"
            revision = parts[3]
            revision_kind = "immutable" if re.fullmatch(r"[0-9a-fA-F]{40}", revision) else "symbolic"
    return {
        "url": clean_url,
        "filename": filename,
        "source_repository": repository,
        "revision": revision,
        "revision_kind": revision_kind,
    }


def extract_model_asset_evidence(files: Iterable[Path], root: Path) -> list[dict[str, Any]]:
    """Preserve concrete documented asset URLs without making a destination executable."""
    assets: dict[str, dict[str, Any]] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        heading = "UNKNOWN"
        raw_lines = _read(path).splitlines()
        line_index = 0
        while line_index < len(raw_lines):
            raw_line = raw_lines[line_index]
            heading_match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", raw_line)
            if heading_match:
                heading = heading_match.group(1).strip().rstrip("#").strip()
            logical_lines = [raw_line.strip()]
            while logical_lines[-1].endswith("\\") and line_index + 1 < len(raw_lines):
                line_index += 1
                logical_lines.append(raw_lines[line_index].strip())
            line_index += 1
            line = "\n".join(logical_lines).strip()
            if not line:
                continue
            for url_match in _DOCUMENTED_HTTPS_URL.finditer(line):
                classified = _documented_model_asset(url_match.group(0))
                if not classified:
                    continue
                url = classified["url"]
                evidence = _evidence(rel, line, heading=heading, kind="documentation")
                output_match = re.search(
                    r"(?:^|\s)(?:-O|--output)\s+(?:['\"]([^'\"]+)['\"]|([^\s]+))",
                    line,
                )
                original_action = line if re.search(r"(?:^|\s)(?:wget|curl)(?:\s|$)", line) else "UNKNOWN"
                destination_evidence = (
                    (output_match.group(1) or output_match.group(2)).strip()
                    if output_match else "UNKNOWN"
                )
                destination_status = "AMBIGUOUS" if original_action != "UNKNOWN" else "UNKNOWN"
                row = assets.get(url)
                if row is None:
                    row = {
                        "name": classified["filename"],
                        "asset_kind": "model_asset",
                        **classified,
                        "sha256": "UNKNOWN",
                        "size_bytes": "UNKNOWN",
                        "destination_evidence": destination_evidence,
                        "destination_status": destination_status,
                        "original_documented_action": original_action,
                        "evidence_location": dict(evidence),
                        "evidence": [evidence],
                        "provenance": {
                            "normalized_from": "documentation",
                            "preserves_original_install_or_dependency_evidence": True,
                        },
                    }
                    assets[url] = row
                elif evidence not in row["evidence"]:
                    row["evidence"].append(evidence)
                    if row["original_documented_action"] == "UNKNOWN" and original_action != "UNKNOWN":
                        row["original_documented_action"] = original_action
                        row["destination_evidence"] = destination_evidence
                        row["destination_status"] = destination_status
    return list(assets.values())


def _markdown_heading_starts(text: str) -> list[tuple[int, int, str, int]]:
    """Find ATX headings, excluding shell comments or other text inside fences."""
    starts: list[tuple[int, int, str, int]] = []
    fence_char = ""
    fence_length = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not fence_char:
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char, fence_length = "", 0
        elif not fence_char:
            heading = re.match(r"^(#{1,4})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$", line)
            if heading:
                starts.append((offset, offset + len(line), heading.group(2).strip(),
                               len(heading.group(1))))
        offset += len(line)
    return starts


def _semantic_blocks(section: str) -> tuple[list[str], bool]:
    """Split between prose/code blocks, never within a fenced code block."""
    blocks: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in section.splitlines(keepends=True):
        current.append(line)
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not fence_char:
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char, fence_length = "", 0
        if not fence_char and not line.strip():
            blocks.append("".join(current))
            current = []
    if current:
        blocks.append("".join(current))
    return blocks, not bool(fence_char)


def _section_chunks(section: str) -> tuple[list[str], bool, bool]:
    blocks, fences_complete = _semantic_blocks(section)
    output: list[str] = []
    current = ""
    oversized = False
    for block in blocks:
        if len(block) > MAX_CHUNK_CHARS:
            oversized = True
        if current and len(current) + len(block) > TARGET_CHUNK_CHARS:
            output.append(current)
            current = ""
        if len(block) > MAX_CHUNK_CHARS:
            # An atomic fenced block cannot safely be cut; omit it and surface
            # an incomplete-evidence quality state instead of truncating it.
            continue
        current += block
    if current:
        output.append(current)
    return output, fences_complete, oversized


def _split_evidence_chunk(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a failed section at complete numbered items or semantic blocks."""
    blocks, complete = _semantic_blocks(chunk["text"])
    if not complete:
        return []
    item_starts = [index for index, block in enumerate(blocks)
                   if re.match(r"^\s*(?:\*\*\d+[.)]|\d+[.)]\s)", block)]
    if len(item_starts) >= 2:
        boundaries = item_starts[1:]
    else:
        # Separate source sections only; a blank line alone may be inside one item.
        boundaries = [index for index, block in enumerate(blocks[1:], 1)
                      if re.match(r"^\s*#{1,4}\s+\S", block)]
    if not boundaries:
        return []
    # Group whole units in source order when the source has more than four.
    boundary_count = min(len(boundaries), MAX_EVIDENCE_SUBCHUNKS - 1)
    selected = [boundaries[(index * len(boundaries)) // boundary_count]
                for index in range(boundary_count)]
    offsets = [0]
    for boundary in selected:
        offsets.append(sum(len(block) for block in blocks[:boundary]))
    offsets.append(len(chunk["text"]))
    parts = []
    for index, (start, end) in enumerate(zip(offsets, offsets[1:])):
        part = dict(chunk)
        part["text"] = chunk["text"][start:end]
        part["start"] = chunk.get("start", 0) + start
        part["end"] = chunk.get("start", 0) + end
        part["char_count"] = end - start
        part["subchunk_id"] = f"{chunk['path']}:{chunk['start']}-{chunk['end']}:{index + 1}"
        parts.append(part)
    return parts


def _chunks(files: list[Path], root: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for path in files:
        text = _read(path)
        rel = path.relative_to(root).as_posix()
        if not text:
            continue
        starts = _markdown_heading_starts(text)
        sections: list[tuple[str, str, int, int, tuple[str, ...]]] = []
        if starts:
            if starts[0][0] > 0:
                sections.append(("Preamble", text[:starts[0][0]], 0, starts[0][0], ()))
            parents: list[tuple[int, str]] = []
            for index, (start, _line_end, heading, level) in enumerate(starts):
                end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
                while parents and parents[-1][0] >= level:
                    parents.pop()
                sections.append((heading, text[start:end], start, end,
                                 tuple(parent_heading for _, parent_heading in parents)))
                parents.append((level, heading))
        else:
            sections = [("UNKNOWN", text, 0, len(text), ())]
        for heading, section, section_start, section_end, parent_headings in sections:
            split, fences_complete, oversized = _section_chunks(section)
            cursor = section_start
            for part_index, part in enumerate(split):
                part_start = text.find(part, cursor, section_end)
                part_start = cursor if part_start < 0 else part_start
                part_end = part_start + len(part)
                cursor = part_end
                chunks.append({
                    "path": rel, "heading": heading, "parent_headings": parent_headings,
                    "text": part,
                    "start": part_start, "end": part_end, "char_count": len(part),
                    "fences_complete": fences_complete,
                    "semantic_complete": fences_complete and not oversized,
                    "section_start": section_start, "section_end": section_end,
                    "part_index": part_index,
                })

    install_heading = re.compile(
        r"\b(?:install(?:ation)?|setup|getting started|quick start|prerequisites?|requirements?)\b",
        re.I,
    )
    acquisition_heading = re.compile(
        r"\b(?:models?|checkpoints?|downloads?|onnxruntime|launch|run)\b", re.I
    )

    def priority(chunk: dict[str, Any]) -> int:
        path = Path(chunk["path"])
        is_readme = path.name.lower().startswith("readme")
        is_root_readme = is_readme and len(path.parts) == 1
        under_install = any(install_heading.search(parent)
                            for parent in chunk["parent_headings"])
        if is_root_readme and install_heading.search(chunk["heading"]):
            return 0
        if is_root_readme and under_install and re.search(r"\bwindows\b", chunk["heading"], re.I):
            return 1
        if is_root_readme and under_install and re.search(
                r"\b(?:from source|build from source|binaries|prebuilt)\b",
                chunk["heading"], re.I):
            return 2
        if is_root_readme and under_install:
            return 3
        if is_root_readme and acquisition_heading.search(chunk["heading"]):
            return 4
        if is_readme and (install_heading.search(chunk["heading"]) or under_install):
            return 5
        if is_readme and acquisition_heading.search(chunk["heading"]):
            return 6
        if is_root_readme:
            return 7
        if is_readme:
            return 8
        return 9

    return sorted(chunks, key=priority)[:MAX_MODEL_CHUNKS]


def _json_object_boundaries(text: str) -> tuple[list[str], str | None]:
    """Return complete top-level object substrings and any incomplete-tail error."""
    candidates: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if start is None:
            if char == "{":
                start = index
                depth = 1
                in_string = False
                escaped = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidates.append(text[start:index + 1])
                start = None
    if start is None:
        return candidates, None
    if in_string:
        return candidates, "Unterminated JSON string."
    return candidates, "Unbalanced JSON object braces."


def _json_response(text: str, diagnostic: dict[str, Any] | None = None) -> dict[str, Any]:
    candidate = text.strip()
    if diagnostic is not None:
        diagnostic["extracted_json_text"] = None
        diagnostic["response_shape"] = {
            "has_think_tags": bool(re.search(r"</?think\b", text, re.I)),
            "has_markdown_fence": "```" in text,
            "starts_with_json_object": candidate.startswith("{"),
            "ends_with_json_object": candidate.endswith("}"),
            "length": len(text),
        }
    objects, boundary_error = _json_object_boundaries(text)
    extraction_method = "pure_json" if len(objects) == 1 and objects[0] == candidate else "json_boundary_scan"
    if boundary_error or len(objects) != 1:
        if boundary_error:
            category = "malformed_json"
            message = boundary_error
        elif len(objects) > 1:
            category = "ambiguous_json"
            message = "Multiple competing top-level JSON objects were returned."
        else:
            category = "malformed_json"
            message = "No complete top-level JSON object was returned."
        if diagnostic is not None:
            diagnostic["parsed_object"] = None
            diagnostic.setdefault("validation_errors", []).append({
                "category": category, "message": message,
            })
        raise InstallResearchError(
            "LOCAL_AI_INVALID_RESPONSE", "Local AI Companion returned invalid structured output."
        )
    extracted = objects[0]
    if diagnostic is not None:
        diagnostic["extracted_json_text"] = extracted
    try:
        value = json.loads(extracted)
    except (ValueError, TypeError) as exc:
        if diagnostic is not None:
            diagnostic["parsed_object"] = None
            diagnostic.setdefault("validation_errors", []).append({
                "category": "malformed_json", "message": str(exc),
                "line": getattr(exc, "lineno", None), "column": getattr(exc, "colno", None),
                "position": getattr(exc, "pos", None),
            })
        raise InstallResearchError(
            "LOCAL_AI_INVALID_RESPONSE", "Local AI Companion returned invalid structured output."
        ) from exc
    if not isinstance(value, dict):
        if diagnostic is not None:
            diagnostic["parsed_object"] = value
            diagnostic.setdefault("validation_errors", []).append({
                "category": "wrong_type", "message": "Top-level JSON value is not an object.",
                "actual_type": type(value).__name__,
            })
        raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Local AI Companion output was not an object.")
    if diagnostic is not None:
        diagnostic["parsed_object"] = value
        diagnostic["extraction_method"] = extraction_method
    return value


def _write_ai_debug(debug: dict[str, Any]) -> None:
    AI_DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
    AI_DEBUG_FILE.write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")


def _prompt(chunk: dict[str, str]) -> str:
    return """You are the Local AI Companion installation research extractor. Return JSON only.
Never invent, rewrite, infer an alternative, or execute anything. Use UNKNOWN for missing facts.
Every claim must include evidence with path, heading, exact_text copied verbatim from SOURCE, confidence, evidence_type.
Extract only these optional arrays: install_methods, dependencies, models, system_dependencies, risks, unknowns; and optional objects python_environment, gpu_runtime, launch.
An install method has name, os, classification (official/inferred/community), exact_commands (verbatim array), prerequisites, evidence.
Dependencies have package, version, source, mandatory, evidence. Python environment has version, environment_manager, package_installer, evidence.
GPU/runtime has pytorch, torchvision, cuda, version, wheel_tag, flashinfer, triton, xformers, compiler, evidence.
Launch has exact_command, port, ui, verification, evidence. Every non-UNKNOWN launch value must appear verbatim in its cited evidence; never derive or synthesize a localhost URL or port.
This is research only. Do not propose executable shell steps.

SOURCE PATH: {path}
SOURCE HEADING: {heading}
SOURCE:
{text}
""".format(**chunk)


def _subchunk_prompt(chunk: dict[str, str]) -> str:
    return _prompt(chunk).replace(
        'Use UNKNOWN for missing facts.',
        'This is a partial result for the current SOURCE only. Output only claims directly '
        'supported by this SOURCE. Every output claim must have at least one evidence item. '
        'Omit any category or claim that this SOURCE does not support; do not output a claim '
        'with evidence=[] and do not fill unsupported fields or categories with placeholders '
        'to complete the research structure. Keep the JSON minimal: include only supported '
        'facts and the shortest contiguous verbatim evidence needed for those facts.',
        1,
    ).replace(
        "SOURCE PATH:",
        'For every evidence item, confidence must be a JSON string and must be exactly '
        '"HIGH", "MEDIUM", or "LOW". Never output a number for confidence.\n'
        'For every evidence item, exact_text must be one contiguous, character-for-character '
        'substring of SOURCE. Preserve newlines, indentation, and spaces exactly. Never join '
        'lines, remove a newline, change indentation or spaces, rewrite a command, or stitch '
        'non-contiguous text together. Prefer short contiguous excerpts instead of copying '
        'long command blocks. If a claim needs support from multiple places, use multiple '
        'separate evidence items; each exact_text must independently occur verbatim in SOURCE.\n\n'
        'SOURCE PATH:',
        1,
    )


def _valid_evidence(item: Any, source_texts: dict[str, str]) -> bool:
    if not isinstance(item, dict):
        return False
    path, exact = str(item.get("path") or ""), str(item.get("exact_text") or "")
    return bool(path in source_texts and exact and exact in source_texts[path])


def _claim_supported(claim: Any, source_texts: dict[str, str]) -> bool:
    if not isinstance(claim, dict):
        return False
    evidence = claim.get("evidence")
    # The local structured model sometimes emits the singular, schema-equivalent
    # evidence object instead of a one-item array.  Canonicalize that shape before
    # applying the unchanged verbatim-source and command-containment checks.
    if isinstance(evidence, dict):
        evidence = [evidence]
        claim["evidence"] = evidence
    if not isinstance(evidence, list) or not evidence or not all(_valid_evidence(x, source_texts) for x in evidence):
        return False
    commands = claim.get("exact_commands", [])
    cited = "\n".join(str(x.get("exact_text", "")) for x in evidence if isinstance(x, dict))
    return not commands or (isinstance(commands, list) and all(str(cmd) in cited for cmd in commands))


def _ground_install_method_commands(claim: Any, source_texts: dict[str, str]) -> bool:
    """Ground a model's method in exact commands when its combined quote is non-contiguous."""
    if not isinstance(claim, dict):
        return False
    commands = claim.get("exact_commands")
    evidence = claim.get("evidence")
    if not isinstance(commands, list) or not commands or not isinstance(evidence, list) or not evidence:
        return False
    paths = {str(item.get("path") or "") for item in evidence if isinstance(item, dict)}
    if len(paths) != 1:
        return False
    path = next(iter(paths))
    source = source_texts.get(path, "")
    if not source or not all(isinstance(command, str) and command and command in source for command in commands):
        return False
    heading = str(next((item.get("heading") for item in evidence
                        if isinstance(item, dict) and item.get("heading")), "UNKNOWN"))
    claim["evidence"] = [{
        "path": path,
        "heading": heading,
        "exact_text": command,
        "confidence": "HIGH",
        "evidence_type": "verbatim_command",
    } for command in commands]
    return True


_OBJECT_FIELDS = {
    "python_environment": ("version", "environment_manager", "package_installer"),
    "gpu_runtime": (
        "pytorch", "torchvision", "cuda", "version", "wheel_tag",
        "flashinfer", "triton", "xformers", "compiler",
    ),
    "launch": ("exact_command", "port", "ui", "verification"),
}


def _canonical_object_claim(field: str, claim: dict[str, Any]) -> dict[str, Any]:
    canonical = dict(claim)
    if field == "python_environment" and "manager" in canonical:
        manager = str(canonical.pop("manager") or "UNKNOWN")
        if manager.lower() in {"conda", "mamba", "micromamba", "venv", "virtualenv"}:
            canonical.setdefault("environment_manager", manager)
        elif manager.lower() in {"pip", "poetry", "uv", "pipenv"}:
            canonical.setdefault("package_installer", manager)
    if field == "launch":
        if "command" in canonical and "exact_command" not in canonical:
            canonical["exact_command"] = canonical["command"]
        if "url" in canonical and "ui" not in canonical:
            canonical["ui"] = canonical["url"]
    return canonical


def _launch_value_supported(key: str, value: Any, claim: dict[str, Any]) -> bool:
    if not _known(value):
        return False
    cited = "\n".join(
        str(item.get("exact_text", ""))
        for item in claim.get("evidence", []) if isinstance(item, dict)
    )
    candidate = str(value)
    if key == "port":
        return bool(re.search(
            rf"(?i)(?:--port(?:\s+|=)|\bport\s*[:=]?\s*){re.escape(candidate)}\b",
            cited,
        ))
    return candidate in cited


def _object_value_supported(value: Any, claim: dict[str, Any]) -> bool:
    cited = "\n".join(
        str(item.get("exact_text", ""))
        for item in claim.get("evidence", []) if isinstance(item, dict)
    )
    candidate = str(value)
    left = r"(?<![A-Za-z0-9._-])" if candidate[0].isalnum() else ""
    right = r"(?![A-Za-z0-9._-])" if candidate[-1].isalnum() else ""
    return bool(re.search(left + re.escape(candidate) + right, cited, re.I))


def _model_claim_fields_supported(field: str, claim: dict[str, Any]) -> bool:
    """Bind executable model fields to the cited words, not just a real quote."""
    cited = "\n".join(
        str(item.get("exact_text", ""))
        for item in claim.get("evidence", []) if isinstance(item, dict)
    )
    if field == "dependencies":
        package = str(claim.get("package") or claim.get("name") or "").strip()
        if not package or not re.search(
            rf"(?<![A-Za-z0-9._-]){re.escape(package)}(?![A-Za-z0-9._-])", cited, re.I
        ):
            return False
        version = str(claim.get("version") or "").strip()
        prefix = r"(?:==\s*)?" if re.fullmatch(r"[0-9][A-Za-z0-9.*+!_-]*", version) else ""
        if _known(version) and not re.search(
            rf"(?<![A-Za-z0-9._-]){re.escape(package)}(?:\[[^]\n]*\])?\s*{prefix}{re.escape(version)}(?![A-Za-z0-9.*+!_-])",
            cited, re.I,
        ):
            return False
    if field == "models":
        for key in ("url", "sha256", "revision", "commit", "relative_destination",
                    "destination_path", "size_bytes", "expected_size_bytes"):
            value = claim.get(key)
            if _known(value) and str(value) not in cited:
                return False
        filename = claim.get("filename")
        if _known(filename) and str(filename) not in cited:
            return False
        if str(claim.get("revision_kind") or "").lower() == "immutable":
            revision = str(claim.get("revision") or claim.get("commit") or "")
            if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
                return False
    return True


def _method_is_self_tool_usage(claim: dict[str, Any], identity: dict[str, Any]) -> bool:
    """A project's CLI installing another package is usage, not its own install method."""
    repository = str(identity.get("repository") or identity.get("repo") or "")
    project = repository.rsplit("/", 1)[-1].removesuffix(".git").casefold().replace("_", "-")
    if not project:
        return False
    for command in claim.get("exact_commands", []):
        match = re.match(
            r"^\s*(?:(?:py|python\d?(?:\.\d+)?)\s+-m\s+)?"
            r"(?:\S*[\\/])?([A-Za-z0-9_.-]+)\s+(install|run|add)\s+(.+)$",
            str(command), re.I,
        )
        if not match:
            continue
        executable = match.group(1).casefold().removesuffix(".exe").replace("_", "-")
        if executable != project:
            continue
        arguments = re.findall(r"[A-Za-z0-9_.-]+", match.group(3).casefold().replace("_", "-"))
        if project not in arguments:
            return True
    return False


def _merge_ai(result: dict[str, Any], payload: dict[str, Any], source_texts: dict[str, str]) -> None:
    for field in ("install_methods", "dependencies", "models", "system_dependencies", "risks"):
        for claim in payload.get(field, []) if isinstance(payload.get(field, []), list) else []:
            supported = _claim_supported(claim, source_texts)
            if field == "install_methods" and not supported:
                supported = _ground_install_method_commands(claim, source_texts)
            if supported and field == "install_methods" and _method_is_self_tool_usage(
                claim, result.get("source_identity", {})
            ):
                supported = False
            if supported and field in {"dependencies", "system_dependencies", "models"}:
                supported = _model_claim_fields_supported(
                    "dependencies" if field == "system_dependencies" else field, claim
                )
            if supported:
                if field in {"dependencies", "system_dependencies"}:
                    # A model cannot infer required/optional status from a cited token.
                    # Deterministic declarations retain their own authoritative status.
                    claim = dict(claim, mandatory="UNKNOWN")
                result[field].append(claim)
            else:
                result["diagnostics"]["rejected_claims"].append({"field": field, "claim": claim})
    for field in ("python_environment", "gpu_runtime", "launch"):
        claim = payload.get(field)
        if claim and _claim_supported(claim, source_texts):
            claim = _canonical_object_claim(field, claim)
            current = result[field]
            accepted = False
            for key in _OBJECT_FIELDS[field]:
                value = claim.get(key)
                if not _known(value):
                    continue
                if not (_launch_value_supported(key, value, claim) if field == "launch"
                        else _object_value_supported(value, claim)):
                    result["diagnostics"]["rejected_claims"].append({
                        "field": f"{field}.{key}", "claim": claim,
                    })
                    continue
                if not _known(current.get(key)) and _known(value):
                    current[key] = value
                    accepted = True
            if accepted:
                _merge_evidence(current, claim)
        elif claim:
            result["diagnostics"]["rejected_claims"].append({"field": field, "claim": claim})
    for item in payload.get("unknowns", []) if isinstance(payload.get("unknowns", []), list) else []:
        row = item if isinstance(item, dict) else {"fact": str(item), "reason": "Not documented"}
        if _meaningful_unknown(row):
            result["unknowns"].append(row)


def _confidence_issues(payload: dict[str, Any], subchunk_id: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for field in ("install_methods", "dependencies", "models", "system_dependencies", "risks",
                  "python_environment", "gpu_runtime", "launch"):
        value = payload.get(field, [] if field in (
            "install_methods", "dependencies", "models", "system_dependencies", "risks") else {})
        claims = value if isinstance(value, list) else [value] if isinstance(value, dict) and value else []
        for claim_index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            evidence = claim.get("evidence")
            items = [evidence] if isinstance(evidence, dict) else evidence if isinstance(evidence, list) else []
            for evidence_index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                confidence = item.get("confidence")
                if not isinstance(confidence, str) or not confidence:
                    issues.append({
                        "id": f"{subchunk_id}:{field}:{claim_index}:{evidence_index}",
                        "subchunk_id": subchunk_id, "field": field,
                        "claim_index": claim_index, "evidence_index": evidence_index,
                        "claim": str(claim.get("name") or claim.get("package") or
                                     claim.get("fact") or field),
                        "path": item.get("path"), "exact_text": item.get("exact_text"),
                        "invalid_value": confidence,
                    })
    return issues


def _normalize_empty_subchunk_categories(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    optional_categories = ("python_environment", "gpu_runtime", "launch")
    removed = [field for field in optional_categories
               if type(payload.get(field)) is dict and payload[field] == {}]
    return {field: value for field, value in payload.items() if field not in removed}, removed


def _validate_subchunk_payload(payload: dict[str, Any], identity: dict[str, Any],
                               chunk: dict[str, Any], *, review_confidence: bool = False) -> None:
    arrays = {"install_methods", "dependencies", "models", "system_dependencies", "risks"}
    objects = {"python_environment", "gpu_runtime", "launch"}
    if set(payload) - arrays - objects - {"unknowns"}:
        raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk JSON has unexpected fields.")
    for field, value in payload.items():
        if field in arrays and (not isinstance(value, list)
                                or not all(isinstance(item, dict) for item in value)):
            raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk JSON has an invalid claim array.")
        if field in objects and not isinstance(value, dict):
            raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk JSON has an invalid claim object.")
        if field in objects and not any(
            _known(_canonical_object_claim(field, value).get(key))
            for key in _OBJECT_FIELDS[field]
        ):
            raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk JSON has an empty claim object.")
        if field == "unknowns" and not isinstance(value, list):
            raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk JSON has invalid unknowns.")
    claims = [claim for field in arrays for claim in payload.get(field, [])]
    claims += [payload[field] for field in objects if payload.get(field)]
    for claim in claims:
        evidence = claim.get("evidence")
        evidence = [evidence] if isinstance(evidence, dict) else evidence
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not item[key]
                    for key in (("path", "heading", "exact_text", "evidence_type")
                                if review_confidence else
                                ("path", "heading", "exact_text", "confidence", "evidence_type"))
                ) for item in evidence)):
            raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk claim has invalid evidence.")
        if not all(_valid_evidence(item, {chunk["path"]: chunk["text"]})
                   for item in evidence):
            raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk claim cites absent evidence.")
    check = _empty_result(identity)
    _merge_ai(check, payload, {chunk["path"]: chunk["text"]})
    if check["diagnostics"]["rejected_claims"]:
        raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk claim failed provenance validation.")


def _subchunk_quote_failures(payload: dict[str, Any], identity: dict[str, Any],
                             chunk: dict[str, Any]) -> list[tuple[str, int, list[int]]]:
    """Identify quote-only failures without relaxing the existing validator."""
    try:
        _validate_subchunk_payload(payload, identity, chunk)
        return []
    except InstallResearchError as exc:
        if str(exc) != "Subchunk claim cites absent evidence.":
            raise
    probe = copy.deepcopy(payload)
    failures: list[tuple[str, int, list[int]]] = []
    for field in ("install_methods", "dependencies", "models", "system_dependencies",
                  "risks", "python_environment", "gpu_runtime", "launch"):
        value = probe.get(field)
        claims = value if isinstance(value, list) else [value] if isinstance(value, dict) and value else []
        for claim_index, claim in enumerate(claims):
            evidence = claim.get("evidence")
            items = [evidence] if isinstance(evidence, dict) else evidence
            bad_indices = []
            for evidence_index, item in enumerate(items):
                if item["path"] != chunk["path"]:
                    raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                               "Subchunk claim cites absent evidence.")
                if item["exact_text"] not in chunk["text"]:
                    bad_indices.append(evidence_index)
                    item["exact_text"] = chunk["text"]
            if bad_indices:
                failures.append((field, claim_index, bad_indices))
    # All other schema, path, claim, and provenance rules must already pass.
    _validate_subchunk_payload(probe, identity, chunk)
    return failures


def _provenance_repair_candidates(source: str) -> dict[str, str]:
    code_lines: list[str] = []
    command_lines: list[str] = []
    prose_lines: list[str] = []
    in_fence = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not stripped or len(stripped) > 240 or stripped.startswith("#"):
            continue
        if in_fence:
            code_lines.append(stripped)
        elif re.match(r"^(?:\$\s*)?(?:pip3?|python3?|uv|conda|git|wget|curl|bash|sh|npm|npx|docker|make|cmake|apt(?:-get)?|brew)\b", stripped):
            command_lines.append(stripped)
        else:
            prose_lines.append(stripped)
    candidates: dict[str, str] = {}
    for quote in code_lines + command_lines + prose_lines:
        if quote in source and quote not in candidates.values():
            candidates[f"C{len(candidates) + 1}"] = quote
        if len(candidates) == 32:
            break
    return candidates


def _provenance_repair_prompt(chunk: dict[str, Any], claim: dict[str, Any],
                              evidence: dict[str, Any], candidates: dict[str, str]) -> str:
    return (
        "Choose one candidate that directly supports the original claim. "
        "Do not regenerate the claim, change its text or path, or add facts absent from SOURCE. "
        "Return only one candidate ID (for example C1), with no quote, JSON, markdown, or explanation. "
        "Do not copy or rewrite the candidate text.\n"
        f"SOURCE PATH: {chunk['path']}\nSOURCE:\n{chunk['text']}\n"
        f"ORIGINAL CLAIM: {json.dumps(claim, ensure_ascii=False)}\n"
        f"ORIGINAL PATH: {evidence['path']}\n"
        f"ORIGINAL EXACT_TEXT: {evidence['exact_text']}\n"
        "CANDIDATES:\n" + "\n".join(
            f"{candidate_id}: {json.dumps(quote, ensure_ascii=False)}"
            for candidate_id, quote in candidates.items()) + "\n"
    )


def _repair_subchunk_quotes(payload: dict[str, Any], identity: dict[str, Any],
                            chunk: dict[str, Any], row: dict[str, Any],
                            store: product_cli.JobStore, ai_debug: dict[str, Any],
                            diagnostic: dict[str, Any]) -> dict[str, Any]:
    failures = _subchunk_quote_failures(payload, identity, chunk)
    if not failures:
        return payload
    if (diagnostic.get("http_status") != 200 or
            diagnostic.get("response_metadata", {}).get("finish_reason") != "stop"):
        raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                   "Subchunk quote repair requires HTTP 200 and finish_reason=stop.")
    repaired = copy.deepcopy(payload)
    row["provenance_repair_attempted"] = True
    row["provenance_repair_candidates"] = []
    row["selected_candidate_id"] = []
    row["original_exact_text"] = []
    row["repaired_exact_text"] = []
    row["repair_result"] = "FAILED"
    for field, claim_index, bad_indices in failures:
        value = repaired[field]
        claim = value[claim_index] if isinstance(value, list) else value
        original_value = payload[field]
        original_claim = (original_value[claim_index] if isinstance(original_value, list)
                          else original_value)
        evidence = claim["evidence"]
        items = [evidence] if isinstance(evidence, dict) else evidence
        original = {i: items[i]["exact_text"] for i in bad_indices}
        row["original_exact_text"].append({"field": field, "claim_index": claim_index,
                                            "values": original})
        by_index: dict[int, str] = {}
        row["repaired_exact_text"].append({"field": field, "claim_index": claim_index,
                                            "values": by_index})
        for evidence_index in bad_indices:
            item = items[evidence_index]
            original_evidence = original_claim["evidence"]
            if isinstance(original_evidence, list):
                original_evidence = original_evidence[evidence_index]
            repair_diagnostic: dict[str, Any] = {
                "subchunk_id": chunk["subchunk_id"], "repair_field": field,
                "repair_claim_index": claim_index, "repair_evidence_index": evidence_index,
                "validation_errors": [],
            }
            candidates = _provenance_repair_candidates(chunk["text"])
            row["provenance_repair_candidates"].append({
                "field": field, "claim_index": claim_index,
                "evidence_index": evidence_index, "candidates": candidates,
            })
            if not candidates:
                raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                           "Subchunk quote repair has no source candidates.")
            ai_debug["chunks_sent"].append(repair_diagnostic)
            answer = invoke_local_model(
                _provenance_repair_prompt(chunk, original_claim, original_evidence, candidates),
                store.path("local_ai_audit.jsonl"), stage="INSTALL_RESEARCH",
                think=False, diagnostic=repair_diagnostic, num_ctx=4096,
                max_output_tokens=INSTALL_RESEARCH_MAX_OUTPUT_TOKENS)
            if (repair_diagnostic.get("http_status") != 200 or
                    repair_diagnostic.get("response_metadata", {}).get("finish_reason") != "stop"):
                raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                           "Subchunk quote repair did not finish normally.")
            selected_id = answer.strip()
            if selected_id not in candidates:
                raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                           "Subchunk quote repair returned invalid candidate ID.")
            row["selected_candidate_id"].append({
                "field": field, "claim_index": claim_index,
                "evidence_index": evidence_index, "candidate_id": selected_id,
            })
            quote = candidates[selected_id]
            by_index[evidence_index] = quote
            item["exact_text"] = quote
            if not _valid_evidence(item, {chunk["path"]: chunk["text"]}):
                raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                           "Subchunk quote repair still cites absent evidence.")
    _validate_subchunk_payload(repaired, identity, chunk)
    row["repair_result"] = "SUCCESS"
    return repaired


def _recover_length_chunk(chunk: dict[str, Any], index: int, result: dict[str, Any],
                          store: product_cli.JobStore, ai_debug: dict[str, Any],
                          call_debug: dict[str, Any]) -> None:
    parts = _split_evidence_chunk(chunk)
    split = {
        "split_trigger": "OPENAI_COMPATIBLE_LENGTH_RESPONSE",
        "original_chunk_id": f"{chunk['path']}:{chunk['start']}-{chunk['end']}",
        "subchunk_count": len(parts),
        "subchunk_ids": [part["subchunk_id"] for part in parts],
        "per_subchunk_result": [], "merge_result": "PENDING",
    }
    result["diagnostics"].setdefault("evidence_splits", []).append(split)
    call_debug["evidence_split"] = split
    store.save(RESEARCH_FILE, result)
    _write_ai_debug(ai_debug)
    if len(parts) < 2:
        split["merge_result"] = {"status": "FAILED", "reason": "NO_SAFE_EVIDENCE_BOUNDARY"}
        raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Truncated chunk has no safe evidence boundary.")
    accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    review_issues: list[dict[str, Any]] = []
    for part in parts:
        row: dict[str, Any] = {"subchunk_id": part["subchunk_id"], "status": "PENDING",
                               "normalized_empty_categories": [],
                               "provenance_repair_attempted": False,
                               "provenance_repair_candidates": [], "selected_candidate_id": [],
                               "original_exact_text": [], "repaired_exact_text": [],
                               "repair_result": "NOT_NEEDED"}
        split["per_subchunk_result"].append(row)
        diagnostic: dict[str, Any] = {
            "index": index, "subchunk_id": part["subchunk_id"],
            "path": part["path"], "heading": part["heading"], "text": part["text"],
            "start": part["start"], "end": part["end"],
            "prompt_length": len(_subchunk_prompt(part)), "validation_errors": [],
        }
        ai_debug["chunks_sent"].append(diagnostic)
        _write_ai_debug(ai_debug)
        try:
            answer = invoke_local_model(_subchunk_prompt(part), store.path("local_ai_audit.jsonl"),
                                        stage="INSTALL_RESEARCH", think=False,
                                        diagnostic=diagnostic, num_ctx=4096,
                                        max_output_tokens=INSTALL_RESEARCH_MAX_OUTPUT_TOKENS)
            row.update(http_status=diagnostic.get("http_status"),
                       finish_reason=diagnostic.get("response_metadata", {}).get("finish_reason"),
                       response_length=len(diagnostic.get("raw_response") or ""))
            if row["finish_reason"] == "length":
                raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE", "Subchunk output reached generation length.")
            payload, normalized = _normalize_empty_subchunk_categories(
                _json_response(answer, diagnostic))
            row["normalized_empty_categories"].extend(normalized)
            issues = _confidence_issues(payload, part["subchunk_id"])
            if issues:
                _validate_subchunk_payload(payload, result["source_identity"], part,
                                           review_confidence=True)
                retry_diagnostic = dict(diagnostic)
                retry_diagnostic["validation_errors"] = []
                ai_debug["chunks_sent"].append(retry_diagnostic)
                retry_answer = invoke_local_model(
                    _subchunk_prompt(part), store.path("local_ai_audit.jsonl"),
                    stage="INSTALL_RESEARCH", think=False,
                    diagnostic=retry_diagnostic, num_ctx=4096,
                    max_output_tokens=INSTALL_RESEARCH_MAX_OUTPUT_TOKENS)
                row["confidence_retry"] = {
                    "http_status": retry_diagnostic.get("http_status"),
                    "finish_reason": retry_diagnostic.get("response_metadata", {}).get("finish_reason"),
                }
                if row["confidence_retry"]["finish_reason"] == "length":
                    raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                               "Subchunk confidence retry reached generation length.")
                payload, normalized = _normalize_empty_subchunk_categories(
                    _json_response(retry_answer, retry_diagnostic))
                row["normalized_empty_categories"].extend(
                    field for field in normalized
                    if field not in row["normalized_empty_categories"])
                issues = _confidence_issues(payload, part["subchunk_id"])
                if issues:
                    _validate_subchunk_payload(payload, result["source_identity"], part,
                                               review_confidence=True)
                else:
                    payload = _repair_subchunk_quotes(
                        payload, result["source_identity"], part, row, store,
                        ai_debug, retry_diagnostic)
                row["confidence_retry"]["result"] = "NEEDS_REVIEW" if issues else "SUCCESS"
            else:
                payload = _repair_subchunk_quotes(
                    payload, result["source_identity"], part, row, store,
                    ai_debug, diagnostic)
            accepted.append((part, payload))
            review_issues.extend(issues)
            row["status"] = "NEEDS_REVIEW" if issues else "SUCCESS"
        except Exception as exc:
            row.update(status="FAILED", error=str(exc))
            if isinstance(exc, LocalModelHTTPError):
                row["http_status"] = exc.status
            split["merge_result"] = {"status": "FAILED", "reason": "SUBCHUNK_FAILED"}
            _write_ai_debug(ai_debug)
            raise
        _write_ai_debug(ai_debug)
    if review_issues:
        review = result.setdefault("confidence_review", {"allowed_values": list(CONFIDENCE_CHOICES),
                                                        "issues": [], "pending_splits": []})
        review["issues"].extend(review_issues)
        review["pending_splits"].append({
            "original_chunk_id": split["original_chunk_id"],
            "subchunks": [{"path": part["path"], "heading": part["heading"],
                           "start": part["start"], "end": part["end"],
                           "subchunk_id": part["subchunk_id"],
                           "text_sha256": hashlib.sha256(part["text"].encode("utf-8")).hexdigest(),
                           "payload": payload} for part, payload in accepted],
        })
        split["merge_result"] = {"status": "NEEDS_REVIEW", "reason": "INVALID_CONFIDENCE"}
        _write_ai_debug(ai_debug)
        return
    _merge_subchunks(result, accepted)
    split["merge_result"] = {"status": "SUCCESS", "validated_subchunks": len(accepted)}
    _write_ai_debug(ai_debug)


def _merge_subchunks(result: dict[str, Any], accepted: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    for part, payload in accepted:
        _merge_ai(result, payload, {part["path"]: part["text"]})
        for field in ("python_environment", "gpu_runtime", "launch"):
            claim = payload.get(field)
            if claim and any(_known(claim.get(key)) and claim.get(key) == result[field].get(key)
                             for key in _OBJECT_FIELDS[field]):
                _merge_evidence(result[field], claim)


def _known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "unknown", "not documented")
    return True


def _meaningful_unknown(item: Any) -> bool:
    if not isinstance(item, dict):
        return _known(item)
    return _known(item.get("fact")) or _known(item.get("reason"))


def _compatible_python_versions(first: Any, second: Any) -> tuple[bool, str]:
    left = str(first).strip()
    right = str(second).strip()
    exact_pattern = r"\d+(?:\.\d+){1,2}"
    exact_left = re.fullmatch(exact_pattern, left)
    exact_right = re.fullmatch(exact_pattern, right)
    constraint_left = re.fullmatch(rf"(>=|<=|>|<|==)\s*({exact_pattern})", left)
    constraint_right = re.fullmatch(rf"(>=|<=|>|<|==)\s*({exact_pattern})", right)
    if bool(exact_left) == bool(exact_right):
        return False, left
    exact = left if exact_left else right
    constraint = constraint_right if exact_left else constraint_left
    if constraint is None:
        return False, left

    def version_tuple(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split("."))

    actual = version_tuple(exact)
    boundary = version_tuple(constraint.group(2))
    width = max(len(actual), len(boundary))
    actual += (0,) * (width - len(actual))
    boundary += (0,) * (width - len(boundary))
    operator = constraint.group(1)
    compatible = {
        ">=": actual >= boundary, "<=": actual <= boundary,
        ">": actual > boundary, "<": actual < boundary, "==": actual == boundary,
    }[operator]
    return compatible, exact if compatible else left


def _merge_evidence(target: dict[str, Any], source: dict[str, Any]) -> None:
    evidence = target.setdefault("evidence", [])
    for item in source.get("evidence", []):
        if item not in evidence:
            evidence.append(item)


def _reconcile_launch_port(result: dict[str, Any]) -> None:
    launch = result.get("launch", {})
    if _known(launch.get("port")):
        return
    ports: set[int] = set()
    for item in launch.get("evidence", []):
        if not isinstance(item, dict):
            continue
        for match in re.finditer(r"(?i)\bhttps?://localhost:(\d{1,5})(?=[/?#`)\]\s]|$)",
                                 str(item.get("exact_text", ""))):
            port = int(match.group(1))
            if 1 <= port <= 65535:
                ports.add(port)
    if len(ports) == 1:
        launch["port"] = str(ports.pop())


def _reconcile_system_dependencies(result: dict[str, Any]) -> None:
    environments = {
        str(row.get("scope") or "UNKNOWN_SCOPE"): row
        for row in result.get("python_environments", []) if isinstance(row, dict)
    }
    runtime = result.setdefault("gpu_runtime", {})
    retained: list[dict[str, Any]] = []
    environment_names = {"conda", "mamba", "micromamba", "venv", "virtualenv"}
    runtime_names = {"cuda", "pytorch", "torch", "torchvision"}

    for row in result.get("system_dependencies", []):
        name = str(row.get("name") or row.get("dependency") or row.get("package") or "")
        normalized_name = name.strip().casefold()
        if normalized_name in environment_names:
            for evidence in row.get("evidence", []):
                if not isinstance(evidence, dict):
                    continue
                scope = _python_environment_scope(str(evidence.get("path", "")))
                target = environments.get(scope) or environments.get("UNKNOWN_SCOPE")
                if target is not None:
                    _merge_evidence(target, {"evidence": [evidence]})
            continue
        if normalized_name in runtime_names:
            _merge_evidence(runtime, row)
            continue
        retained.append(dict(row))

    by_package = {
        str(row.get("name") or row.get("dependency") or row.get("package") or "").casefold(): row
        for row in retained
    }
    for method in result.get("install_methods", []):
        for command in method.get("exact_commands", []):
            identity = _semantic_command_identity(str(command))
            if not identity or identity[0] != "system-package-install":
                continue
            for package in identity[1]:
                target = by_package.get(package)
                if target is None:
                    target = {
                        "package": package, "version": "UNKNOWN", "source": "UNKNOWN",
                        "mandatory": "UNKNOWN", "evidence": [],
                    }
                    retained.append(target)
                    by_package[package] = target
                _merge_evidence(target, method)

    result["system_dependencies"] = retained
    primary = environments.get("CORE") or next(iter(environments.values()), None)
    if primary is not None:
        result["python_environment"] = dict(primary)
        result["python_environment"]["evidence"] = list(primary.get("evidence", []))


def _reconcile_deterministic(result: dict[str, Any], deterministic: dict[str, Any]) -> None:
    """Make accepted machine-readable declarations authoritative without hiding conflicts."""
    deterministic_rows = deterministic.get("dependencies", [])
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    known_packages = {
        str(row.get("package", "")).lower()
        for row in deterministic_rows if _known(row.get("package")) and _known(row.get("version"))
    }
    for row in deterministic_rows + result.get("dependencies", []):
        package = str(row.get("package", ""))
        version = str(row.get("version", ""))
        if package.lower() in known_packages and not _known(version):
            continue
        key = (package.lower(), version, str(row.get("scope") or "CORE"))
        if key in by_key:
            _merge_evidence(by_key[key], row)
            continue
        copy = dict(row)
        copy["evidence"] = list(row.get("evidence", []))
        by_key[key] = copy
        merged.append(copy)
    conflicts: list[dict[str, Any]] = []
    concrete: dict[str, list[dict[str, Any]]] = {}
    for row in merged:
        evidence = row.get("evidence") or []
        lockfile_only = bool(evidence) and all(
            isinstance(item, dict) and item.get("evidence_type") == "deterministic_lockfile"
            for item in evidence
        )
        if not lockfile_only and _known(row.get("package")) and _known(row.get("version")):
            concrete.setdefault(str(row["package"]).lower(), []).append(row)
    for package, rows in concrete.items():
        versions = sorted({str(row["version"]) for row in rows})
        if len(versions) > 1:
            for row in rows:
                row["conflict"] = True
                row["conflicting_versions"] = versions
            conflicts.append({
                "package": package, "versions": versions,
                "evidence": [item for row in rows for item in row.get("evidence", [])],
            })
    result["dependencies"] = merged

    deterministic_python = deterministic.get("python_environment", [])
    python_conflicts: list[dict[str, Any]] = []
    scoped_python: dict[str, dict[str, Any]] = {}
    for row in deterministic_python:
        normalized = _canonical_object_claim("python_environment", row)
        scope = str(normalized.get("scope") or "UNKNOWN_SCOPE").upper()
        if scope not in _PYTHON_ENVIRONMENT_SCOPES:
            scope = "UNKNOWN_SCOPE"
        authoritative = scoped_python.setdefault(scope, {
            "scope": scope, "version": "UNKNOWN", "environment_manager": "UNKNOWN",
            "package_installer": "UNKNOWN", "evidence": [],
        })
        for key in _OBJECT_FIELDS["python_environment"]:
            value = normalized.get(key)
            if not _known(value):
                continue
            if _known(authoritative.get(key)) and authoritative[key] != value:
                if key == "version":
                    compatible, preferred = _compatible_python_versions(authoritative[key], value)
                    if compatible:
                        authoritative[key] = preferred
                        continue
                python_conflicts.append({
                    "scope": scope, "field": key, "documented_value": authoritative[key],
                    "alternative_value": value, "evidence": normalized.get("evidence", []),
                })
            elif not _known(authoritative.get(key)):
                authoritative[key] = value
        _merge_evidence(authoritative, normalized)

    ai_python = _canonical_object_claim("python_environment", result.get("python_environment", {}))
    ai_paths = {
        str(item.get("path", "")) for item in ai_python.get("evidence", [])
        if isinstance(item, dict) and item.get("path")
    }
    ai_scopes = {_python_environment_scope(path) for path in ai_paths}
    if not deterministic_python or len(ai_scopes) == 1:
        ai_scope = next(iter(ai_scopes), "UNKNOWN_SCOPE")
        authoritative = scoped_python.setdefault(ai_scope, {
            "scope": ai_scope, "version": "UNKNOWN", "environment_manager": "UNKNOWN",
            "package_installer": "UNKNOWN", "evidence": [],
        })
        for key in _OBJECT_FIELDS["python_environment"]:
            value = ai_python.get(key)
            if _known(value) and not _known(authoritative.get(key)):
                authoritative[key] = value
            elif (_known(value) and _known(authoritative.get(key))
                  and value != authoritative[key]):
                if key == "version":
                    compatible, preferred = _compatible_python_versions(authoritative[key], value)
                    if compatible:
                        authoritative[key] = preferred
                        continue
                python_conflicts.append({
                    "scope": ai_scope, "field": key, "documented_value": authoritative[key],
                    "ai_value": value, "evidence": ai_python.get("evidence", []),
                })
        _merge_evidence(authoritative, ai_python)

    for conflict in python_conflicts:
        scoped_python[conflict["scope"]]["conflict"] = True
    result["python_environments"] = [
        scoped_python[scope] for scope in _PYTHON_ENVIRONMENT_SCOPES if scope in scoped_python
    ]
    primary_scope = "CORE" if "CORE" in scoped_python else next(
        (scope for scope in _PYTHON_ENVIRONMENT_SCOPES if scope in scoped_python), None
    )
    if primary_scope:
        result["python_environment"] = dict(scoped_python[primary_scope])
        result["python_environment"]["evidence"] = list(scoped_python[primary_scope]["evidence"])
    result["diagnostics"]["python_environment_conflicts"] = python_conflicts

    current_runtime = result.get("gpu_runtime", {})
    runtime_evidence = current_runtime.get("evidence") or []
    if (_known(current_runtime.get("cuda")) and runtime_evidence
            and all(isinstance(item, dict) and (
                "benchmark" in str(item.get("heading") or "").lower()
                or "benchmark" in str(item.get("path") or "").lower()
                or re.search(r"(?i)benchmarks?\s+(?:are\s+)?executed", str(item.get("exact_text") or ""))
            ) for item in runtime_evidence)):
        current_runtime["cuda"] = "UNKNOWN"
    deterministic_runtime = deterministic.get("gpu_runtime", {})
    runtime_conflicts: list[dict[str, Any]] = []
    if deterministic_runtime:
        current_runtime = result.get("gpu_runtime", {})
        for key in _OBJECT_FIELDS["gpu_runtime"]:
            documented = deterministic_runtime.get(key)
            previous = current_runtime.get(key)
            if not _known(documented):
                continue
            if _known(previous) and previous != documented:
                runtime_conflicts.append({
                    "field": key, "ai_value": previous,
                    "documented_value": documented,
                })
            current_runtime[key] = documented
        _merge_evidence(current_runtime, deterministic_runtime)
        result["gpu_runtime"] = current_runtime
    result["diagnostics"]["gpu_runtime_conflicts"] = runtime_conflicts

    _reconcile_launch_port(result)
    _reconcile_system_dependencies(result)

    removed_unknowns: list[dict[str, Any]] = []
    retained_unknowns: list[dict[str, Any]] = []
    runtime_terms = {
        "pytorch": "pytorch", "torch": "pytorch", "torchvision": "torchvision",
        "cuda": "cuda", "cu128": "cuda",
    }
    known_runtime_terms = {
        term for term, field in runtime_terms.items()
        if _known(deterministic_runtime.get(field))
    }
    known_environment_managers = {
        str(row.get("environment_manager", "")).casefold()
        for row in result.get("python_environments", [])
        if _known(row.get("environment_manager"))
    }
    known_package_installers = {
        str(row.get("package_installer", "")).casefold()
        for row in result.get("python_environments", [])
        if _known(row.get("package_installer"))
    }
    for item in result.get("unknowns", []):
        if not _meaningful_unknown(item):
            removed_unknowns.append(item)
            continue
        text = f"{item.get('fact', '')} {item.get('reason', '')}".lower() if isinstance(item, dict) else str(item).lower()
        contradicted = any(package and package in text and any(
            token in text for token in ("version", "unknown", "not documented", "not present")
        ) for package in known_packages)
        if deterministic_python and "python" in text and any(
                token in text for token in ("version", "unknown", "not documented", "not present")):
            contradicted = True
        if any(term in text for term in known_runtime_terms) and any(
                token in text for token in ("version", "unknown", "not documented", "not present")):
            contradicted = True
        unresolved_marker = any(token in text for token in (
            "unknown", "not documented", "not present", "details",
        ))
        manager_named = any(token in text for token in (
            "environment_manager", "environment manager", "conda", "mamba", "micromamba",
        ))
        installer_named = any(token in text for token in (
            "package_installer", "package installer", "pip", "poetry", "uv",
        ))
        if unresolved_marker and (
            (manager_named and known_environment_managers)
            or (installer_named and known_package_installers)
        ):
            contradicted = True
        if result.get("install_methods") and re.search(
                r"\bno (?:installation|install) (?:instructions?|methods?|commands?)\b", text):
            contradicted = True
        (removed_unknowns if contradicted else retained_unknowns).append(item)
    result["unknowns"] = retained_unknowns
    result["diagnostics"]["dependency_conflicts"] = conflicts
    result["diagnostics"]["removed_contradicted_unknowns"] = removed_unknowns
    result["diagnostics"]["reconciliation_completed"] = True


def _evidence_signature(row: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted({
        (str(item.get("path", "")), str(item.get("heading", "")), str(item.get("exact_text", "")))
        for item in row.get("evidence", []) if isinstance(item, dict)
    }))


def _semantic_command_identity(command: str) -> tuple[Any, ...]:
    normalized = re.sub(r"\s+#.*$", "", str(command)).strip().casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    system_install = re.match(
        r"^(?:sudo\s+)?(?:apt(?:-get)?|brew|dnf|yum|choco|winget)\s+install\s+(.+)$",
        normalized,
    )
    if system_install:
        packages = tuple(sorted(
            token.strip("'\";, ") for token in system_install.group(1).split()
            if token and not token.startswith("-")
        ))
        return ("system-package-install", packages)
    pip_install = re.match(r"^(?:(?:python|python3|py)\s+-m\s+)?pip3?\s+install\s+(.+)$", normalized)
    if pip_install:
        packages = tuple(sorted(
            token.strip("'\";, ") for token in pip_install.group(1).split()
            if token and not token.startswith("-")
        ))
        return ("python-package-install", packages)
    return ("exact", normalized)


def _command_identity(commands: tuple[str, ...]) -> tuple[tuple[Any, ...], ...]:
    return tuple(sorted({_semantic_command_identity(command) for command in commands}, key=repr))


def _merge_commands(target: dict[str, Any], source: dict[str, Any]) -> None:
    commands = target.setdefault("exact_commands", [])
    for command in source.get("exact_commands", []):
        if command not in commands:
            commands.append(command)


def _merge_method_provenance(target: dict[str, Any], source: dict[str, Any]) -> None:
    _merge_evidence(target, source)
    _merge_commands(target, source)
    systems = target.setdefault("operating_systems", [])
    for value in (target.get("os"), source.get("os")):
        if _known(value) and str(value).casefold() != "multiple" and value not in systems:
            systems.append(value)
    if len(systems) > 1:
        target["os"] = "multiple"


def _deduplicate_install_methods(methods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    positions: dict[tuple[Any, ...], int] = {}
    for method in methods:
        commands = tuple(dict.fromkeys(str(value) for value in method.get("exact_commands", [])))
        evidence = _evidence_signature(method)
        if commands:
            key = ("commands", _command_identity(commands))
        elif evidence:
            key = ("evidence", evidence)
        else:
            key = (
                "metadata",
                str(method.get("name", "")).strip().casefold(),
                str(method.get("os", "")).strip().casefold(),
                str(method.get("classification", "")).strip().casefold(),
            )
        if key in positions:
            _merge_method_provenance(unique[positions[key]], method)
            continue
        copy = dict(method)
        copy["exact_commands"] = list(commands)
        if _known(copy.get("os")):
            copy["operating_systems"] = [copy["os"]]
        positions[key] = len(unique)
        unique.append(copy)
    return unique


def _deduplicate_review_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    positions: dict[tuple[Any, ...], int] = {}
    for action in actions:
        commands = tuple(dict.fromkeys(str(value) for value in action.get("exact_commands", [])))
        evidence = _evidence_signature(action)
        action_type = str(action.get("type", "")).strip().casefold()
        if commands:
            key = ("commands", action_type, _command_identity(commands))
        elif evidence:
            key = ("evidence", action_type, evidence)
        else:
            key = ("description", action_type, str(action.get("description", "")).strip().casefold())
        if key in positions:
            _merge_evidence(unique[positions[key]], action)
            _merge_commands(unique[positions[key]], action)
            continue
        copy = dict(action)
        copy["exact_commands"] = list(commands)
        positions[key] = len(unique)
        unique.append(copy)
    return unique


def _reconcile_model_assets(result: dict[str, Any], assets: list[dict[str, Any]]) -> None:
    """Merge exact documented asset facts without promoting an inferred destination."""
    rows = [dict(row) for row in result.get("models", []) if isinstance(row, dict)]
    by_url = {
        str(row.get("url")): index
        for index, row in enumerate(rows)
        if _known(row.get("url"))
    }
    conflicts: list[dict[str, Any]] = []
    deterministic_keys = (
        "asset_kind", "url", "filename", "source_repository", "revision",
        "revision_kind", "destination_evidence", "destination_status",
        "original_documented_action", "evidence_location",
    )
    for asset in assets:
        url = str(asset["url"])
        if url not in by_url:
            copy = dict(asset)
            copy["evidence"] = list(asset.get("evidence", []))
            rows.append(copy)
            by_url[url] = len(rows) - 1
            continue
        current = rows[by_url[url]]
        for key in deterministic_keys:
            documented = asset.get(key)
            previous = current.get(key)
            if _known(previous) and _known(documented) and previous != documented:
                conflicts.append({
                    "url": url, "field": key, "ai_value": previous,
                    "documented_value": documented,
                })
            current[key] = documented
        if not _known(current.get("name")):
            current["name"] = asset["name"]
        for key in ("sha256", "size_bytes"):
            if not _known(current.get(key)):
                current[key] = asset[key]
        # The documentation did not establish an approved deterministic target.
        # Never retain a model-inferred planner destination for this same URL.
        if asset["destination_status"] != "EXPLICIT":
            for key in ("destination_category", "relative_destination", "destination_path"):
                if _known(current.get(key)):
                    conflicts.append({
                        "url": url, "field": key, "ai_value": current[key],
                        "documented_value": "UNKNOWN",
                    })
                current.pop(key, None)
        _merge_evidence(current, asset)
        current["provenance"] = dict(asset["provenance"])
    result["models"] = rows
    result["diagnostics"]["deterministic_model_assets"] = assets
    result["diagnostics"]["model_asset_conflicts"] = conflicts


def _source_identity(store: product_cli.JobStore) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = store.load("plan.json", {})
    state = store.load("execution_state.json", {})
    final = store.load("result.json", {})
    step = next((row for row in plan.get("steps", []) if row.get("type") == "ACQUIRE_GITHUB_SNAPSHOT"), None)
    completed = next((row for row in state.get("steps", []) if row.get("type") == "ACQUIRE_GITHUB_SNAPSHOT" and row.get("status") == "SUCCESS"), None)
    if not step or not completed:
        raise InstallResearchError("SOURCE_ACQUISITION_INCOMPLETE", "Verified source acquisition has not completed.")
    if (final.get("final_status") != "VERIFIED" or final.get("source_verified") is not True
            or final.get("install_verified") is not True):
        raise InstallResearchError("SOURCE_ACQUISITION_UNVERIFIED", "Source acquisition does not have a verified final result.")
    inputs, actual = step.get("inputs") or {}, completed.get("result") or {}
    for key in ("repository", "canonical_url", "exact_commit", "destination"):
        if not inputs.get(key) or actual.get(key) != inputs.get(key):
            raise InstallResearchError("SOURCE_IDENTITY_MISMATCH", f"Acquired source identity mismatch: {key}.")
    provenance = inputs.get("provenance")
    if (not isinstance(provenance, dict) or not provenance
            or plan.get("source_provenance") != provenance
            or (provenance.get("requested_repository")
                and provenance.get("requested_repository") != inputs["repository"])
            or (provenance.get("canonical_url")
                and provenance.get("canonical_url").casefold() != inputs["canonical_url"].casefold())):
        raise InstallResearchError("SOURCE_PROVENANCE_MISMATCH", "Persisted source provenance no longer matches the approved plan.")
    runtime_inputs = completed.get("inputs")
    if runtime_inputs and any(runtime_inputs.get(key) != inputs.get(key)
                              for key in ("repository", "canonical_url", "exact_commit", "destination", "provenance")):
        raise InstallResearchError("SOURCE_IDENTITY_MISMATCH", "Executed source inputs no longer match the approved plan.")
    root = Path(inputs["destination"]).resolve()
    if not root.is_dir():
        raise InstallResearchError("ACQUIRED_SOURCE_MISSING", "The acquired source directory is unavailable.")
    identity = {"repo": str(inputs["repository"]), "repository": str(inputs["repository"]),
                "canonical_url": str(inputs["canonical_url"]), "commit": str(inputs["exact_commit"]),
                "exact_commit": str(inputs["exact_commit"]), "local_path": str(root),
                "acquisition_step_id": str(completed.get("step_id") or step.get("step_id") or "UNKNOWN"),
                "acquisition_verified": True}
    return identity, completed


def find_completed_acquisition(jobs_root: Path, plan: dict[str, Any], *,
                               exclude_job_id: str | None = None) -> dict[str, Any] | None:
    """Find a persisted, still-available acquisition matching this exact plan target."""
    expected_step = next((row for row in plan.get("steps", [])
                          if row.get("type") == "ACQUIRE_GITHUB_SNAPSHOT"), None)
    expected = (expected_step or {}).get("inputs") or {}
    required = ("repository", "canonical_url", "exact_commit")
    if not all(expected.get(key) for key in required):
        return None
    expected_destination = (str(Path(expected["destination"]).resolve())
                            if expected.get("destination") else None)
    root = Path(jobs_root)
    if not root.is_dir():
        return None
    matches: list[dict[str, Any]] = []
    for path in sorted((row for row in root.iterdir() if row.is_dir()),
                       key=lambda row: row.stat().st_mtime, reverse=True):
        if path.name == exclude_job_id:
            continue
        store = product_cli.JobStore(root, path.name)
        try:
            identity, completed = _source_identity(store)
        except (InstallResearchError, OSError, ValueError):
            continue
        if (identity["repository"] == expected["repository"]
                and identity["canonical_url"] == expected["canonical_url"]
                and identity["exact_commit"] == expected["exact_commit"]
                and (expected_destination is None or identity["local_path"] == expected_destination)):
            matches.append({**identity, "job_id": path.name,
                            "acquisition_result": completed.get("result") or {}})
    by_destination = {row["local_path"]: row for row in matches}
    if len(by_destination) > 1:
        return {
            "status": "AMBIGUOUS",
            "error_code": "MULTIPLE_COMPLETED_ACQUISITIONS",
            "candidates": list(by_destination.values()),
        }
    return next(iter(by_destination.values()), None)


def analyze_job(jobs_root: Path, job_id: str, *,
                model_invoke: Callable[..., str] = invoke_local_model,
                progress: Callable[[str], None] | None = None,
                acquisition_job_id: str | None = None,
                expected_source_path: str | None = None) -> dict[str, Any]:
    store = product_cli.JobStore(Path(jobs_root), job_id)
    source_store = product_cli.JobStore(Path(jobs_root), acquisition_job_id or job_id)
    identity, completed = _source_identity(source_store)
    if acquisition_job_id and acquisition_job_id != job_id:
        current_plan = store.load("plan.json", {})
        expected_step = next((row for row in current_plan.get("steps", [])
                              if row.get("type") == "ACQUIRE_GITHUB_SNAPSHOT"), None)
        expected = (expected_step or {}).get("inputs") or {}
        comparisons = {
            "repository": identity["repository"], "canonical_url": identity["canonical_url"],
            "exact_commit": identity["exact_commit"],
        }
        if expected.get("destination"):
            comparisons["destination"] = identity["local_path"]
        for key, actual in comparisons.items():
            value = expected.get(key)
            if key == "destination" and value:
                value = str(Path(value).resolve())
            if value != actual:
                raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                           f"Acquired source identity mismatch: {key}.")
    if expected_source_path and str(Path(expected_source_path).resolve()) != identity["local_path"]:
        raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                   "Acquired source identity mismatch: destination.")
    result = _empty_result(identity)
    previous = store.load(RESEARCH_FILE, {})
    history = list(previous.get("attempt_history") or [])
    if (previous.get("source_identity") == identity
            and previous.get("status") in {"BLOCKED", "PARTIAL"}
            and previous.get("error_code")):
        history.append({key: previous.get(key) for key in (
            "status", "error_code", "error", "backend_error", "updated_at", "source_identity")})
    result["attempt_history"] = history
    result["source_acquisition_job_id"] = acquisition_job_id or job_id

    def update(stage: str) -> None:
        result["analysis_stage"] = stage
        result["updated_at"] = _now()
        store.save(RESEARCH_FILE, result)
        if progress:
            progress(stage)

    update("READING_PROJECT_DOCUMENTATION")
    root = Path(identity["local_path"])
    files = discover_research_files(root)
    result["diagnostics"]["files_inspected"] = [path.relative_to(root).as_posix() for path in files]
    result["source_fidelity"] = _source_fidelity(completed, root, files)
    if result["source_fidelity"]["review_required"]:
        result.update(status="BLOCKED", error_code="SOURCE_FIDELITY_REVIEW_REQUIRED",
                      error="Skipped archive members may affect installation evidence or package contents; review the source snapshot before planning.")
        result["unknowns"].append({"fact": "source snapshot completeness",
                                   "reason": "Installation-relevant or uncertain symlink members were not materialized."})
        update("SOURCE_FIDELITY_REVIEW_REQUIRED")
        return result
    if not files:
        result.update(status="BLOCKED", error_code="INSUFFICIENT_INSTALL_EVIDENCE")
        result["unknowns"].append({"fact": "installation method", "reason": "No supported documentation or manifest was found."})
        update("INSUFFICIENT_INSTALL_EVIDENCE")
        return result

    update("PARSING_MANIFESTS")
    deterministic = parse_deterministic(files, root)
    deterministic_assets = extract_model_asset_evidence(files, root)
    result["dependencies"] = deterministic["dependencies"]
    result["diagnostics"]["deterministic_manifests"] = deterministic["manifests"]
    chunks = _chunks(files, root)
    result["diagnostics"]["chunking"] = [{
        key: chunk.get(key) for key in (
            "path", "heading", "parent_headings", "start", "end", "char_count",
            "fences_complete", "semantic_complete", "section_start", "section_end", "part_index",
        )
    } for chunk in chunks]
    source_texts = {path.relative_to(root).as_posix(): _read(path) for path in files}
    ai_debug: dict[str, Any] = {
        "model": MODEL_NAME,
        "endpoint": f"{MODEL_ENDPOINT}/chat/completions",
        "job_id": job_id,
        "source_identity": identity,
        "source_files": [path.relative_to(root).as_posix() for path in files],
        "chunks_sent": [],
        "raw_response": None,
        "sanitized_response": None,
        "extracted_json_text": None,
        "parsed_object": None,
        "validation_errors": [],
    }
    _write_ai_debug(ai_debug)
    update("CONSULTING_LOCAL_ORNITH")
    try:
        for index, chunk in enumerate(chunks):
            prompt = _prompt(chunk)
            call_debug: dict[str, Any] = {
                "index": index, "path": chunk["path"], "heading": chunk["heading"],
                "text": chunk["text"], "prompt_length": len(prompt),
                "start": chunk.get("start"), "end": chunk.get("end"),
                "char_count": chunk.get("char_count"),
                "fences_complete": chunk.get("fences_complete"),
                "semantic_complete": chunk.get("semantic_complete"),
                "raw_response": None, "sanitized_response": None,
                "extracted_json_text": None,
                "parsed_object": None, "validation_errors": [],
            }
            ai_debug["chunks_sent"].append(call_debug)
            _write_ai_debug(ai_debug)
            payload: dict[str, Any] | None = None
            split_recovered = False
            try:
                if model_invoke is invoke_local_model:
                    answer = model_invoke(
                        prompt, store.path("local_ai_audit.jsonl"),
                        stage="INSTALL_RESEARCH", think=False, diagnostic=call_debug,
                        max_output_tokens=INSTALL_RESEARCH_MAX_OUTPUT_TOKENS)
                else:
                    answer = model_invoke(prompt, store.path("local_ai_audit.jsonl"),
                                          stage="INSTALL_RESEARCH", think=False)
                    call_debug["sanitized_response"] = answer
                ai_debug["raw_response"] = call_debug.get("raw_response")
                ai_debug["sanitized_response"] = call_debug.get("sanitized_response")
                try:
                    payload = _json_response(answer, call_debug)
                except InstallResearchError as parse_error:
                    malformed = any(error.get("category") == "malformed_json"
                                    for error in call_debug.get("validation_errors", []))
                    if (model_invoke is not invoke_local_model
                            or parse_error.code != "LOCAL_AI_INVALID_RESPONSE"
                            or call_debug.get("http_status") != 200
                            or call_debug.get("response_metadata", {}).get("finish_reason") != "length"
                            or not malformed):
                        raise
                    _recover_length_chunk(chunk, index, result, store, ai_debug, call_debug)
                    split_recovered = True
            except LocalModelTruncatedResponseError:
                if model_invoke is not invoke_local_model:
                    raise
                _recover_length_chunk(chunk, index, result, store, ai_debug, call_debug)
                split_recovered = True
            ai_debug["extracted_json_text"] = call_debug.get("extracted_json_text")
            ai_debug["parsed_object"] = call_debug.get("parsed_object")
            ai_debug["validation_errors"] = call_debug.get("validation_errors", [])
            _write_ai_debug(ai_debug)
            if not split_recovered:
                assert payload is not None
                _merge_ai(result, payload, source_texts)
            result["diagnostics"]["model_chunks"] += 1
    except LocalModelHTTPError as exc:
        result.update(status="BLOCKED", error_code="LOCAL_AI_HTTP_ERROR", error=str(exc),
                      backend_error={"kind": "HTTP_SERVER_FAILURE", "http_status": exc.status,
                                     "endpoint": exc.endpoint,
                                     "response_body": exc.response_body[:2000]})
        update("LOCAL_AI_HTTP_ERROR")
        return result
    except LocalModelTimeoutError as exc:
        result.update(status="BLOCKED", error_code="LOCAL_AI_TIMEOUT", error=str(exc),
                      backend_error={"kind": "TIMEOUT"})
        update("LOCAL_AI_TIMEOUT")
        return result
    except LocalModelTruncatedResponseError as exc:
        result.update(status="BLOCKED", error_code="LOCAL_AI_TRUNCATED_RESPONSE", error=str(exc),
                      backend_error={"kind": "TRUNCATED_RESPONSE"})
        update("LOCAL_AI_TRUNCATED_RESPONSE")
        return result
    except LocalModelInvalidResponseError as exc:
        result.update(status="BLOCKED", error_code="LOCAL_AI_INVALID_RESPONSE", error=str(exc),
                      backend_error={"kind": "MALFORMED_RESPONSE"})
        update("LOCAL_AI_INVALID_RESPONSE")
        return result
    except LocalModelNotFoundError as exc:
        result.update(status="BLOCKED", error_code="LOCAL_AI_MODEL_NOT_FOUND", error=str(exc),
                      backend_error={"kind": "MODEL_NOT_FOUND"})
        update("LOCAL_AI_MODEL_NOT_FOUND")
        return result
    except (LocalModelUnavailableError, LocalOnlyError) as exc:
        result.update(status="BLOCKED", error_code="LOCAL_AI_UNAVAILABLE", error=str(exc))
        update("LOCAL_AI_UNAVAILABLE")
        return result
    except InstallResearchError as exc:
        if ai_debug["chunks_sent"]:
            last = ai_debug["chunks_sent"][-1]
            ai_debug["raw_response"] = last.get("raw_response")
            ai_debug["sanitized_response"] = last.get("sanitized_response")
            ai_debug["extracted_json_text"] = last.get("extracted_json_text")
            ai_debug["parsed_object"] = last.get("parsed_object")
            ai_debug["validation_errors"] = last.get("validation_errors", [])
        _write_ai_debug(ai_debug)
        result.update(status="BLOCKED", error_code=exc.code, error=str(exc))
        update(exc.code)
        return result
    except Exception as exc:
        result.update(status="BLOCKED", error_code="LOCAL_AI_INTERNAL_ERROR", error=str(exc))
        update("LOCAL_AI_INTERNAL_ERROR")
        return result

    if result.get("confidence_review", {}).get("issues"):
        result.update(status="NEEDS_REVIEW", error_code="EVIDENCE_CONFIDENCE_REVIEW_REQUIRED",
                      error="Choose confidence for the listed evidence before research can continue.")
        update("EVIDENCE_CONFIDENCE_REVIEW_REQUIRED")
        return result
    return _finish_research(result, deterministic, deterministic_assets, chunks, update)


def _finish_research(result: dict[str, Any], deterministic: dict[str, Any],
                     deterministic_assets: Any, chunks: list[dict[str, Any]],
                     update: Callable[[str], None]) -> dict[str, Any]:
    update("RECONCILING_EVIDENCE")
    _reconcile_deterministic(result, deterministic)
    _reconcile_model_assets(result, deterministic_assets)
    result["install_methods"] = _deduplicate_install_methods(result["install_methods"])
    update("VALIDATING_EVIDENCE")
    for method in result["install_methods"]:
        result["proposed_plan"].append({
            "type": FIXED_PLAN_TYPE, "executable": False,
            "description": f"Review documented installation method: {method.get('name', 'UNKNOWN')}",
            "exact_commands": method.get("exact_commands", []), "evidence": method.get("evidence", []),
        })
    if result["dependencies"]:
        result["proposed_plan"].append({
            "type": FIXED_PLAN_TYPE, "executable": False,
            "description": "Review deterministically parsed dependency declarations",
            "dependency_count": len(result["dependencies"]), "exact_commands": [],
            "evidence": [evidence for row in result["dependencies"] for evidence in row.get("evidence", [])],
        })
    if any(_known(result["python_environment"].get(key)) for key in (
            "version", "environment_manager", "package_installer")):
        result["proposed_plan"].append({
            "type": FIXED_PLAN_TYPE, "executable": False,
            "description": "Review documented Python environment requirements",
            "exact_commands": [], "evidence": result["python_environment"].get("evidence", []),
        })
    for field, description in (
        ("models", "Review documented model or checkpoint acquisition"),
        ("system_dependencies", "Review documented system dependencies"),
    ):
        if result[field]:
            result["proposed_plan"].append({
                "type": FIXED_PLAN_TYPE, "executable": False, "description": description,
                "exact_commands": [],
                "evidence": [e for row in result[field] for e in row.get("evidence", [])],
            })
    if result["launch"].get("exact_command") != "UNKNOWN":
        result["proposed_plan"].append({
            "type": FIXED_PLAN_TYPE, "executable": False,
            "description": "Review documented launch and verification instructions",
            "exact_commands": [result["launch"]["exact_command"]],
            "evidence": result["launch"].get("evidence", []),
        })
    result["proposed_plan"] = _deduplicate_review_actions(result["proposed_plan"])
    if not result["proposed_plan"]:
        result.update(status="BLOCKED", error_code="INSUFFICIENT_INSTALL_EVIDENCE")
        result["unknowns"].append({"fact": "actionable installation method", "reason": "Documentation did not provide sufficiently supported instructions."})
        update("INSUFFICIENT_INSTALL_EVIDENCE")
        return result
    primary_install_chunks = [
        chunk for chunk in chunks
        if chunk["path"].lower() == "readme.md"
        and (re.search(r"\binstall(?:ation)?\b", str(chunk["heading"]), re.I)
             or any(re.search(r"\binstall(?:ation)?\b", parent, re.I)
                    for parent in chunk.get("parent_headings", ())))
    ]
    incomplete_primary_install = bool(primary_install_chunks) and any(
        not chunk.get("semantic_complete", False) for chunk in primary_install_chunks
    )
    if incomplete_primary_install and not result["install_methods"]:
        result.update(status="PARTIAL", error_code="INSUFFICIENT_INSTALL_EVIDENCE")
        result["unknowns"].append({
            "fact": "installation method",
            "reason": "The primary README installation section could not be submitted as complete semantic units.",
        })
        completion_stage = "INSUFFICIENT_INSTALL_EVIDENCE"
    elif not result["diagnostics"].get("reconciliation_completed"):
        result.update(status="PARTIAL", error_code="RECONCILIATION_INCOMPLETE")
        completion_stage = "RECONCILIATION_INCOMPLETE"
    else:
        result["status"] = "COMPLETE"
        completion_stage = "COMPLETE"
    result["safety"] = {"executable": False, "repository_content_executed": False,
                        "cloud_fallback": False, "approval_required_for_future_execution": True}
    update(completion_stage)
    return result


def confirm_confidence_review(jobs_root: Path, job_id: str,
                              choices: dict[str, str]) -> dict[str, Any]:
    """Apply explicit confidence choices to deferred subchunks, then resume research."""
    store = product_cli.JobStore(Path(jobs_root), job_id)
    result = store.load(RESEARCH_FILE, {})
    review = result.get("confidence_review") or {}
    issues = review.get("issues") or []
    if result.get("status") != "NEEDS_REVIEW" or not issues or not review.get("pending_splits"):
        raise InstallResearchError("CONFIDENCE_REVIEW_UNAVAILABLE", "No confidence review is pending.")
    expected_ids = {issue["id"] for issue in issues}
    if (not isinstance(choices, dict) or set(choices) != expected_ids
            or any(value not in CONFIDENCE_CHOICES for value in choices.values())):
        raise InstallResearchError("CONFIDENCE_SELECTION_INVALID",
                                   "Choose HIGH, MEDIUM, or LOW for every listed claim.")

    def update(stage: str) -> None:
        result["analysis_stage"] = stage
        result["updated_at"] = _now()
        store.save(RESEARCH_FILE, result)

    try:
        source_job_id = result.get("source_acquisition_job_id") or job_id
        source_store = product_cli.JobStore(Path(jobs_root), source_job_id)
        identity, completed = _source_identity(source_store)
        if identity != result.get("source_identity"):
            raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                       "Verified source identity changed during confidence review.")
        if source_job_id != job_id:
            plan = store.load("plan.json", {})
            step = next((row for row in plan.get("steps", [])
                         if row.get("type") == "ACQUIRE_GITHUB_SNAPSHOT"), None)
            expected = (step or {}).get("inputs") or {}
            if (expected.get("repository") != identity["repository"]
                    or expected.get("canonical_url") != identity["canonical_url"]
                    or expected.get("exact_commit") != identity["exact_commit"]
                    or (expected.get("destination") and
                        str(Path(expected["destination"]).resolve()) != identity["local_path"])):
                raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                           "Approved source identity changed during confidence review.")
        root = Path(identity["local_path"])
        files = discover_research_files(root)
        if [path.relative_to(root).as_posix() for path in files] != result["diagnostics"]["files_inspected"]:
            raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                       "Research source files changed during confidence review.")
        fidelity = _source_fidelity(completed, root, files)
        if fidelity != result.get("source_fidelity") or fidelity["review_required"]:
            raise InstallResearchError("SOURCE_FIDELITY_REVIEW_REQUIRED",
                                       "Source fidelity changed during confidence review.")

        working = copy.deepcopy(result)
        remaining = set(expected_ids)
        by_path = {path.relative_to(root).as_posix(): path for path in files}
        for pending in working["confidence_review"]["pending_splits"]:
            accepted = []
            for row in pending["subchunks"]:
                path = row["path"]
                if path not in by_path:
                    raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                               "Reviewed subchunk source is unavailable.")
                source_text = _read(by_path[path])
                start, end = row["start"], row["end"]
                if (not isinstance(start, int) or not isinstance(end, int)
                        or start < 0 or end <= start or end > len(source_text)):
                    raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                               "Reviewed subchunk boundary changed.")
                part = {**row, "text": source_text[start:end]}
                if hashlib.sha256(part["text"].encode("utf-8")).hexdigest() != row["text_sha256"]:
                    raise InstallResearchError("SOURCE_IDENTITY_MISMATCH",
                                               "Reviewed subchunk evidence changed.")
                payload = row["payload"]
                for issue in _confidence_issues(payload, row["subchunk_id"]):
                    issue_id = issue["id"]
                    if issue_id not in remaining:
                        raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                                   "Pending confidence claim changed.")
                    claim = payload[issue["field"]]
                    if isinstance(claim, list):
                        claim = claim[issue["claim_index"]]
                    evidence = claim["evidence"]
                    if isinstance(evidence, dict):
                        evidence = [evidence]
                    evidence[issue["evidence_index"]]["confidence"] = choices[issue_id]
                    remaining.remove(issue_id)
                _validate_subchunk_payload(payload, identity, part)
                accepted.append((part, payload))
            _merge_subchunks(working, accepted)
            split = next((item for item in working["diagnostics"].get("evidence_splits", [])
                          if item["original_chunk_id"] == pending["original_chunk_id"]), None)
            if split is None:
                raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                           "Pending evidence split audit is missing.")
            split["merge_result"] = {"status": "SUCCESS", "validated_subchunks": len(accepted)}
        if remaining:
            raise InstallResearchError("LOCAL_AI_INVALID_RESPONSE",
                                       "Pending confidence claims are missing.")
        for issue in working["confidence_review"]["issues"]:
            issue["selected_value"] = choices[issue["id"]]
        working["confidence_review"]["pending_splits"] = []
        working["confidence_review"]["result"] = "CONFIRMED"
        working.pop("error", None)
        working.pop("error_code", None)
        result = working
        deterministic = parse_deterministic(files, root)
        deterministic_assets = extract_model_asset_evidence(files, root)
        chunks = _chunks(files, root)
        return _finish_research(result, deterministic, deterministic_assets, chunks, update)
    except InstallResearchError as exc:
        result.update(status="BLOCKED", error_code=exc.code, error=str(exc))
        update(exc.code)
        return result


def load_research(jobs_root: Path, job_id: str) -> dict[str, Any]:
    return product_cli.JobStore(Path(jobs_root), job_id).load(RESEARCH_FILE, {})


_REPORT_LABELS = {
    "en": {
        "status": "Status", "repository": "Repository", "commit": "Commit",
        "local_source": "Local source", "evidence": "Evidence",
        "install_methods": "Documented install methods", "dependencies": "Dependencies",
        "models": "Models / checkpoints", "system_dependencies": "System dependencies",
        "risks": "Risks", "none_documented": "None documented",
        "python_environment": "Python environment", "gpu_runtime": "GPU/runtime",
        "launch": "Launch", "unknowns": "Unknowns", "none": "None",
        "proposed_plan": "Proposed next steps (review only; not executed)",
        "stopped": "Stopped",
    },
    "zh-CN": {
        "status": "状态", "repository": "仓库", "commit": "提交", "local_source": "本地源代码",
        "evidence": "证据", "install_methods": "已记录的安装方法", "dependencies": "依赖项",
        "models": "模型 / 检查点", "system_dependencies": "系统依赖项", "risks": "风险",
        "none_documented": "未记录", "python_environment": "Python 环境",
        "gpu_runtime": "GPU / 运行时", "launch": "启动", "unknowns": "未知项",
        "none": "无", "proposed_plan": "建议的后续步骤（仅供审阅；未执行）", "stopped": "已停止",
    },
}

_REPORT_FIELD_LABELS_ZH = {
    "version": "版本", "environment_manager": "环境管理器", "package_installer": "软件包安装器",
    "pytorch": "PyTorch", "torchvision": "torchvision", "cuda": "CUDA",
    "wheel_tag": "wheel 标签", "flashinfer": "FlashInfer", "triton": "Triton",
    "xformers": "xFormers", "compiler": "编译器", "exact_command": "精确命令",
    "port": "端口", "ui": "UI", "verification": "验证", "alternatives": "备选项",
    "conflict": "冲突",
}


_REPORT_PROSE_ZH_CN = {
    "UNKNOWN": "未知",
    "official": "官方",
    "documented": "已记录",
    "inferred": "推断",
    "Operating system target": "目标操作系统",
    "Model download commands / method": "模型下载命令 / 方法",
    "OS target for scripts": "脚本的目标操作系统",
    "launch URL": "启动 URL",
    "Not documented": "未记录",
    "Review deterministically parsed dependency declarations": "审阅确定性解析的依赖声明",
    "Review documented Python environment requirements": "审阅已记录的 Python 环境要求",
    "Review documented model or checkpoint acquisition": "审阅已记录的模型或检查点获取方式",
    "Review documented system dependencies": "审阅已记录的系统依赖项",
    "Review documented launch and verification instructions": "审阅已记录的启动和验证说明",
    "README section lists repositories but provides no download commands or instructions.":
        "README 章节列出了仓库，但未提供下载命令或说明。",
    "OS target for install_lingbot_map.sh and install_bench.sh":
        "install_lingbot_map.sh 和 install_bench.sh 的目标操作系统",
    "Kaolin must be built from source if a newer PyTorch than 2.8.0 is used, because NVIDIA Kaolin has prebuilt wheels only for torch-2.8.0_cu128":
        "如果使用高于 2.8.0 的 PyTorch，则必须从源代码构建 Kaolin，因为 NVIDIA Kaolin 仅为 torch-2.8.0_cu128 提供预构建 wheel",
    "FlashInfer may fail to install if the default pip index is an internal mirror without flashinfer-python":
        "如果默认 pip 索引是不包含 flashinfer-python 的内部镜像，FlashInfer 可能安装失败",
    "If FlashInfer is not installed, the model falls back to SDPA":
        "如果未安装 FlashInfer，模型将回退到 SDPA",
}


def _is_simplified_chinese(language: str) -> bool:
    tag = str(language).replace("_", "-").casefold()
    return tag == "zh-cn" or tag == "zh-hans" or tag.startswith("zh-hans-")


def _report_prose(value: Any, simplified_chinese: bool) -> str:
    text = str(value or "UNKNOWN")
    if not simplified_chinese:
        return text
    if text in _REPORT_PROSE_ZH_CN:
        return _REPORT_PROSE_ZH_CN[text]
    if text.startswith("Exact version of "):
        return f"{text.removeprefix('Exact version of ')} 的确切版本"
    if text.startswith("Not documented in "):
        return f"{text.removeprefix('Not documented in ')} 中未记录"
    if text.startswith("Review documented installation method: "):
        return f"审阅已记录的安装方法：{text.removeprefix('Review documented installation method: ')}"
    return text


def _risk_report_prose(row: dict[str, Any], simplified_chinese: bool) -> str:
    summary = row.get("description") or row.get("risk")
    if _known(summary):
        return _report_prose(summary, simplified_chinese)
    kaolin_statement = "NVIDIA Kaolin does not publish prebuilt wheels for PyTorch 2.9.x"
    if simplified_chinese and any(
        kaolin_statement in str(item.get("exact_text", ""))
        for item in row.get("evidence", []) if isinstance(item, dict)
    ):
        return "NVIDIA Kaolin 不为 PyTorch 2.9.x 提供预构建 wheel"
    return _report_prose("UNKNOWN", simplified_chinese)


def review_text(result: dict[str, Any], safety_notice: str, language: str = "en") -> str:
    """Render an evidence-visible, plain-text review without making anything executable."""
    locale = "zh-CN" if str(language).lower().startswith("zh") else "en"
    simplified_chinese = _is_simplified_chinese(language)
    labels = _REPORT_LABELS[locale]
    identity = result.get("source_identity", {})
    lines = [
        safety_notice, "", f"{labels['status']}: {result.get('status', 'UNKNOWN')}",
        f"{labels['repository']}: {identity.get('repository', 'UNKNOWN')}",
        f"{labels['commit']}: {identity.get('exact_commit', 'UNKNOWN')}",
        f"{labels['local_source']}: {identity.get('local_path', 'UNKNOWN')}", "",
    ]
    fidelity = result.get("source_fidelity") or {}
    if fidelity.get("skipped_members"):
        lines.append("Source fidelity: extracted tree only; skipped symlinks are not present.")
        for member in fidelity["skipped_members"]:
            lines.append(f"• {member.get('archive_member_path', 'UNKNOWN')} "
                         f"[{member.get('type', 'UNKNOWN')} {member.get('mode', 'UNKNOWN')}]: "
                         f"{member.get('reason_skipped', 'UNKNOWN')}; "
                         f"{member.get('impact_reason', 'UNKNOWN')}")
        lines.append("")

    def evidence_lines(row: dict[str, Any]) -> list[str]:
        return [
            f"    {labels['evidence']}: {e.get('path', 'UNKNOWN')} — {e.get('heading', 'UNKNOWN')} — {e.get('exact_text', 'UNKNOWN')}"
            for e in row.get("evidence", []) if isinstance(e, dict)
        ]

    sections = (
        (labels["install_methods"], "install_methods", lambda r: f"{r.get('name', 'UNKNOWN')} [{_report_prose(r.get('classification', 'UNKNOWN'), simplified_chinese)}]"),
        (labels["dependencies"], "dependencies", lambda r: f"{r.get('package', 'UNKNOWN')} {r.get('version', 'UNKNOWN')}"),
        (labels["models"], "models", lambda r: f"{r.get('name', 'UNKNOWN')} — {r.get('size', 'UNKNOWN')}"),
        (labels["system_dependencies"], "system_dependencies", lambda r: str(r.get('name') or r.get('dependency') or r.get('package') or 'UNKNOWN')),
        (labels["risks"], "risks", lambda r: _risk_report_prose(r, simplified_chinese)),
    )
    for title, field, label in sections:
        lines.append(f"{title}:")
        rows = result.get(field, [])
        if not rows:
            lines.append("• UNKNOWN" if field != "risks" else f"• {labels['none_documented']}")
        for row in rows:
            lines.append(f"• {label(row)}")
            lines.extend(evidence_lines(row))
        lines.append("")
    python_environments = result.get("python_environments", [])
    if python_environments:
        for environment in python_environments:
            row = _canonical_object_claim("python_environment", environment)
            scope = str(row.get("scope") or "UNKNOWN_SCOPE")
            visible = {
                key: value for key, value in row.items()
                if key not in {"evidence", "scope"}
            }
            if locale == "zh-CN":
                visible = {_REPORT_FIELD_LABELS_ZH.get(key, key): value for key, value in visible.items()}
            lines.extend((f"{labels['python_environment']} [{scope}]:", json.dumps(visible, ensure_ascii=False)))
            lines.extend(evidence_lines(row))
            lines.append("")
    else:
        row = _canonical_object_claim("python_environment", result.get("python_environment", {}))
        visible = {key: value for key, value in row.items() if key != "evidence"}
        if locale == "zh-CN":
            visible = {_REPORT_FIELD_LABELS_ZH.get(key, key): value for key, value in visible.items()}
        lines.extend((f"{labels['python_environment']}:", json.dumps(visible, ensure_ascii=False)))
        lines.extend(evidence_lines(row))
        lines.append("")
    for title, field in ((labels["gpu_runtime"], "gpu_runtime"), (labels["launch"], "launch")):
        row = result.get(field, {})
        visible = {key: value for key, value in row.items() if key != "evidence"}
        if locale == "zh-CN":
            visible = {_REPORT_FIELD_LABELS_ZH.get(key, key): value for key, value in visible.items()}
        lines.extend((f"{title}:", json.dumps(visible, ensure_ascii=False)))
        lines.extend(evidence_lines(row))
        lines.append("")
    lines.append(f"{labels['unknowns']}:")
    unknowns = [row for row in result.get("unknowns", []) if _meaningful_unknown(row)]
    for row in unknowns:
        fact = str(row.get("fact") or "UNKNOWN").strip()
        reason = str(row.get("reason") or "UNKNOWN").strip()
        if _known(fact) and _known(reason):
            lines.append(f"• {_report_prose(fact, simplified_chinese)}: {_report_prose(reason, simplified_chinese)}")
        else:
            lines.append(f"• {_report_prose(fact if _known(fact) else reason, simplified_chinese)}")
    if not unknowns:
        lines.append(f"• {labels['none']}")
    lines.extend(("", f"{labels['proposed_plan']}:"))
    lines.extend(
        f"• {_report_prose(row.get('description', 'UNKNOWN'), simplified_chinese)}"
        for row in result.get("proposed_plan", [])
    )
    if result.get("error_code"):
        lines.extend(("", f"{labels['stopped']}: {result['error_code']}", str(result.get("error", ""))))
    return "\n".join(lines)
