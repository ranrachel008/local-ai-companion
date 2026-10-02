"""Build approval-gated Product Core execution plans."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any
from urllib.parse import urlparse

from preflight import MATCH, COMPATIBLE, UNKNOWN, compare_assets

PLAN_STATUSES = {
    "READY_FOR_APPROVAL", "BLOCKED", "WAITING_FOR_SOURCE",
    "WAITING_FOR_USER_SELECTION", "WAITING_FOR_USER_APPROVAL",
}

_FULL_GIT_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _github_snapshot(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return the fixed review row and step for one verified GitHub snapshot."""
    if source.get("source_type") not in {"github_repository", "official_repository"}:
        return None
    repository = str(source.get("repo") or "")
    commit = str(source.get("commit") or "")
    canonical_url = str(source.get("url") or "").rstrip("/")
    parsed = urlparse(canonical_url)
    canonical_repo = parsed.path.strip("/")
    if canonical_repo.endswith(".git"):
        canonical_repo = canonical_repo[:-4]
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or not _GITHUB_REPOSITORY.fullmatch(repository)
            or canonical_repo.lower() != repository.lower()
            or not _FULL_GIT_COMMIT.fullmatch(commit)):
        return None
    commit = commit.lower()
    repository_name = repository.split("/", 1)[1]
    archive_url = f"https://codeload.github.com/{repository}/zip/{commit}"
    provenance = deepcopy(source.get("provenance") or {})
    review = {
        "identifier": repository,
        "name": f"Repository source: {repository} @ {commit}",
        "type": "repository_source", "model": False, "size": None,
        "url": archive_url, "canonical_url": canonical_url,
        "repository": repository, "exact_commit": commit,
        "destination": None, "destination_name": repository_name,
        "expected_new_directories": [repository_name],
        "expected_new_files": [f"verified snapshot contents under {repository_name}"],
        "provenance": provenance,
    }
    expected = {
        "acquired": True, "repository": repository,
        "canonical_url": canonical_url, "exact_commit": commit,
        "destination": None,
    }
    step = {
        "step_id": "acquire-verified-github-snapshot",
        "type": "ACQUIRE_GITHUB_SNAPSHOT",
        "inputs": {
            "repository": repository, "canonical_url": canonical_url,
            "source_url": canonical_url, "exact_commit": commit,
            "archive_url": archive_url, "destination": None,
            "destination_name": repository_name, "provenance": provenance,
        },
        "expected_result": expected, "approved": False,
        "rollback": {"delete_created_directory": None},
        "timeout": 120, "status": "PENDING", "files_changed": [],
    }
    return review, step


def combo_options(schema: Any) -> list[Any]:
    """Read COMBO choices from the live schema's metadata options field."""
    if (isinstance(schema, (list, tuple)) and len(schema) >= 2 and schema[0] == "COMBO"
            and isinstance(schema[1], dict) and isinstance(schema[1].get("options"), list)):
        return list(schema[1]["options"])
    return []


def serialize_api_payload(payload: dict[str, Any], ensure_ascii: bool = True) -> str:
    import json
    return json.dumps(payload, ensure_ascii=ensure_ascii, separators=(",", ":"))


