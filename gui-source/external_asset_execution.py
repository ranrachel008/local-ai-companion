"""Approval-bound execution of verified ComfyUI external asset plans."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from audit import read_tail
from external_asset_followup import _documented_links, _verified_source


PLAN_NAME = "external_asset_followup_plan.json"
SOURCE_PLAN_NAME = "external_asset_followup_source_plan.json"
KIND = "EXTERNAL_ASSET_FOLLOWUP"
TYPES = {"ACQUIRE_CUSTOM_NODE_REPOSITORY", "INSTALL_CUSTOM_NODE_DEPENDENCIES",
         "PLACE_COMFYUI_WORKFLOW", "DOWNLOAD_MODEL_ASSET"}
HEX40 = re.compile(r"[0-9a-f]{40}", re.I)
HEX64 = re.compile(r"[0-9a-f]{64}", re.I)


class ExternalAssetExecutionError(ValueError):
    pass


def _core():
    import product_cli
    return product_cli


def _inside(path: Path, directory: Path) -> bool:
    return path.is_absolute() and path.resolve().is_relative_to(directory.resolve())


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, shell=False, capture_output=True, text=True,
                          timeout=600, check=False)


def _git(path: Path, *args: str) -> str:
    result = _run(["git", "-c", f"safe.directory={path}", "-C", str(path), *args])
    if result.returncode:
        raise ExternalAssetExecutionError(f"Git verification failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _ephemeral_cache_only(path: Path) -> bool:
    """Reject repository changes except untracked Python bytecode in __pycache__."""
    result = _run(["git", "-c", f"safe.directory={path}", "-C", str(path),
                   "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if result.returncode:
        raise ExternalAssetExecutionError(f"Git verification failed: {result.stderr.strip()}")
    cache_present = False
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        if not entry.startswith("?? "):
            raise ExternalAssetExecutionError("REVIEW_REQUIRED: existing custom node has tracked changes")
        relative = PurePosixPath(entry[3:])
        if relative.parent.name != "__pycache__" or relative.suffix not in {".pyc", ".pyo"}:
            raise ExternalAssetExecutionError("REVIEW_REQUIRED: existing custom node has untracked files")
        cache_present = True
    return cache_present


def _repo_url(url: str) -> str:
    parsed = urlparse(url)
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port not in (None, 443) or len(parts) != 2 or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."} for part in parts
    ) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ExternalAssetExecutionError("Invalid GitHub repository identity")
    return "https://github.com/" + "/".join(parts)


def _python_from_launcher(root: Path) -> Path:
    candidates: set[Path] = set()
    for launcher in root.parent.glob("run_*.bat"):
        text = launcher.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"(?i)(?:\.\\)?(python_embeded|python_embedded)\\python\.exe", text):
            candidate = root.parent / match.group(1) / "python.exe"
            if candidate.is_file():
                candidates.add(candidate.resolve())
    if len(candidates) != 1:
        raise ExternalAssetExecutionError("ComfyUI Python is not uniquely documented by an existing launcher")
    return next(iter(candidates))


def _workflow_directory(root: Path) -> Path:
    directories = [path for path in (root / "user").glob("*/workflows") if path.is_dir()]
    if len(directories) != 1:
        raise ExternalAssetExecutionError("ComfyUI workflow persistence directory is not unique")
    return directories[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_step(repo: Path, parent_id: str, python: Path) -> dict | None:
    if (repo / "requirements.txt").is_file():
        manifest = repo / "requirements.txt"
        argv = [str(python), "-m", "pip", "install", "-r", str(manifest)]
    elif (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file():
        manifest = repo / ("pyproject.toml" if (repo / "pyproject.toml").is_file() else "setup.py")
        argv = [str(python), "-m", "pip", "install", str(repo)]
    else:
        return None
    return {"step_id": parent_id + "-dependencies", "type": "INSTALL_CUSTOM_NODE_DEPENDENCIES",
            "repository_step_id": parent_id, "source_root": str(repo), "manifest": str(manifest),
            "manifest_sha256": _sha256(manifest), "python_executable": str(python), "argv": argv}


def _remote_dependency_step(url: str, revision: str, repo: Path, parent_id: str,
                            python: Path) -> dict | None:
    """Pin a documented manifest before approving acquisition of an absent node."""
    owner, name = urlparse(url).path.strip("/").split("/")
    for filename in ("requirements.txt", "pyproject.toml", "setup.py"):
        manifest_url = f"https://raw.githubusercontent.com/{owner}/{name}/{revision}/{filename}"
        try:
            with urlopen(Request(manifest_url, headers={"User-Agent": "Ornith-Solution-Scout"}), timeout=20) as response:
                if response.geturl() != manifest_url:
                    raise ExternalAssetExecutionError("Dependency manifest URL changed")
                content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ExternalAssetExecutionError("Dependency manifest is too large")
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                continue
            raise ExternalAssetExecutionError(f"Cannot verify custom node dependency manifest: {exc}") from exc
        manifest = repo / filename
        argv = ([str(python), "-m", "pip", "install", "-r", str(manifest)]
                if filename == "requirements.txt" else [str(python), "-m", "pip", "install", str(repo)])
        return {"step_id": parent_id + "-dependencies", "type": "INSTALL_CUSTOM_NODE_DEPENDENCIES",
                "repository_step_id": parent_id, "source_root": str(repo), "manifest": str(manifest),
                "manifest_sha256": hashlib.sha256(content).hexdigest(),
                "python_executable": str(python), "argv": argv}
    return None


def prepare_execution_plan(jobs_root: Path, job_id: str) -> dict:
    """Upgrade a persisted evidence plan without rerunning research or selecting a target."""
    core = _core()
    store = core.JobStore(Path(jobs_root), job_id)
    with store.lifecycle_lock():
        old = store.load(PLAN_NAME)
        if not isinstance(old, dict) or old.get("schema") != "ornith.external-asset-followup":
            raise ExternalAssetExecutionError("External asset plan is missing")
        if old.get("version") == 2:
            if old.get("plan_hash") != core.plan_hash(old):
                raise ExternalAssetExecutionError("External plan hash changed")
            return old
        if old.get("version") != 1:
            raise ExternalAssetExecutionError("Unsupported external plan version")
        source, identity = _verified_source(store.root)
        if identity != old.get("source_identity") or str(source) != old.get("verified_source"):
            raise ExternalAssetExecutionError("Verified source changed")
        root = Path(old["comfyui_root"])
        if not root.is_dir() or not (root / "custom_nodes").is_dir() or not (root / "models").is_dir():
            raise ExternalAssetExecutionError("Existing ComfyUI root is unavailable")
        python = _python_from_launcher(root)
        hf, nodes = _documented_links(source)
        if hf != old.get("documented_hf_repositories") or nodes != old.get("documented_custom_node_repositories"):
            raise ExternalAssetExecutionError("Documented source links changed")
        steps: list[dict] = []
        for item in old.get("steps", []):
            kind = item.get("type")
            if kind == "ACQUIRE_CUSTOM_NODE_REPOSITORY":
                url = _repo_url(item["source_url"])
                if url not in nodes or item.get("repository", "").casefold() != urlparse(url).path.strip("/").casefold():
                    raise ExternalAssetExecutionError("Custom node is not documented")
                target = Path(item["target"])
                if target != root / "custom_nodes" / urlparse(url).path.strip("/").split("/")[1]:
                    raise ExternalAssetExecutionError("Custom node target is outside the allowed directory")
                if target.exists():
                    if not target.is_dir() or not (target / ".git").exists():
                        raise ExternalAssetExecutionError("Existing custom node target is not a Git repository")
                    if _repo_url(_git(target, "remote", "get-url", "origin")) != url:
                        raise ExternalAssetExecutionError("Existing custom node repository identity differs")
                    revision = _git(target, "rev-parse", "HEAD")
                else:
                    remote = _run(["git", "ls-remote", url, "HEAD"])
                    if remote.returncode:
                        raise ExternalAssetExecutionError("Cannot pin custom node revision")
                    revision = remote.stdout.split()[0] if remote.stdout.split() else ""
                if not HEX40.fullmatch(revision):
                    raise ExternalAssetExecutionError("Custom node revision is not immutable")
                steps.append({"step_id": item["step_id"], "type": kind, "repository": item["repository"],
                              "source_url": url, "target": str(target), "revision": revision})
                dep = (_dependency_step(target, item["step_id"], python) if target.is_dir()
                       else _remote_dependency_step(url, revision, target, item["step_id"], python))
                if dep:
                    steps.append(dep)
            elif kind == "PLACE_COMFYUI_WORKFLOW":
                original = Path(item["source"])
                destination = Path(item["target"])
                workflow_dir = _workflow_directory(root)
                if not _inside(original, source) or destination.parent != workflow_dir or not workflow_dir.is_dir():
                    raise ExternalAssetExecutionError("Workflow source or persistence directory is unverified")
                steps.append({"step_id": item["step_id"], "type": kind, "source": str(original),
                              "source_sha256": _sha256(original), "target": str(destination)})
            elif kind == "DOWNLOAD_DOCUMENTED_MODEL_ASSET":
                asset = item.get("selected_asset")
                if not isinstance(asset, dict):
                    raise ExternalAssetExecutionError("Model asset was not uniquely selected")
                repo = item["repository_url"]
                name = asset.get("filename")
                revision = asset.get("revision")
                if (repo not in hf or urlparse(repo).scheme != "https" or urlparse(repo).hostname != "huggingface.co"
                        or not HEX40.fullmatch(str(revision)) or not isinstance(name, str)
                        or Path(name).name != name or not isinstance(asset.get("size_bytes"), int)
                        or asset["size_bytes"] <= 0 or asset.get("download_url") !=
                        f"{repo}/resolve/{revision}/{quote(name, safe='/')}"):
                    raise ExternalAssetExecutionError("Model URL, revision, size, or filename is unverified")
                sha = asset.get("sha256")
                if sha is not None and not HEX64.fullmatch(str(sha)):
                    raise ExternalAssetExecutionError("Invalid model SHA256")
                target = item.get("target_path")
                steps.append({"step_id": item["step_id"], "type": "DOWNLOAD_MODEL_ASSET",
                              "repository_url": repo, "filename": name, "revision": revision,
                              "url": asset["download_url"], "expected_size": asset["size_bytes"],
                              "expected_sha256": sha, "target_path": None,
                              "proposed_target_path": target,
                              "target_evidence": None,
                              "review_reason": "USER_CONFIRMED_TARGET_REQUIRED"})
            else:
                raise ExternalAssetExecutionError("Unsupported source external asset step")
        order = {"ACQUIRE_CUSTOM_NODE_REPOSITORY": 1, "INSTALL_CUSTOM_NODE_DEPENDENCIES": 2,
                 "PLACE_COMFYUI_WORKFLOW": 3, "DOWNLOAD_MODEL_ASSET": 4}
        steps.sort(key=lambda row: (order[row["type"]], row["step_id"]))
        plan = {"schema": old["schema"], "version": 2, "status": "REVIEW_REQUIRED",
                "source_plan_hash": core.plan_hash(old),
                "source_identity": identity, "verified_source": str(source), "comfyui_root": str(root),
                "comfyui_python": str(python), "original_intent": old["original_intent"],
                "documented_hf_repositories": hf, "documented_custom_node_repositories": nodes,
                "steps": steps}
        plan["plan_hash"] = core.plan_hash(plan)
        store.save(SOURCE_PLAN_NAME, old)
        store.save(PLAN_NAME, plan)
        job = store.job()
        job["external_asset_plan_approval"] = None
        store.save_job(job)
        return plan


def confirm_model_target(jobs_root: Path, job_id: str, step_id: str, target_path: str,
                         displayed_hash: str, evidence_type: str = "USER_SELECTED_TARGET") -> dict:
    core = _core()
    store = core.JobStore(Path(jobs_root), job_id)
    with store.lifecycle_lock():
        plan = store.load(PLAN_NAME)
        if not plan or plan.get("version") != 2:
            raise ExternalAssetExecutionError("Prepared external plan is required")
        if displayed_hash != plan.get("plan_hash") or displayed_hash != core.plan_hash(plan):
            raise ExternalAssetExecutionError("Displayed external plan changed before target confirmation")
        steps = [s for s in plan["steps"] if s["step_id"] == step_id and s["type"] == "DOWNLOAD_MODEL_ASSET"]
        if len(steps) != 1:
            raise ExternalAssetExecutionError("Unknown model step")
        step = steps[0]
        root = Path(plan["comfyui_root"])
        target = Path(target_path)
        if (not _inside(target, root / "models") or target.name != step["filename"]):
            raise ExternalAssetExecutionError("Model target must be an exact file under ComfyUI models")
        if evidence_type != "USER_SELECTED_TARGET":
            raise ExternalAssetExecutionError("Model directory requires an explicit user-selected target")
        old_hash = core.plan_hash(plan)
        step.update(target_path=str(target), target_evidence=evidence_type, review_reason=None)
        if all(s.get("target_path") for s in plan["steps"] if s["type"] == "DOWNLOAD_MODEL_ASSET"):
            plan["status"] = "READY_FOR_APPROVAL"
        plan["plan_hash"] = core.plan_hash(plan)
        store.save(PLAN_NAME, plan)
        job = store.job()
        job["external_asset_plan_approval"] = None
        store.save_job(job)
        core._audit(store, KIND).append("USER_CONFIRMED_TARGET", str(target), "APPROVED",
                                        metadata={"step_id": step_id, "evidence_type": evidence_type,
                                                  "previous_plan_hash": old_hash,
                                                  "plan_hash": plan["plan_hash"],
                                                  "step_digest": _model_step_digest(step),
                                                  "author_documentation_specified_directory": False})
        return plan


def _model_step_digest(step: dict) -> str:
    payload = {key: step.get(key) for key in ("step_id", "repository_url", "filename", "revision",
                                               "url", "expected_size", "expected_sha256", "target_path")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ready(plan: dict) -> bool:
    return (plan.get("version") == 2 and plan.get("status") == "READY_FOR_APPROVAL"
            and plan.get("plan_hash") == _core().plan_hash(plan)
            and bool(plan.get("steps")) and all(s.get("type") in TYPES for s in plan["steps"])
            and all(s.get("target_path") and not s.get("review_reason")
                    for s in plan["steps"] if s["type"] == "DOWNLOAD_MODEL_ASSET"))


def approve_external_plan(jobs_root: Path, job_id: str, displayed_hash: str) -> dict:
    core = _core()
    store = core.JobStore(Path(jobs_root), job_id)
    with store.lifecycle_lock():
        plan = store.load(PLAN_NAME)
        if not plan or not _ready(plan) or displayed_hash != core.plan_hash(plan):
            raise ExternalAssetExecutionError("Exact executable external asset plan must be reviewed")
        _verify_contract(store, plan)
        _confirmed_targets(store, plan)
        record = {"plan_kind": KIND, "plan_hash": displayed_hash, "plan_version": 2,
                  "approved_step_ids": [s["step_id"] for s in plan["steps"]], "timestamp": core.utc_now()}
        job = store.job()
        job["external_asset_plan_approval"] = record
        store.save_job(job)
        core._audit(store, KIND).append("EXTERNAL_ASSET_PLAN_APPROVAL", "approve exact external plan", "APPROVED",
                                        metadata=record)
        return record


def _approved(store, plan: dict) -> None:
    approval = store.job().get("external_asset_plan_approval") or {}
    if (not _ready(plan) or approval.get("plan_kind") != KIND
            or approval.get("plan_hash") != _core().plan_hash(plan)
            or approval.get("plan_version") != plan.get("version")
            or approval.get("approved_step_ids") != [s["step_id"] for s in plan["steps"]]):
        raise ExternalAssetExecutionError("Exact external asset plan approval is missing or invalid")
    _confirmed_targets(store, plan)


def _confirmed_targets(store, plan: dict) -> None:
    confirmations = {(r.get("metadata", {}).get("step_id"), r.get("command_or_operation"),
                      r.get("metadata", {}).get("step_digest"))
                     for r in read_tail(store.path("audit.jsonl"), limit=10**9)
                     if r.get("action") == "USER_CONFIRMED_TARGET" and r.get("result") == "APPROVED"}
    for step in plan["steps"]:
        if (step["type"] == "DOWNLOAD_MODEL_ASSET"
                and (step["step_id"], step.get("target_path"), _model_step_digest(step)) not in confirmations):
            raise ExternalAssetExecutionError("USER_CONFIRMED_TARGET is required for reviewed model placement")


def _verify_contract(store, plan: dict) -> None:
    if plan.get("schema") != "ornith.external-asset-followup" or plan.get("version") != 2:
        raise ExternalAssetExecutionError("Invalid external asset plan")
    if not isinstance(plan.get("steps"), list) or not plan["steps"] or any(
            not isinstance(step, dict) or step.get("type") not in TYPES or not isinstance(step.get("step_id"), str)
            for step in plan["steps"]):
        raise ExternalAssetExecutionError("Unknown or malformed external asset step")
    source_plan = store.load(SOURCE_PLAN_NAME)
    core = _core()
    if (not isinstance(source_plan, dict) or source_plan.get("version") != 1
            or plan.get("source_plan_hash") != core.plan_hash(source_plan)
            or source_plan.get("source_identity") != plan.get("source_identity")
            or source_plan.get("verified_source") != plan.get("verified_source")
            or source_plan.get("comfyui_root") != plan.get("comfyui_root")
            or source_plan.get("documented_hf_repositories") != plan.get("documented_hf_repositories")
            or source_plan.get("documented_custom_node_repositories") != plan.get("documented_custom_node_repositories")):
        raise ExternalAssetExecutionError("Original external asset evidence plan changed")
    source_steps = {s["step_id"]: s for s in source_plan.get("steps", [])}
    if len(source_steps) != len(source_plan.get("steps", [])):
        raise ExternalAssetExecutionError("Original external asset steps changed")
    for step in plan["steps"]:
        original = source_steps.get(step.get("repository_step_id", step["step_id"]))
        if not original:
            raise ExternalAssetExecutionError("External step lacks original evidence")
        if step["type"] == "ACQUIRE_CUSTOM_NODE_REPOSITORY" and (
                original.get("type") != step["type"] or original.get("source_url") != step.get("source_url")
                or original.get("repository") != step.get("repository")
                or original.get("target") != step.get("target")):
            raise ExternalAssetExecutionError("Custom node step differs from original evidence")
        if step["type"] == "PLACE_COMFYUI_WORKFLOW" and (
                original.get("type") != step["type"] or original.get("source") != step.get("source")
                or original.get("target") != step.get("target")):
            raise ExternalAssetExecutionError("Workflow step differs from original evidence")
        if step["type"] == "DOWNLOAD_MODEL_ASSET":
            asset = original.get("selected_asset") or {}
            if (original.get("type") != "DOWNLOAD_DOCUMENTED_MODEL_ASSET"
                    or original.get("repository_url") != step.get("repository_url")
                    or any(asset.get(key) != step.get(target_key) for key, target_key in (
                        ("filename", "filename"), ("revision", "revision"),
                        ("download_url", "url"), ("size_bytes", "expected_size"),
                        ("sha256", "expected_sha256")))):
                raise ExternalAssetExecutionError("Model step differs from original evidence")
    primary_ids = {s["step_id"] for s in plan["steps"] if s["type"] != "INSTALL_CUSTOM_NODE_DEPENDENCIES"}
    if primary_ids != set(source_steps):
        raise ExternalAssetExecutionError("External asset plan omitted an evidenced step")
    source, identity = _verified_source(store.root)
    if identity != plan["source_identity"] or str(source) != plan["verified_source"]:
        raise ExternalAssetExecutionError("Verified source identity changed")
    root = Path(plan["comfyui_root"])
    if not root.is_dir() or not (root / "models").is_dir() or not (root / "custom_nodes").is_dir():
        raise ExternalAssetExecutionError("ComfyUI root changed")
    if str(_python_from_launcher(root)) != plan["comfyui_python"]:
        raise ExternalAssetExecutionError("ComfyUI Python identity changed")
    hf, nodes = _documented_links(source)
    if hf != plan["documented_hf_repositories"] or nodes != plan["documented_custom_node_repositories"]:
        raise ExternalAssetExecutionError("Verified upstream links changed")
    ids = [s["step_id"] for s in plan["steps"]]
    if len(ids) != len(set(ids)):
        raise ExternalAssetExecutionError("Duplicate external asset step")
    for repository_step in (s for s in plan["steps"] if s["type"] == "ACQUIRE_CUSTOM_NODE_REPOSITORY"):
        target = Path(repository_step["target"])
        documented = (_dependency_step(target, repository_step["step_id"], Path(plan["comfyui_python"]))
                      if target.is_dir() else None)
        matches = [s for s in plan["steps"] if s["type"] == "INSTALL_CUSTOM_NODE_DEPENDENCIES"
                   and s.get("repository_step_id") == repository_step["step_id"]]
        if documented is not None and matches != [documented]:
            raise ExternalAssetExecutionError("Documented custom node dependency step is missing or changed")
    if [s["type"] for s in plan["steps"]] != sorted(
            (s["type"] for s in plan["steps"]),
            key=lambda kind: {"ACQUIRE_CUSTOM_NODE_REPOSITORY": 1,
                              "INSTALL_CUSTOM_NODE_DEPENDENCIES": 2,
                              "PLACE_COMFYUI_WORKFLOW": 3,
                              "DOWNLOAD_MODEL_ASSET": 4}[kind]):
        raise ExternalAssetExecutionError("External step order changed")
    for step in plan["steps"]:
        kind = step["type"]
        if kind == "ACQUIRE_CUSTOM_NODE_REPOSITORY":
            url = _repo_url(step["source_url"])
            name = urlparse(url).path.strip("/").split("/")[1]
            if (url not in nodes or step["repository"].casefold() != urlparse(url).path.strip("/").casefold()
                    or Path(step["target"]) != root / "custom_nodes" / name
                    or not HEX40.fullmatch(step["revision"])):
                raise ExternalAssetExecutionError("Custom node repository contract changed")
        elif kind == "INSTALL_CUSTOM_NODE_DEPENDENCIES":
            parent = next((s for s in plan["steps"] if s["step_id"] == step["repository_step_id"]
                           and s["type"] == "ACQUIRE_CUSTOM_NODE_REPOSITORY"), None)
            if not parent:
                raise ExternalAssetExecutionError("Documented custom node dependencies changed")
            target = Path(parent["target"])
            expected = (_dependency_step(target, parent["step_id"], Path(plan["comfyui_python"]))
                        if target.is_dir() else None)
            if target.is_dir() and (expected is None or step != expected):
                raise ExternalAssetExecutionError("Documented custom node dependencies changed")
            if not target.is_dir() and (step.get("source_root") != str(target)
                    or step.get("step_id") != parent["step_id"] + "-dependencies"
                    or Path(step.get("manifest", "")).parent != target
                    or Path(step.get("manifest", "")).name not in {"requirements.txt", "pyproject.toml", "setup.py"}
                    or not HEX64.fullmatch(step.get("manifest_sha256", ""))
                    or step.get("python_executable") != plan["comfyui_python"]
                    or step.get("argv") not in ([plan["comfyui_python"], "-m", "pip", "install", "-r", step["manifest"]],
                                                [plan["comfyui_python"], "-m", "pip", "install", str(target)])):
                raise ExternalAssetExecutionError("Unverified custom node dependency command")
        elif kind == "PLACE_COMFYUI_WORKFLOW":
            src, target = Path(step["source"]), Path(step["target"])
            workflow_dir = _workflow_directory(root)
            if (not _inside(src, source) or not src.is_file() or target.parent != workflow_dir
                    or not workflow_dir.is_dir() or target.name != src.name
                    or _sha256(src) != step["source_sha256"]):
                raise ExternalAssetExecutionError("Verified workflow changed")
        elif kind == "DOWNLOAD_MODEL_ASSET":
            target, repo = Path(step["target_path"]), step["repository_url"]
            if (repo not in hf or not HEX40.fullmatch(step["revision"])
                    or urlparse(repo).scheme != "https" or urlparse(repo).hostname != "huggingface.co"
                    or step["url"] != f"{repo}/resolve/{step['revision']}/{quote(step['filename'], safe='/')}"
                    or target.name != step["filename"] or not _inside(target, root / "models")
                    or not isinstance(step["expected_size"], int) or step["expected_size"] <= 0
                    or (step.get("expected_sha256") is not None
                        and not HEX64.fullmatch(step["expected_sha256"]))):
                raise ExternalAssetExecutionError("Model asset contract changed")


def _preflight(plan: dict) -> None:
    root = Path(plan["comfyui_root"])
    for step in plan["steps"]:
        kind = step["type"]
        if kind == "ACQUIRE_CUSTOM_NODE_REPOSITORY":
            target = Path(step["target"])
            if not _inside(target, root / "custom_nodes") or target.parent != root / "custom_nodes":
                raise ExternalAssetExecutionError("Custom node target escapes ComfyUI")
            if target.exists():
                if not target.is_dir() or not (target / ".git").exists():
                    raise ExternalAssetExecutionError("Custom node target already exists and is not Git")
                if _repo_url(_git(target, "remote", "get-url", "origin")) != step["source_url"]:
                    raise ExternalAssetExecutionError("Existing custom node remote differs")
                if _git(target, "rev-parse", "HEAD") != step["revision"]:
                    raise ExternalAssetExecutionError("REVIEW_REQUIRED: existing custom node revision differs")
                _ephemeral_cache_only(target)
        elif kind == "PLACE_COMFYUI_WORKFLOW":
            target = Path(step["target"])
            if target.exists() and (not target.is_file() or _sha256(target) != step["source_sha256"]):
                raise ExternalAssetExecutionError("REVIEW_REQUIRED: workflow target exists with different content")
        elif kind == "DOWNLOAD_MODEL_ASSET":
            target = Path(step["target_path"])
            if target.exists():
                raise ExternalAssetExecutionError("REVIEW_REQUIRED: model target already exists")
            anchor = target.parent
            while not anchor.exists() and anchor != root:
                anchor = anchor.parent
            if not _inside(anchor, root / "models") and anchor != root / "models":
                raise ExternalAssetExecutionError("Model target parent escapes ComfyUI models")
            if shutil.disk_usage(anchor).free < step["expected_size"] + 64 * 1024 * 1024:
                raise ExternalAssetExecutionError("Insufficient target disk space for model asset")


def _execute_repository(step: dict) -> dict:
    target = Path(step["target"])
    if target.exists():
        if (_repo_url(_git(target, "remote", "get-url", "origin")) != step["source_url"]
                or _git(target, "rev-parse", "HEAD") != step["revision"]):
            raise ExternalAssetExecutionError("Existing repository identity or revision changed")
        cache_present = _ephemeral_cache_only(target)
        outcome = ("REUSE_EXISTING_VERIFIED_REPOSITORY_WITH_EPHEMERAL_CACHE" if cache_present
                   else "REUSE_EXISTING_VERIFIED_REPOSITORY")
        return {"outcome": outcome, "head": step["revision"]}
    temp = Path(tempfile.mkdtemp(prefix=".ornith-node-", dir=target.parent))
    try:
        result = _run(["git", "clone", "--no-checkout", step["source_url"], str(temp)])
        if result.returncode:
            raise ExternalAssetExecutionError(f"Custom node clone failed: {result.stderr.strip()}")
        _git(temp, "checkout", "--detach", step["revision"])
        if _repo_url(_git(temp, "remote", "get-url", "origin")) != step["source_url"]:
            raise ExternalAssetExecutionError("Cloned repository identity mismatch")
        if _git(temp, "rev-parse", "HEAD") != step["revision"]:
            raise ExternalAssetExecutionError("Custom node checkout revision mismatch")
        os.rename(temp, target)
        return {"outcome": "ACQUIRED_FIXED_REPOSITORY", "head": step["revision"]}
    finally:
        if temp.exists():
            if not _inside(temp, target.parent):
                raise ExternalAssetExecutionError("Temporary clone escaped custom node directory")
            shutil.rmtree(temp)


def _verify_acquired_dependencies(step: dict, plan: dict) -> None:
    """A newly acquired source must match the exact approved dependency steps."""
    documented = _dependency_step(Path(step["target"]), step["step_id"],
                                  Path(plan["comfyui_python"]))
    planned = [item for item in plan["steps"]
               if item["type"] == "INSTALL_CUSTOM_NODE_DEPENDENCIES"
               and item.get("repository_step_id") == step["step_id"]]
    if planned != ([documented] if documented else []):
        raise ExternalAssetExecutionError(
            "Acquired custom node dependency manifest differs from approved plan")


def _execute_dependencies(step: dict, plan: dict) -> dict:
    source = Path(step["source_root"])
    parent = next((s for s in plan["steps"] if s["step_id"] == step["repository_step_id"]
                   and s["type"] == "ACQUIRE_CUSTOM_NODE_REPOSITORY"), None)
    if (not parent or source != Path(parent["target"]) or not source.is_dir()
            or _repo_url(_git(source, "remote", "get-url", "origin")) != parent["source_url"]
            or _git(source, "rev-parse", "HEAD") != parent["revision"]):
        raise ExternalAssetExecutionError("Custom node source changed before dependency installation")
    _ephemeral_cache_only(source)
    expected = _dependency_step(source, step["repository_step_id"], Path(plan["comfyui_python"]))
    if step != expected:
        raise ExternalAssetExecutionError("Custom node dependency manifest changed")
    result = _run(step["argv"], cwd=source)
    if result.returncode:
        raise ExternalAssetExecutionError(f"THIRD_PARTY_DEPENDENCY_ISSUE: exit {result.returncode}: {result.stderr[-1200:]}")
    return {"outcome": "DOCUMENTED_DEPENDENCIES_INSTALLED", "argv": step["argv"],
            "exit_code": result.returncode}


def _execute_workflow(step: dict) -> dict:
    source, target = Path(step["source"]), Path(step["target"])
    if _sha256(source) != step["source_sha256"]:
        raise ExternalAssetExecutionError("Workflow source changed")
    if target.exists():
        if target.is_file() and _sha256(target) == step["source_sha256"]:
            return {"outcome": "REUSE_EXISTING_IDENTICAL_WORKFLOW", "sha256": step["source_sha256"]}
        raise ExternalAssetExecutionError("Workflow target exists")
    fd, temp_name = tempfile.mkstemp(prefix=".ornith-workflow-", suffix=".partial", dir=target.parent)
    temp = Path(temp_name)
    try:
        with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        if _sha256(temp) != step["source_sha256"]:
            raise ExternalAssetExecutionError("Workflow placement integrity mismatch")
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {"outcome": "WORKFLOW_PLACED", "sha256": step["source_sha256"]}


def _execute_model(step: dict) -> dict:
    target = Path(step["target_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ExternalAssetExecutionError("Model target exists")
    fd, temp_name = tempfile.mkstemp(prefix=".ornith-download-", suffix=".partial", dir=target.parent)
    temp = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as writer, urlopen(Request(step["url"], headers={"User-Agent": "Ornith-Solution-Scout"}), timeout=45) as response:
            final_url = urlparse(response.geturl())
            if final_url.scheme != "https":
                raise ExternalAssetExecutionError("Model download redirected away from HTTPS")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != step["expected_size"]:
                raise ExternalAssetExecutionError("Model Content-Length differs from approved plan")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > step["expected_size"]:
                    raise ExternalAssetExecutionError("Model download exceeds approved size")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if size != step["expected_size"]:
            raise ExternalAssetExecutionError("Model download size mismatch")
        sha = digest.hexdigest()
        if step.get("expected_sha256") and sha.casefold() != step["expected_sha256"].casefold():
            raise ExternalAssetExecutionError("Model SHA256 mismatch")
        # Hard link creation is atomic and fails if the user created the target meanwhile.
        os.link(temp, target)
        temp.unlink()
        return {"outcome": "MODEL_VERIFIED_AND_PLACED", "size": size, "sha256": sha,
                "hash_status": "VERIFIED_SHA256" if step.get("expected_sha256") else "NO_VERIFIED_HASH"}
    finally:
        temp.unlink(missing_ok=True)


def execute_external_plan(jobs_root: Path, job_id: str) -> dict:
    core = _core()
    store = core.JobStore(Path(jobs_root), job_id)
    with store.lifecycle_lock():
        plan = store.load(PLAN_NAME)
        if not isinstance(plan, dict):
            raise ExternalAssetExecutionError("External asset plan is missing")
        _approved(store, plan)
        _verify_contract(store, plan)
        audit = core._audit(store, KIND)
        try:
            _preflight(plan)
            audit.append("EXTERNAL_ASSET_PREFLIGHT", core.plan_hash(plan), "SUCCESS")
        except Exception as exc:
            audit.append("EXTERNAL_ASSET_PREFLIGHT", str(exc), "FAILED")
            store.save("external_asset_execution_state.json", {
                "schema": "ornith.external-asset-execution", "version": 1,
                "plan_hash": core.plan_hash(plan), "status": "BLOCKED", "error": str(exc), "steps": []})
            raise
        state = {"schema": "ornith.external-asset-execution", "version": 1,
                 "plan_hash": core.plan_hash(plan), "status": "RUNNING", "steps": []}
        store.save("external_asset_execution_state.json", state)
        for step in plan["steps"]:
            kind = step["type"]
            audit.append(kind, step, "STARTED", metadata={"step_id": step["step_id"],
                                                    "plan_hash": state["plan_hash"]})
            try:
                if kind == "ACQUIRE_CUSTOM_NODE_REPOSITORY":
                    result = _execute_repository(step)
                    _verify_acquired_dependencies(step, plan)
                elif kind == "INSTALL_CUSTOM_NODE_DEPENDENCIES":
                    result = _execute_dependencies(step, plan)
                elif kind == "PLACE_COMFYUI_WORKFLOW":
                    result = _execute_workflow(step)
                elif kind == "DOWNLOAD_MODEL_ASSET":
                    result = _execute_model(step)
                else:
                    raise ExternalAssetExecutionError("Unsupported external asset step")
                state["steps"].append({"step_id": step["step_id"], "type": kind, "status": "SUCCESS", **result})
                store.save("external_asset_execution_state.json", state)
                audit.append(kind, result, "SUCCESS", metadata={"step_id": step["step_id"],
                                                                "plan_hash": state["plan_hash"]})
            except Exception as exc:
                state["status"] = "FAILED"
                state["failed_step_id"] = step["step_id"]
                state["error"] = str(exc)
                store.save("external_asset_execution_state.json", state)
                audit.append(kind, str(exc), "FAILED", metadata={"step_id": step["step_id"],
                                                                  "plan_hash": state["plan_hash"]})
                raise
        try:
            for step in plan["steps"]:
                if step["type"] == "ACQUIRE_CUSTOM_NODE_REPOSITORY" and _git(Path(step["target"]), "rev-parse", "HEAD") != step["revision"]:
                    raise ExternalAssetExecutionError("Final custom node revision changed")
                if step["type"] == "PLACE_COMFYUI_WORKFLOW" and _sha256(Path(step["target"])) != step["source_sha256"]:
                    raise ExternalAssetExecutionError("Final workflow hash changed")
                if step["type"] == "DOWNLOAD_MODEL_ASSET":
                    target = Path(step["target_path"])
                    if target.stat().st_size != step["expected_size"] or (step.get("expected_sha256") and _sha256(target) != step["expected_sha256"]):
                        raise ExternalAssetExecutionError("Final model integrity failed")
        except Exception as exc:
            state["status"] = "FAILED"
            state["error"] = str(exc)
            store.save("external_asset_execution_state.json", state)
            audit.append("EXTERNAL_ASSET_FINAL_INTEGRITY", str(exc), "FAILED")
            raise
        state["status"] = "COMPLETE"
        store.save("external_asset_execution_state.json", state)
        audit.append("EXTERNAL_ASSET_FINAL_INTEGRITY", state["plan_hash"], "SUCCESS")
        return state
