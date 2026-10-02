#!/usr/bin/env python3
"""Evidence-first solution scouting with explicit human phase gates."""
from __future__ import annotations

import argparse
import html as html_lib
import hashlib
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.error
import urllib.request
import uuid
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import product_cli
import product_config
import first_party_identity
from runtime_paths import runtime_path
from storage_safety import atomic_write_json

ROOT = Path(__file__).resolve().parent
TASKS = runtime_path("tasks")
JOBS = runtime_path("jobs")
SNAPSHOTS = runtime_path("source_snapshots")
REPORTS = runtime_path("reports")
BUNDLED_FIRST_PARTY_YOUTUBE_INDEX = ROOT / "data" / "first_party_youtube_index.json"
FIRST_PARTY_YOUTUBE_INDEX = runtime_path("first_party_youtube_index.json")
BUNDLED_FIRST_PARTY_WEBSITE_INDEX = ROOT / "data" / "first_party_website_index.json"
FIRST_PARTY_WEBSITE_INDEX = runtime_path("first_party_website_index.json")
UA = "Ornith-Solution-Scout/1.0 evidence-research"

# Windows may expose a legacy console code page even when fetched source titles
# contain Unicode.  Keep persisted data and human-readable output lossless.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SOURCE_PRIORITY = {
    "github_repository": 60,
    "first_party_website": 20,
    "documentation": 70,
    "github_release": 55,
    "github_issue": 50,
    "huggingface": 70,
    "civitai": 60,
    "reddit": 45,
    "author_video": 40,
    "blog": 20,
    "web": 10,
}

HF_MODELS_API = "https://huggingface.co/api/models"
HF_SEARCH_TIMEOUT_SECONDS = 8
HF_MAX_CANDIDATES = 20
HF_EMPTY_CHECK_BUDGET = 4
GITHUB_EMPTY_CHECK_BUDGET = 4
_HF_CONTENT_EVIDENCE_CACHE: dict[str, bool] = {}
_GITHUB_CONTENT_EVIDENCE_CACHE: dict[str, bool] = {}
_SEARCH_ERROR_KINDS = {"timeout", "rate_limited", "invalid_response", "failed"}


