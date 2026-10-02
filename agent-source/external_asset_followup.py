"""Plan-only follow-up for external assets documented by a verified source tree.

This module never downloads an asset, clones a repository, or changes ComfyUI.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from executor import verify_extracted_tree
from job_context import JobContext


class FollowupError(ValueError):
    pass


_GITHUB_REPO = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?=[)\]\s`]|$)")
_HF_REPO = re.compile(r"https://huggingface\.co/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?=[)\s`]|$)")
_HASH = re.compile(r"[0-9a-fA-F]{40}")
_ASSET_SUFFIXES = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"}


def _verified_source(job_dir: Path) -> tuple[Path, dict]:
    context = JobContext.load(job_dir)
    research = json.loads((job_dir / "install_research.json").read_text(encoding="utf-8"))
    if research.get("status") != "COMPLETE":
        raise FollowupError("Research is not COMPLETE")
    identity = research.get("source_identity") or {}
    source = Path(str(context.resolved_project_destination or ""))
    claimed = Path(str(identity.get("local_path") or ""))
    repo = str(identity.get("repository") or "")
    commit = str(identity.get("exact_commit") or "")
    url = str(identity.get("canonical_url") or "")
    if (context.state.value not in {"SOURCE_ACQUIRED", "RESEARCH_COMPLETE", "PLAN_REVIEW", "APPROVED", "EXECUTING", "STOPPED", "RESUMABLE"}
            or identity.get("acquisition_verified") is not True
            or not source.is_absolute() or not claimed.is_absolute() or source.resolve() != claimed.resolve()
            or repo.casefold() != str(context.repository_identity or "").casefold()
            or not _HASH.fullmatch(commit) or commit.casefold() != str(context.exact_revision or "").casefold()
            or url.casefold().rstrip("/") != str(context.canonical_repo_url or "").casefold().rstrip("/")):
        raise FollowupError("Research source differs from verified job identity")
    state = json.loads((job_dir / "execution_state.json").read_text(encoding="utf-8"))
    matches = [s for s in state.get("steps", []) if s.get("step_id") == identity.get("acquisition_step_id")]
    if len(matches) != 1 or matches[0].get("status") != "SUCCESS":
        raise FollowupError("Acquisition has not succeeded")
    acquired = matches[0]
    inputs = acquired.get("inputs") or {}
    evidence = acquired.get("acquisition_evidence") or {}
    manifest = evidence.get("extracted_tree_manifest") or {}
    if (str(inputs.get("repository") or "").casefold() != repo.casefold()
            or str(inputs.get("canonical_url") or "").casefold().rstrip("/") != url.casefold().rstrip("/")
            or str(inputs.get("exact_commit") or "").casefold() != commit.casefold()
            or Path(str(inputs.get("destination") or "")).resolve() != source.resolve()
            or str(manifest.get("repository") or "").casefold() != repo.casefold()
            or str(manifest.get("exact_commit") or "").casefold() != commit.casefold()):
        raise FollowupError("Acquisition identity changed")
    tree = verify_extracted_tree(source, evidence)
    if tree.get("state") != "VERIFIED":
        raise FollowupError(f"Source tree verification failed: {tree.get('reason')}")
    return source, {"repository": repo, "commit": commit, "tree_sha256": tree["tree_sha256"]}


def _documented_links(source: Path) -> tuple[list[str], list[str]]:
    hf: set[str] = set()
    nodes: set[str] = set()
    for readme in sorted(source.rglob("README*")):
        if not readme.is_file() or readme.is_symlink():
            continue
        heading = ""
        in_fence = False
        for line in readme.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence and line.lstrip().startswith("#"):
                heading = line.casefold()
            for match in _HF_REPO.finditer(line):
                hf.add(f"https://huggingface.co/{match[1]}/{match[2]}")
            if "custom node" in heading:
                for match in _GITHUB_REPO.finditer(line):
                    nodes.add(f"https://github.com/{match[1]}/{match[2].removesuffix('.git')}")
    return sorted(hf), sorted(nodes)


def _fetch_hf_metadata(repo_url: str) -> dict:
    parts = urlparse(repo_url).path.strip("/").split("/")
    if len(parts) != 2 or urlparse(repo_url).hostname != "huggingface.co":
        raise FollowupError("Invalid documented Hugging Face repository")
    api = "https://huggingface.co/api/models/" + "/".join(quote(part, safe="") for part in parts) + "?blobs=true"
    with urlopen(Request(api, headers={"User-Agent": "Ornith-Asset-Followup/1"}), timeout=20) as response:
        return json.load(response)


def _intent_tokens(intent: str) -> list[str]:
    # Version, quantization, and variant words must all match the actual filename.
    return [token.casefold() for token in re.findall(r"v\d+(?:\.\d+)+|int\d+|pruned|nvfp\d+|fp\d+|w\d+a\d+|(?:ref|fl|t|i|l)2va", intent, re.I)]


def _model_candidates(metadata: dict, repo_url: str, intent: str) -> list[dict]:
    repo_id = urlparse(repo_url).path.strip("/")
    revision = str(metadata.get("sha") or "")
    if str(metadata.get("id") or "").casefold() != repo_id.casefold() or not _HASH.fullmatch(revision):
        raise FollowupError("Hugging Face response lacks matching immutable repository identity")
    tokens = _intent_tokens(intent)
    if not tokens:
        return []
    matches = []
    for sibling in metadata.get("siblings") or []:
        name = str(sibling.get("rfilename") or "")
        path = Path(name)
        if (not name or name.startswith("/") or "\\" in name or ".." in path.parts
                or path.suffix.casefold() not in _ASSET_SUFFIXES
                or not all(token in path.name.casefold() for token in tokens)):
            continue
        size = sibling.get("size") or (sibling.get("lfs") or {}).get("size")
        if not isinstance(size, int) or size <= 0:
            continue
        matches.append({"filename": name, "size_bytes": size, "revision": revision,
                        "download_url": f"{repo_url}/resolve/{revision}/{quote(name, safe='/')}",
                        "sha256": (sibling.get("lfs") or {}).get("sha256")})
    return matches


def _workflows(source: Path, root: Path) -> list[dict]:
    results = []
    for file in sorted(source.rglob("*.json")):
        if file.is_symlink() or not file.is_file():
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            continue
        loaders = []
        for node in data["nodes"]:
            kind = str(node.get("type") or "")
            if "loader" not in kind.casefold():
                continue
            values = node.get("widgets_values") or []
            filename = next((v for v in values if isinstance(v, str) and Path(v).suffix.casefold() in _ASSET_SUFFIXES), None)
            directories = sorted({str(m.get("directory")) for m in (node.get("properties") or {}).get("models", [])
                                  if isinstance(m, dict) and re.fullmatch(r"[A-Za-z0-9_-]+", str(m.get("directory") or ""))})
            if filename:
                loaders.append({"node_id": node.get("id"), "node_class": kind,
                                "filename": filename, "metadata_directories": directories})
        results.append({"source_path": str(file), "filename": file.name,
                        "target_path": str(root / "user" / "default" / "workflows" / file.name),
                        "loaders": loaders})
    return results


def build_followup_plan(job_dir: str | Path, *, original_intent: str, comfyui_root: str | Path,
                        metadata_fetcher=_fetch_hf_metadata) -> dict:
    job_dir = Path(job_dir)
    root = Path(comfyui_root)
    if not root.is_absolute() or not root.is_dir() or not (root / "custom_nodes").is_dir() or not (root / "models").is_dir():
        raise FollowupError("Existing ComfyUI root with models and custom_nodes is required")
    source, identity = _verified_source(job_dir)
    hf_repos, node_repos = _documented_links(source)
    workflows = _workflows(source, root)
    steps = []
    for index, workflow in enumerate(workflows, 1):
        steps.append({"step_id": f"workflow-{index}", "type": "PLACE_COMFYUI_WORKFLOW",
                      "execution_mode": "PLAN_ONLY", "source": workflow["source_path"],
                      "target": workflow["target_path"], "loaders": workflow["loaders"]})
    model_findings = []
    inventories = []
    for repo_url in hf_repos:
        try:
            candidates = _model_candidates(metadata_fetcher(repo_url), repo_url, original_intent)
            issue = None
        except (OSError, ValueError, KeyError) as exc:
            candidates, issue = [], f"METADATA_UNAVAILABLE: {exc}"
        inventories.append((repo_url, candidates, issue))
    match_count = sum(len(candidates) for _, candidates, _ in inventories)
    inventory_failed = any(issue for _, _, issue in inventories)
    for index, (repo_url, candidates, issue) in enumerate(inventories, 1):
        if issue is None:
            issue = ("METADATA_INCOMPLETE" if inventory_failed else
                     "AMBIGUOUS_ASSET" if match_count > 1 else
                     "NO_MATCHING_ASSET" if match_count == 0 else None)
        selected = candidates[0] if not inventory_failed and match_count == 1 and len(candidates) == 1 else None
        bindings = [dict(workflow=workflow["filename"], **loader)
                    for workflow in workflows for loader in workflow["loaders"]
                    if selected and Path(loader["filename"]).name.casefold() == Path(selected["filename"]).name.casefold()]
        target = None
        if len(bindings) == 1 and len(bindings[0]["metadata_directories"]) == 1:
            target = str(root / "models" / bindings[0]["metadata_directories"][0] / Path(selected["filename"]).name)
        directory_evidence = [dict(workflow=workflow["filename"], **loader)
                              for workflow in workflows for loader in workflow["loaders"]
                              if loader["metadata_directories"]]
        finding = {"repository_url": repo_url, "selected_asset": selected, "candidates": candidates,
                   "workflow_bindings": bindings, "workflow_directory_evidence": directory_evidence,
                   "target_path": target,
                   "review_reason": issue or (None if target else "WORKFLOW_MODEL_FILENAME_OR_DIRECTORY_NOT_CONFIRMED")}
        model_findings.append(finding)
        steps.append({"step_id": f"model-{index}", "type": "DOWNLOAD_DOCUMENTED_MODEL_ASSET",
                      "execution_mode": "PLAN_ONLY", **finding})
    for index, repo_url in enumerate(node_repos, 1):
        repo_id = urlparse(repo_url).path.strip("/")
        repo_name = repo_id.split("/")[1]
        steps.append({"step_id": f"custom-node-{index}", "type": "ACQUIRE_CUSTOM_NODE_REPOSITORY",
                      "execution_mode": "PLAN_ONLY", "repository": repo_id, "source_url": repo_url,
                      "target": str(root / "custom_nodes" / repo_name),
                      "review_reason": "IMMUTABLE_REVISION_NOT_YET_VERIFIED"})
    return {"schema": "ornith.external-asset-followup", "version": 1, "status": "REVIEW_REQUIRED",
            "execution_mode": "PLAN_ONLY", "source_identity": identity, "verified_source": str(source),
            "comfyui_root": str(root), "original_intent": original_intent,
            "documented_hf_repositories": hf_repos, "documented_custom_node_repositories": node_repos,
            "workflows": workflows, "models": model_findings, "steps": steps}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--comfyui-root", required=True)
    args = parser.parse_args()
    plan = build_followup_plan(args.job_dir, original_intent=args.intent, comfyui_root=args.comfyui_root)
    output = Path(args.job_dir) / "external_asset_followup_plan.json"
    output.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
