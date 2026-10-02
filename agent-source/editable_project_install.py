"""Bind a fixed editable install to the acquired, immutable job source."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class EditableSourceError(ValueError):
    pass


def verify_editable_command_evidence(source: str | Path, evidence: list[Any]) -> None:
    """Require the exact command to exist in an upstream file inside the verified tree."""
    root = Path(source).resolve()
    for item in evidence:
        if not isinstance(item, dict) or item.get("evidence_type") != "verbatim_command" \
                or item.get("exact_text") != "pip install -e .":
            continue
        relative = Path(str(item.get("path") or ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        file = (root / relative).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            continue
        try:
            if "pip install -e ." in file.read_text(encoding="utf-8"):
                return
        except (OSError, UnicodeError):
            continue
    raise EditableSourceError("The exact editable install command is missing from verified upstream evidence")


def verified_project_source(context: Any, identity: dict[str, Any],
                            expected_tree_sha256: str | None = None) -> dict[str, Any]:
    """Recheck the JobContext source and acquisition manifest without trusting a plan path."""
    from executor import verify_extracted_tree

    if context is None or context.state.value not in {
        "SOURCE_ACQUIRED", "RESEARCH_COMPLETE", "PLAN_REVIEW", "APPROVED", "EXECUTING", "STOPPED", "RESUMABLE",
    }:
        raise EditableSourceError("Editable install requires an acquired job source")
    repository = str(identity.get("repository") or identity.get("repo") or "")
    commit = str(identity.get("exact_commit") or identity.get("commit") or "")
    url = str(identity.get("canonical_url") or "")
    source = Path(str(context.resolved_project_destination or ""))
    claimed_source = Path(str(identity.get("local_path") or ""))
    if (identity.get("acquisition_verified") is not True
            or repository.casefold() != str(context.repository_identity or "").casefold()
            or url.casefold().rstrip("/") != str(context.canonical_repo_url or "").casefold().rstrip("/")
            or not re.fullmatch(r"[0-9a-fA-F]{40}", commit)
            or commit.casefold() != str(context.exact_revision or "").casefold()
            or not source.is_absolute() or not claimed_source.is_absolute()
            or source.resolve() != claimed_source.resolve()):
        raise EditableSourceError("Editable install repository, commit, or source path differs from JobContext")
    if not any((source / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg")):
        raise EditableSourceError("Editable source has no Python project metadata")
    step_id = str(identity.get("acquisition_step_id") or "")
    if not step_id:
        raise EditableSourceError("Editable source has no acquisition step binding")
    try:
        state = json.loads((context.job_dir / "execution_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EditableSourceError("Editable source acquisition state is unavailable") from exc
    matches = [row for row in state.get("steps", []) if row.get("step_id") == step_id]
    if len(matches) != 1 or matches[0].get("status") != "SUCCESS":
        raise EditableSourceError("Editable source acquisition has not succeeded")
    acquired = matches[0]
    inputs = acquired.get("inputs") or {}
    evidence = acquired.get("acquisition_evidence") or {}
    manifest = evidence.get("extracted_tree_manifest") or {}
    if (str(inputs.get("repository") or "").casefold() != repository.casefold()
            or str(inputs.get("canonical_url") or "").casefold().rstrip("/") != url.casefold().rstrip("/")
            or str(inputs.get("exact_commit") or "").casefold() != commit.casefold()
            or Path(str(inputs.get("destination") or "")).resolve() != source.resolve()
            or str(manifest.get("repository") or "").casefold() != repository.casefold()
            or str(manifest.get("exact_commit") or "").casefold() != commit.casefold()):
        raise EditableSourceError("Editable source acquisition identity changed")
    tree = verify_extracted_tree(source, evidence)
    if tree.get("state") != "VERIFIED":
        raise EditableSourceError(f"Editable source tree is not verified: {tree.get('reason')}")
    digest = str(tree["tree_sha256"])
    if expected_tree_sha256 is not None and digest != expected_tree_sha256:
        raise EditableSourceError("Editable source tree differs from the approved plan")
    return {"source_path": str(source), "repository": str(context.repository_identity),
            "canonical_url": str(context.canonical_repo_url),
            "exact_commit": str(context.exact_revision), "tree_sha256": digest,
            "tree_manifest": manifest}


def verified_editable_source(context: Any, identity: dict[str, Any],
                             expected_tree_sha256: str | None = None) -> dict[str, str]:
    bound = verified_project_source(context, identity, expected_tree_sha256)
    return {key: bound[key] for key in ("source_path", "repository", "exact_commit", "tree_sha256")}


def documented_project_installs(research: dict[str, Any], identity: dict[str, Any]) -> list[dict[str, Any]]:
    """Recognize evidenced plain archive or local-snapshot pip installs."""
    repository = str(identity.get("repository") or identity.get("repo") or "")
    canonical = str(identity.get("canonical_url") or "").rstrip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or canonical.casefold() != (
            "https://github.com/" + repository).casefold():
        return []
    pattern = re.compile(
        r"(?:python(?:3)? -m )?pip(?:3)? install "
        r"https://github\.com/([\w.-]+/[\w.-]+)/archive/([\w./-]+\.zip)", re.IGNORECASE)
    local_pattern = re.compile(r"(?:python(?:3)? -m )?pip(?:3)? install \.", re.IGNORECASE)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("evidence_type") in {"code_block", "verbatim_command", "deterministic_manifest"}:
                for line in str(value.get("exact_text") or "").splitlines():
                    command = line.strip()
                    if command.startswith("$ "):
                        command = command[2:]
                    match = pattern.fullmatch(command)
                    if ((match and match[1].casefold() == repository.casefold())
                            or local_pattern.fullmatch(command)) and command not in seen:
                        seen.add(command)
                        result.append({"author_command": command, "evidence": value})
            for key, item in value.items():
                if key != "exact_text":
                    visit(item)

    for field in ("dependencies", "python_environment", "python_environments", "install_methods"):
        visit(research.get(field))
    return result


def verify_project_command_evidence(source: str | Path, command: str,
                                    evidence: list[Any], identity: dict[str, Any]) -> None:
    recognized = documented_project_installs({"dependencies": evidence}, identity)
    root = Path(source).resolve()
    for item in recognized:
        if item["author_command"] != command:
            continue
        relative = Path(str(item["evidence"].get("path") or ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        file = (root / relative).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            continue
        try:
            lines = (line.strip() for line in file.read_text(encoding="utf-8").splitlines())
            if command in (line[2:] if line.startswith("$ ") else line for line in lines):
                return
        except (OSError, UnicodeError):
            continue
    raise EditableSourceError("The ordinary project install command is missing from verified upstream evidence")


def load_source_job_context(job_dir: str | Path, binding: dict[str, Any]) -> Any:
    """Follow the explicitly recorded acquisition job, never search job history."""
    from job_context import JobContext

    root = Path(job_dir)
    source_job = str(binding.get("source_acquisition_job_id") or "")
    if source_job:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source_job):
            raise EditableSourceError("Invalid source acquisition job binding")
        root = root.parent / source_job
    return JobContext.load(root)