def detect_cross_project_conflicts(request: dict[str, Any], preflight: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    active_ports = {row.get("port") for row in preflight.get("ports", []) if row.get("listening") is True}
    for service in request.get("services", []):
        if service.get("port") in active_ports and not service.get("reuse_existing", False):
            conflicts.append({"type": "port_conflict", "port": service["port"], "service": service.get("name")})
    env = preflight.get("environment_variables", {})
    for change in request.get("environment_changes", []):
        current = env.get(change.get("name"))
        if change.get("persistent") and current != change.get("value"):
            conflicts.append({"type": "persistent_environment_change", "name": change.get("name")})
        if (change.get("name") == "OLLAMA_MODELS" and current != change.get("value")
                and 11434 in active_ports):
            conflicts.append({"type": "model_store_switch", "name": "OLLAMA_MODELS", "port": 11434})
    if request.get("shared_python_environment_change"):
        conflicts.append({"type": "shared_python_environment_change"})
    for dependency in request.get("existing_process_dependencies", []):
        if dependency.get("would_disrupt"):
            conflicts.append({"type": "existing_process_dependency", "detail": dependency})
    return conflicts


def build_plan(goal: str, source: dict[str, Any] | None, preflight: dict[str, Any],
               request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = deepcopy(request or {})
    base = {
        "goal": goal, "source_url": None, "repo": None, "exact_commit": None,
        "package_metadata": {}, "local_assets_to_reuse": [], "planned_downloads": [],
        "planned_modifications": [], "forbidden_downloads": [], "forbidden_modifications": [],
        "dependency_status": request.get("dependency_status", []), "port_service_conflicts": [],
        "cross_project_conflicts": [], "manual_user_actions": request.get("manual_user_actions", []),
        "verification_criteria": request.get("verification_criteria", []),
        "stop_conditions": request.get("stop_conditions", [
            "source mismatch", "timeout", "new dependency", "unexpected version",
            "unexpected port conflict", "unapproved persistent environment modification",
        ]),
        "rollback_scope": request.get("rollback_scope", []), "steps": request.get("steps", []),
        "status": "WAITING_FOR_SOURCE", "block_reasons": [],
    }
    if not source:
        return base
    base.update({
        "source_url": source.get("url"), "repo": source.get("repo"),
        "exact_commit": source.get("commit"),
        "package_metadata": deepcopy(source.get("package_metadata", {})),
        "source_type": source.get("source_type"),
        "source_provenance": deepcopy(source.get("provenance") or {}),
    })
    if not source.get("selected", True):
        base["status"] = "WAITING_FOR_USER_SELECTION"
        return base
    if not source.get("url") or source.get("commit") in (None, "", "UNKNOWN"):
        base["block_reasons"].append("Selected source lacks an exact URL or commit/revision.")
    snapshot = _github_snapshot(source)
    if source.get("source_type") in {"github_repository", "official_repository"} and snapshot is None:
        base["block_reasons"].append("VERIFIED_GITHUB_SOURCE_REQUIRES_EXACT_40_CHARACTER_COMMIT")
    expected_version = request.get("expected_version")
    actual_version = source.get("package_metadata", {}).get("version")
    if expected_version is not None and expected_version != actual_version:
        base["block_reasons"].append("Exact commit has unexpected version metadata.")
    assets = preflight.get("local_assets", [])
    for download in request.get("downloads", []):
        classified = [(asset, compare_assets(download, asset)) for asset in assets]
        match = next(((asset, state) for asset, state in classified if state in (MATCH, COMPATIBLE)), None)
        unknown = any(state == UNKNOWN and asset.get("identifier") == download.get("identifier")
                      for asset, state in classified)
        if match:
            reused = dict(match[0])
            reused["compatibility"] = match[1]
            base["local_assets_to_reuse"].append(reused)
            base["forbidden_downloads"].append({**download, "reason": "compatible local asset exists"})
        elif unknown:
            base["block_reasons"].append(f"UNKNOWN compatibility for {download.get('identifier')}")
        else:
            base["planned_downloads"].append(download)
    base["planned_modifications"] = request.get("modifications", [])
    base["forbidden_modifications"] = request.get("forbidden_modifications", [])
    conflicts = detect_cross_project_conflicts(request, preflight)
    base["cross_project_conflicts"] = conflicts
    base["port_service_conflicts"] = [x for x in conflicts if x["type"] in ("port_conflict", "model_store_switch")]
    if conflicts:
        base["block_reasons"].append("Cross-project conflict detected.")
    if snapshot and not request.get("steps"):
        review, step = snapshot
        base["planned_downloads"].append(review)
        base["steps"].append(step)
    if not base["steps"]:
        base["block_reasons"].append("NO_ACTIONABLE_INSTALL_PLAN")
    if base["block_reasons"]:
        base["status"] = "BLOCKED"
    elif request.get("approved") is True:
        base["status"] = "READY_FOR_APPROVAL"
    else:
        base["status"] = "WAITING_FOR_USER_APPROVAL"
    return base