class SearchProviderError(RuntimeError):
    """A provider failure with a stable category for UI/reporting callers."""

    def __init__(self, provider: str, kind: str, detail: str):
        self.provider = str(provider or "unknown")
        self.kind = kind if kind in _SEARCH_ERROR_KINDS else "failed"
        self.detail = str(detail or self.kind)
        super().__init__(f"{self.provider} {self.kind}: {self.detail}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    for path in (TASKS, JOBS, SNAPSHOTS, REPORTS):
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _refresh_status_path(index_path: Path) -> Path:
    return index_path.with_suffix(index_path.suffix + ".refresh.json")


def _document_fingerprint(document: dict) -> str:
    ignored = {"content_fingerprint", "discovered_url", "sitemap_lastmod", "source_sitemap"}
    stable = {key: value for key, value in document.items() if key not in ignored}
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _youtube_document_fingerprint(document: dict) -> str:
    comparable = dict(document)
    if (comparable.get("item_type") or comparable.get("content_type")) == "channel_post":
        # YouTube localizes relative community-post dates between otherwise
        # identical responses (for example, "1 day ago" and "1 日前").
        comparable.pop("date", None)
        comparable.pop("publication_date", None)
    return _document_fingerprint(comparable)


def _with_fingerprint(document: dict) -> dict:
    result = dict(document)
    result["content_fingerprint"] = _document_fingerprint(result)
    return result


def first_party_refresh_due(index_path: Path, interval_seconds: int,
                            at: datetime | None = None) -> bool:
    """Return whether a persisted index is missing, invalid, or older than policy."""
    if not index_path.exists():
        return True
    try:
        payload = read_json(index_path)
        stamp = str(payload.get("last_refresh") or payload.get("ingested_at") or "")
        refreshed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True
    current = at or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - refreshed.astimezone(timezone.utc) >= timedelta(
        seconds=max(0, interval_seconds))


def _record_refresh_status(index_path: Path, status: str, **details) -> None:
    atomic_write_json(_refresh_status_path(index_path), {
        "status": status, "at": now(), **details,
    })


def ensure_first_party_website_baseline(
        index_path: Path = FIRST_PARTY_WEBSITE_INDEX,
        bundled_path: Path = BUNDLED_FIRST_PARTY_WEBSITE_INDEX) -> dict:
    """Seed a missing or empty runtime index from the packaged, nonempty baseline."""
    current = load_first_party_website_index(index_path)
    if current.get("documents") or index_path == bundled_path:
        return current
    bundled = load_first_party_website_index(bundled_path)
    if bundled.get("documents"):
        atomic_write_json(index_path, bundled)
        return bundled
    return current


def task_path(task_id: str) -> Path:
    return TASKS / task_id / "session.json"


def load_task(task_id: str) -> dict:
    path = task_path(task_id)
    if not path.exists():
        raise SystemExit(f"Task not found: {task_id}")
    return read_json(path)


def save_task(task: dict) -> None:
    task["updated_at"] = now()
    write_json(task_path(task["task_id"]), task)


def fetch(url: str, accept: str = "*/*", timeout: int = 20) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def generate_queries(goal: str) -> list[str]:
    from query_planner import separator_query_variants

    original = str(goal)
    compact = " ".join(goal.split())
    variants = separator_query_variants(compact)
    return list(dict.fromkeys([
        original,
        *variants[1:],
        f"site:github.com {compact}",
        f'"{compact}" official documentation',
        f'"{compact}" README requirements workflow',
        f'"{compact}" GitHub issues troubleshooting',
        f'site:reddit.com "{compact}"',
        f'"{compact}" Windows CUDA VRAM version requirements',
        f'"{compact}" tool model workflow',
    ]))


def load_external_queries(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"Unable to read queries file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid queries file JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise SystemExit("Invalid queries file: expected a JSON array of strings.")
    if not 1 <= len(payload) <= 20:
        raise SystemExit("Invalid queries file: expected 1-20 queries.")

    queries: list[str] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, str):
            raise SystemExit(f"Invalid queries file: item {index} must be a string.")
        cleaned = item.strip()
        if not cleaned:
            raise SystemExit(f"Invalid queries file: item {index} must not be empty.")
        if cleaned not in queries:
            queries.append(cleaned)
    return queries


def github_search_terms(goal: str) -> str:
    """Keep product/model identifiers and discard prose that harms repo search."""
    from query_planner import extract_task, project_anchor

    goal = " ".join(str(goal).split())
    named_project = project_anchor(goal)
    task, _task_terms = extract_task(goal)
    if named_project and task == "UNKNOWN":
        return named_project
    stop = {
        "a", "an", "find", "the", "official", "installation", "install", "windows", "workflow",
        "and", "or", "with", "for", "sources", "source", "troubleshooting",
        "guide", "setup", "public", "technical", "goal", "github", "readme",
    }
    terms = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*|[\u3400-\u9fff]+", goal):
        if token.lower() not in stop and token.lower() not in {x.lower() for x in terms}:
            terms.append(token)
    if task != "UNKNOWN" and re.search(r"[\u3400-\u9fff]", goal):
        for token in task.split():
            if token.casefold() not in {term.casefold() for term in terms}:
                terms.append(token)
    if task == "acceleration" and [term.casefold() for term in terms] in (["acceleration"], ["speedup"]):
        terms.insert(0, "inference")
    return " ".join(terms[:6]) or " ".join(goal.split())


_GITHUB_REPOSITORY_PART = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?"
_GITHUB_REPOSITORY_INPUT = re.compile(
    rf"^(?:https://github\.com/)?({_GITHUB_REPOSITORY_PART})/({_GITHUB_REPOSITORY_PART})/?$",
    re.IGNORECASE,
)


def exact_github_repository_request(value: str) -> tuple[str, str, str] | None:
    """Return a strict owner/repo navigation target, never a fuzzy search phrase."""
    match = _GITHUB_REPOSITORY_INPUT.fullmatch(str(value or "").strip())
    if not match:
        return None
    owner, repository = match.groups()
    return owner, repository, f"https://github.com/{owner}/{repository}"


def named_github_repository_request(value: str) -> tuple[str, str, str] | None:
    """Recognize explicit owner and repository names in navigation prose."""
    slash_match = re.fullmatch(
        rf"(?:find|show|locate|open)\s+(?:the\s+)?(?:official\s+)?"
        rf"({_GITHUB_REPOSITORY_PART})/({_GITHUB_REPOSITORY_PART})\s+"
        r"(?:(?:GitHub\s+)?(?:repository|repo)"
        r"(?:\s+and\s+installation\s+(?:instructions|guide))?|"
        r"installation\s+(?:instructions|guide))[.!?]?",
        str(value or "").strip(), re.IGNORECASE,
    )
    if slash_match:
        owner, repository = slash_match.groups()
        return owner, repository, f"https://github.com/{owner}/{repository}"
    match = re.fullmatch(
        rf"(?:find|show|locate|open)\s+(?:the\s+)?(?:official\s+)?"
        rf"({_GITHUB_REPOSITORY_PART})\s+({_GITHUB_REPOSITORY_PART})\s+"
        r"(?:(?:GitHub\s+)?(?:repository|repo)|"
        r"installation\s+(?:instructions|guide)(?:\s+for\s+[A-Za-z0-9 ._-]+)?)[.!?]?",
        str(value or "").strip(), re.IGNORECASE,
    )
    if not match:
        return None
    owner, repository = match.groups()
    return owner, repository, f"https://github.com/{owner}/{repository}"


def _optional_nonnegative_int(value: Any) -> int | None:
    """Preserve a provider count while keeping missing/invalid values unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _metric_collection_status(value: Any) -> str:
    return "obtained" if _optional_nonnegative_int(value) is not None else "not_obtained"


def github_repository_exact(value: str) -> list[dict]:
    """Verify and return only the exact GitHub repository requested by the user."""
    requested = exact_github_repository_request(value)
    if not requested:
        return []
    owner, repository, _canonical = requested
    api_url = ("https://api.github.com/repos/" +
               urllib.parse.quote(owner, safe="") + "/" +
               urllib.parse.quote(repository, safe=""))
    try:
        item = json.loads(fetch(api_url, "application/vnd.github+json"))
    except Exception:
        return []
    full_name = str(item.get("full_name") or "")
    if full_name.casefold() != f"{owner}/{repository}".casefold():
        return []
    if github_repository_is_confirmed_empty(item):
        return []
    canonical = f"https://github.com/{full_name}"
    return [{
        "title": full_name,
        "url": canonical,
        "date": item.get("updated_at", "UNKNOWN"),
        "summary": item.get("description") or "UNKNOWN",
        "discovered_by": f"Exact GitHub repository verification: {canonical}",
        "exact_repository_request": True,
        "requested_repository": f"{owner}/{repository}",
        "canonical_url": canonical,
        "stargazers_count": _optional_nonnegative_int(item.get("stargazers_count")),
        "stars": _optional_nonnegative_int(item.get("stargazers_count")),
        "stars_status": _metric_collection_status(item.get("stargazers_count")),
        "source_platform": "github",
        "content_role": "github_project",
        "presentation_role": "github_project",
        "result_identity": f"github:{full_name.casefold()}",
        "provenance": {
            "provider": "github_repository_api",
            "request_mode": "exact_repository_navigation",
            "requested_repository": f"{owner}/{repository}",
            "canonical_url": canonical,
            "result_identity": f"github:{full_name.casefold()}",
        },
        "search_provenance": [{
            "provider": "github_repository_api",
            "source": "GitHub",
            "input_query": str(value),
            "effective_query": f"{owner}/{repository}",
            "request_mode": "exact_repository_navigation",
            "canonical_url": canonical,
        }],
    }]


def github_repository_search(goal: str, limit: int) -> list[dict]:
    from query_planner import extract_task

    query = github_search_terms(goal)
    search_fields = ["name", "description", "topics"]
    if extract_task(goal)[0] != "UNKNOWN":
        search_fields.append("readme")
    api_query = query + (" in:" + ",".join(search_fields) if "readme" in search_fields else "")
    params = urllib.parse.urlencode({"q": api_query, "sort": "stars", "order": "desc", "per_page": limit})
    payload = json.loads(fetch(f"https://api.github.com/search/repositories?{params}", "application/vnd.github+json"))
    items = []
    empty_checks = 0
    for item in payload.get("items", []):
        should_check = item.get("size") == 0 and empty_checks < GITHUB_EMPTY_CHECK_BUDGET
        if should_check:
            empty_checks += 1
            if github_repository_is_confirmed_empty(item):
                continue
        items.append(item)
    rows = [{
        "title": item.get("full_name", "UNKNOWN"),
        "url": item.get("html_url", ""),
        "date": item.get("updated_at", "UNKNOWN"),
        "summary": item.get("description") or "UNKNOWN",
        "discovered_by": f"GitHub repository search: {query}",
        "topics": item.get("topics") or [],
        "stargazers_count": _optional_nonnegative_int(item.get("stargazers_count")),
        "stars": _optional_nonnegative_int(item.get("stargazers_count")),
        "stars_status": _metric_collection_status(item.get("stargazers_count")),
        "source_platform": "github",
        "content_role": "github_project",
        "presentation_role": "github_project",
        "result_identity": f"github:{str(item.get('full_name') or '').casefold()}",
        "provenance": {
            "provider": "github_repository_search_api",
            "query": query,
            "api_query": api_query,
            "search_fields": search_fields,
            "result_identity": f"github:{str(item.get('full_name') or '').casefold()}",
            "canonical_url": item.get("html_url", ""),
        },
        "search_provenance": [{
            "provider": "github_repository_search_api",
            "source": "GitHub",
            "input_query": str(goal),
            "effective_query": query,
            "api_query": api_query,
            "search_fields": list(search_fields),
        }],
    } for item in items]
    # A repository's host proves its type, not ownership.  PyPI project metadata
    # provides independent provenance only when it links back to this exact repo.
    for row in rows:
        owner, repo = github_identity(row["url"])
        if repo and re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(repo)}(?![A-Za-z0-9_.-])",
                              query, re.IGNORECASE):
            evidence = pypi_repository_authority(repo, row["url"])
            if evidence:
                row["authority"] = "FIRST_PARTY"
                row["authority_evidence"] = evidence
    return rows


def _provider_error_kind(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return "rate_limited"
        if exc.code in {408, 504}:
            return "timeout"
        if exc.code == 403:
            remaining = str((exc.headers or {}).get("X-RateLimit-Remaining", ""))
            details = f"{exc.reason} {exc}".casefold()
            if remaining == "0" or "rate" in details or "limit" in details:
                return "rate_limited"
        return "failed"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).casefold():
            return "timeout"
    return "failed"


def _huggingface_model_identity(model_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(model_id or "")).strip().casefold()
    return f"huggingface:model:{normalized}" if normalized else ""


def _huggingface_language_tags(item: dict, tags: list[str]) -> list[str]:
    languages: list[str] = []
    card_data = item.get("cardData") if isinstance(item.get("cardData"), dict) else {}
    values: list[Any] = [item.get("language"), card_data.get("language")]
    for value in values:
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and text.casefold() not in {entry.casefold() for entry in languages}:
                languages.append(text)
    for tag in tags:
        text = str(tag or "").strip()
        if text.casefold().startswith("language:"):
            text = text.split(":", 1)[1].strip()
        # Unprefixed HF language tags are commonly ISO-639-1 codes.  Do not
        # mislabel short task/format tags such as "tts" or "gguf" as languages.
        elif not re.fullmatch(r"[A-Za-z]{2}(?:-[A-Za-z]{2,4})?", text):
            continue
        if text and text.casefold() not in {entry.casefold() for entry in languages}:
            languages.append(text)
    return languages


def _huggingface_content_role(item: dict, tags: list[str], pipeline_tag: str) -> str:
    evidence = {str(tag).strip().casefold() for tag in tags if str(tag).strip()}
    evidence.update(str(value or "").strip().casefold()
                    for value in (item.get("library_name"), item.get("libraryName")))
    if any(token in value for value in evidence for token in ("lora", "adapter", "peft")):
        return "hf_lora_adapter"
    model_markers = {"safetensors", "transformers", "pytorch", "tensorflow", "onnx", "gguf",
                     "checkpoint", "model"}
    if pipeline_tag or evidence.intersection(model_markers):
        return "hf_model_weights"
    return "hf_item_unverified"


def _huggingface_api_json(model_id: str) -> tuple[int, Any]:
    """Read the exact model endpoint while preserving HTTP failure status."""
    url = f"{HF_MODELS_API}/{urllib.parse.quote(model_id, safe='/')}"
    try:
        body = fetch(url, "application/json", timeout=HF_SEARCH_TIMEOUT_SECONDS)
        return 200, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, AttributeError):
            payload = {}
        return int(exc.code), payload


def huggingface_repository_is_confirmed_empty(
        item: dict[str, Any], requester: Any = None) -> bool:
    """Exclude only an exact HF repository conclusively containing config files alone.

    A successful exact-model response and a complete ``siblings`` file list are
    required. Any permission/network/timeout or ambiguous response keeps the
    candidate, and failures are never cached.
    """
    model_id = str(item.get("id") or item.get("modelId") or "").strip()
    if not re.fullmatch(r"(?:[^/?#\s]+/)?[^/?#\s]+", model_id):
        return False
    cache_key = model_id.casefold()
    if cache_key in _HF_CONTENT_EVIDENCE_CACHE:
        return _HF_CONTENT_EVIDENCE_CACHE[cache_key]
    request = requester or _huggingface_api_json
    try:
        status, payload = request(model_id)
    except Exception:
        return False
    if status != 200 or not isinstance(payload, dict):
        return False
    returned_id = str(payload.get("id") or payload.get("modelId") or "").strip()
    if returned_id.casefold() != model_id.casefold():
        return False
    siblings = payload.get("siblings")
    if not isinstance(siblings, list):
        return False

    repository_config_files = {
        ".editorconfig", ".gitattributes", ".gitignore", ".hfignore", ".mailmap",
    }
    filenames: list[str] = []
    for sibling in siblings:
        if not isinstance(sibling, dict):
            return False
        filename = str(sibling.get("rfilename") or "").strip()
        if not filename:
            return False
        filenames.append(filename)
    confirmed_empty = all(
        "/" not in filename
        and "\\" not in filename
        and filename.casefold() in repository_config_files
        for filename in filenames
    )
    _HF_CONTENT_EVIDENCE_CACHE[cache_key] = confirmed_empty
    return confirmed_empty


def _huggingface_confirmed_empty_indexes(items: list[dict[str, Any]]) -> set[int]:
    """Run a small parallel evidence check inside the existing background search."""
    selected = list(enumerate(items[:HF_EMPTY_CHECK_BUDGET]))
    if not selected:
        return set()
    confirmed: set[int] = set()
    with ThreadPoolExecutor(max_workers=len(selected),
                            thread_name_prefix="hf-repository-quality") as pool:
        pending = {
            pool.submit(huggingface_repository_is_confirmed_empty, item): index
            for index, item in selected
        }
        for future in as_completed(pending):
            try:
                is_empty = future.result()
            except Exception:
                is_empty = False
            if is_empty:
                confirmed.add(pending[future])
    return confirmed


def huggingface_model_search(goal: str, limit: int = HF_MAX_CANDIDATES) -> list[dict]:
    """Search public HF model metadata without fetching cards or invoking a model."""
    from query_planner import huggingface_search_spec

    spec = huggingface_search_spec(goal)
    if spec is None:
        return []
    try:
        requested_limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        requested_limit = HF_MAX_CANDIDATES
    if requested_limit <= 0:
        return []
    bounded_limit = min(requested_limit, HF_MAX_CANDIDATES)
    params: list[tuple[str, str | int]] = [("limit", bounded_limit)]
    if spec.get("search"):
        params.append(("search", spec["search"]))
    if spec.get("filter"):
        params.append(("filter", spec["filter"]))
    if spec.get("pipeline_tag"):
        params.append(("pipeline_tag", spec["pipeline_tag"]))
    for field in ("downloads", "likes", "pipeline_tag", "tags", "lastModified", "cardData"):
        params.append(("expand", field))
    request_url = f"{HF_MODELS_API}?{urllib.parse.urlencode(params, doseq=True)}"
    try:
        raw = fetch(request_url, "application/json", timeout=HF_SEARCH_TIMEOUT_SECONDS)
    except Exception as exc:
        raise SearchProviderError("huggingface", _provider_error_kind(exc),
                                  f"{type(exc).__name__}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise SearchProviderError("huggingface", "invalid_response",
                                  f"invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise SearchProviderError("huggingface", "invalid_response",
                                  "models response was not a list")

    candidate_items = [item for item in payload[:bounded_limit] if isinstance(item, dict)]
    confirmed_empty_indexes = _huggingface_confirmed_empty_indexes(candidate_items)
    rows: list[dict] = []
    malformed_items = 0
    candidate_index = 0
    for item in payload[:bounded_limit]:
        if not isinstance(item, dict):
            malformed_items += 1
            continue
        if candidate_index in confirmed_empty_indexes:
            candidate_index += 1
            continue
        candidate_index += 1
        model_id = str(item.get("id") or item.get("modelId") or "").strip()
        if not model_id:
            malformed_items += 1
            continue
        canonical = "https://huggingface.co/" + urllib.parse.quote(model_id, safe="/")
        tags = [str(tag) for tag in (item.get("tags") or []) if str(tag).strip()]
        pipeline_tag = str(item.get("pipeline_tag") or "").strip()
        languages = _huggingface_language_tags(item, tags)
        details = ["Hugging Face model"]
        if pipeline_tag:
            details.append(f"task: {pipeline_tag}")
        if languages:
            details.append(f"languages: {', '.join(languages[:6])}")
        identity = _huggingface_model_identity(model_id)
        likes = _optional_nonnegative_int(item.get("likes"))
        downloads = _optional_nonnegative_int(item.get("downloads"))
        content_role = _huggingface_content_role(item, tags, pipeline_tag)
        rows.append({
            "title": model_id,
            "model_id": model_id,
            "url": canonical,
            "canonical_url": canonical,
            "date": item.get("lastModified") or "UNKNOWN",
            "summary": " · ".join(details),
            "source_type": "Hugging Face Model",
            "pipeline_tag": pipeline_tag,
            "task": pipeline_tag,
            "tags": tags,
            "language_tags": languages,
            "likes": likes,
            "downloads_30d": downloads,
            "likes_status": _metric_collection_status(item.get("likes")),
            "downloads_30d_status": _metric_collection_status(item.get("downloads")),
            "source_platform": "huggingface",
            "content_role": content_role,
            "presentation_role": content_role,
            "tutorial_evidence": False,
            "discovered_by": f"Hugging Face public model search: {spec.get('search') or '*'}",
            "result_identity": identity,
            "provenance": {
                "provider": "huggingface_models_api",
                "query": spec,
                "canonical_url": canonical,
                "result_identity": identity,
            },
            "search_provenance": [{
                "provider": "huggingface_models_api",
                "source": "Hugging Face",
                "input_query": str(goal),
                "effective_query": spec.get("search") or "",
                "filter": spec.get("filter") or "",
                "pipeline_tag": spec.get("pipeline_tag") or "",
                "request_url": request_url,
            }],
        })
    if payload and not rows and malformed_items:
        raise SearchProviderError("huggingface", "invalid_response",
                                  "models response contained no valid model IDs")
    return rows


def _github_api_json(url: str) -> tuple[int, Any]:
    """Return an HTTP status and JSON payload without hiding conclusive API bodies."""
    try:
        body = fetch(url, "application/vnd.github+json", timeout=8)
        return 200, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, AttributeError):
            payload = {}
        return int(exc.code), payload


def github_repository_is_confirmed_empty(item: dict[str, Any],
                                         requester: Any = _github_api_json) -> bool:
    """Exclude only a conclusively empty/config-only repository.

    The size field is only a probe gate. Any permission/network/timeout or
    ambiguous response keeps the candidate, and failures are never cached.
    """
    if item.get("size") != 0:
        return False
    full_name = str(item.get("full_name") or "")
    if not re.fullmatch(r"[^/]+/[^/]+", full_name):
        return False
    cache_key = full_name.casefold()
    if cache_key in _GITHUB_CONTENT_EVIDENCE_CACHE:
        return _GITHUB_CONTENT_EVIDENCE_CACHE[cache_key]
    owner, repository = full_name.split("/", 1)
    base = ("https://api.github.com/repos/" + urllib.parse.quote(owner, safe="") +
            "/" + urllib.parse.quote(repository, safe=""))
    try:
        content_status, content = requester(base + "/contents")
    except Exception:
        return False

    content_message = str(content.get("message") or "").casefold() if isinstance(content, dict) else ""
    if content_status in {404, 409} and "empty" in content_message:
        _GITHUB_CONTENT_EVIDENCE_CACHE[cache_key] = True
        return True
    if content_status != 200 or not isinstance(content, list):
        return False
    repository_config_files = {".gitattributes", ".gitignore", ".editorconfig", ".mailmap"}
    confirmed_empty = all(
        isinstance(entry, dict)
        and str(entry.get("type") or "").casefold() == "file"
        and str(entry.get("name") or "").casefold() in repository_config_files
        for entry in content
    )
    _GITHUB_CONTENT_EVIDENCE_CACHE[cache_key] = confirmed_empty
    return confirmed_empty


def pypi_repository_authority(package: str, repository_url: str) -> str:
    """Return the registry link that independently confirms repository ownership."""
    identity = exact_github_repository_request(repository_url)
    if not identity:
        return ""
    metadata_url = f"https://pypi.org/pypi/{urllib.parse.quote(package, safe='')}/json"
    try:
        info = json.loads(fetch(metadata_url, "application/json", timeout=8)).get("info") or {}
    except (OSError, ValueError, TypeError):
        return ""
    normalized = lambda value: re.sub(r"[-_.]+", "-", str(value).casefold())
    if normalized(info.get("name")) != normalized(package):
        return ""
    links = dict(info.get("project_urls") or {})
    if info.get("home_page"):
        links["Home page"] = info["home_page"]
    for label, url in links.items():
        if str(label).casefold() not in {"source", "repository", "homepage", "home page", "source code", "code"}:
            continue
        linked = exact_github_repository_request(str(url))
        if linked and tuple(part.casefold() for part in linked[:2]) == tuple(
                part.casefold() for part in identity[:2]):
            return f"PyPI {metadata_url} info.project_urls[{label!r}] -> {linked[2]}"
    return ""


def classify(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    path = urllib.parse.urlparse(url).path.lower()
    if host in {"github.com", "www.github.com"}:
        if "/issues/" in path or path.endswith("/issues"):
            return "github_issue"
        if "/releases" in path:
            return "github_release"
        return "github_repository"
    if "huggingface.co" in host:
        return "huggingface"
    if "civitai.com" in host:
        return "civitai"
    if "reddit.com" in host:
        return "reddit"
    if "youtube.com" in host or "youtu.be" in host:
        return "author_video"
    if any(x in host for x in ("docs.", "readthedocs", "developer.", "learn.")):
        return "documentation"
    return "web"


def github_identity(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return "", ""
    bits = [x for x in parsed.path.split("/") if x]
    return (bits[0], bits[1].removesuffix(".git")) if len(bits) >= 2 else ("", "")


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
        return f"https://youtube.com/watch?v={video_id}" if video_id else ""
    if host in {"youtube.com", "m.youtube.com"} and parsed.path.rstrip("/") == "/watch":
        video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        return f"https://youtube.com/watch?v={video_id}" if video_id else ""
    clean = parsed._replace(query="", fragment="", path=parsed.path.rstrip("/"))
    return urllib.parse.urlunparse(clean).lower()


def bing_rss(query: str, limit: int) -> list[dict]:
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    root = ET.fromstring(fetch(url, "application/rss+xml"))
    found = []
    for item in root.findall("./channel/item")[:limit]:
        found.append({
            "title": item.findtext("title", "UNKNOWN"),
            "url": item.findtext("link", ""),
            "date": item.findtext("pubDate", "UNKNOWN"),
            "summary": re.sub("<[^>]+>", "", item.findtext("description", "UNKNOWN")),
            "discovered_by": query,
            "search_provenance": [{
                "provider": "bing_rss",
                "source": "Bing",
                "input_query": str(query),
                "effective_query": str(query),
                "request_url": url,
            }],
        })
    return found


def extract_project_links(text: str) -> list[str]:
    """Extract supported project hosts from source-provided metadata."""
    links: list[str] = []
    for match in re.findall(r"https?://[^\s<>\]\[(){}\"']+", str(text or ""), re.IGNORECASE):
        url = match.rstrip(".,;:!?")
        host = urllib.parse.urlparse(url).netloc.casefold()
        if any(domain in host for domain in ("github.com", "huggingface.co", "civitai.com")):
            canonical = canonical_url(url)
            if canonical and all(canonical_url(existing) != canonical for existing in links):
                links.append(url)
    return links


def _youtube_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("simpleText") is not None:
        return str(value["simpleText"])
    return "".join(str(run.get("text") or "") for run in value.get("runs", [])
                   if isinstance(run, dict))


def _walk_youtube_renderers(value: object):
    if isinstance(value, dict):
        for renderer_type in ("videoRenderer", "gridVideoRenderer", "playlistVideoRenderer",
                              "backstagePostRenderer"):
            renderer = value.get(renderer_type)
            if isinstance(renderer, dict):
                yield renderer_type, renderer
        for child in value.values():
            yield from _walk_youtube_renderers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_youtube_renderers(child)


def _youtube_post_url(renderer: dict) -> str:
    """Return a source-provided channel-post URL, resolving only relative URLs."""
    def walk(value: object):
        if isinstance(value, dict):
            metadata = value.get("webCommandMetadata")
            if isinstance(metadata, dict):
                url = str(metadata.get("url") or "")
                if url:
                    yield url
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    for url in walk(renderer):
        parsed = urllib.parse.urlparse(url)
        if parsed.path.startswith("/post/"):
            return urllib.parse.urljoin("https://www.youtube.com", url)
    return ""


def _external_urls(text: str) -> list[str]:
    links: list[str] = []
    for match in re.findall(r"https?://[^\s<>\]\[(){}\"']+", str(text or ""), re.IGNORECASE):
        url = match.rstrip(".,;:!?")
        if url and canonical_url(url) not in {canonical_url(link) for link in links}:
            links.append(url)
    return links


def normalize_youtube_outbound_url(value: str) -> str:
    """Return an evidenced external destination, decoding YouTube wrappers only."""
    source = html_lib.unescape(str(value or "").strip())
    if not source:
        return ""
    absolute = urllib.parse.urljoin("https://www.youtube.com", source)
    parsed = urllib.parse.urlparse(absolute)
    host = parsed.netloc.casefold().split(":", 1)[0].removeprefix("www.")
    youtube_hosts = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
    if host in youtube_hosts:
        if parsed.path.rstrip("/").casefold() != "/redirect":
            return ""
        params = urllib.parse.parse_qs(parsed.query)
        target = next((params[key][0] for key in ("q", "url", "target")
                       if params.get(key)), "")
        for _ in range(3):
            decoded = urllib.parse.unquote(html_lib.unescape(target))
            if decoded == target:
                break
            target = decoded
        candidate = urllib.parse.urlparse(target)
        if candidate.scheme.casefold() not in {"http", "https"} or not candidate.netloc:
            return ""
        return target
    if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
        return absolute
    return ""


def repository_identity_from_url(url: str) -> str:
    """Return a GitHub owner/repository identity while preserving source casing."""
    parsed = urllib.parse.urlparse(str(url or ""))
    host = parsed.netloc.casefold().split(":", 1)[0].removeprefix("www.")
    if host != "github.com":
        return ""
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    owner = parts[0]
    repository = parts[1].removesuffix(".git")
    if not re.fullmatch(_GITHUB_REPOSITORY_PART, owner, re.I):
        return ""
    if not re.fullmatch(_GITHUB_REPOSITORY_PART, repository, re.I):
        return ""
    return f"{owner}/{repository}"


def _outbound_link_record(display_text: str, source_url: str, payload_field: str) -> dict | None:
    outbound_url = normalize_youtube_outbound_url(source_url)
    if not outbound_url:
        return None
    parsed = urllib.parse.urlparse(outbound_url)
    return {
        "displayed_text": str(display_text or ""),
        "source_url": str(source_url),
        "outbound_url": outbound_url,
        "canonical_url": canonical_url(outbound_url),
        "source_platform": parsed.netloc.casefold().split(":", 1)[0].removeprefix("www."),
        "repository_identity": repository_identity_from_url(outbound_url),
        "evidence": [{"provider": "youtube_payload", "payload_field": payload_field,
                      "payload_value": str(source_url)}],
    }


def extract_youtube_outbound_links(value: object, root_path: str = "youtube") -> list[dict]:
    """Extract literal or endpoint-backed external links from YouTube text runs."""
    records: dict[str, dict] = {}

    def add(display_text: str, source_url: str, field: str) -> None:
        record = _outbound_link_record(display_text, source_url, field)
        if not record:
            return
        key = record["canonical_url"]
        existing = records.get(key)
        if not existing:
            records[key] = record
            return
        for evidence in record["evidence"]:
            if evidence not in existing["evidence"]:
                existing["evidence"].append(evidence)
        if not existing["displayed_text"] and record["displayed_text"]:
            existing["displayed_text"] = record["displayed_text"]

    def endpoint_urls(node: object, path: str):
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{path}.{key}"
                if key in {"url", "href", "canonicalUrl", "canonicalHref"} and isinstance(child, str):
                    yield child, child_path
                else:
                    yield from endpoint_urls(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                yield from endpoint_urls(child, f"{path}[{index}]")

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            display_text = str(node.get("text") or "")
            if display_text:
                for literal_url in _external_urls(display_text):
                    add(literal_url, literal_url, f"{path}.text")
                for endpoint_key in ("navigationEndpoint", "endpoint", "command"):
                    endpoint = node.get(endpoint_key)
                    if endpoint is not None:
                        for endpoint_url, field in endpoint_urls(endpoint, f"{path}.{endpoint_key}"):
                            add(display_text, endpoint_url, field)
            for key, child in node.items():
                walk(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(value, root_path)
    return list(records.values())


def extract_repository_identities(text: str, urls: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Extract source-provided GitHub owner/repository identities, preserving spelling."""
    source_text = str(text or "")
    values = [source_text, *[str(url) for url in urls]]
    identities: list[str] = []
    github_pattern = re.compile(
        r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
    plain_pattern = re.compile(
        r"(?<![\w.:/-])([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)(?![/\w.-])")

    def append(owner: str, repository: str) -> None:
        repository = repository.removesuffix(".git").rstrip(".,;:!?")
        identity = f"{owner}/{repository}"
        if identity.casefold() not in {item.casefold() for item in identities}:
            identities.append(identity)

    for value in values:
        for owner, repository in github_pattern.findall(value):
            append(owner, repository)
    for match in plain_pattern.finditer(source_text):
        owner, repository = match.groups()
        line = source_text[source_text.rfind("\n", 0, match.start()) + 1:
                           source_text.find("\n", match.end()) if "\n" in source_text[match.end():]
                           else len(source_text)].strip()
        prefix = source_text[max(0, match.start() - 24):match.start()]
        contextual = bool(re.search(r"(?:repository|repo|github|source)\s*[:=-]?\s*$", prefix, re.I))
        distinctive = bool(re.search(r"[-_.]", owner) or re.search(r"[-_.]", repository))
        standalone = line.rstrip(".,;:!?") == match.group(0)
        if contextual or distinctive or standalone:
            append(owner, repository)
    return identities


def _normalized_search_text(*values: object) -> str:
    text = " ".join(str(value or "") for value in values)
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[\w.+/-]+", text, flags=re.UNICODE))


def _youtube_json(html: str, variable: str) -> dict:
    marker = re.search(rf"(?:var\s+{re.escape(variable)}\s*=|{re.escape(variable)}\s*=)\s*", html)
    if not marker:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(html[marker.end():])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _youtube_channel_metadata(value: object) -> dict:
    if isinstance(value, dict):
        metadata = value.get("channelMetadataRenderer")
        if isinstance(metadata, dict):
            return metadata
        for child in value.values():
            found = _youtube_channel_metadata(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _youtube_channel_metadata(child)
            if found:
                return found
    return {}


def parse_youtube_search_html(payload: bytes | str, query: str, limit: int) -> list[dict]:
    """Parse public YouTube result metadata without retrieving media."""
    html = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    data = _youtube_json(html, "ytInitialData")
    if not data:
        return []
    results: list[dict] = []
    for renderer_type, renderer in _walk_youtube_renderers(data):
        if renderer_type == "backstagePostRenderer":
            content = _youtube_text(renderer.get("contentText"))
            if not content.strip():
                continue
            title = next((line.strip() for line in content.splitlines() if line.strip()), content.strip())
            channel = (_youtube_text(renderer.get("authorText")) or
                       _youtube_text(renderer.get("ownerText")) or "UNKNOWN")
            published = _youtube_text(renderer.get("publishedTimeText")) or "UNKNOWN"
            outbound_links = extract_youtube_outbound_links(
                renderer.get("contentText"), "backstagePostRenderer.contentText")
            outbound_urls = [link["outbound_url"] for link in outbound_links]
            project_links = list(dict.fromkeys([
                *extract_project_links(content), *extract_project_links("\n".join(outbound_urls))]))
            post_url = _youtube_post_url(renderer)
            results.append({
                "title": title,
                "url": project_links[0] if project_links else post_url,
                "video_url": "",
                "post_url": post_url,
                "post_id": str(renderer.get("postId") or ""),
                "first_party_reference_url": post_url,
                "channel": channel,
                "date": published,
                "summary": content,
                "external_urls": list(dict.fromkeys([*_external_urls(content), *outbound_urls])),
                "outbound_links": outbound_links,
                "project_links": project_links,
                "source_type": "YouTube Post",
                "content_type": "channel_post",
                "discovered_by": f"YouTube public search: {query}",
            })
            if len(results) >= limit:
                break
            continue
        video_id = str(renderer.get("videoId") or "")
        if not video_id:
            continue
        title = _youtube_text(renderer.get("title")) or "UNKNOWN"
        channel = _youtube_text(renderer.get("ownerText")) or _youtube_text(renderer.get("longBylineText")) or "UNKNOWN"
        published = _youtube_text(renderer.get("publishedTimeText")) or "UNKNOWN"
        summary = _youtube_text(renderer.get("descriptionSnippet")) or "UNKNOWN"
        outbound_links = extract_youtube_outbound_links(
            renderer.get("descriptionSnippet"), f"{renderer_type}.descriptionSnippet")
        outbound_urls = [link["outbound_url"] for link in outbound_links]
        project_links = list(dict.fromkeys([
            *extract_project_links(summary), *extract_project_links("\n".join(outbound_urls))]))
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        results.append({
            "title": title,
            "url": project_links[0] if project_links else video_url,
            "video_id": video_id,
            "video_url": video_url,
            "channel": channel,
            "date": published,
            "summary": summary,
            "external_urls": list(dict.fromkeys([*_external_urls(summary), *outbound_urls])),
            "outbound_links": outbound_links,
            "project_links": project_links,
            "source_type": "YouTube",
            "discovered_by": f"YouTube public search: {query}",
        })
        if len(results) >= limit:
            break
    return results


def _first_party_document(row: dict, channel_url: str, channel_id: str,
                          channel_title: str) -> dict:
    video_id = str(row.get("video_id") or "")
    post_id = str(row.get("post_id") or "")
    content_type = "video" if video_id else "channel_post"
    item_id = video_id or post_id
    item_url = (f"https://www.youtube.com/watch?v={video_id}" if video_id else
                str(row.get("post_url") or row.get("url") or ""))
    description = str(row.get("description") or row.get("summary") or "")
    description_links = extract_youtube_outbound_links(
        {"runs": [{"text": description}]},
        "videoDetails.shortDescription" if video_id else "backstagePostRenderer.contentText")
    outbound_by_url: dict[str, dict] = {}
    for link in [*(row.get("outbound_links") or []), *description_links]:
        canonical = str(link.get("canonical_url") or "")
        if not canonical:
            continue
        existing = outbound_by_url.get(canonical)
        if not existing:
            outbound_by_url[canonical] = dict(link)
            continue
        evidence = existing.setdefault("evidence", [])
        for item in link.get("evidence") or []:
            if item not in evidence:
                evidence.append(item)
    outbound_links = list(outbound_by_url.values())
    outbound_urls = [str(link.get("outbound_url") or "") for link in outbound_links
                     if link.get("outbound_url")]
    external_urls = list(dict.fromkeys([*(row.get("external_urls") or []),
                                        *_external_urls(description), *outbound_urls]))
    project_links = list(dict.fromkeys([*(row.get("project_links") or []),
                                        *extract_project_links(description)]))
    repositories: list[str] = []
    for link in outbound_links:
        repository = str(link.get("repository_identity") or "")
        if repository and repository.casefold() not in {item.casefold() for item in repositories}:
            repositories.append(repository)
    for repository in extract_repository_identities("", external_urls):
        if repository.casefold() not in {item.casefold() for item in repositories}:
            repositories.append(repository)
    identity = f"youtube:{content_type}:{item_id}"
    searchable = _normalized_search_text(
        row.get("title"), description, repositories, external_urls,
        [link.get("canonical_url") for link in outbound_links], channel_title)
    document = {
        **row,
        "document_id": identity,
        "result_identity": identity,
        "video_id": video_id,
        "post_id": post_id,
        "url": item_url,
        "video_url": item_url if video_id else "",
        "post_url": item_url if post_id else "",
        "channel": str(row.get("channel") or channel_title or "UNKNOWN"),
        "channel_id": channel_id,
        "channel_url": channel_url,
        "description": description,
        "summary": description,
        "publication_date": str(row.get("publication_date") or row.get("date") or "UNKNOWN"),
        "external_urls": external_urls,
        "outbound_links": outbound_links,
        "project_links": project_links,
        "repository_identities": repositories,
        "normalized_search_text": searchable,
        "source_type": "YouTube" if video_id else "YouTube Post",
        "content_type": content_type,
        "first_party": False,
        "first_party_reference_url": item_url,
        "tutorial_url": item_url if video_id else "",
        "project_url": project_links[0] if project_links else "",
        "discovered_by": f"Configured first-party channel: {channel_url}",
        "provenance": {"provider": "youtube_channel", "channel_url": channel_url,
                       "channel_id": channel_id, "item_id": item_id},
    }
    return first_party_identity.normalize_content_identity(document)


def _apply_reviewed_youtube_project_metadata(payload: dict) -> dict:
    """Carry reviewed links for an exact channel item through index regeneration."""
    if not BUNDLED_FIRST_PARTY_YOUTUBE_INDEX.exists():
        return payload
    reviewed = read_json(BUNDLED_FIRST_PARTY_YOUTUBE_INDEX).get("reviewed_project_metadata") or {}
    if not reviewed:
        return payload
    documents = []
    for row in payload.get("documents", []):
        metadata = reviewed.get(str(row.get("result_identity") or ""))
        if not isinstance(metadata, dict) or any(
            str(row.get(field) or "") != str(metadata.get(field) or "")
            for field in ("channel_url", "channel_id")
        ):
            documents.append(row)
            continue
        project_url = str(metadata.get("project_url") or "")
        if not exact_github_repository_request(project_url):
            documents.append(row)
            continue
        enriched = dict(row)
        enriched["project_links"] = list(dict.fromkeys([
            project_url, *(row.get("project_links") or [])]))
        enriched["external_urls"] = list(dict.fromkeys([
            *(row.get("external_urls") or []), project_url,
            *(metadata.get("external_urls") or [])]))
        documents.append(_with_fingerprint(_first_party_document(
            enriched, str(row["channel_url"]), str(row["channel_id"]),
            str(payload.get("channel_title") or payload.get("channel") or ""))))
    result = dict(payload)
    result["documents"] = documents
    result["reviewed_project_metadata"] = reviewed
    return result


def _collect_first_party_youtube_index(channel_url: str, fetcher=fetch) -> dict:
    """Collect one complete channel snapshot without mutating the persisted cache."""
    base = channel_url.rstrip("/")
    rows: list[dict] = []
    channel_id = ""
    channel_title = ""
    for tab in ("videos", "posts"):
        payload = fetcher(f"{base}/{tab}", "text/html", timeout=20)
        html = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        data = _youtube_json(html, "ytInitialData")
        metadata = _youtube_channel_metadata(data)
        channel_id = channel_id or str(metadata.get("externalId") or metadata.get("channelId") or "")
        channel_title = channel_title or str(metadata.get("title") or "")
        rows.extend(parse_youtube_search_html(html, f"channel:{tab}", 1000))

    documents: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        video_id = str(row.get("video_id") or "")
        if not video_id and row.get("video_url"):
            video_id = urllib.parse.parse_qs(
                urllib.parse.urlparse(str(row["video_url"])).query).get("v", [""])[0]
            row["video_id"] = video_id
        if video_id:
            watch_payload = fetcher(f"https://www.youtube.com/watch?v={video_id}", "text/html", timeout=20)
            watch_html = (watch_payload.decode("utf-8", errors="replace")
                          if isinstance(watch_payload, bytes) else str(watch_payload))
            details = _youtube_json(watch_html, "ytInitialPlayerResponse").get("videoDetails") or {}
            micro = (((_youtube_json(watch_html, "ytInitialPlayerResponse").get("microformat") or {})
                      .get("playerMicroformatRenderer")) or {})
            row["title"] = str(details.get("title") or row.get("title") or "UNKNOWN")
            row["channel"] = str(details.get("author") or row.get("channel") or channel_title or "UNKNOWN")
            row["description"] = str(details.get("shortDescription") or row.get("summary") or "")
            row["publication_date"] = str(micro.get("publishDate") or row.get("date") or "UNKNOWN")
        document = _first_party_document(row, base, channel_id, channel_title)
        if document["result_identity"] in seen or not (document.get("video_id") or document.get("post_id")):
            continue
        seen.add(document["result_identity"])
        documents.append(_with_fingerprint(document))
    documents.sort(key=lambda item: str(item.get("result_identity") or ""))
    return _apply_reviewed_youtube_project_metadata({
        "schema_version": 3, "channel_url": base, "channel_id": channel_id,
        "channel": channel_title, "channel_title": channel_title,
        "ingested_at": now(), "last_refresh": now(), "documents": documents})


def build_first_party_youtube_index(channel_url: str, index_path: Path = FIRST_PARTY_YOUTUBE_INDEX,
                                    fetcher=fetch) -> dict:
    """Ingest public channel metadata directly and persist a normalized local index."""
    payload = _collect_first_party_youtube_index(channel_url, fetcher)
    if not payload["documents"]:
        raise ValueError("first-party channel refresh returned no documents")
    payload["refresh"] = {
        "status": "completed",
        "previous_document_count": 0,
        "current_source_count": len(payload["documents"]),
        "new": len(payload["documents"]), "changed": 0, "removed": 0,
        "unchanged": 0, "final_persisted_count": len(payload["documents"]),
    }
    atomic_write_json(index_path, payload)
    _record_refresh_status(index_path, "completed", statistics=payload["refresh"])
    return payload


def refresh_first_party_youtube_index(channel_url: str,
                                      index_path: Path = FIRST_PARTY_YOUTUBE_INDEX,
                                      fetcher=fetch) -> dict:
    """Atomically replace a channel snapshot, retaining the old cache on failure."""
    previous = load_first_party_youtube_index(index_path)
    old_documents = {
        str(item.get("result_identity") or ""): item
        for item in previous.get("documents", []) if item.get("result_identity")
    }
    try:
        candidate = _collect_first_party_youtube_index(channel_url, fetcher)
        if not candidate.get("documents"):
            raise ValueError("first-party channel refresh returned no documents")
        new_documents = {
            str(item.get("result_identity") or ""): item
            for item in candidate.get("documents", []) if item.get("result_identity")
        }
        new_ids = new_documents.keys() - old_documents.keys()
        removed_ids = old_documents.keys() - new_documents.keys()
        common_ids = old_documents.keys() & new_documents.keys()
        changed_ids = {
            identity for identity in common_ids
            if _youtube_document_fingerprint(old_documents[identity]) !=
               _youtube_document_fingerprint(new_documents[identity])
        }
        unchanged_ids = common_ids - changed_ids
        candidate["documents"] = [
            old_documents[identity] if identity in unchanged_ids else new_documents[identity]
            for identity in sorted(new_documents)
        ]
        statistics = {
            "previous_document_count": len(old_documents),
            "current_source_count": len(new_documents),
            "new": len(new_ids), "changed": len(changed_ids),
            "removed": len(removed_ids), "unchanged": len(unchanged_ids),
            "final_persisted_count": len(new_documents),
        }
        candidate["refresh"] = {
            "status": "completed", **statistics,
            "removed_documents": sorted(removed_ids),
        }
        atomic_write_json(index_path, candidate)
        _record_refresh_status(index_path, "completed", statistics=statistics)
        return candidate
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _record_refresh_status(index_path, "failed", error=error,
                               retained_document_count=len(old_documents))
        result = dict(previous)
        result["refresh_result"] = {
            "status": "failed", "error": error,
            "retained_document_count": len(old_documents),
        }
        return result


def load_first_party_youtube_index(index_path: Path = FIRST_PARTY_YOUTUBE_INDEX) -> dict:
    payload = read_json(index_path) if index_path.exists() else {"schema_version": 2, "documents": []}
    return _apply_reviewed_youtube_project_metadata(payload)


def ensure_first_party_youtube_baseline() -> None:
    if (FIRST_PARTY_YOUTUBE_INDEX != BUNDLED_FIRST_PARTY_YOUTUBE_INDEX and
            not FIRST_PARTY_YOUTUBE_INDEX.exists() and BUNDLED_FIRST_PARTY_YOUTUBE_INDEX.exists()):
        atomic_write_json(FIRST_PARTY_YOUTUBE_INDEX, read_json(BUNDLED_FIRST_PARTY_YOUTUBE_INDEX))
    if FIRST_PARTY_YOUTUBE_INDEX.exists():
        persisted = read_json(FIRST_PARTY_YOUTUBE_INDEX)
        enriched = _apply_reviewed_youtube_project_metadata(persisted)
        if enriched != persisted:
            atomic_write_json(FIRST_PARTY_YOUTUBE_INDEX, enriched)


def search_first_party_youtube_index(index: dict | Path, query: str, limit: int = 5) -> list[dict]:
    payload = load_first_party_youtube_index(index) if isinstance(index, Path) else index
    query_text = _normalized_search_text(query)
    query_tokens = set(query_text.split())
    query_repo = {item.casefold() for item in extract_repository_identities(query)}
    scored: list[tuple[int, dict]] = []
    for source in payload.get("documents", []):
        row = first_party_identity.normalize_content_identity(source)
        if not row["first_party"]:
            continue
        title = _normalized_search_text(row.get("title"))
        description = _normalized_search_text(row.get("description") or row.get("summary"))
        repositories = {str(item).casefold() for item in row.get("repository_identities", [])}
        outbound_urls = _normalized_search_text(*[
            link.get("canonical_url") or link.get("outbound_url")
            for link in row.get("outbound_links", []) if isinstance(link, dict)])
        score = 0
        matched: list[str] = []
        if query_repo and query_repo & repositories:
            score += 1000
            matched.append("repository_identity")
        if query_text and query_text in outbound_urls:
            score += 800
            matched.append("outbound_canonical_url")
        if query_text and query_text == title:
            score += 700
            matched.append("title_exact")
        elif query_text and query_text in title:
            score += 500
            matched.append("title")
        if query_text and query_text in description:
            score += 250
            matched.append("description")
        title_tokens = set(title.split())
        description_tokens = set(description.split())
        if query_tokens and query_tokens <= title_tokens:
            score += 200
            if "title" not in matched:
                matched.append("title_tokens")
        elif query_tokens and query_tokens <= description_tokens:
            score += 100
            if "description" not in matched:
                matched.append("description_tokens")
        if not score:
            continue
        row["retrieval_score"] = score + 10
        row["matched_fields"] = matched
        scored.append((row["retrieval_score"], enrich(row)))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("title") or "").casefold()))
    results = [dict(row, rank=rank, number=rank) for rank, (_score, row) in enumerate(scored[:limit], 1)]
    return results


