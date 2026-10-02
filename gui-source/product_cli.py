"""Persistent, approval-gated CLI integration for Product Core v1."""
from __future__ import annotations

import hashlib
import json
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from audit import AuditLogger, read_tail
from conda_bootstrap import MISSING_CONDA, detect_conda, install_miniforge
from executor import PlanExecutor, verify_extracted_tree
from storage_safety import InterProcessFileLock, LockUnavailable, atomic_write_json
from plan import build_plan
from installation_plan import EXECUTABLE_TYPES, execution_status
from preflight import inspect_environment
from resume import resume_status
import handoff_debug


SOURCE_PLAN_KIND = "SOURCE_ACQUISITION"
INSTALLATION_PLAN_KIND = "POST_RESEARCH_INSTALLATION"
_EXISTING_ACQUISITION_DESTINATION_REASON = (
    "Repository acquisition destination already exists; nothing was overwritten"
)
_MAX_SNAPSHOT_REUSE_CANDIDATES = 256


def _audit(store: "JobStore", plan_kind: str) -> AuditLogger:
    return AuditLogger(
        store.path("audit.jsonl"), store.job_id,
        default_metadata={"plan_kind": plan_kind},
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


_PLAN_IDENTITY_ENVELOPE_FIELDS = frozenset({
    "job_id",
    "created_at",
    "updated_at",
    "plan_hash",
})


def _plan_identity_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable approval/execution contract without storage metadata."""
    return {
        key: item
        for key, item in value.items()
        if key not in _PLAN_IDENTITY_ENVELOPE_FIELDS
    }


def plan_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        _plan_identity_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class JobStore:
    def __init__(self, jobs_root: Path, job_id: str) -> None:
        if not job_id or Path(job_id).name != job_id or job_id in (".", ".."):
            raise SystemExit("Invalid job id.")
        self.root = jobs_root / job_id
        self.job_id = job_id

    def path(self, name: str) -> Path:
        return self.root / name

    def lifecycle_lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(self.path(".execution.lock"), blocking=False)

    def state_lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(
            self.path(".execution-state.lock"), blocking=True, timeout=30.0
        )

    def load(self, name: str, default: Any = None) -> Any:
        path = self.path(name)
        return read_json(path) if path.exists() else default

    def save(self, name: str, value: Any) -> None:
        write_json(self.path(name), value)

    def update_execution_state(
        self,
        name: str,
        default: dict[str, Any],
        mutation: Callable[[dict[str, Any]], bool | None],
    ) -> dict[str, Any]:
        """Apply one short, cross-process-safe execution-state transaction."""
        if name not in {"execution_state.json", "installation_execution_state.json"}:
            raise ValueError(f"Unsupported execution-state artifact: {name}")
        with self.state_lock():
            state = self.load(name, dict(default))
            should_save = mutation(state)
            if should_save is not False:
                self.save(name, state)
            return state

    def job(self, goal: str | None = None) -> dict[str, Any]:
        value = self.load("job.json")
        if value is None:
            value = {
                "schema_version": 1, "job_id": self.job_id, "goal": goal,
                "state": "NEW", "created_at": utc_now(), "updated_at": utc_now(),
                "approval": None,
            }
            self.save_job(value)
            handoff_debug.trace(
                "JOB_CREATED",
                job_id=self.job_id,
                goal=goal,
                job_path=str(self.path("job.json").resolve()),
            )
        return value

    def save_job(self, value: dict[str, Any]) -> None:
        value["updated_at"] = utc_now()
        self.save("job.json", value)


@contextmanager
def _job_lifecycle(store: "JobStore"):
    try:
        with store.lifecycle_lock():
            yield
    except LockUnavailable as exc:
        raise SystemExit(
            f"JOB_BUSY: job {store.job_id} is already being approved, executed, or resumed"
        ) from exc


def sync_scout_job(jobs_root: Path, task: dict[str, Any]) -> None:
    store = JobStore(jobs_root, task["task_id"])
    job = store.job(task.get("goal"))
    job["goal"] = task.get("goal")
    job["state"] = "WAITING_FOR_SELECTION" if task.get("state") == "WAITING_FOR_USER_SELECTION" else job["state"]
    job["selected_sources"] = task.get("selected", [])
    store.save_job(job)


def record_selection(jobs_root: Path, task: dict[str, Any]) -> None:
    store = JobStore(jobs_root, task["task_id"])
    job = store.job(task.get("goal"))
    job["selected_sources"] = task.get("selected", [])
    job["state"] = "NEW"
    store.save_job(job)


def run_preflight(jobs_root: Path, job_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    job = store.job()
    if not job.get("selected_sources"):
        raise SystemExit("Preflight blocked: select a source first.")
    selected = job["selected_sources"][0]
    verified = job.get("verified_source") or {}
    source = {**selected, **{
        "url": verified.get("url") or selected.get("url"),
        "repository": verified.get("repo") or selected.get("repository"),
        "exact_revision": verified.get("commit") or selected.get("exact_revision"),
    }}
    if not source.get("url") or source.get("exact_revision") in (None, "", "UNKNOWN"):
        raise SystemExit("Preflight blocked: original source and exact revision must be verified first.")
    result = inspect_environment(spec)
    result["source_verification"] = {
        "url": source.get("url"), "repository": source.get("repository"),
        "source_type": source.get("source_type"), "exact_revision": source.get("exact_revision"),
        "verified_original_project_url": source.get("verified_original_project_url"),
        "reference_source": source.get("reference_source"), "result_identity": source.get("result_identity"),
    }
    result["reusable_assets_verified"] = True
    result["destination_requirements_verified"] = True
    job["preflight"] = result
    job["preflight_spec"] = spec
    job["state"] = "PREFLIGHT_COMPLETE"
    store.save_job(job)
    handoff_debug.trace(
        "PREFLIGHT_RESULT",
        job_id=job_id,
        job_path=str(store.path("job.json").resolve()),
        persisted_url=result["source_verification"].get("url"),
        persisted_repo=result["source_verification"].get("repository"),
        persisted_revision=result["source_verification"].get("exact_revision"),
    )
    return result


def _selected_source(job: dict[str, Any], task: dict[str, Any] | None) -> dict[str, Any] | None:
    explicit = job.get("verified_source")
    if explicit:
        return explicit
    snapshots = (task or {}).get("source_snapshots", [])
    if not snapshots:
        return None
    first = snapshots[0]
    return {
        "selected": True, "url": first.get("url"), "repo": first.get("repository"),
        "commit": first.get("commit"), "package_metadata": {"version": first.get("tag")},
    }


def create_plan(jobs_root: Path, job_id: str, task: dict[str, Any] | None,
                request: dict[str, Any]) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        return _create_plan_locked(store, task, request)


def _create_plan_locked(store: JobStore, task: dict[str, Any] | None,
                        request: dict[str, Any]) -> dict[str, Any]:
    job = store.job((task or {}).get("goal"))
    if job.get("state") != "PREFLIGHT_COMPLETE" or "preflight" not in job:
        raise SystemExit("Plan blocked: preflight must complete first.")
    source = _selected_source(job, task)
    request = dict(request)
    request["approved"] = True  # means structurally ready for approval, not user-approved
    value = build_plan(job.get("goal") or (task or {}).get("goal", ""), source, job["preflight"], request)
    value.update({"schema_version": 1, "plan_version": 1, "job_id": store.job_id, "created_at": utc_now()})
    store.save("plan.json", value)
    job["approval"] = None
    job["state"] = "WAITING_FOR_APPROVAL" if value["status"] == "READY_FOR_APPROVAL" else "STOPPED"
    store.save_job(job)
    return value


def format_plan(value: dict[str, Any]) -> str:
    sections = [
        ("REUSE EXISTING", value.get("local_assets_to_reuse", [])),
        ("DOWNLOAD", value.get("planned_downloads", [])),
        ("MODIFY", value.get("planned_modifications", [])),
        ("MANUAL ACTION REQUIRED", value.get("manual_user_actions", [])),
        ("CONFLICTS", value.get("cross_project_conflicts", [])),
        ("UNKNOWN", [x for x in value.get("block_reasons", []) if "UNKNOWN" in str(x)]),
        ("FORBIDDEN", value.get("forbidden_downloads", []) + value.get("forbidden_modifications", [])),
        ("VERIFICATION", value.get("verification_criteria", [])),
        ("ROLLBACK", value.get("rollback_scope", [])),
    ]
    lines: list[str] = []
    for title, rows in sections:
        lines.extend([title, json.dumps(rows, ensure_ascii=False, indent=2), ""])
    lines.extend(["FINAL PLAN STATUS", str(value.get("status", "UNKNOWN"))])
    return "\n".join(lines)


def approve_plan(jobs_root: Path, job_id: str, step_ids: list[str], scope: str) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        return _approve_plan_locked(store, step_ids, scope)


def _approve_plan_locked(store: JobStore, step_ids: list[str], scope: str) -> dict[str, Any]:
    job, value = store.job(), store.load("plan.json")
    if not value:
        raise SystemExit("Approval blocked: plan.json does not exist.")
    if value.get("status") != "READY_FOR_APPROVAL":
        raise SystemExit(f"Approval blocked: plan status is {value.get('status')}.")
    available = {step.get("step_id") for step in value.get("steps", [])}
    approved = list(dict.fromkeys(step_ids))
    if not approved or not set(approved) <= available:
        raise SystemExit("Approval must name one or more valid step IDs.")
    record = {
        "plan_hash": plan_hash(value), "plan_version": value.get("plan_version"),
        "timestamp": utc_now(), "approval_scope": scope, "approved_step_ids": approved,
    }
    job["approval"] = record
    job["state"] = "APPROVED"
    store.save_job(job)
    AuditLogger(store.path("audit.jsonl"), store.job_id).append(
        "PLAN_APPROVAL",
        "approve persisted plan",
        "APPROVED",
        metadata={
            "plan_hash": record["plan_hash"],
            "plan_version": record["plan_version"],
            "approval_scope": record["approval_scope"],
            "approved_step_ids": record["approved_step_ids"],
        },
    )
    return record


def _approved_plan(store: JobStore) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    job, value = store.job(), store.load("plan.json")
    approval = job.get("approval")
    if not value or not approval:
        raise SystemExit("Execution rejected: explicit approval is required.")
    if approval.get("plan_hash") != plan_hash(value) or approval.get("plan_version") != value.get("plan_version"):
        job["state"] = "WAITING_FOR_APPROVAL"
        job["approval"] = None
        store.save_job(job)
        raise SystemExit("Execution rejected: plan changed after approval; approval is INVALID.")
    if value.get("status") != "READY_FOR_APPROVAL":
        raise SystemExit(f"Execution rejected: plan status is {value.get('status')}.")
    approved = set(approval.get("approved_step_ids", []))
    runtime = json.loads(json.dumps(value))
    for step in runtime.get("steps", []):
        step["approved"] = step.get("step_id") in approved
    return job, value, runtime


def _bind_execution_state_to_plan(
    store: JobStore,
    saved_plan: dict[str, Any],
    state: dict[str, Any],
    *,
    plan_kind: str = SOURCE_PLAN_KIND,
    version_field: str = "plan_version",
) -> None:
    """Reject persisted execution results that do not belong to this exact plan."""
    current_identity = plan_hash(saved_plan)
    persisted_identity = state.get("plan_hash")
    current_version = saved_plan.get(version_field)
    persisted_version = state.get("plan_version")
    persisted_kind = state.get("plan_kind")
    has_persisted_results = bool(state.get("steps"))

    stale_reason = ""
    if has_persisted_results and not persisted_identity:
        stale_reason = "MISSING_PLAN_IDENTITY"
    elif has_persisted_results and plan_kind == INSTALLATION_PLAN_KIND and not persisted_kind:
        stale_reason = "MISSING_PLAN_KIND"
    elif persisted_kind and persisted_kind != plan_kind:
        stale_reason = "PLAN_KIND_MISMATCH"
    elif persisted_identity and (
        persisted_identity != current_identity or persisted_version != current_version
    ):
        stale_reason = "PLAN_IDENTITY_MISMATCH"

    if stale_reason:
        _audit(store, plan_kind).append(
            "STALE_EXECUTION_STATE",
            "execution state plan-identity validation",
            "STOPPED",
            metadata={
                "reason": stale_reason,
                "current_plan_hash": current_identity,
                "persisted_plan_hash": persisted_identity,
                "current_plan_version": current_version,
                "persisted_plan_version": persisted_version,
                "persisted_plan_kind": persisted_kind,
            },
        )
        raise SystemExit(
            f"Execution state rejected: STALE_EXECUTION_STATE ({stale_reason}); "
            "fresh approval/execution state is required."
        )

    state["plan_hash"] = current_identity
    state["plan_version"] = current_version
    state["plan_kind"] = plan_kind


def _restore_execution(runtime: dict[str, Any], state: dict[str, Any]) -> None:
    """Restore execution results without allowing saved state to widen approval."""
    prior = {row.get("step_id"): row for row in state.get("steps", [])}
    approved = {step["step_id"]: step.get("approved") is True for step in runtime.get("steps", [])}
    for step in runtime.get("steps", []):
        saved = prior.get(step.get("step_id"))
        if saved:
            for key in (
                "status", "result", "reason", "deviation_id", "files_changed",
                "acquisition_evidence",
            ):
                if key in saved:
                    step[key] = saved[key]
        step["approved"] = approved.get(step.get("step_id"), False)


def _canonical_source_url(value: Any) -> str:
    normalized = str(value or "").strip().casefold().rstrip("/")
    return normalized[:-4] if normalized.endswith(".git") else normalized


def _snapshot_identity(step: dict[str, Any]) -> dict[str, str] | None:
    if step.get("type") != "ACQUIRE_GITHUB_SNAPSHOT":
        return None
    values = step.get("inputs") or {}
    repository = str(values.get("repository") or "").strip().casefold()
    canonical_url = _canonical_source_url(values.get("canonical_url"))
    exact_commit = str(values.get("exact_commit") or "").strip().casefold()
    destination_value = str(values.get("destination") or "").strip()
    if not all((repository, canonical_url, exact_commit, destination_value)):
        return None
    destination = str(Path(destination_value).expanduser().resolve(strict=False)).casefold()
    return {
        "repository": repository,
        "canonical_url": canonical_url,
        "exact_commit": exact_commit,
        "destination": destination,
    }


def _recent_job_directories(store: JobStore) -> list[Path]:
    candidates: list[tuple[int, Path]] = []
    try:
        entries = list(store.root.parent.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or entry.name == store.job_id:
            continue
        try:
            candidates.append((entry.stat().st_mtime_ns, entry))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in candidates[:_MAX_SNAPSHOT_REUSE_CANDIDATES]]


def _verified_prior_snapshot(
    store: JobStore, identity: dict[str, str], destination: Path,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    for directory in _recent_job_directories(store):
        candidate = JobStore(store.root.parent, directory.name)
        try:
            job = candidate.load("job.json") or {}
            plan = candidate.load("plan.json") or {}
            state = candidate.load("execution_state.json") or {}
            result = candidate.load("result.json") or {}
            approval = job.get("approval") or {}
            candidate_hash = plan_hash(plan)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if (
            job.get("state") not in {"VERIFIED", "CLOSED"}
            or result.get("final_status") != "VERIFIED"
            or result.get("source_verified") is not True
            or plan.get("status") != "READY_FOR_APPROVAL"
            or approval.get("plan_hash") != candidate_hash
            or approval.get("plan_version") != plan.get("plan_version")
            or state.get("plan_hash") != candidate_hash
            or state.get("plan_version") != plan.get("plan_version")
            or state.get("plan_kind") != SOURCE_PLAN_KIND
        ):
            continue
        approved_steps = set(approval.get("approved_step_ids") or [])
        plan_steps = {row.get("step_id"): row for row in plan.get("steps", [])}
        for saved in state.get("steps", []):
            step_id = saved.get("step_id")
            planned = plan_steps.get(step_id) or {}
            if (
                step_id not in approved_steps
                or saved.get("status") != "SUCCESS"
                or _snapshot_identity(planned) != identity
            ):
                continue
            evidence = saved.get("acquisition_evidence") or {}
            fidelity = evidence.get("source_fidelity") or {}
            manifest = evidence.get("extracted_tree_manifest") or {}
            if (
                fidelity.get("archive_tree_equivalent") is not True
                or fidelity.get("scope") != "COMPLETE_ARCHIVE_MEMBERS"
                or str(manifest.get("repository") or "").strip().casefold()
                != identity["repository"]
                or str(manifest.get("exact_commit") or "").strip().casefold()
                != identity["exact_commit"]
            ):
                continue
            verification = verify_extracted_tree(destination, evidence)
            if verification.get("state") == "VERIFIED":
                return directory.name, saved, evidence, verification
    return None


def _reuse_verified_github_snapshots(store: JobStore, runtime: dict[str, Any]) -> None:
    for step in runtime.get("steps", []):
        if step.get("approved") is not True:
            continue
        status = step.get("status") or "PENDING"
        if status == "STOPPED" and step.get("reason") != _EXISTING_ACQUISITION_DESTINATION_REASON:
            continue
        if status not in {"PENDING", "STOPPED"}:
            continue
        identity = _snapshot_identity(step)
        if identity is None:
            continue
        destination = Path((step.get("inputs") or {}).get("destination", ""))
        if not destination.is_dir():
            continue
        prior = _verified_prior_snapshot(store, identity, destination)
        if prior is None:
            continue
        prior_job_id, saved, evidence, verification = prior
        step["status"] = "SUCCESS"
        step["result"] = copy.deepcopy(saved.get("result") or {})
        step["result"]["destination"] = str(destination)
        step["acquisition_evidence"] = copy.deepcopy(evidence)
        step["files_changed"] = []
        step.pop("reason", None)
        step.pop("deviation_id", None)
        _audit(store, SOURCE_PLAN_KIND).append(
            "REUSE_VERIFIED_GITHUB_SNAPSHOT",
            "read-only verified source snapshot reuse",
            "SUCCESS",
            working_directory=destination,
            metadata={
                "prior_job_id": prior_job_id,
                "repository": identity["repository"],
                "exact_commit": identity["exact_commit"],
                "destination": str(destination.resolve(strict=False)),
                "tree_sha256": verification.get("tree_sha256"),
                "file_count": verification.get("file_count"),
                "verification_reason": verification.get("reason"),
            },
        )


def request_cancel(jobs_root: Path, job_id: str, requested_by: str = "user") -> dict[str, Any]:
    """Persist a cooperative cancellation request without interrupting an atomic step."""
    store = JobStore(jobs_root, job_id)
    job = store.job()
    requested = False

    def apply_cancel(state: dict[str, Any]) -> bool:
        nonlocal requested
        if state.get("cancel_requested"):
            return False
        requested = True
        state["cancel_requested"] = True
        state["cancel_requested_at"] = utc_now()
        state["cancel_requested_by"] = requested_by
        state["updated_at"] = utc_now()
        return True

    state = store.update_execution_state(
        "execution_state.json", {"approved_deviations": [], "steps": []}, apply_cancel,
    )
    if requested:
        AuditLogger(store.path("audit.jsonl"), job_id).append(
            "CANCEL_REQUESTED", "cooperative cancellation", "STARTED",
            metadata={"current_step": state.get("current_step"),
                      "last_successful_step": state.get("last_success"), "result": "REQUESTED"},
        )
    return {"job_state": job.get("state"), **state}


def execute(jobs_root: Path, job_id: str, handlers: dict[str, Any] | None = None) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        return _execute_locked(store, handlers=handlers)


def _execute_locked(store: JobStore, handlers: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = store.job_id
    job, saved_plan, runtime = _approved_plan(store)
    state = store.load("execution_state.json", {"approved_deviations": []})
    _bind_execution_state_to_plan(store, saved_plan, state)
    _restore_execution(runtime, state)
    _reuse_verified_github_snapshots(store, runtime)
    job["state"] = "RUNNING"
    store.save_job(job)
    executor = PlanExecutor(
        runtime, AuditLogger(store.path("audit.jsonl"), job_id), handlers=handlers,
        cancellation_requested=lambda: bool(store.load("execution_state.json", {}).get("cancel_requested")),
    )
    executor.run()
    steps = runtime.get("steps", [])
    stopped = next((x for x in steps if x.get("status") in ("STOPPED", "FAILED", "WAITING_FOR_USER")), None)
    if stopped and stopped.get("status") == "WAITING_FOR_USER":
        stopped.setdefault("reason", "user action required")
        stopped.setdefault("deviation_id", stopped["step_id"])
    stop_reason = ("USER_CANCELLED" if executor.cancelled else
                   (stopped or {}).get("reason") or ("user action required" if stopped else None))

    def persist_worker_result(latest: dict[str, Any]) -> bool:
        _bind_execution_state_to_plan(store, saved_plan, latest)
        latest["steps"] = steps
        latest["current_step"] = next(
            (x["step_id"] for x in steps if x.get("status") != "SUCCESS"), None,
        )
        latest["last_success"] = next(
            (x["step_id"] for x in reversed(steps) if x.get("status") == "SUCCESS"), None,
        )
        latest["stop_reason"] = stop_reason
        latest["updated_at"] = utc_now()
        return True

    state = store.update_execution_state(
        "execution_state.json", {"approved_deviations": []}, persist_worker_result,
    )
    if executor.cancelled:
        job["state"] = "STOPPED"
        AuditLogger(store.path("audit.jsonl"), job_id).append(
            "CANCEL_CHECKPOINT_STOP", "cooperative cancellation checkpoint", "STOPPED",
            metadata={"checkpoint": executor.cancel_phase, "current_step": state.get("current_step"),
                      "last_successful_step": state.get("last_success"), "result": "USER_CANCELLED"},
        )
    elif stopped and stopped.get("status") == "WAITING_FOR_USER":
        job["state"] = "WAITING_FOR_USER"
    elif stopped and stopped.get("status") == "FAILED":
        job["state"] = "FAILED"
    elif stopped:
        job["state"] = "STOPPED"
    elif all(x.get("status") == "SUCCESS" for x in state["steps"]):
        job["state"] = "VERIFIED"
    store.save_job(job)
    return state


def installation_plan_approval_status(jobs_root: Path, job_id: str) -> dict[str, Any] | None:
    """Return only an approval that still matches the exact persisted research plan."""
    store = JobStore(jobs_root, job_id)
    job = store.job()
    value = store.load("installation_plan.json")
    approval = job.get("installation_plan_approval")
    if not value or not approval:
        return None
    version = value.get("schema_version")
    executable = [step.get("step_id") for step in value.get("steps", [])
                  if step.get("type") in EXECUTABLE_TYPES]
    reviewed = [step.get("step_id") for step in value.get("steps", [])]
    review_only = [step.get("step_id") for step in value.get("steps", [])
                   if step.get("type") not in EXECUTABLE_TYPES]
    if (
        approval.get("plan_kind") != INSTALLATION_PLAN_KIND
        or approval.get("plan_hash") != plan_hash(value)
        or approval.get("plan_version") != version
        or approval.get("approved_step_ids") != executable
        or approval.get("reviewed_step_ids") != reviewed
        or approval.get("review_only_step_ids") != review_only
    ):
        return None
    return approval


def approve_installation_plan(jobs_root: Path, job_id: str) -> dict[str, Any]:
    """Approve the executable subset while binding approval to the complete review plan."""
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        job = store.job()
        value = store.load("installation_plan.json")
        if not value:
            raise SystemExit("Approval blocked: installation_plan.json does not exist.")
        if value.get("status") not in {"REVIEW_REQUIRED", "READY_FOR_APPROVAL"}:
            raise SystemExit(f"Approval blocked: plan status is {value.get('status')}.")
        if _editable_review_acceptance_required(value) and not _accepted_editable_review_risk(store, value):
            raise SystemExit("Approval blocked: USER_ACCEPTED_UNRESOLVED_RISK is required for reviewed editable install.")
        executable = [step.get("step_id") for step in value.get("steps", [])
                      if step.get("type") in EXECUTABLE_TYPES]
        reviewed = [step.get("step_id") for step in value.get("steps", [])]
        review_only = [step.get("step_id") for step in value.get("steps", [])
                       if step.get("type") not in EXECUTABLE_TYPES]
        if not executable:
            raise SystemExit("Approval blocked: plan has no executable steps.")
        record = {
            "plan_kind": INSTALLATION_PLAN_KIND,
            "plan_hash": plan_hash(value),
            "plan_version": value.get("schema_version"),
            "timestamp": utc_now(),
            "approval_scope": "executable steps in reviewed installation plan only",
            "approved_step_ids": executable,
            "reviewed_step_ids": reviewed,
            "review_only_step_ids": review_only,
        }
        job["installation_plan_approval"] = record
        job["installation_plan_state"] = "APPROVED"
        store.save_job(job)
        _audit(store, INSTALLATION_PLAN_KIND).append(
            "INSTALLATION_PLAN_APPROVAL", "approve persisted installation plan", "APPROVED",
            metadata=record,
        )
        return record


def _editable_review_acceptance_required(plan: dict[str, Any]) -> bool:
    return plan.get("status") == "REVIEW_REQUIRED" and any(
        step.get("type") in {"INSTALL_LOCAL_EDITABLE_PROJECT", "INSTALL_LOCAL_PYTHON_PROJECT"}
        for step in plan.get("steps", []))


def _accepted_editable_review_risk(store: JobStore, plan: dict[str, Any]) -> bool:
    path = store.path("audit.jsonl")
    if not path.exists():
        return False
    return any(
        row.get("action") == "USER_ACCEPTED_UNRESOLVED_RISK" and row.get("result") == "APPROVED"
        and row.get("metadata", {}).get("plan_kind") == INSTALLATION_PLAN_KIND
        and row.get("metadata", {}).get("plan_hash") == plan_hash(plan)
        and row.get("metadata", {}).get("source_identity") == plan.get("source_identity")
        for row in read_tail(path, limit=10**9))


def _approved_installation_plan(store: JobStore) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    job = store.job()
    value = store.load("installation_plan.json")
    approval = job.get("installation_plan_approval")
    if not value or not approval:
        raise SystemExit("Execution rejected: explicit installation plan approval is required.")
    version = value.get("schema_version")
    executable = [step.get("step_id") for step in value.get("steps", [])
                  if step.get("type") in EXECUTABLE_TYPES]
    reviewed = [step.get("step_id") for step in value.get("steps", [])]
    review_only = [step.get("step_id") for step in value.get("steps", [])
                   if step.get("type") not in EXECUTABLE_TYPES]
    if (
        approval.get("plan_kind") != INSTALLATION_PLAN_KIND
        or approval.get("plan_hash") != plan_hash(value)
        or approval.get("plan_version") != version
        or approval.get("approved_step_ids") != executable
        or approval.get("reviewed_step_ids") != reviewed
        or approval.get("review_only_step_ids") != review_only
    ):
        job["installation_plan_approval"] = None
        job["installation_plan_state"] = "WAITING_FOR_APPROVAL"
        store.save_job(job)
        raise SystemExit("Execution rejected: installation plan changed after approval; approval is INVALID.")
    if value.get("status") not in {"REVIEW_REQUIRED", "READY_FOR_APPROVAL"}:
        raise SystemExit(f"Execution rejected: plan status is {value.get('status')}.")
    if _editable_review_acceptance_required(value) and not _accepted_editable_review_risk(store, value):
        raise SystemExit("Execution rejected: USER_ACCEPTED_UNRESOLVED_RISK is required for reviewed editable install.")
    approved = set(executable)
    runtime = json.loads(json.dumps(value))
    runtime["steps"] = [step for step in runtime.get("steps", [])
                        if step.get("type") in EXECUTABLE_TYPES and step.get("step_id") in approved]
    runtime["review_required"] = value.get("status") == "REVIEW_REQUIRED"
    for step in runtime["steps"]:
        step["approved"] = True
    runtime["status"] = "READY_FOR_APPROVAL"
    return job, value, runtime


def execute_installation_plan(jobs_root: Path, job_id: str,
                              handlers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute only the approved fixed steps through the existing guarded executor."""
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        return _execute_installation_plan_locked(store, handlers=handlers)


def check_conda_precondition(jobs_root: Path, job_id: str) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        _approved_installation_plan(store)
        return detect_conda(_audit(store, INSTALLATION_PLAN_KIND))


def install_recommended_conda(jobs_root: Path, job_id: str) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        _approved_installation_plan(store)
        return install_miniforge(_audit(store, INSTALLATION_PLAN_KIND))


def _installation_audit_tail(store: JobStore, limit: int = 200) -> list[dict[str, Any]]:
    return [
        entry for entry in read_tail(store.path("audit.jsonl"), limit=limit)
        if entry.get("metadata", {}).get("plan_kind") == INSTALLATION_PLAN_KIND
    ]


def request_installation_cancel(jobs_root: Path, job_id: str,
                                requested_by: str = "user") -> dict[str, Any]:
    """Persist a cooperative cancellation request for installation execution."""
    store = JobStore(jobs_root, job_id)
    saved_plan = store.load("installation_plan.json")
    if not saved_plan:
        raise SystemExit("Cancellation rejected: installation_plan.json does not exist.")
    def apply_cancel(state: dict[str, Any]) -> bool:
        _bind_execution_state_to_plan(
            store, saved_plan, state,
            plan_kind=INSTALLATION_PLAN_KIND, version_field="schema_version",
        )
        state.update({
            "cancel_requested": True,
            "cancel_requested_at": utc_now(),
            "cancel_requested_by": requested_by,
            "updated_at": utc_now(),
        })
        return True

    state = store.update_execution_state(
        "installation_execution_state.json",
        {"approved_deviations": [], "steps": []},
        apply_cancel,
    )
    _audit(store, INSTALLATION_PLAN_KIND).append(
        "INSTALLATION_CANCEL_REQUESTED", "request installation cancellation", "STARTED",
        metadata={"requested_by": requested_by, "plan_hash": plan_hash(saved_plan)},
    )
    return state


def _execute_installation_plan_locked(
    store: JobStore,
    handlers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job, saved_plan, runtime = _approved_installation_plan(store)
    state = store.load("installation_execution_state.json", {"approved_deviations": [], "steps": []})
    _bind_execution_state_to_plan(
        store, saved_plan, state,
        plan_kind=INSTALLATION_PLAN_KIND, version_field="schema_version",
    )
    _restore_execution(runtime, state)
    pending_conda = any(
        step.get("type") == "CREATE_PYTHON_ENV" and step.get("status") != "SUCCESS"
        for step in runtime["steps"]
    )
    conda_executable = None
    if pending_conda and "CREATE_PYTHON_ENV" not in (handlers or {}):
        conda_state = detect_conda(_audit(store, INSTALLATION_PLAN_KIND))
        if conda_state["status"] == MISSING_CONDA:
            return {"status": MISSING_CONDA, "reason": MISSING_CONDA, "steps": runtime["steps"]}
        conda_executable = str(conda_state["executable"])
    if any(step.get("type") == "INSTALL_LOCAL_PYTHON_PROJECT" for step in runtime["steps"]):
        from editable_project_install import load_source_job_context
        context = load_source_job_context(store.root, saved_plan)
    else:
        from job_context import JobContext
        context = JobContext.load(store.root) if any(
            step.get("type") == "INSTALL_LOCAL_EDITABLE_PROJECT" for step in runtime["steps"]) else None
    review_risk_accepted = _accepted_editable_review_risk(store, saved_plan) if context is not None else False
    job["installation_plan_state"] = "RUNNING"
    store.save_job(job)
    audit = _audit(store, INSTALLATION_PLAN_KIND)
    audit.append(
        "INSTALLATION_EXECUTION_START", "start approved installation plan", "STARTED",
        metadata={
            "plan_hash": plan_hash(saved_plan),
            "schema_version": saved_plan.get("schema_version"),
            "executable_step_ids": [step.get("step_id") for step in runtime.get("steps", [])],
        },
    )

    def cancellation_requested() -> bool:
        current = store.load("installation_execution_state.json", {})
        return bool(current.get("cancel_requested"))

    executor = PlanExecutor(
        runtime, audit, handlers=handlers,
        cancellation_requested=cancellation_requested,
        job_context=context, review_risk_accepted=review_risk_accepted,
        conda_executable=conda_executable,
    )
    executor.run()
    steps = runtime.get("steps", [])
    stopped = next((step for step in steps
                    if step.get("status") in {"STOPPED", "FAILED", "WAITING_FOR_USER"}), None)
    stop_reason = (stopped or {}).get("reason") or (stopped or {}).get("stop_reason")
    cancelled_incomplete = executor.cancelled and not (
        steps and all(step.get("status") == "SUCCESS" for step in steps)
    )
    if cancelled_incomplete:
        stop_reason = "USER_CANCELLED"
    def persist_worker_result(latest: dict[str, Any]) -> bool:
        _bind_execution_state_to_plan(
            store, saved_plan, latest,
            plan_kind=INSTALLATION_PLAN_KIND, version_field="schema_version",
        )
        latest["steps"] = steps
        latest["current_step"] = next(
            (step.get("step_id") for step in steps if step.get("status") != "SUCCESS"), None,
        )
        latest["last_success"] = next(
            (step.get("step_id") for step in reversed(steps)
             if step.get("status") == "SUCCESS"), None,
        )
        latest["stop_reason"] = stop_reason
        latest["status"] = execution_status(latest)
        latest["updated_at"] = utc_now()
        return True

    state = store.update_execution_state(
        "installation_execution_state.json",
        {"approved_deviations": [], "steps": []},
        persist_worker_result,
    )
    job = store.job()
    job["installation_plan_state"] = state["status"]
    store.save_job(job)
    if cancelled_incomplete:
        audit.append(
            "INSTALLATION_CANCEL_CHECKPOINT_STOP",
            "stop installation execution at cancellation checkpoint", "STOPPED",
            metadata={"stop_reason": "USER_CANCELLED", "current_step": state["current_step"]},
        )
    audit.append(
        "INSTALLATION_EXECUTION_FINAL", "finish installation execution attempt",
        "SUCCESS" if state["status"] == "VERIFIED" else state["status"],
        metadata={
            "installation_plan_state": job["installation_plan_state"],
            "stop_reason": stop_reason,
            "last_success": state["last_success"],
        },
    )
    return state


def resume_installation_plan(
    jobs_root: Path,
    job_id: str,
    approve_deviation: str | None = None,
    handlers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume the exact approved installation plan after an exact corrective approval."""
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        job, saved_plan, runtime = _approved_installation_plan(store)
        audit = _audit(store, INSTALLATION_PLAN_KIND)
        audit_tail = _installation_audit_tail(store)
        decision: dict[str, Any] = {}
        required_approval: str | None = None

        def prepare_resume(state: dict[str, Any]) -> bool:
            nonlocal decision, required_approval
            _bind_execution_state_to_plan(
                store, saved_plan, state,
                plan_kind=INSTALLATION_PLAN_KIND, version_field="schema_version",
            )
            _restore_execution(runtime, state)
            required_approval = "USER_CANCELLED" if state.get("cancel_requested") else None
            if not required_approval:
                stopped = next((step for step in runtime.get("steps", [])
                                if step.get("status") == "STOPPED"), None)
                if stopped:
                    required_approval = stopped.get("deviation_id") or stopped.get("reason") \
                        or stopped.get("stop_reason")
            if required_approval and approve_deviation != required_approval:
                decision = {
                    "status": "NEEDS_APPROVAL", "reason": required_approval,
                    "required_approval": required_approval,
                }
                return False
            if required_approval:
                approved = state.setdefault("approved_deviations", [])
                if required_approval not in approved:
                    approved.append(required_approval)
            state["cancel_requested"] = False
            state["cancel_requested_at"] = None
            state["cancel_requested_by"] = None
            decision = resume_status(runtime, state, audit_tail)
            if decision.get("status") != "CAN_RESUME":
                return False
            stopped_state = next((step for step in state.get("steps", [])
                                  if step.get("status") == "STOPPED"), None)
            if stopped_state:
                stopped_state["status"] = "PENDING"
                stopped_state.pop("reason", None)
                stopped_state.pop("stop_reason", None)
                stopped_state.pop("deviation_id", None)
            state["stop_reason"] = None
            state["updated_at"] = utc_now()
            return True

        store.update_execution_state(
            "installation_execution_state.json",
            {"approved_deviations": [], "steps": []},
            prepare_resume,
        )
        if decision.get("status") != "CAN_RESUME":
            return decision
        if required_approval:
            audit.append(
                "INSTALLATION_CORRECTIVE_APPROVAL",
                "approve exact installation execution deviation", "APPROVED",
                metadata={"deviation_id": required_approval,
                          "plan_hash": plan_hash(saved_plan)},
            )
        job["installation_plan_state"] = "APPROVED"
        store.save_job(job)
        audit.append(
            "INSTALLATION_EXECUTION_RESUME", "resume approved installation plan", "SESSION_RESUME",
            metadata={"plan_hash": plan_hash(saved_plan),
                      "from_step": decision.get("stopped_step")},
        )
        return {
            **decision,
            "execution_state": _execute_installation_plan_locked(store, handlers=handlers),
        }


def status(jobs_root: Path, job_id: str) -> str:
    store = JobStore(jobs_root, job_id)
    job, value, state = store.job(), store.load("plan.json", {}), store.load("execution_state.json", {})
    approval = job.get("approval")
    approval_state = "NONE"
    if approval:
        approval_state = "VALID" if (
            value
            and approval.get("plan_hash") == plan_hash(value)
            and approval.get("plan_version") == value.get("plan_version")
        ) else "INVALID"
    next_action = {
        "WAITING_FOR_SELECTION": "select a source", "PREFLIGHT_COMPLETE": "create and review plan",
        "WAITING_FOR_APPROVAL": "approve explicit step IDs", "APPROVED": "execute",
        "WAITING_FOR_USER": "complete manual action, then resume with explicit approval",
        "STOPPED": "review deviation and explicitly approve before resume",
        "FAILED": "review failure and explicitly approve any deviation before resume",
        "VERIFIED": "close job", "CLOSED": "none",
    }.get(job.get("state"), "run preflight")
    return "\n".join([
        f"JOB: {job_id} ({job.get('state')})", f"PLAN: {value.get('status', 'NONE')}",
        f"APPROVAL: {approval_state}", f"CURRENT STEP: {state.get('current_step') or 'NONE'}",
        f"LAST SUCCESS: {state.get('last_success') or 'NONE'}",
        f"STOP REASON: {state.get('stop_reason') or 'NONE'}",
        f"CANCEL REQUESTED: {'YES' if state.get('cancel_requested') else 'NO'}",
        f"NEXT REQUIRED ACTION: {next_action}",
    ])


def resume_job(jobs_root: Path, job_id: str, approve_deviation: str | None = None,
               handlers: dict[str, Any] | None = None) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    with _job_lifecycle(store):
        _job, saved_plan, runtime = _approved_plan(store)
        audit_tail = read_tail(store.path("audit.jsonl"))
        decision: dict[str, Any] = {}
        resumed_after_cancel = False
        stopped_for_audit: dict[str, Any] | None = None

        def prepare_resume(state: dict[str, Any]) -> bool:
            nonlocal decision, resumed_after_cancel, stopped_for_audit
            _bind_execution_state_to_plan(store, saved_plan, state)
            if state.get("cancel_requested"):
                if approve_deviation != "USER_CANCELLED":
                    decision = {
                        "status": "NEEDS_APPROVAL", "reason": "USER_CANCELLED",
                        "required_approval": "USER_CANCELLED",
                    }
                    return False
                resumed_after_cancel = True
                state["cancel_requested"] = False
                state["cancel_requested_at"] = None
                state["cancel_requested_by"] = None
                state["stop_reason"] = None
                state.setdefault("approved_deviations", []).append("USER_CANCELLED")
                state["updated_at"] = utc_now()
                decision = {"status": "CAN_RESUME"}
                return True
            if approve_deviation:
                state.setdefault("approved_deviations", []).append(approve_deviation)
            _restore_execution(runtime, state)
            decision = resume_status(runtime, state, audit_tail)
            if decision["status"] != "CAN_RESUME":
                return bool(approve_deviation)
            stopped_id = decision.get("stopped_step")
            if stopped_id:
                stopped = next(x for x in runtime["steps"] if x["step_id"] == stopped_id)
                stopped_for_audit = stopped
                if stopped["type"] == "USER_ACTION_REQUIRED":
                    stopped["status"] = "SUCCESS"
                    stopped["result"] = stopped["expected_result"]
                else:
                    stopped["status"] = "PENDING"
                stopped.pop("reason", None)
                stopped.pop("deviation_id", None)
            state["steps"] = runtime["steps"]
            state["updated_at"] = utc_now()
            return True

        state = store.update_execution_state(
            "execution_state.json", {}, prepare_resume,
        )
        if decision.get("status") != "CAN_RESUME":
            return decision
        audit = AuditLogger(store.path("audit.jsonl"), job_id)
        if resumed_after_cancel:
            audit.append(
                "RESUME_AFTER_CANCEL", "explicit cooperative resume", "SESSION_RESUME",
                metadata={"current_step": state.get("current_step"),
                          "last_successful_step": state.get("last_success"), "result": "APPROVED"},
            )
            job = store.job()
            job["state"] = "APPROVED"
            store.save_job(job)
        elif stopped_for_audit:
            audit.append(
                stopped_for_audit["step_id"], stopped_for_audit["type"], "SESSION_RESUME",
                metadata={"approved_deviation": approve_deviation},
            )
        return {
            **decision,
            "execution_state": _execute_locked(store, handlers=handlers),
        }


def close_job(jobs_root: Path, job_id: str, incomplete: bool = False) -> dict[str, Any]:
    store = JobStore(jobs_root, job_id)
    job, value = store.job(), store.load("plan.json", {})
    if store.path("installation_plan.json").exists():
        verified = job.get("installation_plan_state") == "VERIFIED"
    else:
        verified = job.get("state") == "VERIFIED"
    if not verified and not incomplete:
        raise SystemExit("Close blocked: verification has not passed; use --incomplete to close explicitly.")
    reused = value.get("local_assets_to_reuse", [])
    downloads = value.get("planned_downloads", [])
    reused_keys = {
        (asset.get("identifier"), asset.get("hash"))
        for asset in reused
        if asset.get("identifier") is not None or asset.get("hash") is not None
    }
    model_duplication = any(
        (artifact.get("identifier"), artifact.get("hash")) in reused_keys
        for artifact in downloads
    )
    result = {
        "source_verified": bool(value.get("source_url") and value.get("exact_commit") not in (None, "UNKNOWN")),
        "install_verified": verified, "dependencies_verified": verified,
        "local_asset_reuse": reused,
        "model_duplication": model_duplication,
        "cross_project_safety": not bool(value.get("cross_project_conflicts")),
        "live_schema_verified": False, "inference_verified": False, "generation_verified": False,
        "additional_downloads": downloads,
        "final_status": "VERIFIED" if verified else "INCOMPLETE", "closed_at": utc_now(),
    }
    store.save("result.json", result)
    AuditLogger(store.path("audit.jsonl"), job_id).append("close", "close job", "CLOSED", metadata=result)
    job["state"] = "CLOSED"
    store.save_job(job)
    return result
