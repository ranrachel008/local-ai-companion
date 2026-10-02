"""Read-only local environment inspection for Product Core plans."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any, Iterable

MATCH = "MATCH"
COMPATIBLE = "COMPATIBLE"
DIFFERENT_FORMAT = "DIFFERENT_FORMAT"
DIFFERENT_MODEL = "DIFFERENT_MODEL"
UNKNOWN = "UNKNOWN"
COMPATIBILITY_VALUES = {MATCH, COMPATIBLE, DIFFERENT_FORMAT, DIFFERENT_MODEL, UNKNOWN}


def local_asset(path: str | Path, asset_type: str, identifier: str,
                version: str | None = None, digest: str | None = None,
                source: str | None = None) -> dict[str, Any]:
    """Return the stable JSON representation of a discovered local asset."""
    item = Path(path)
    try:
        size = item.stat().st_size if item.is_file() else sum(
            child.stat().st_size for child in item.rglob("*") if child.is_file()
        )
    except OSError:
        size = None
    return {
        "path": str(item), "type": asset_type, "identifier": identifier,
        "size": size, "version": version, "hash": digest, "source": source,
    }


def cheap_hash(path: str | Path, maximum_bytes: int = 16 * 1024 * 1024) -> str | None:
    """Hash only small files; large files must not be read merely for preflight."""
    item = Path(path)
    try:
        if not item.is_file() or item.stat().st_size > maximum_bytes:
            return None
        digest = hashlib.sha256()
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def compare_assets(required: dict[str, Any], existing: dict[str, Any]) -> str:
    """Classify only facts present in both records; absence stays UNKNOWN."""
    required_id, existing_id = required.get("identifier"), existing.get("identifier")
    if required_id and existing_id and required_id != existing_id:
        return DIFFERENT_MODEL
    required_format, existing_format = required.get("format"), existing.get("format")
    if required_format and existing_format and required_format != existing_format:
        return DIFFERENT_FORMAT
    keys = ("identifier", "format", "quantization", "version", "hash")
    known = [key for key in keys if required.get(key) is not None]
    if known and all(existing.get(key) == required.get(key) for key in known):
        return MATCH
    # A known same model in a different quantization may be usable, but is never exact.
    if (required_id and required_id == existing_id and required.get("quantization")
            and existing.get("quantization") and required["quantization"] != existing["quantization"]):
        return COMPATIBLE if required.get("allow_other_quantization") is True else UNKNOWN
    if required_id and required_id == existing_id and required.get("compatible_formats"):
        if existing_format in required["compatible_formats"]:
            return COMPATIBLE
    return UNKNOWN


def _port_listener(host: str, port: int, timeout: float = 0.15) -> dict[str, Any]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        result = sock.connect_ex((host, port))
        return {"host": host, "port": port, "listening": result == 0}
    except OSError as exc:
        return {"host": host, "port": port, "listening": None, "error": str(exc)}
    finally:
        sock.close()


def _ollama_assets(root: Path) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    manifests = root / "manifests"
    if manifests.exists():
        for path in manifests.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(manifests).parts
            identifier = "/".join(relative[-3:]) if len(relative) >= 3 else "/".join(relative)
            assets.append(local_asset(path, "ollama_manifest", identifier,
                                      digest=cheap_hash(path), source="ollama"))
    blobs = root / "blobs"
    if blobs.exists():
        for path in blobs.iterdir():
            if path.is_file():
                assets.append(local_asset(path, "ollama_blob", path.name,
                                          digest=path.name.replace("sha256-", "sha256:"), source="ollama"))
    return assets


def inspect_environment(spec: dict[str, Any]) -> dict[str, Any]:
    """Perform only requested read-only checks and return JSON-compatible facts."""
    result: dict[str, Any] = {
        "local_assets": [], "files": [], "model_folders": [], "ollama_models": [],
        "ports": [], "python_packages": [], "custom_nodes": [], "comfyui_models": [],
        "environment_variables": {}, "running_services": [], "errors": [],
    }
    for raw in spec.get("files", []):
        path = Path(raw)
        exists = path.is_file()
        row = {"path": str(path), "exists": exists}
        result["files"].append(row)
        if exists:
            result["local_assets"].append(local_asset(path, "file", path.name,
                                                       digest=cheap_hash(path), source="filesystem"))
    for raw in spec.get("model_folders", []):
        path = Path(raw)
        exists = path.is_dir()
        result["model_folders"].append({"path": str(path), "exists": exists})
        if exists:
            result["local_assets"].append(local_asset(path, "model_folder", path.name,
                                                       source="filesystem"))
    ollama_root = spec.get("ollama_root")
    if ollama_root:
        result["local_assets"].extend(_ollama_assets(Path(ollama_root)))
    result["ollama_models"] = [dict(item) for item in spec.get("ollama_model_tags", [])]
    result["ports"] = [_port_listener(item.get("host", "127.0.0.1"), int(item["port"]))
                       for item in spec.get("ports", [])]
    for name in spec.get("python_packages", []):
        try:
            version = importlib.metadata.version(name)
            result["python_packages"].append({"name": name, "installed": True, "version": version})
        except importlib.metadata.PackageNotFoundError:
            result["python_packages"].append({"name": name, "installed": False, "version": None})
    for key in spec.get("environment_variables", []):
        result["environment_variables"][key] = os.environ.get(key)
    for root_key, output_key, asset_type in (
        ("comfyui_custom_nodes", "custom_nodes", "comfyui_custom_node"),
        ("comfyui_model_paths", "comfyui_models", "comfyui_model"),
    ):
        for raw in spec.get(root_key, []):
            path = Path(raw)
            row = {"path": str(path), "exists": path.exists()}
            result[output_key].append(row)
            if path.exists():
                result["local_assets"].append(local_asset(path, asset_type, path.name, source="comfyui"))
    if spec.get("running_services"):
        # tasklist is read-only and available on supported Windows hosts.
        try:
            output = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], check=False,
                                    capture_output=True, text=True, timeout=5).stdout
            lowered = output.lower()
            result["running_services"] = [
                {"name": name, "running": name.lower() in lowered}
                for name in spec["running_services"]
            ]
        except (OSError, subprocess.SubprocessError) as exc:
            result["errors"].append({"check": "running_services", "error": str(exc)})
    return json.loads(json.dumps(result))