class _WebsiteDocumentParser(HTMLParser):
    """Collect source-evidenced metadata, links, and visible text from a published page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.links: list[tuple[str, str]] = []
        self.visible: list[str] = []
        self.title_parts: list[str] = []
        self.json_ld_parts: list[str] = []
        self.tag_labels: list[str] = []
        self._hidden_depth = 0
        self._in_title = False
        self._in_json_ld = False
        self._in_tag_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        if lowered == "title":
            self._in_title = True
        if lowered == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._in_json_ld = True
        if lowered == "meta":
            key = (values.get("property") or values.get("name") or
                   values.get("itemprop") or "").casefold()
            content = values.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key, []).append(content)
        if lowered in {"a", "link"} and values.get("href"):
            rel = values.get("rel", "").casefold()
            self.links.append((values["href"].strip(), rel))
            self._in_tag_link = "tag" in rel.split()

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = False
        if lowered == "script":
            self._in_json_ld = False
        if lowered == "a":
            self._in_tag_link = False
        if lowered in {"script", "style", "noscript", "svg"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        if self._in_json_ld:
            self.json_ld_parts.append(data)
        if self._in_title:
            self.title_parts.append(value)
        if self._in_tag_link:
            self.tag_labels.append(value)
        if not self._hidden_depth:
            self.visible.append(value)


def _website_json_ld(parser: _WebsiteDocumentParser) -> list[dict]:
    values: list[dict] = []
    for raw in parser.json_ld_parts:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                values.extend(item for item in graph if isinstance(item, dict))
            values.append(candidate)
    return values


def _first_value(values: list[object]) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _page_absolute_url(page_url: str, candidate: str) -> str:
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(page_url, candidate.strip()))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, parsed.query, ""))


def parse_first_party_website_page(payload: bytes | str, page_url: str,
                                   website_url: str = "") -> dict:
    """Normalize one accepted first-party page without inferring absent content."""
    source = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    parser = _WebsiteDocumentParser()
    parser.feed(source)
    json_ld = _website_json_ld(parser)

    canonical_candidates = [href for href, rel in parser.links if "canonical" in rel.split()]
    canonical_page_url = _page_absolute_url(page_url, _first_value(canonical_candidates) or page_url)
    title = _first_value([
        *(parser.meta.get("og:title") or []),
        *[item.get("headline") for item in json_ld],
        *[item.get("name") for item in json_ld],
        " ".join(parser.title_parts),
    ]) or "UNKNOWN"
    summary = _first_value([
        *(parser.meta.get("description") or []),
        *(parser.meta.get("og:description") or []),
        *[item.get("description") for item in json_ld],
    ])
    publication_date = _first_value([
        *(parser.meta.get("article:published_time") or []),
        *[item.get("datePublished") for item in json_ld],
    ]) or "UNKNOWN"
    body_text = "\n".join(parser.visible)
    entry_match = re.search(r"\bENTRY_ID\s*:\s*([A-Za-z0-9_.-]+)", body_text, re.I)
    publication_identity = (entry_match.group(1) if entry_match else _first_value([
        *[item.get("identifier") for item in json_ld],
        *[item.get("@id") for item in json_ld], canonical_page_url,
    ]))

    keyword_values: list[str] = []
    for value in [*(parser.meta.get("keywords") or []),
                  *(parser.meta.get("article:tag") or []),
                  *[item.get("keywords") for item in json_ld]]:
        if isinstance(value, list):
            keyword_values.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, str):
            keyword_values.extend(item.strip() for item in value.split(",") if item.strip())
    keywords = list(dict.fromkeys(keyword_values))
    tags = list(dict.fromkeys([*parser.tag_labels, *(parser.meta.get("article:tag") or [])]))

    exact_links = list(dict.fromkeys(
        absolute for href, _rel in parser.links
        if (absolute := _page_absolute_url(canonical_page_url or page_url, href))))
    github_urls = [url for url in exact_links
                   if (urllib.parse.urlsplit(url).hostname or "").casefold() in
                   {"github.com", "www.github.com"}]
    repository_identities = extract_repository_identities("", github_urls)
    normalized_repositories = [identity.casefold() for identity in repository_identities]
    source_root = website_url or f"{urllib.parse.urlsplit(canonical_page_url).scheme}://{urllib.parse.urlsplit(canonical_page_url).netloc}"
    result_identity = f"website:{hashlib.sha256(canonical_page_url.encode('utf-8')).hexdigest()[:24]}"
    searchable = _normalized_search_text(
        title, summary, body_text, *github_urls, *repository_identities, *tags, *keywords)
    return {
        "document_id": result_identity,
        "result_identity": result_identity,
        "publication_identity": publication_identity or canonical_page_url,
        "title": title,
        "url": canonical_page_url,
        "canonical_page_url": canonical_page_url,
        "date": publication_date,
        "publication_date": publication_date,
        "summary": summary,
        "description": body_text,
        "body_text": body_text,
        "github_urls": github_urls,
        "canonical_github_urls": [canonical_url(url) for url in github_urls],
        "external_urls": exact_links,
        "project_links": github_urls,
        "repository_identities": repository_identities,
        "normalized_repository_identities": normalized_repositories,
        "tags": tags,
        "keywords": keywords,
        "normalized_search_text": searchable,
        "source_type": "First-party Website",
        "content_type": "published_page",
        "content_trust": "",
        "first_party": False,
        "first_party_reference_url": canonical_page_url,
        "project_url": github_urls[0] if github_urls else "",
        "discovered_by": f"Configured first-party website: {source_root}",
        "first_party_provenance": {
            "title": "meta/JSON-LD/title",
            "canonical_page_url": "link[rel=canonical] or configured page URL",
            "body_text": "visible page text",
            "github_urls": "a[href]",
            "publication_date": "article:published_time or JSON-LD datePublished",
            "publication_identity": "visible ENTRY_ID, JSON-LD identifier, or canonical URL",
        },
        "provenance": {"provider": "first_party_website", "website_url": source_root,
                       "page_url": canonical_page_url},
    }


def _website_source_entries(source_urls: list[str] | tuple[str, ...], fetcher,
                            website_url: str) -> tuple[list[dict], list[dict]]:
    """Resolve same-site sitemap sources with deterministic discovery metadata."""
    allowed_hosts = {
        (urllib.parse.urlsplit(url).hostname or "").casefold()
        for url in [website_url, *source_urls] if url
    }
    entries: dict[str, dict] = {}
    failures: list[dict] = []
    pending = list(source_urls)
    visited_sources: set[str] = set()
    while pending:
        source_url = pending.pop(0)
        if source_url in visited_sources:
            continue
        visited_sources.add(source_url)
        if not urllib.parse.urlsplit(source_url).path.casefold().endswith(".xml"):
            entries[source_url] = {
                "url": source_url, "lastmod": "", "source_sitemap": "",
            }
            continue
        try:
            root = ET.fromstring(fetcher(source_url, "application/xml", timeout=20))
        except Exception as exc:
            failures.append({"url": source_url, "error": f"{type(exc).__name__}: {exc}"})
            continue
        root_type = root.tag.rsplit("}", 1)[-1].casefold()
        if root_type == "sitemapindex":
            locations = [
                str(node.text or "").strip()
                for node in root.findall(".//{*}loc") if str(node.text or "").strip()
            ]
            accepted = [url for url in locations
                        if (urllib.parse.urlsplit(url).hostname or "").casefold() in allowed_hosts]
            pending.extend(accepted)
        elif root_type == "urlset":
            for url_node in root.findall(".//{*}url"):
                location_node = url_node.find("{*}loc")
                location = str(location_node.text or "").strip() if location_node is not None else ""
                if not location or (urllib.parse.urlsplit(location).hostname or "").casefold() not in allowed_hosts:
                    continue
                lastmod_node = url_node.find("{*}lastmod")
                lastmod = str(lastmod_node.text or "").strip() if lastmod_node is not None else ""
                entries[location] = {
                    "url": location, "lastmod": lastmod,
                    "source_sitemap": source_url,
                }
        else:
            failures.append({"url": source_url, "error": f"unsupported sitemap root: {root_type}"})
    return list(entries.values()), failures


def _website_source_pages(source_urls: list[str] | tuple[str, ...], fetcher,
                          website_url: str) -> tuple[list[str], list[dict]]:
    """Backward-compatible URL-only sitemap resolver."""
    entries, failures = _website_source_entries(source_urls, fetcher, website_url)
    return [entry["url"] for entry in entries], failures


def _fetch_website_documents(entries: list[dict], fetcher, website_url: str) -> tuple[list[dict], list[dict]]:
    documents: list[dict] = []
    failures: list[dict] = []
    seen: set[str] = set()
    workers = min(8, max(1, len(entries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_entries = {
            pool.submit(fetcher, entry["url"], "text/html", timeout=20): entry
            for entry in entries
        }
        for future in as_completed(future_entries):
            entry = future_entries[future]
            try:
                document = parse_first_party_website_page(
                    future.result(), entry["url"], website_url)
                document.update({
                    "discovered_url": entry["url"],
                    "sitemap_lastmod": entry.get("lastmod") or "",
                    "source_sitemap": entry.get("source_sitemap") or "",
                })
                document = _with_fingerprint(document)
            except Exception as exc:
                failures.append({"url": entry["url"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            identity = str(document.get("result_identity") or "")
            if identity and identity not in seen:
                seen.add(identity)
                documents.append(document)
    documents.sort(key=lambda item: str(item.get("canonical_page_url") or ""))
    failures.sort(key=lambda item: str(item.get("url") or ""))
    return documents, failures


def build_first_party_website_index(source_urls: list[str] | tuple[str, ...],
                                    index_path: Path = FIRST_PARTY_WEBSITE_INDEX,
                                    fetcher=fetch, website_url: str = "") -> dict:
    previous = load_first_party_website_index(index_path)
    try:
        entries, failures = _website_source_entries(source_urls, fetcher, website_url)
        if failures:
            raise RuntimeError("sitemap discovery failed: " + json.dumps(
                failures, ensure_ascii=False, sort_keys=True))
        if not entries:
            raise RuntimeError("sitemap discovery returned no pages")
        documents, page_failures = _fetch_website_documents(entries, fetcher, website_url)
        if page_failures:
            raise RuntimeError("page ingestion failed: " + json.dumps(
                page_failures, ensure_ascii=False, sort_keys=True))
        if not documents:
            raise RuntimeError("website ingestion returned no documents")
        statistics = {
            "previous_document_count": len(previous.get("documents", [])),
            "current_sitemap_count": len(entries),
            "new": len(documents), "changed": 0, "removed": 0, "unchanged": 0,
            "final_persisted_count": len(documents),
        }
        index = {"schema_version": 3, "website_url": website_url,
                 "source_urls": list(source_urls), "page_count": len(entries),
                 "ingested_at": now(), "last_refresh": now(), "failures": [],
                 "refresh": {"status": "completed", **statistics}, "documents": documents}
        atomic_write_json(index_path, index)
        _record_refresh_status(index_path, "completed", statistics=statistics)
        return index
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _record_refresh_status(index_path, "failed", error=error,
                               retained_document_count=len(previous.get("documents", [])))
        return dict(previous, refresh_result={"status": "failed", "error": error,
                                              "retained_document_count": len(previous.get("documents", []))})


def refresh_first_party_website_index(source_urls: list[str] | tuple[str, ...],
                                      index_path: Path = FIRST_PARTY_WEBSITE_INDEX,
                                      fetcher=fetch, website_url: str = "") -> dict:
    """Incrementally refresh a sitemap-backed index as one atomic transaction."""
    previous = load_first_party_website_index(index_path)
    old_documents = previous.get("documents", [])
    old_by_url = {
        str(item.get("discovered_url") or item.get("canonical_page_url") or item.get("url") or ""): item
        for item in old_documents
        if item.get("discovered_url") or item.get("canonical_page_url") or item.get("url")
    }
    try:
        entries, discovery_failures = _website_source_entries(source_urls, fetcher, website_url)
        if discovery_failures:
            raise RuntimeError("sitemap discovery failed: " + json.dumps(
                discovery_failures, ensure_ascii=False, sort_keys=True))
        if not entries:
            raise RuntimeError("sitemap discovery returned no pages")
        current_by_url = {entry["url"]: entry for entry in entries}
        removed_urls = old_by_url.keys() - current_by_url.keys()
        fetch_entries: list[dict] = []
        reused: list[dict] = []
        for url, entry in current_by_url.items():
            old = old_by_url.get(url)
            current_lastmod = str(entry.get("lastmod") or "")
            old_lastmod = str((old or {}).get("sitemap_lastmod") or "")
            if old is None or not current_lastmod or not old_lastmod or current_lastmod != old_lastmod:
                fetch_entries.append(entry)
            else:
                preserved = dict(old)
                preserved["source_sitemap"] = entry.get("source_sitemap") or ""
                reused.append(preserved)
        fetched, page_failures = _fetch_website_documents(fetch_entries, fetcher, website_url)
        if page_failures:
            raise RuntimeError("page refresh failed: " + json.dumps(
                page_failures, ensure_ascii=False, sort_keys=True))
        fetched_by_url = {str(item.get("discovered_url") or ""): item for item in fetched}
        new_urls = current_by_url.keys() - old_by_url.keys()
        changed_urls = {
            url for url in current_by_url.keys() & old_by_url.keys()
            if url in fetched_by_url and
               _document_fingerprint(old_by_url[url]) != _document_fingerprint(fetched_by_url[url])
        }
        refetched_unchanged = {
            url for url in current_by_url.keys() & old_by_url.keys()
            if url in fetched_by_url and url not in changed_urls
        }
        final_documents = sorted([*reused, *fetched], key=lambda item: str(
            item.get("canonical_page_url") or ""))
        if not final_documents:
            raise RuntimeError("website refresh returned no documents")
        unchanged_count = len(reused) + len(refetched_unchanged)
        statistics = {
            "previous_document_count": len(old_documents),
            "current_sitemap_count": len(entries),
            "new": len(new_urls), "changed": len(changed_urls),
            "removed": len(removed_urls), "unchanged": unchanged_count,
            "final_persisted_count": len(final_documents),
        }
        candidate = {
            "schema_version": 3, "website_url": website_url,
            "source_urls": list(source_urls), "page_count": len(entries),
            "ingested_at": previous.get("ingested_at") or now(),
            "last_refresh": now(), "failures": [],
            "refresh": {"status": "completed", **statistics,
                        "removed_documents": sorted(removed_urls)},
            "documents": final_documents,
        }
        atomic_write_json(index_path, candidate)
        _record_refresh_status(index_path, "completed", statistics=statistics)
        return candidate
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _record_refresh_status(index_path, "failed", error=error,
                               retained_document_count=len(old_documents))
        result = dict(previous)
        result["refresh_result"] = {
            "status": "failed", "error": error,
            "retained_document_count": len(old_documents),
        }
        return result


def load_first_party_website_index(index_path: Path = FIRST_PARTY_WEBSITE_INDEX) -> dict:
    return read_json(index_path) if index_path.exists() else {"schema_version": 1, "documents": []}


def search_first_party_website_index(index: dict | Path, query: str, limit: int = 5) -> list[dict]:
    payload = load_first_party_website_index(index) if isinstance(index, Path) else index
    query_text = _normalized_search_text(query)
    query_words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", query_text)
    query_tokens = set(query_words)
    query_repositories = {item.casefold() for item in extract_repository_identities(query)}
    scored: list[tuple[int, dict]] = []
    for source in payload.get("documents", []):
        row = dict(source)
        title = _normalized_search_text(row.get("title"))
        title_words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", title)
        project_title = _normalized_search_text(
            re.split(r"\s*[:|–—]\s*", str(row.get("title") or ""), maxsplit=1)[0])
        project_words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", project_title)
        body = _normalized_search_text(row.get("body_text") or row.get("description"))
        metadata = _normalized_search_text(*row.get("tags", []), *row.get("keywords", []))
        repository_urls = _normalized_search_text(*row.get("github_urls", []),
                                                  *row.get("canonical_github_urls", []))
        repositories = {str(item).casefold() for item in row.get("repository_identities", [])}
        score = 0
        matched: list[str] = []
        if query_repositories and query_repositories & repositories:
            score += 1000
            matched.append("repository_identity")
        if query_text and query_text in repository_urls:
            score += 800
            matched.append("github_url")
        if query_text and query_text == title:
            score += 700
            matched.append("title_exact")
        elif query_text and query_text in title:
            score += 500
            matched.append("title")
        elif (project_words and len(project_words) >= 2 and
              any(query_words[index:index + len(project_words)] == project_words
                  for index in range(len(query_words) - len(project_words) + 1))):
            score += 400
            matched.append("title_phrase")
        if query_text and query_text in body:
            score += 250
            matched.append("body_text")
        if query_text and query_text in metadata:
            score += 225
            matched.append("tags_keywords")
        title_tokens = set(title_words)
        body_tokens = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", body))
        if query_tokens and query_tokens <= title_tokens:
            score += 200
            if "title" not in matched:
                matched.append("title_tokens")
        elif query_tokens and query_tokens <= body_tokens:
            score += 100
            if "body_text" not in matched:
                matched.append("body_tokens")
        if not score:
            continue
        row["retrieval_score"] = score + 10
        row["matched_fields"] = matched
        scored.append((row["retrieval_score"], enrich(row)))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("title") or "").casefold()))
    return [dict(row, rank=rank, number=rank)
            for rank, (_score, row) in enumerate(scored[:limit], 1)]


def youtube_search(query: str, limit: int) -> list[dict]:
    params = urllib.parse.urlencode({"search_query": query})
    payload = fetch(f"https://www.youtube.com/results?{params}", "text/html", timeout=20)
    return parse_youtube_search_html(payload, query, limit)


def youtube_channel_search(channel_url: str, query: str, limit: int) -> list[dict]:
    """Search a configured YouTube channel while retaining project-link provenance."""
    base = channel_url.rstrip("/")
    params = urllib.parse.urlencode({"query": query})
    payload = fetch(f"{base}/search?{params}", "text/html", timeout=20)
    rows = parse_youtube_search_html(payload, query, limit)
    for row in rows:
        row["channel_url"] = channel_url
        row.update(first_party_identity.normalize_content_identity(row))
        row["tutorial_url"] = str(row.get("video_url") or "")
        row["first_party_reference_url"] = str(
            row.get("first_party_reference_url") or row.get("video_url") or "")
        links = list(row.get("project_links") or [])
        row["project_url"] = str(links[0] if links else "")
        row["discovered_by"] = f"Configured first-party channel: {channel_url}"
    return rows


def enrich(item: dict) -> dict:
    item = first_party_identity.normalize_content_identity(item)
    url = item["url"]
    owner, repo = github_identity(url)
    if item.get("source_type") == "First-party Website":
        kind = "first_party_website"
    else:
        kind = "author_video" if item.get("source_type") == "YouTube" else classify(url)
    post_identity = (f"youtube:channel_post:{item['post_id']}"
                     if item.get("source_type") == "YouTube Post" and item.get("post_id") else "")
    video_identity = (f"youtube:video:{item['video_id']}" if item.get("video_id") else "")
    model_identity = (_huggingface_model_identity(str(item.get("model_id") or item.get("title") or ""))
                      if kind == "huggingface" else "")
    result_identity = str(item.get("result_identity") or model_identity or video_identity or post_identity or
                          canonical_url(str(item.get("video_url") or url)))
    is_website = item.get("source_type") == "First-party Website"
    return {
        **item,
        "type": kind,
        "document_type": kind,
        "content_trust": item.get("content_trust", ""),
        "authority": ("FIRST_PARTY" if item.get("first_party") else "UNKNOWN"),
        "authority_evidence": (str(item.get("authority_evidence") or item.get("discovered_by") or "")
                               if not is_website and (item.get("first_party") or
                               item.get("authority") == "FIRST_PARTY") else ""),
        "author": owner or "UNKNOWN",
        "repository": f"{owner}/{repo}" if owner else "UNKNOWN",
        "readme": "UNKNOWN",
        "artifacts": "UNKNOWN",
        "workflow": "UNKNOWN",
        "release": "UNKNOWN",
        "issues": "UNKNOWN",
        "hardware_requirements": "UNKNOWN",
        "software_versions": "UNKNOWN",
        "source_priority": SOURCE_PRIORITY[kind],
        "result_identity": result_identity,
        "group_id": (canonical_url(str(item.get("video_url") or url))
                     if item.get("source_type") == "YouTube" else
                     f"github:{owner.lower()}/{repo.lower()}" if owner else
                     model_identity if kind == "huggingface" else
                     canonical_url(url) or post_identity),
    }


def deduplicate(items: list[dict]) -> list[dict]:
    def merge_provenance(target: dict, incoming: dict) -> None:
        records: list[dict] = []
        for candidate in (target, incoming):
            explicit = candidate.get("search_provenance")
            if isinstance(explicit, list):
                records.extend(record for record in explicit if isinstance(record, dict))
            elif isinstance(candidate.get("provenance"), dict):
                records.append(candidate["provenance"])
        merged: list[dict] = []
        seen: set[str] = set()
        for record in records:
            marker = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                merged.append(dict(record))
        if merged:
            target["search_provenance"] = merged

    unique: dict[str, dict] = {}
    for item in items:
        key = (str(item.get("result_identity") or "") or
               canonical_url(str(item.get("video_url") or item["url"])) or
               (f"youtube-post:{item['post_id']}"
                if item.get("source_type") == "YouTube Post" and item.get("post_id") else ""))
        key = unicodedata.normalize("NFKC", key).strip()
        if key.casefold().startswith(("github:", "huggingface:model:")):
            key = key.casefold()
        if key and key not in unique:
            unique[key] = enrich(item)
            merge_provenance(unique[key], item)
        elif key:
            merge_provenance(unique[key], item)
    ordered = sorted(unique.values(), key=lambda x: -x["source_priority"])
    for index, item in enumerate(ordered, 1):
        item["number"] = index
    return ordered


def print_candidates(task: dict) -> None:
    for item in task["candidates"]:
        print(f'{item["number"]}. {item["title"]}')
        print(f'   URL: {item["url"]}')
        print(f'   Type: {item["type"]} | Authority: {item["authority"]} | Author/Repo: {item["author"]} / {item["repository"]}')
        print(f'   Date: {item["date"]} | Group: {item["group_id"]}')
        print(f'   README: {item["readme"]} | Artifacts: {item["artifacts"]} | Workflow: {item["workflow"]}')
        print(f'   Release: {item["release"]} | Issues: {item["issues"]}')
        print(f'   Hardware: {item["hardware_requirements"]} | Versions: {item["software_versions"]}')
        print(f'   Summary: {item["summary"][:320]}')
    print("\nWAITING FOR USER SELECTION")


def cmd_scout(args: argparse.Namespace) -> None:
    ensure_dirs()
    task_id = args.task_id or f"scout-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    queries_file = getattr(args, "queries_file", None)
    if queries_file is None:
        queries = generate_queries(args.goal)
        query_source = "deterministic"
    else:
        queries = load_external_queries(queries_file)
        query_source = "external_local_ornith"
    raw, errors = [], []
    try:
        raw.extend(github_repository_search(args.goal, args.per_query))
    except Exception as exc:
        errors.append({"query": f"GitHub API: {github_search_terms(args.goal)}", "error": f"{type(exc).__name__}: {exc}"})
    for query in queries:
        try:
            raw.extend(bing_rss(query, args.per_query))
        except Exception as exc:
            errors.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
    task = {
        "schema_version": 1,
        "task_id": task_id,
        "state": "WAITING_FOR_USER_SELECTION",
        "goal": args.goal,
        "queries": queries,
        "query_source": query_source,
        "candidates": deduplicate(raw),
        "selected": [],
        "rejected": [],
        "search_errors": errors,
        "outcome": "UNKNOWN",
        "success": False,
        "created_at": now(),
        "updated_at": now(),
        "history": [{"at": now(), "event": "SCOUT_COMPLETE"}],
    }
    save_task(task)
    product_cli.sync_scout_job(JOBS, task)
    write_json(TASKS / task_id / "scout_results.json", task["candidates"])
    print(f"Task: {task_id}\nGoal: {args.goal}\n")
    print_candidates(task)


def parse_choices(text: str) -> list[int]:
    try:
        return sorted(set(int(x.strip()) for x in text.split(",") if x.strip()))
    except ValueError as exc:
        raise SystemExit("Choices must be comma-separated result numbers.") from exc


def cmd_select(args: argparse.Namespace) -> None:
    task = load_task(args.task_id)
    if task["state"] != "WAITING_FOR_USER_SELECTION":
        raise SystemExit(f'Selection blocked in state: {task["state"]}')
    choices = parse_choices(args.choices)
    valid = {x["number"] for x in task["candidates"]}
    if not choices or not set(choices) <= valid:
        raise SystemExit("Selection contains missing result numbers.")
    task["selected"] = [x for x in task["candidates"] if x["number"] in choices]
    task["rejected"] = [x["number"] for x in task["candidates"] if x["number"] not in choices]
    task["state"] = "SELECTED"
    task["history"].append({"at": now(), "event": "HUMAN_SELECTION", "choices": choices})
    save_task(task)
    product_cli.record_selection(JOBS, task)
    print("Selection recorded. SOURCE READER is now permitted; no source has been fetched yet.")


def _reusable_repository_snapshot(repository: str, revision: str, current_job: str) -> tuple[bytes, str] | None:
    """Reuse only a pinned repository body whose persisted digest still matches."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        return None
    for path in SNAPSHOTS.glob("*/snapshot_manifest.json"):
        if path.parent.name == current_job:
            continue
        try:
            rows = read_json(path)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                source = row.get("install_source") or {}
                candidate_url = source.get("canonical_url") or row.get("url") or ""
                requested = exact_github_repository_request(candidate_url)
                pinned_url = f"https://github.com/{repository}/tree/{revision}"
                if (not requested or
                        f"{requested[0]}/{requested[1]}".casefold() != repository.casefold() or
                        str(row.get("repository") or "").casefold() != repository.casefold() or
                        str(row.get("commit") or "").casefold() != revision.casefold() or
                        str(source.get("exact_revision") or "").casefold() != revision.casefold() or
                        str(row.get("snapshot_url") or "").casefold() != pinned_url.casefold()):
                    continue
                filename = str(row.get("readme") or "")
                digest = str(row.get("sha256") or "")
                if (not filename or Path(filename).name != filename or
                        not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
                    continue
                body_path = path.parent / filename
                if not body_path.is_file():
                    continue
                body = body_path.read_bytes()
                if hashlib.sha256(body).hexdigest().casefold() == digest.casefold():
                    return body, path.parent.name
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return None


def snapshot_selected(task: dict) -> list[dict]:
    target = SNAPSHOTS / task["task_id"]
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for item in task["selected"]:
        discovery_url = item["url"]
        selected_reference = item.get("selected_reference") or item.get("reference_source")
        install_source = dict(item.get("install_source") or {})
        url = str(install_source.get("canonical_url") or install_source.get("url") or discovery_url)
        requested = exact_github_repository_request(url)
        github_source = str(install_source.get("source_type") or "").casefold() in {
            "github_repository", "official_repository"} or bool(requested)
        if github_source and install_source.get("repository_identity"):
            expected = str(install_source["repository_identity"])
            if not requested or f"{requested[0]}/{requested[1]}".casefold() != expected.casefold():
                raise SystemExit("Source Reader blocked: canonical install source conflicts with repository identity.")
        owner, repo = github_identity(url)
        revision = (install_source.get("exact_revision") or install_source.get("commit") or
                    item.get("exact_revision") or item.get("commit") or item.get("revision") or item.get("tag")
                    if install_source else
                    item.get("exact_revision") or item.get("commit") or item.get("revision") or item.get("tag"))
        revision_error = None
        if owner and (revision in (None, "", "UNKNOWN") or
                      not re.fullmatch(r"[0-9a-fA-F]{40}", str(revision))):
            try:
                ref = "HEAD" if revision in (None, "", "UNKNOWN") else str(revision)
                data = json.loads(fetch(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/"
                    f"{urllib.parse.quote(ref, safe='')}",
                    "application/vnd.github+json").decode("utf-8"))
                sha = str(data.get("sha") or "")
                if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                    raise ValueError("GitHub did not return an immutable commit SHA")
                revision = sha
            except Exception as exc:
                revision = "UNKNOWN"
                revision_error = f"{type(exc).__name__}: {exc}"
        body, error, reused_from = b"", None, None
        snapshot_url = f"https://github.com/{owner}/{repo}/tree/{revision}" if owner and revision != "UNKNOWN" else url
        if owner and revision not in (None, "", "UNKNOWN"):
            reused = _reusable_repository_snapshot(f"{owner}/{repo}", str(revision), task["task_id"])
            if reused:
                body, reused_from = reused
        if not body and (not owner or revision != "UNKNOWN"):
            try:
                body = fetch(snapshot_url)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        name = f'{item["number"]:03d}-{hashlib.sha256(url.encode()).hexdigest()[:10]}.html'
        if body:
            (target / name).write_bytes(body)
        install_source.update({
            "url": url,
            "canonical_url": canonical_url(url),
            "repository_identity": f"{owner}/{repo}" if owner else install_source.get("repository_identity", "UNKNOWN"),
            "exact_revision": revision or "UNKNOWN",
            "source_type": install_source.get("source_type") or item.get("source_type"),
        })
        record = {
            "url": url, "title": item["title"], "repository": f"{owner}/{repo}" if owner else "UNKNOWN",
            "commit": revision or "UNKNOWN", "tag": item.get("tag") or "UNKNOWN", "updated_at": item["date"],
            "readme": name if body else "UNKNOWN", "requirements": "UNKNOWN", "workflow": "UNKNOWN",
            "model_downloads": "UNKNOWN", "issues": "UNKNOWN", "troubleshooting": "UNKNOWN",
            "sha256": hashlib.sha256(body).hexdigest() if body else "UNKNOWN", "fetch_error": error,
            "revision_error": revision_error, "source_type": install_source.get("source_type"),
            "verified_original_project_url": url,
            "snapshot_url": snapshot_url if body else None,
            "reused_from_job_id": reused_from,
            "discovery_url": discovery_url,
            "result_identity": item.get("result_identity"), "provenance": item.get("provenance"),
            "selected_reference": selected_reference,
            "reference_source": selected_reference,
            "install_source": install_source,
        }
        records.append(record)
    write_json(target / "snapshot_manifest.json", records)
    return records


def cmd_read(args: argparse.Namespace) -> None:
    task = load_task(args.task_id)
    if task["state"] != "SELECTED" or not task["selected"]:
        raise SystemExit("SOURCE READER blocked: explicit human selection is required.")
    records = snapshot_selected(task)
    task["state"] = "SOURCES_READ"
    task["source_snapshots"] = records
    task["history"].append({"at": now(), "event": "SOURCE_READER_COMPLETE"})
    save_task(task)
    print("Selected originals were snapshotted. UNKNOWN fields were not guessed.")


def _load_optional_json(path: Path | None) -> dict:
    return read_json(path) if path else {}


def cmd_preflight(args: argparse.Namespace) -> None:
    result = product_cli.run_preflight(JOBS, args.task_id, _load_optional_json(args.spec))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_plan(args: argparse.Namespace) -> None:
    task = load_task(args.task_id)
    value = product_cli.create_plan(JOBS, args.task_id, task, _load_optional_json(args.request))
    print(f"Plan saved. FINAL PLAN STATUS: {value['status']}")


def cmd_show_plan(args: argparse.Namespace) -> None:
    store = product_cli.JobStore(JOBS, args.task_id)
    value = store.load("plan.json")
    if not value:
        raise SystemExit("Plan not found.")
    print(product_cli.format_plan(value))


def cmd_approve(args: argparse.Namespace) -> None:
    if getattr(args, "phrase", None) not in (None, "執行"):
        raise SystemExit("Approval phrase, when supplied, must be exactly: 執行")
    record = product_cli.approve_plan(JOBS, args.task_id, args.steps.split(","), args.scope)
    print(f"Plan approved: {record['plan_hash']}")


def cmd_execute(args: argparse.Namespace) -> None:
    state = product_cli.execute(JOBS, args.task_id)
    stopped = next((x for x in state.get("steps", []) if x.get("status") == "WAITING_FOR_USER"), None)
    if stopped:
        message = stopped.get("inputs", {}).get("message", stopped.get("expected_result", "Complete the manual action."))
        print(f"USER ACTION REQUIRED:\n{message}\n\nAfter completion run:\npython solution_scout.py resume --task-id {args.task_id} --approve-deviation {stopped['step_id']}")
    else:
        print(product_cli.status(JOBS, args.task_id))


def cmd_status(args: argparse.Namespace) -> None:
    print(product_cli.status(JOBS, args.task_id))


def cmd_resume(args: argparse.Namespace) -> None:
    result = product_cli.resume_job(JOBS, args.task_id, args.approve_deviation)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_close(args: argparse.Namespace) -> None:
    print(json.dumps(product_cli.close_job(JOBS, args.task_id, args.incomplete), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scout = sub.add_parser("scout")
    scout.add_argument("--goal", required=True)
    scout.add_argument("--queries-file", type=Path)
    scout.add_argument("--task-id")
    scout.add_argument("--per-query", type=int, default=5)
    scout.set_defaults(func=cmd_scout)
    select = sub.add_parser("select")
    select.add_argument("--task-id", required=True)
    select.add_argument("--choices", required=True)
    select.set_defaults(func=cmd_select)
    reader = sub.add_parser("read")
    reader.add_argument("--task-id", required=True)
    reader.set_defaults(func=cmd_read)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--task-id", required=True)
    preflight.add_argument("--spec", type=Path)
    preflight.set_defaults(func=cmd_preflight)
    plan = sub.add_parser("plan")
    plan.add_argument("--task-id", required=True)
    plan.add_argument("--request", type=Path)
    plan.set_defaults(func=cmd_plan)
    show_plan = sub.add_parser("show-plan")
    show_plan.add_argument("--task-id", required=True)
    show_plan.set_defaults(func=cmd_show_plan)
    approve = sub.add_parser("approve")
    approve.add_argument("--task-id", required=True)
    approve.add_argument("--steps", required=True, help="Comma-separated step IDs")
    approve.add_argument("--scope", default="listed steps only")
    approve.add_argument("--phrase")
    approve.set_defaults(func=cmd_approve)
    execute = sub.add_parser("execute")
    execute.add_argument("--task-id", required=True)
    execute.set_defaults(func=cmd_execute)
    status = sub.add_parser("status")
    status.add_argument("--task-id", required=True)
    status.set_defaults(func=cmd_status)
    resume = sub.add_parser("resume")
    resume.add_argument("--task-id", required=True)
    resume.add_argument("--approve-deviation")
    resume.set_defaults(func=cmd_resume)
    close = sub.add_parser("close")
    close.add_argument("--task-id", required=True)
    close.add_argument("--incomplete", action="store_true")
    close.set_defaults(func=cmd_close)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
