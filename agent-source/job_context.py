"""Canonical, read-only projection of a persisted Solution Scout job.

This module deliberately owns no persistence.  It validates the artifacts that
the existing product lifecycle writes and derives the highest continuous state
that those artifacts prove.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse, urlunparse

from installation_plan import EXECUTABLE_TYPES
from product_cli import INSTALLATION_PLAN_KIND, SOURCE_PLAN_KIND, plan_hash


class JobLifecycleState(str, Enum):
    SEARCH = "SEARCH"
    SOURCE_SELECTED = "SOURCE_SELECTED"
    SOURCE_VERIFIED = "SOURCE_VERIFIED"
    DESTINATION_SELECTED = "DESTINATION_SELECTED"
    SOURCE_ACQUIRED = "SOURCE_ACQUIRED"
    RESEARCH_COMPLETE = "RESEARCH_COMPLETE"
    PLAN_REVIEW = "PLAN_REVIEW"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    STOPPED = "STOPPED"
    RESUMABLE = "RESUMABLE"
    VERIFIED = "VERIFIED"


@dataclass(frozen=True)
class JobContextValidationFailure:
    code: str
    message: str
    artifact: str | None = None
    field: str | None = None


class JobContextValidationError(ValueError):
    """A precise, machine-readable persisted-state validation failure."""

    def __init__(self, failure: JobContextValidationFailure) -> None:
        self.failure = failure
        location = ".".join(part for part in (failure.artifact, failure.field) if part)
        super().__init__(f"{failure.code}: {failure.message}" + (f" ({location})" if location else ""))


@dataclass(frozen=True)
class JobContext:
    """Validated immutable identity and lifecycle view for one job directory."""

    schema_version: int
    job_dir: Path
    job_id: str
    state: JobLifecycleState
    selected_reference: Mapping[str, Any] | None
    install_source: Mapping[str, Any] | None
    repository_identity: str | None
    canonical_repo_url: str | None
    exact_revision: str | None
    destination_root: str | None
    resolved_project_destination: str | None
    artifact_names: tuple[str, ...]

    @classmethod
    def load(cls, job_dir: str | Path) -> "JobContext":
        return _Loader(Path(job_dir)).load()


def load_job_context(job_dir: str | Path) -> JobContext:
    return JobContext.load(job_dir)


_ARTIFACTS = (
    "job.json", "session.json", "preflight.json", "plan.json", "execution_state.json",
    "result.json", "install_research.json", "installation_plan.json",
    "installation_execution_state.json",
)
_PLACEHOLDERS = {"", "unknown", "none", "null", "n/a"}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _fail(code: str, message: str, artifact: str | None = None,
          field: str | None = None) -> None:
    raise JobContextValidationError(JobContextValidationFailure(code, message, artifact, field))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _known(value: Any) -> str | None:
    text = str(value or "").strip()
    return None if text.casefold() in _PLACEHOLDERS else text


def _repo_from_url(value: Any) -> str | None:
    text = _known(value)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.hostname and parsed.hostname.casefold() == "github.com":
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return None


def _normal_repo(value: Any) -> str | None:
    text = _known(value)
    if not text:
        return None
    from_url = _repo_from_url(text)
    text = from_url or text.removesuffix(".git").strip("/")
    return text.casefold() if text.count("/") == 1 else None


def _normal_url(value: Any) -> str | None:
    text = _known(value)
    if not text:
        return None
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    if parsed.hostname and parsed.hostname.casefold() == "github.com":
        path = path.removesuffix(".git")
    return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", "", ""))


def _normal_path(value: Any) -> str | None:
    text = _known(value)
    if not text:
        return None
    return os.path.normcase(os.path.normpath(os.path.abspath(text)))


def _one(values: Iterable[tuple[str, Any]], normalizer: Any, code: str,
         description: str) -> tuple[str | None, str | None]:
    normalized: dict[str, list[tuple[str, str]]] = {}
    for source, raw in values:
        display = _known(raw)
        normalized_value = normalizer(raw)
        if display and normalized_value:
            normalized.setdefault(normalized_value, []).append((source, display))
    if len(normalized) > 1:
        detail = "; ".join(f"{key}: {', '.join(src for src, _ in rows)}"
                           for key, rows in normalized.items())
        _fail(code, f"Conflicting persisted {description}: {detail}")
    if not normalized:
        return None, None
    rows = next(iter(normalized.values()))
    return next(iter(normalized)), rows[0][1]


class _Loader:
    def __init__(self, job_dir: Path) -> None:
        self.root = job_dir.resolve()
        self.data: dict[str, Any] = {}
        self.present: list[str] = []

    def _read_json(self, path: Path, label: str) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _fail("INVALID_PERSISTED_ARTIFACT", f"Cannot read valid JSON: {exc}", label)

    def _load_artifacts(self) -> None:
        if not self.root.is_dir():
            _fail("JOB_DIRECTORY_NOT_FOUND", "Persisted job directory does not exist.")
        for name in _ARTIFACTS:
            path = self.root / name
            if path.is_file():
                self.data[name] = self._read_json(path, name)
                self.present.append(name)
        if "job.json" not in self.data or not isinstance(self.data["job.json"], dict):
            _fail("MISSING_JOB_ARTIFACT", "job.json is required.", "job.json")
        # Legacy UI sessions and source snapshots live beside the jobs tree.
        if "session.json" not in self.data and self.root.parent.name in {"jobs", "jobs_dry_run"}:
            tasks_name = "tasks" if self.root.parent.name == "jobs" else "tasks_dry_run"
            candidate = self.root.parent.parent / tasks_name / self.root.name / "session.json"
            if candidate.is_file():
                self.data["session.json"] = self._read_json(candidate, "session.json")
                self.present.append("session.json")
        manifest = self.root / "snapshot_manifest.json"
        if not manifest.is_file() and self.root.parent.name in {"jobs", "jobs_dry_run"}:
            snapshots_name = "source_snapshots" if self.root.parent.name == "jobs" else "source_snapshots_dry_run"
            manifest = self.root.parent.parent / snapshots_name / self.root.name / "snapshot_manifest.json"
        if manifest.is_file():
            self.data["snapshot_manifest.json"] = self._read_json(manifest, "snapshot_manifest.json")
            self.data["_snapshot_dir"] = manifest.parent
            self.present.append("snapshot_manifest.json")
        audit = self.root / "audit.jsonl"
        if audit.is_file():
            rows = []
            for number, line in enumerate(audit.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    _fail("INVALID_AUDIT_RECORD", f"Invalid JSON on line {number}: {exc}", "audit.jsonl")
            self.data["audit.jsonl"] = rows
            self.present.append("audit.jsonl")

    @staticmethod
    def _acquire_step(plan: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        if not isinstance(plan, dict):
            return None
        return next((step for step in plan.get("steps", [])
                     if isinstance(step, dict) and step.get("type") == "ACQUIRE_GITHUB_SNAPSHOT"), None)

    def _selection(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
        job = self.data["job.json"]
        selected = [row for row in job.get("selected_sources", []) if isinstance(row, dict)]
        reference = job.get("selected_reference") if isinstance(job.get("selected_reference"), dict) else None
        source = job.get("install_source") if isinstance(job.get("install_source"), dict) else None
        if not reference:
            candidates = [row.get("selected_reference") or row.get("reference_source") for row in selected]
            candidates = [row for row in candidates if isinstance(row, dict)]
            if len(candidates) == 1:
                reference = candidates[0]
        if not source:
            candidates = [row.get("install_source") for row in selected]
            candidates = [row for row in candidates if isinstance(row, dict)]
            if len(candidates) == 1:
                source = candidates[0]
        if not source and len(selected) == 1:
            row = selected[0]
            if _normal_repo(row.get("repository") or row.get("repo") or row.get("canonical_url") or row.get("url")):
                source = row
        if not reference and len(selected) == 1:
            row = selected[0]
            if _known(row.get("url") or row.get("canonical_url")):
                reference = row
        return reference, source, selected

    def _validate_bindings(self, job: dict[str, Any], plan: dict[str, Any] | None,
                           install_plan: dict[str, Any] | None) -> tuple[bool, bool]:
        source_approval = job.get("approval")
        if source_approval:
            if not plan:
                _fail("APPROVAL_WITHOUT_PLAN", "Source approval has no source plan.", "job.json", "approval")
            ids = [step.get("step_id") for step in plan.get("steps", [])]
            if (source_approval.get("plan_hash") != plan_hash(plan)
                    or source_approval.get("plan_version") != plan.get("plan_version")
                    or not set(source_approval.get("approved_step_ids", [])).issubset(ids)):
                _fail("PLAN_HASH_MISMATCH", "Source approval is not bound to the current source plan.",
                      "job.json", "approval")

        source_state = self.data.get("execution_state.json")
        if isinstance(source_state, dict):
            has_state = bool(source_state.get("steps") or source_state.get("plan_hash")
                             or source_state.get("plan_kind"))
            if has_state and not plan:
                _fail("EXECUTION_STATE_WITHOUT_PLAN", "Source execution state has no source plan.",
                      "execution_state.json")
            if has_state:
                if not source_approval:
                    _fail("EXECUTION_WITHOUT_CURRENT_APPROVAL",
                          "Source execution state exists without current-plan approval.",
                          "execution_state.json")
                if source_state.get("plan_kind") not in (None, SOURCE_PLAN_KIND):
                    _fail("PLAN_KIND_MISMATCH", "Source execution state is bound to the wrong plan kind.",
                          "execution_state.json", "plan_kind")
                if (source_state.get("plan_hash") != plan_hash(plan)
                        or source_state.get("plan_version") != plan.get("plan_version")):
                    _fail("PLAN_HASH_MISMATCH", "Source execution state is not bound to the current source plan.",
                          "execution_state.json")

        install_approval = job.get("installation_plan_approval")
        approved = False
        if install_approval:
            if not install_plan:
                _fail("APPROVAL_WITHOUT_PLAN", "Installation approval has no installation plan.",
                      "job.json", "installation_plan_approval")
            executable = [step.get("step_id") for step in install_plan.get("steps", [])
                          if step.get("type") in EXECUTABLE_TYPES]
            reviewed = [step.get("step_id") for step in install_plan.get("steps", [])]
            review_only = [step.get("step_id") for step in install_plan.get("steps", [])
                           if step.get("type") not in EXECUTABLE_TYPES]
            approved = (
                install_approval.get("plan_kind") == INSTALLATION_PLAN_KIND
                and install_approval.get("plan_hash") == plan_hash(install_plan)
                and install_approval.get("plan_version") == install_plan.get("schema_version")
                and install_approval.get("approved_step_ids") == executable
                and install_approval.get("reviewed_step_ids") == reviewed
                and install_approval.get("review_only_step_ids") == review_only
            )
            if not approved:
                _fail("PLAN_HASH_MISMATCH", "Installation approval is not bound exactly to the current plan.",
                      "job.json", "installation_plan_approval")

        install_state = self.data.get("installation_execution_state.json")
        has_install_state = isinstance(install_state, dict) and bool(
            install_state.get("steps") or install_state.get("plan_hash") or install_state.get("plan_kind")
        )
        if has_install_state:
            if not install_plan:
                _fail("EXECUTION_STATE_WITHOUT_PLAN", "Installation execution state has no plan.",
                      "installation_execution_state.json")
            if install_state.get("plan_kind") != INSTALLATION_PLAN_KIND:
                _fail("PLAN_KIND_MISMATCH", "Installation execution state is bound to the wrong plan kind.",
                      "installation_execution_state.json", "plan_kind")
            if (install_state.get("plan_hash") != plan_hash(install_plan)
                    or install_state.get("plan_version") != install_plan.get("schema_version")):
                _fail("PLAN_HASH_MISMATCH", "Installation execution state is not bound to the current plan.",
                      "installation_execution_state.json")
            if not approved:
                _fail("EXECUTION_WITHOUT_CURRENT_APPROVAL",
                      "Installation execution state exists without exact current-plan approval.",
                      "installation_execution_state.json")
        return approved, has_install_state

    def _validate_snapshot(self, repo: str | None, revision: str | None,
                           reference_id: str | None) -> None:
        manifest = self.data.get("snapshot_manifest.json")
        if manifest is None:
            return
        if not isinstance(manifest, list):
            _fail("INVALID_SNAPSHOT_MANIFEST", "Snapshot manifest must be a list.", "snapshot_manifest.json")
        matching = []
        for row in manifest:
            if not isinstance(row, dict):
                continue
            row_repo = _normal_repo(row.get("repository") or row.get("repo")
                                    or row.get("canonical_url") or row.get("url"))
            if repo and row_repo == repo:
                matching.append(row)
        if repo and not matching:
            _fail("SNAPSHOT_IDENTITY_MISMATCH", "No source snapshot matches the persisted repository identity.",
                  "snapshot_manifest.json")
        for row in matching:
            row_revision = _known(row.get("commit") or row.get("exact_commit") or row.get("exact_revision"))
            if revision and row_revision and row_revision.casefold() != revision.casefold():
                _fail("SNAPSHOT_IDENTITY_MISMATCH", "Snapshot revision conflicts with the selected revision.",
                      "snapshot_manifest.json", "commit")
            nested = row.get("selected_reference")
            nested_id = _known((nested or {}).get("publication_identity")) if isinstance(nested, dict) else None
            if reference_id and nested_id and nested_id.casefold() != reference_id.casefold():
                _fail("PUBLICATION_IDENTITY_MISMATCH", "Snapshot publication identity conflicts with selection.",
                      "snapshot_manifest.json", "selected_reference.publication_identity")
            digest = _known(row.get("sha256"))
            filename = _known(row.get("readme"))
            if digest and not _SHA256.fullmatch(digest):
                _fail("SNAPSHOT_INTEGRITY_MISMATCH", "Snapshot SHA-256 is malformed.",
                      "snapshot_manifest.json", "sha256")
            if digest and filename:
                body = self.data["_snapshot_dir"] / filename
                if body.is_file() and hashlib.sha256(body.read_bytes()).hexdigest().casefold() != digest.casefold():
                    _fail("SNAPSHOT_INTEGRITY_MISMATCH", "Snapshot body does not match its persisted SHA-256.",
                          "snapshot_manifest.json", "sha256")

    def load(self) -> JobContext:
        self._load_artifacts()
        job = self.data["job.json"]
        job_id = _known(job.get("job_id"))
        if not job_id or job_id != self.root.name:
            _fail("JOB_ID_MISMATCH", "job.json job_id must equal the persisted directory name.",
                  "job.json", "job_id")
        for row in self.data.get("audit.jsonl", []):
            audit_id = _known(row.get("job_id")) if isinstance(row, dict) else None
            if audit_id and audit_id != job_id:
                _fail("JOB_ID_MISMATCH", "Audit record belongs to a different job.", "audit.jsonl", "job_id")

        reference, install_source, selected = self._selection()
        plan = self.data.get("plan.json") if isinstance(self.data.get("plan.json"), dict) else None
        install_plan = (self.data.get("installation_plan.json")
                        if isinstance(self.data.get("installation_plan.json"), dict) else None)
        research = (self.data.get("install_research.json")
                    if isinstance(self.data.get("install_research.json"), dict) else None)
        acquire = self._acquire_step(plan)

        reference_claims: list[tuple[str, Any]] = []
        reference_url_claims: list[tuple[str, Any]] = []
        if reference:
            reference_claims.extend([
                ("selected_reference.publication_identity", reference.get("publication_identity")),
            ])
            reference_url_claims.append((
                "selected_reference.canonical_url",
                reference.get("canonical_url") or reference.get("url"),
            ))
        for index, row in enumerate(selected):
            nested = row.get("selected_reference") or row.get("reference_source")
            if isinstance(nested, dict):
                reference_claims.append((f"selected_sources[{index}].publication_identity",
                                         nested.get("publication_identity")))
                reference_url_claims.append((
                    f"selected_sources[{index}].canonical_url",
                    nested.get("canonical_url") or nested.get("url"),
                ))
        _, reference_id = _one(reference_claims, lambda x: _known(x).casefold() if _known(x) else None,
                               "PUBLICATION_IDENTITY_MISMATCH", "publication identity")
        _one(reference_url_claims, _normal_url, "PUBLICATION_IDENTITY_MISMATCH",
             "publication canonical URL")

        identity_maps: list[tuple[str, Mapping[str, Any]]] = []
        if install_source:
            identity_maps.append(("install_source", install_source))
        verified = job.get("verified_source")
        if isinstance(verified, dict):
            identity_maps.append(("verified_source", verified))
        if acquire:
            identity_maps.append(("plan.acquire.inputs", acquire.get("inputs", {})))
        source_state = self.data.get("execution_state.json")
        if isinstance(source_state, dict):
            state_step = self._acquire_step(source_state)
            if state_step:
                identity_maps.append(("execution_state.acquire.inputs", state_step.get("inputs", {})))
                identity_maps.append(("execution_state.acquire.result", state_step.get("result", {})))
        if research and isinstance(research.get("source_identity"), dict):
            identity_maps.append(("install_research.source_identity", research["source_identity"]))
        if install_plan and isinstance(install_plan.get("source_identity"), dict):
            identity_maps.append(("installation_plan.source_identity", install_plan["source_identity"]))

        repo_claims: list[tuple[str, Any]] = []
        url_claims: list[tuple[str, Any]] = []
        revision_claims: list[tuple[str, Any]] = []
        destination_claims: list[tuple[str, Any]] = []
        for label, value in identity_maps:
            repo_claims.append((label, value.get("repository_identity") or value.get("repository")
                                or value.get("repo") or value.get("canonical_url") or value.get("url")))
            url_claims.append((label, value.get("canonical_url") or value.get("url")))
            revision_claims.append((label, value.get("exact_revision") or value.get("exact_commit")
                                    or value.get("commit")))
            destination_claims.append((label, value.get("destination") or value.get("local_path")))
        repo_key, repo_display = _one(repo_claims, _normal_repo, "REPOSITORY_IDENTITY_MISMATCH",
                                      "repository identity")
        _, url_display = _one(url_claims, _normal_url, "REPOSITORY_URL_MISMATCH", "repository URL")
        revision_key, revision_display = _one(
            revision_claims, lambda x: _known(x).casefold() if _known(x) else None,
            "REVISION_MISMATCH", "exact revision",
        )
        _, project_display = _one(destination_claims, _normal_path, "DESTINATION_MISMATCH",
                                  "project destination")
        if repo_key and url_display and _normal_repo(url_display) != repo_key:
            _fail("REPOSITORY_URL_MISMATCH", "Canonical repository URL conflicts with repository identity.")

        root_claims = [
            ("job.destination_root", job.get("destination_root")),
            ("job.normal_ui_destination", job.get("normal_ui_destination")),
            ("plan.normal_ui_destination", (plan or {}).get("normal_ui_destination")),
        ]
        _, root_display = _one(root_claims, _normal_path, "DESTINATION_MISMATCH", "destination root")
        if root_display and project_display:
            root_norm = _normal_path(root_display)
            project_norm = _normal_path(project_display)
            try:
                if os.path.commonpath((root_norm, project_norm)) != root_norm or root_norm == project_norm:
                    _fail("DESTINATION_MISMATCH",
                          "Resolved project destination must be a child of the selected destination root.")
            except ValueError:
                _fail("DESTINATION_MISMATCH", "Destination root and project destination are on different roots.")

        has_progress = bool(selected or reference or install_source or plan or research or install_plan
                            or self.data.get("execution_state.json") or self.data.get("result.json"))
        if has_progress and not reference:
            _fail("MISSING_PUBLICATION_IDENTITY", "Persisted progress has no immutable selected publication identity.")
        if has_progress and not install_source:
            _fail("MISSING_INSTALL_SOURCE_IDENTITY", "Persisted progress has no immutable install-source identity.")
        if install_source and not repo_display:
            _fail("MISSING_REPOSITORY_IDENTITY", "Install source has no stable repository identity.")

        self._validate_snapshot(repo_key, revision_key, reference_id)
        approved, has_install_state = self._validate_bindings(job, plan, install_plan)

        state = JobLifecycleState.SEARCH
        if reference and install_source:
            state = JobLifecycleState.SOURCE_SELECTED
        if state == JobLifecycleState.SOURCE_SELECTED and repo_display and url_display and revision_display:
            state = JobLifecycleState.SOURCE_VERIFIED
        if state == JobLifecycleState.SOURCE_VERIFIED and root_display and project_display:
            state = JobLifecycleState.DESTINATION_SELECTED

        result = self.data.get("result.json")
        acquired = False
        if state == JobLifecycleState.DESTINATION_SELECTED and acquire and isinstance(source_state, dict) \
                and isinstance(result, dict):
            executed = self._acquire_step(source_state)
            acquired = bool(
                executed and executed.get("status") == "SUCCESS"
                and result.get("final_status") == "VERIFIED"
                and result.get("source_verified") is True
                and result.get("install_verified") is True
            )
            if acquired:
                state = JobLifecycleState.SOURCE_ACQUIRED
        if state == JobLifecycleState.DESTINATION_SELECTED and acquire and isinstance(source_state, dict):
            if job.get("state") == "STOPPED" and source_state.get("stop_reason"):
                state = JobLifecycleState.STOPPED
            elif job.get("state") == "RUNNING":
                state = JobLifecycleState.EXECUTING

        research_complete = bool(
            state == JobLifecycleState.SOURCE_ACQUIRED and research
            and research.get("status") in {"COMPLETE", "REVIEW_REQUIRED"}
            and isinstance(research.get("source_identity"), dict)
            and research["source_identity"].get("acquisition_verified") is True
        )
        if research_complete:
            state = JobLifecycleState.RESEARCH_COMPLETE
        if state == JobLifecycleState.RESEARCH_COMPLETE and install_plan \
                and install_plan.get("status") in {"BLOCKED", "REVIEW_REQUIRED", "READY_FOR_APPROVAL"}:
            state = JobLifecycleState.PLAN_REVIEW
        if state == JobLifecycleState.PLAN_REVIEW and approved:
            state = JobLifecycleState.APPROVED

        if has_install_state and state == JobLifecycleState.APPROVED:
            install_state = self.data["installation_execution_state.json"]
            steps = install_state.get("steps", [])
            stopped = next((step for step in steps if step.get("status") in {
                "STOPPED", "FAILED", "WAITING_FOR_USER"
            }), None)
            if steps and all(step.get("status") == "SUCCESS" for step in steps):
                state = JobLifecycleState.VERIFIED
            elif stopped or install_state.get("stop_reason") or install_state.get("cancel_requested"):
                reason = (stopped or {}).get("deviation_id") or (stopped or {}).get("reason") \
                    or install_state.get("stop_reason")
                if reason and reason in install_state.get("approved_deviations", []):
                    state = JobLifecycleState.RESUMABLE
                else:
                    state = JobLifecycleState.STOPPED
            else:
                state = JobLifecycleState.EXECUTING

        return JobContext(
            schema_version=1,
            job_dir=self.root,
            job_id=job_id,
            state=state,
            selected_reference=_freeze(dict(reference)) if reference else None,
            install_source=_freeze(dict(install_source)) if install_source else None,
            repository_identity=repo_display,
            canonical_repo_url=url_display,
            exact_revision=revision_display,
            destination_root=root_display,
            resolved_project_destination=project_display,
            artifact_names=tuple(sorted(self.present)),
        )


__all__ = [
    "JobContext", "JobContextValidationError", "JobContextValidationFailure",
    "JobLifecycleState", "load_job_context",
]
