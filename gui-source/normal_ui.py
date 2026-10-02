"""Simple normal-user presentation over the unchanged Product Core controller."""
from __future__ import annotations

import json
import html
import hashlib
import inspect
import os
import platform
import re
import shutil
import sys
import textwrap
import threading
import webbrowser
import urllib.error
from html.parser import HTMLParser
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import NamedTuple
from typing import Any
from urllib.parse import quote, urlsplit

import product_cli
import query_planner
import solution_scout
import curated_catalog
import product_config
import localization
import handoff_debug
import build_identity
import win11_theme
import first_party_identity
import install_research
import installation_plan
import external_asset_execution
from runtime_paths import runtime_path
from job_context import (
    JobContext,
    JobContextValidationError,
    JobContextValidationFailure,
    JobLifecycleState,
)


AI_RECOMMENDATION = (
    "Copy the problem report below and paste it into ChatGPT, Claude Code, Grok, or Gemini "
    "to ask for a repair method. Based on experience, do not rely on the local computer to "
    "invent the repair."
)
REPORT_END = (
    "Please diagnose this exact failure and propose the smallest verified repair step. "
    "Do not redesign the installation."
)


INTRO_FALLBACK = "Original source does not provide a usable introduction."
HF_CARD_READ_SUCCESS = "READ_SUCCESS"
HF_CARD_NO_CONTENT = "NO_CONTENT"
HF_CARD_FETCH_FAILED = "FETCH_FAILED"
PLAIN_FALLBACK = "Purpose is not clear enough from the original description."
ZERO_RESULT_MESSAGE = "No proven solution found."
SEARCH_FAILED_MESSAGE = "Some search requests failed; no matching results are available."
OUTSIDE_SCOPE_MESSAGE = "This request is outside the current supported local-AI solution categories."
SEARCH_HINTS = (
    "Film / Video / Image", "Voice / Audio / Subtitles", "OCR / PDF / Documents",
    "Private Knowledge Base / RAG", "Office Automation / Agents",
    "Local Models / AI Tools", "3D / Blender / Previs",
)
SEARCH_HINT_KEYS = dict(zip(SEARCH_HINTS, (
    "category.film", "category.voice", "category.documents", "category.rag",
    "category.office", "category.models", "category.3d",
)))

_DOMAIN_TERMS = {
    "film": {"film", "video", "image", "comfyui", "minimax", "h3", "图像", "影片", "视频", "影像"},
    "voice": {"voice", "audio", "subtitle", "subtitles", "speech", "tts", "asr", "whisper", "声音", "語音", "语音", "字幕", "音频"},
    "documents": {"ocr", "pdf", "document", "documents", "scan", "scanned", "文档", "文件", "辨識", "识别"},
    "knowledge": {"rag", "knowledge", "retrieval", "embedding", "vector", "知识库", "知識庫", "資料庫", "数据库"},
    "office": {"office", "automation", "agent", "agents", "excel", "word", "powerpoint", "workflow", "办公", "辦公", "自动化", "自動化"},
    "models": {"model", "models", "ollama", "llm", "inference", "local ai", "ai tool", "ai tools", "模型", "本地ai", "本地 ai"},
    "3d": {"3d", "blender", "previs", "render", "mesh", "animation", "建模", "预演", "預演"},
}
_TASK_TERMS = {
    "face": {"face", "swap", "replacement", "replace", "refine", "identity", "portrait", "facial", "换脸", "換臉", "换人脸", "換人臉", "人脸", "人臉"},
    "create": {"create", "generate", "generation", "edit", "editing", "animate", "render", "生成", "制作", "製作", "剪辑", "剪輯"},
    "transcribe": {"transcribe", "transcription", "subtitle", "subtitles", "speech", "recognition", "字幕", "转录", "轉錄", "识别", "辨識"},
    "extract": {"ocr", "extract", "parse", "convert", "read", "scan", "提取", "转换", "轉換", "读取", "讀取"},
    "retrieve": {"search", "retrieve", "retrieval", "question", "answer", "chat", "查询", "查詢", "检索", "檢索", "问答", "問答"},
    "automate": {"automate", "automation", "agent", "workflow", "schedule", "自动化", "自動化", "代理", "流程"},
    "run": {"run", "serve", "host", "install", "deploy", "local", "运行", "運行", "部署", "安装", "安裝"},
    "model3d": {"modeling", "model", "mesh", "render", "animate", "previs", "建模", "渲染", "动画", "動畫", "预演", "預演"},
}
_HINT_KEYS = {hint: (key,) for hint, key in zip(SEARCH_HINTS, ("film", "voice", "documents", "knowledge", "office", "models", "3d"))}
_OUTSIDE_TERMS = {"recipe", "restaurant", "hotel", "flight", "weather", "stock price", "shopping", "sports score", "食谱", "食譜", "天气", "天氣", "餐厅", "餐廳"}


MAX_SECOND_STAGE_CANDIDATES = 5

_DIRECT_ENRICHMENT_TERMS = {
    "face replacement": ("face replacement", "face swap", "face/head swap", "face refinement",
                         "identity replacement", "replace face", "顔交換", "顔入れ替え", "换脸", "換臉"),
    "background replacement": ("background replacement", "replace background", "background removal", "remove background"),
    "camera control": ("camera", "camera transform", "set camera", "camera position", "camera rotation",
                       "scene camera", "camera operations", "カメラ操作", "控制攝影機", "控制摄影机"),
    "voice cloning": ("voice cloning", "voice clone", "clone voice", "speaker cloning"),
    "retrieval-augmented generation": ("RAG", "retrieval augmented generation", "retrieval-augmented generation"),
    "acceleration": ("acceleration", "speedup", "speed up", "faster inference", "performance optimization"),
    "subtitle transcription / translation": ("subtitle translation", "translate subtitles", "transcription and translation"),
}
_IMPLIED_CAPABILITY_TERMS = (
    "arbitrary operations", "arbitrary blender", "execute python", "python execution",
    "any blender command", "automation tool", "general-purpose agent",
)
_YOUTUBE_ROUTE_TASKS = {
    "face replacement", "background replacement", "camera control", "acceleration", "workflow",
    "subtitle transcription / translation",
}
_YOUTUBE_ROUTE_TERMS = (
    "workflow", "tutorial", "how to", "setup", "install", "installation",
    "安裝", "安装", "實測", "实测", "配置", "教程", "教學", "教学", "加速", "換人臉", "换人脸",
)


def _matches(text: str, terms: set[str]) -> list[str]:
    lowered = text.casefold()
    return sorted(term for term in terms if term.casefold() in lowered)


def build_search_context(original_query: str, selected_hints: list[str] | None = None) -> dict[str, Any]:
    """Create a conservative, inspectable Normal User search context."""
    original = str(original_query)
    if not original.strip():
        raise ValueError("Enter a goal before searching.")
    hints = list(dict.fromkeys(selected_hints or []))
    if len(hints) > 2:
        raise ValueError("Choose no more than two search hints.")
    if any(hint not in SEARCH_HINTS for hint in hints):
        raise ValueError("Unknown search hint.")
    lowered = original.casefold()
    outside = not hints and any(term in lowered for term in _OUTSIDE_TERMS)
    return {**query_planner.deterministic_intent(original, hints),
            "outside_supported_scope": outside}


def _candidate_text(row: dict[str, Any]) -> str:
    fields = ("title", "name", "description", "official_description", "summary", "readme_intro",
              "readme", "tags", "topics", "source_page", "url", "video_url", "channel",
              "body_text", "keywords", "canonical_page_url", "github_urls",
              "project_links", "external_urls", "repository_identities")
    return " ".join(str(row.get(key) or "") for key in fields)


def _field_values(row: dict[str, Any], field: str) -> list[str]:
    value = row.get(field)
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if value is not None and str(value).strip() else []


def _candidate_repository_identities(row: dict[str, Any]) -> set[str]:
    """Return only repository identities evidenced by structured identity or URL fields."""
    identities: set[str] = set()
    for field in ("normalized_repository_identities", "repository_identities",
                  "repository_identity", "requested_repository"):
        for value in _field_values(row, field):
            request = solution_scout.exact_github_repository_request(value)
            if request:
                identities.add(f"{request[0]}/{request[1]}".casefold())
    for field in ("canonical_github_urls", "github_urls", "project_links", "external_urls",
                  "canonical_url", "url"):
        for value in _field_values(row, field):
            identity = solution_scout.repository_identity_from_url(value)
            if identity:
                identities.add(identity.casefold())
    for link in row.get("outbound_links") or []:
        if not isinstance(link, dict):
            continue
        for field in ("repository_identity", "canonical_url", "outbound_url"):
            for value in _field_values(link, field):
                request = solution_scout.exact_github_repository_request(value)
                identity = (f"{request[0]}/{request[1]}" if request else
                            solution_scout.repository_identity_from_url(value))
                if identity:
                    identities.add(identity.casefold())
    return identities


def _resolve_explicit_original_project(row: dict[str, Any]) -> dict[str, Any]:
    """Resolve only structured original-project evidence carried by a reference."""
    github_claims: list[tuple[str, str, str]] = []
    invalid_claims: list[tuple[str, str]] = []
    other_claims: list[tuple[str, str, str]] = []

    def add_claim(field: str, value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        request = solution_scout.exact_github_repository_request(text)
        if request:
            owner, repo, canonical = request
            github_claims.append((f"{owner}/{repo}", canonical, field))
            return
        source_type = solution_scout.classify(text)
        if source_type in {"huggingface", "civitai"}:
            other_claims.append((text, source_type, field))
        else:
            invalid_claims.append((field, text))

    for field in ("repository_identity", "repository_identities",
                  "normalized_repository_identities", "requested_repository"):
        for value in _field_values(row, field):
            add_claim(field, value)
    for field in ("verified_original_project_url", "original_project_url", "project_url",
                  "canonical_github_urls", "github_urls", "project_links"):
        for value in _field_values(row, field):
            add_claim(field, value)
    for index, link in enumerate(row.get("outbound_links") or []):
        if not isinstance(link, dict):
            continue
        for field in ("repository_identity", "canonical_url", "outbound_url"):
            for value in _field_values(link, field):
                add_claim(f"outbound_links[{index}].{field}", value)

    identities = {identity.casefold() for identity, _url, _field in github_claims}
    if len(identities) > 1:
        return {"status": "CONFLICTING", "error": "conflicting exact GitHub repository identities",
                "candidates": sorted({url for _identity, url, _field in github_claims})}
    if github_claims:
        identity, canonical, field = github_claims[0]
        return {"status": "RESOLVED", "url": canonical, "repository": identity,
                "source_type": "github_repository", "evidence_field": field}
    if len(other_claims) > 1 and len({url.casefold() for url, _type, _field in other_claims}) > 1:
        return {"status": "CONFLICTING", "error": "conflicting original-project URLs",
                "candidates": sorted({url for url, _type, _field in other_claims})}
    if other_claims:
        url, source_type, field = other_claims[0]
        return {"status": "RESOLVED", "url": url, "repository": "UNKNOWN",
                "source_type": source_type, "evidence_field": field}
    if invalid_claims:
        field, value = invalid_claims[0]
        return {"status": "INVALID", "error": "malformed or unsupported original-project identity",
                "evidence_field": field, "evidence_value": value}
    return {"status": "MISSING"}


class InstallEntryState(NamedTuple):
    status: str
    reason: str = ""


def _resolved_install_project_url(selected_result: dict[str, Any]) -> str:
    """Return a project URL explicitly present in the selected detail content."""
    row = selected_result.get("selected_result")
    row = row if isinstance(row, dict) else selected_result
    explicit = _resolve_explicit_original_project(row)
    if explicit.get("status") == "CONFLICTING":
        chosen = str(row.get("user_selected_project_url") or "")
        return chosen if chosen in explicit.get("candidates", []) else ""
    resolution = selected_result.get("project_resolution") or {}
    candidates = (
        selected_result.get("project_url"),
        resolution.get("url") if isinstance(resolution, dict) else "",
        row.get("project_url"),
        row.get("verified_original_project_url"),
        row.get("original_project_url"),
        *(row.get("project_links") or []),
    )
    for value in candidates:
        url = str(value or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            continue
        if parts.hostname in {"github.com", "www.github.com"}:
            request = solution_scout.exact_github_repository_request(url)
            if request:
                return request[2]
        elif solution_scout.classify(url) != "author_video":
            return url
    resolved = _resolve_explicit_original_project(row)
    if resolved.get("status") == "RESOLVED":
        return str(resolved["url"])
    direct_url = str(row.get("url") or "")
    if urlsplit(direct_url).scheme == "file" and urlsplit(direct_url).path:
        return direct_url
    if solution_scout.classify(direct_url) in {"github_repository", "huggingface", "civitai"}:
        request = solution_scout.exact_github_repository_request(direct_url)
        if request or solution_scout.classify(direct_url) != "github_repository":
            return request[2] if request else direct_url
    return ""


def get_install_entry_state(selected_result: dict[str, Any]) -> InstallEntryState:
    """Decide the detail-page install entry from its resolved project URL alone."""
    row = selected_result.get("selected_result")
    row = row if isinstance(row, dict) else selected_result
    if _resolved_install_project_url(selected_result):
        return InstallEntryState("AVAILABLE")
    if _resolve_explicit_original_project(row).get("status") == "CONFLICTING":
        return InstallEntryState("REVIEW_REQUIRED", reason="MULTIPLE_PROJECT_SOURCES")
    return InstallEntryState("UNAVAILABLE", reason="NO_PROJECT_SOURCE")


class _ReferenceBodyParser(HTMLParser):
    """Read visible article content and its explicit links, excluding page chrome."""

    def __init__(self) -> None:
        super().__init__()
        self._excluded = 0
        self._primary = 0
        self.all_parts: list[str] = []
        self.primary_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "header", "footer", "aside"}:
            self._excluded += 1
        if tag in {"main", "article"}:
            self._primary += 1
        if tag == "a" and not self._excluded:
            href = dict(attrs).get("href")
            if href:
                self.all_parts.append(href)
                if self._primary:
                    self.primary_parts.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "header", "footer", "aside"}:
            self._excluded = max(0, self._excluded - 1)
        if tag in {"main", "article"}:
            self._primary = max(0, self._primary - 1)

    def handle_data(self, data: str) -> None:
        if not self._excluded:
            self.all_parts.append(data)
            if self._primary:
                self.primary_parts.append(data)


def _explicit_repository_urls(body: str) -> list[str]:
    """Extract repository URLs literally present in fetched source content."""
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s<>\"']+", html.unescape(body)):
        raw = match.group(0).rstrip(".,;:!?)]}>\\")
        parts = urlsplit(raw)
        path = [part for part in parts.path.strip("/").split("/") if part]
        if len(path) < 2:
            continue
        if parts.hostname in {"github.com", "www.github.com"}:
            candidate = f"https://github.com/{path[0]}/{path[1].removesuffix('.git')}"
            if solution_scout.exact_github_repository_request(candidate):
                urls.append(candidate)
        elif parts.hostname == "huggingface.co" and path[0] not in {"datasets", "spaces"}:
            urls.append(f"https://huggingface.co/{path[0]}/{path[1]}")
    return list(dict.fromkeys(urls))


def read_reference_project_links(row: dict[str, Any]) -> list[str]:
    """Fetch a reference body and verify explicit external repository identities."""
    source_url = str(row.get("canonical_page_url") or row.get("url") or "")
    if urlsplit(source_url).scheme not in {"http", "https"}:
        return []
    page = solution_scout.fetch(source_url, "text/html").decode("utf-8", errors="replace")
    if urlsplit(source_url).hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        player = solution_scout._youtube_json(page, "ytInitialPlayerResponse")
        body = str((player.get("videoDetails") or {}).get("shortDescription") or "")
    else:
        parser = _ReferenceBodyParser()
        parser.feed(page)
        body = " ".join(parser.primary_parts or parser.all_parts)
    verified: list[str] = []
    for candidate in _explicit_repository_urls(body):
        if solution_scout.exact_github_repository_request(candidate):
            records = solution_scout.github_repository_exact(candidate)
            if records:
                verified.append(str(records[0]["url"]))
        elif urlsplit(candidate).hostname == "huggingface.co":
            parts = urlsplit(candidate).path.strip("/").split("/")
            api_url = f"https://huggingface.co/api/models/{quote(parts[0], safe='')}/{quote(parts[1], safe='')}"
            try:
                metadata = json.loads(solution_scout.fetch(api_url, "application/json"))
            except Exception:
                continue
            if str(metadata.get("id") or "").casefold() == f"{parts[0]}/{parts[1]}".casefold():
                verified.append(candidate)
    return list(dict.fromkeys(verified))


def should_route_youtube(context: dict[str, Any]) -> bool:
    text = str(context.get("original_query") or "").casefold()
    preferred = {str(value).casefold() for value in context.get("preferred_sources", [])}
    return ("youtube" in preferred or context.get("task") in _YOUTUBE_ROUTE_TASKS or
            any(term.casefold() in text for term in _YOUTUBE_ROUTE_TERMS))


def youtube_search_queries(context: dict[str, Any], limit: int = 3) -> list[str]:
    """Build tutorial queries while retaining every required explicit entity."""
    entities = list(context.get("entities", []))
    anchor = " ".join(entities)
    task = str(context.get("task") or "").strip()
    candidates = [
        " ".join(part for part in (anchor, task, "workflow") if part and part != "UNKNOWN"),
        " ".join(part for part in (anchor, task, "tutorial") if part and part != "UNKNOWN"),
        str(context.get("original_query") or ""),
    ]
    output: list[str] = []
    for query in candidates:
        query = " ".join(query.split())
        if not query or any(not _entity_present(entity, query) for entity in entities):
            continue
        if query.casefold() not in {existing.casefold() for existing in output}:
            output.append(query)
        if len(output) >= limit:
            break
    return output


def _entity_present(entity: str, candidate_text: str) -> bool:
    """Match a required entity conservatively across punctuation and version spacing."""
    folded_entity = entity.casefold()
    folded_text = candidate_text.casefold()
    if folded_entity in folded_text:
        return True
    equivalent_entity = query_planner.separator_equivalent_key(folded_entity)
    equivalent_text = query_planner.separator_equivalent_key(folded_text)
    if equivalent_entity and re.search(
            rf"(?<!\w){re.escape(equivalent_entity)}(?!\w)", equivalent_text):
        return True
    compact_text = re.sub(r"[^a-z0-9]+", "", folded_text)
    versioned = re.fullmatch(r"([a-z][a-z0-9]*?)(\d+(?:[._-]\d+)*)", folded_entity)
    if versioned:
        base, version = versioned.groups()
        digits = re.findall(r"\d+", version)
        return base in compact_text and all(digit in compact_text for digit in digits)
    return False


def _contains_text(text: str, phrase: str) -> bool:
    if phrase.casefold() in text.casefold():
        return True
    normalized_text = re.sub(r"[\W_]+", " ", text.casefold()).strip()
    normalized_phrase = re.sub(r"[\W_]+", " ", phrase.casefold()).strip()
    return bool(normalized_phrase and normalized_phrase in normalized_text)


def classify_candidate(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Require explicit entities plus independently visible task/domain evidence."""
    text = _candidate_text(row)
    original_for_matching = query_planner.normalize_search_query(context["original_query"])
    query_text = " ".join([original_for_matching, *context["selected_hints"], *context["expanded_queries"]])
    desired_domains = {key for key, terms in _DOMAIN_TERMS.items() if _matches(query_text, terms)}
    desired_domains.update(key for hint in context["selected_hints"] for key in _HINT_KEYS[hint])
    if context.get("task") == "acceleration" and not context.get("entities") and not desired_domains:
        desired_domains.add("models")
    desired_tasks = {key for key, terms in _TASK_TERMS.items() if _matches(original_for_matching + " " + " ".join(context["expanded_queries"]), terms)}
    domain_evidence = sorted({term for key in desired_domains for term in _matches(text, _DOMAIN_TERMS[key])})
    if context.get("task") not in (None, "", "UNKNOWN"):
        candidate_task_terms = [*context.get("task_terms", []),
                                *_DIRECT_ENRICHMENT_TERMS.get(context.get("task"), ())]
        task_evidence = sorted({term for term in candidate_task_terms
                                if _contains_text(text, term)})
    else:
        task_evidence = sorted({term for key in desired_tasks
                                for term in _matches(text, _TASK_TERMS[key])})
    required_entities = list(context.get("entities", [])) if context.get("entity_required") else []
    entity_evidence = [entity for entity in required_entities if _entity_present(entity, text)]
    missing_entities = [entity for entity in required_entities if entity not in entity_evidence]
    project_anchor = str(context.get("project_anchor") or "")
    identity_text = " ".join(str(row.get(key) or "") for key in ("title", "name", "url"))
    project_identity_ok = not project_anchor or bool(re.search(
        rf"(?<![A-Za-z0-9]){re.escape(project_anchor)}(?![A-Za-z0-9])",
        identity_text, re.I,
    ))
    if project_identity_ok and project_anchor and len(project_anchor) <= 2:
        # Short names can collide with unrelated subjects (for example, UV
        # mapping). Require visible context beyond the name when provided.
        context_terms = {
            token.casefold() for token in re.findall(r"[A-Za-z]{3,}", context["original_query"])
            if token.casefold() not in {
                "find", "locate", "show", "the", "official", "for", "and",
                "with", "installation", "install", "setup", "options",
                "instructions", "windows", "github",
            }
        }
        visible_text = " ".join(str(row.get(key) or "") for key in ("title", "name", "summary", "description"))
        if context_terms and not any(_contains_text(visible_text, token) for token in context_terms):
            project_identity_ok = False
    if str(row.get("discovered_by")) == "fixture catalog":
        domain_evidence, task_evidence = ["fixture catalog"], ["fixture validation"]
    if required_entities:
        has_task_requirement = bool(context.get("task") not in (None, "", "UNKNOWN") or
                                    desired_tasks - {"run"})
        task_ok = bool(task_evidence) if has_task_requirement else True
        if not missing_entities and task_ok and project_identity_ok:
            relevance = "RELEVANT"
        elif entity_evidence or task_evidence or domain_evidence:
            relevance = "WEAK"
        else:
            relevance = "IRRELEVANT"
    elif desired_domains and (desired_tasks or context.get("task") not in (None, "", "UNKNOWN")):
        relevance = "RELEVANT" if domain_evidence and task_evidence else ("WEAK" if domain_evidence or task_evidence else "IRRELEVANT")
    elif desired_domains:
        relevance = "RELEVANT" if domain_evidence else "IRRELEVANT"
        task_evidence = task_evidence or (["supported-category request"] if domain_evidence else [])
    elif context.get("task") not in (None, "", "UNKNOWN"):
        # Recognized non-English tasks are checked through their translated evidence terms.
        relevance = "RELEVANT" if task_evidence else "IRRELEVANT"
    else:
        tokens = [x.casefold() for x in re.findall(r"[A-Za-z0-9_+-]{2,}|[\u3400-\u9fff]{2,}", context["original_query"])]
        exact = sorted({token for token in tokens if token in text.casefold()})
        relevance = "RELEVANT" if exact else "UNKNOWN"
        domain_evidence, task_evidence = exact, exact
    reason = "" if relevance == "RELEVANT" else (
        "Missing required entity evidence: " + ", ".join(missing_entities) + "." if missing_entities else
        "Named project is absent from result title and URL." if not project_identity_ok else
        "Missing task evidence." if domain_evidence and not task_evidence else
        "Missing target/domain evidence." if task_evidence and not domain_evidence else
        "No meaningful target/domain and task evidence.")
    return {"relevance_class": relevance, "entity_evidence": entity_evidence,
            "missing_entities": missing_entities, "domain_evidence": domain_evidence,
            "task_evidence": task_evidence, "rejection_reason": reason}


def _github_repository(url: str) -> tuple[str, str] | None:
    match = re.match(r"^https?://github\.com/([^/?#]+)/([^/?#]+)", str(url), re.IGNORECASE)
    if not match:
        return None
    owner, repo = match.groups()
    repo = repo.removesuffix(".git")
    if not owner or not repo or repo.casefold() in {"issues", "pulls", "releases"}:
        return None
    return owner, repo


def fetch_official_candidate_evidence(row: dict[str, Any]) -> list[dict[str, str]]:
    """Fetch official GitHub metadata/README through the existing Scout retriever."""
    identity = _github_repository(str(row.get("url") or ""))
    if not identity:
        return []
    owner, repo = identity
    project_url = f"https://github.com/{owner}/{repo}"
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    documents: list[dict[str, str]] = []
    metadata = json.loads(solution_scout.fetch(
        api_url, accept="application/vnd.github+json", timeout=12).decode("utf-8"))
    description = str(metadata.get("description") or "").strip()
    if description:
        documents.append({"source_url": project_url, "evidence_type": "OFFICIAL_DESCRIPTION", "text": description})
    try:
        readme = solution_scout.fetch(
            api_url + "/readme", accept="application/vnd.github.raw+json", timeout=12
        ).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        readme = ""
    if readme.strip():
        documents.append({"source_url": project_url + "#readme", "evidence_type": "README", "text": readme})
    return documents


def _extract_source_passage(text: str, phrase: str, limit: int = 280) -> str:
    """Return a short exact source passage surrounding a matched phrase."""
    lines = [" ".join(line.split()) for line in str(text).splitlines() if line.strip()]
    for line in lines:
        if _contains_text(line, phrase):
            return line[:limit]
    return ""


def _source_passages(text: str) -> list[str]:
    """Return bounded README passages so unrelated list entries cannot be joined."""
    passages: list[str] = []
    for paragraph in re.split(r"\n\s*\n", str(text)):
        lines = [" ".join(line.split()) for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        joined = " ".join(lines)
        if len(joined) <= 600:
            passages.append(joined)
        else:
            passages.extend(line for line in lines if line)
    return passages


def evaluate_enriched_evidence(documents: list[dict[str, str]], context: dict[str, Any]) -> dict[str, Any]:
    """Distinguish direct task evidence from merely broad/implied capability."""
    task = str(context.get("task") or "")
    direct_terms = list(dict.fromkeys([
        *_DIRECT_ENRICHMENT_TERMS.get(task, ()), *context.get("task_terms", [])
    ]))
    required_entities = list(context.get("entities", []))
    records: list[dict[str, Any]] = []
    best_type = "TASK_NOT_FOUND"
    task_evidence: list[str] = []
    for document in documents:
        text = str(document.get("text") or "")
        entity_evidence = [entity for entity in required_entities if _entity_present(entity, text)]
        direct = next((term for term in direct_terms if _contains_text(text, term)), "")
        joint_passage = ""
        for passage in _source_passages(text):
            passage_direct = next((term for term in direct_terms if _contains_text(passage, term)), "")
            if not passage_direct:
                continue
            if required_entities:
                if all(_entity_present(entity, passage) for entity in required_entities):
                    joint_passage = passage
                    break
            elif task == "acceleration" and _matches(passage, _DOMAIN_TERMS["models"]):
                joint_passage = passage
                break
        implied = next((term for term in _IMPLIED_CAPABILITY_TERMS if _contains_text(text, term)), "")
        evidence_type = "TASK_DIRECT" if direct else ("TASK_IMPLIED" if implied else "TASK_NOT_FOUND")
        matched = direct or implied
        if evidence_type == "TASK_DIRECT":
            best_type = "TASK_DIRECT"
            task_evidence.append(direct)
        elif evidence_type == "TASK_IMPLIED" and best_type != "TASK_DIRECT":
            best_type = "TASK_IMPLIED"
        records.append({
            "source_url": document.get("source_url", ""),
            "evidence_type": evidence_type,
            "source_kind": document.get("evidence_type", ""),
            "short_extracted_evidence": _extract_source_passage(text, matched) if matched else "",
            "entity_evidence": entity_evidence,
            "task_evidence": [direct] if direct else [],
            "joint_task_evidence": bool(joint_passage),
            "joint_evidence_passage": joint_passage[:280],
        })
    return {"evidence_type": best_type, "task_evidence": list(dict.fromkeys(task_evidence)),
            "source_evidence": records}


def _apply_source_capability_verdict(row: dict[str, Any], context: dict[str, Any],
                                     records: list[dict[str, Any]]) -> None:
    """Keep source-matched targets while separately recording capability certainty."""
    task = str(context.get("task") or "")
    if task in ("", "UNKNOWN"):
        return
    required_entities = list(context.get("entities", []))
    if any(record.get("joint_task_evidence") for record in records):
        row["relevance_class"] = "RELEVANT"
        row["entity_evidence"] = list(dict.fromkeys([
            *row.get("entity_evidence", []), *required_entities]))
        row["missing_entities"] = []
        row["capability_verification"] = "VERIFIED"
        row.pop("capability_notice_key", None)
        row["rejection_reason"] = ""
        return
    evidenced_entities = {
        str(entity).casefold()
        for record in records
        for entity in (record.get("entity_evidence") or [])
    }
    target_supported = bool(required_entities) and all(
        str(entity).casefold() in evidenced_entities for entity in required_entities)
    if target_supported:
        row["relevance_class"] = "RELEVANT"
        row["entity_evidence"] = list(dict.fromkeys([
            *row.get("entity_evidence", []), *required_entities]))
        row["missing_entities"] = []
        row["capability_verification"] = "UNVERIFIED"
        row["capability_notice_key"] = (
            "results.acceleration_applicability_unverified"
            if task == "acceleration" else "results.requested_capability_unverified")
        row["rejection_reason"] = ""
        return
    row["relevance_class"] = "WEAK"
    row.pop("capability_verification", None)
    row.pop("capability_notice_key", None)
    row["rejection_reason"] = "No official source evidence contains the required target."


def compact_result_title(number: int, title: str, line_width: int = 34) -> str:
    """Format a result title with no more than two compact lines."""
    prefix = ""
    lines = textwrap.wrap(
        " ".join(str(title).split()),
        width=max(12, line_width - len(prefix)),
        break_long_words=True,
        break_on_hyphens=False,
    ) or ["Unnamed solution"]
    if len(lines) == 1:
        return prefix + lines[0]
    second = lines[1]
    if len(lines) > 2:
        second = second[: max(1, line_width - 1)].rstrip() + "…"
    return prefix + lines[0] + "\n" + second


def configured_first_party_website_name(row: dict[str, Any]) -> str:
    """Resolve a first-party website's display name from configured source metadata."""
    if (str(row.get("source_type") or "") != "First-party Website" or
            not bool(row.get("first_party"))):
        return ""
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    candidates = (
        row.get("canonical_page_url"), row.get("url"),
        row.get("first_party_reference_url"), provenance.get("website_url"),
    )
    for source in product_config.FIRST_PARTY_WEBSITE_SOURCES:
        source_root = solution_scout.canonical_url(str(source.get("url") or ""))
        if not source_root:
            continue
        for candidate in candidates:
            candidate_url = solution_scout.canonical_url(str(candidate or ""))
            if candidate_url == source_root or candidate_url.startswith(source_root + "/"):
                return str(source.get("name") or "").strip()
    return ""


def result_card_text(card: dict[str, Any], line_width: int = 34) -> str:
    """Render a compact title plus a configured website publication name when present."""
    title = compact_result_title(int(card["number"]), str(card["name"]), line_width=line_width)
    role_label = str(card.get("role_label") or "")
    if role_label:
        title = f"[{role_label}] {title}"
    source_name = str(card.get("source_name") or "").strip()
    lines = [title]
    if source_name:
        lines.append(source_name)
    lines.extend(str(line) for line in card.get("popularity_lines") or [] if str(line).strip())
    return "\n".join(lines)


def popularity_display_lines(data: dict[str, Any], translate: Any) -> list[str]:
    """Format source-local popularity without treating missing values as zero."""
    platform = str(data.get("source_platform") or "").casefold()
    if not platform:
        source_kind = solution_scout.classify(str(data.get("url") or ""))
        platform = "github" if source_kind == "github_repository" else (
            "huggingface" if source_kind == "huggingface" else "")

    def value(name: str) -> str:
        raw = data.get(name)
        status = str(data.get(name + "_status") or "").casefold()
        obtained = status == "obtained" or (not status and raw is not None)
        return str(raw) if obtained and raw is not None else translate("results.not_obtained")

    if platform == "github":
        return [translate("results.stars", value=value("stars"))]
    if platform == "huggingface":
        return [translate("results.likes", value=value("likes")),
                translate("results.downloads_30d", value=value("downloads_30d"))]
    return []


_README_STOP_SECTIONS = re.compile(
    r"^(?:table of contents|contents|installation|install|downloads?|model downloads?|"
    r"model zoo|quick start|getting started|usage|demo(?:s|nstration)?|news|todo|"
    r"changelog|release notes?|benchmarks?|acknowledgements?|citation|license)\s*[:：]?$",
    re.I,
)
_README_COMMAND = re.compile(
    r"^(?:[$>]\s*)?(?:pip|python\s+-m\s+pip|npm|pnpm|yarn|git\s+clone|docker|"
    r"conda|mamba|wget|curl|bash|sh|cd)\b",
    re.I,
)


def _source_sentences(text: str) -> list[str]:
    """Extract contiguous README prose without generating or paraphrasing it."""
    source = re.sub(r"```.*?```|~~~.*?~~~", "\n", text or "", flags=re.DOTALL)
    source = re.sub(r"<!--.*?-->|<details\b.*?</details\s*>", "\n", source,
                    flags=re.DOTALL | re.I)
    source = re.sub(r"<(?:img|picture|source|svg)\b[^>]*>(?:.*?</(?:picture|svg)\s*>)?",
                    " ", source, flags=re.DOTALL | re.I)
    source = re.sub(r"</?(?:div|p|section|article|header|footer|center|br|hr)\b[^>]*>",
                    "\n", source, flags=re.I)

    lines: list[str] = []
    seen: set[str] = set()
    prose_started = False
    for raw in source.splitlines():
        original = html.unescape(raw).strip()
        if not original:
            continue
        is_heading = bool(re.match(r"^#{1,6}\s+|^<h[1-6]\b", original, re.I))
        line = re.sub(r"<[^>]+>", " ", original)
        line = re.sub(r"^#{1,6}\s+", "", line).strip()
        heading_text = re.sub(r"[*_`~]", "", line).strip(" :：")
        if is_heading:
            if prose_started and _README_STOP_SECTIONS.fullmatch(heading_text):
                break
            continue
        if prose_started and _README_STOP_SECTIONS.fullmatch(heading_text):
            break
        if (line.startswith("|") or re.match(r"^\s*[-:| ]+\s*$", line) or
                re.match(r"^\[[^]]+\]:\s*\S+", line) or _README_COMMAND.match(line)):
            if prose_started and (_README_COMMAND.match(line) or line.startswith("|")):
                break
            continue
        if re.fullmatch(r"https?://\S+", line, re.I):
            continue
        if (re.search(r"(?:shields\.io|badge|github\.com/.*/(?:releases|archive))", line, re.I) or
                re.match(r"^[-*+]\s*\[[^]]+\]\([^)]*\)\s*$", line) or
                (len(re.findall(r"\[[^]]+\]\([^)]*\)", line)) >= 2)):
            continue
        # Standalone emphasized labels are titles/menu items rather than prose.
        if re.fullmatch(r"(?:\*\*|__)[^.!?]+(?:\*\*|__)", line):
            continue
        line = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", line)
        line = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"https?://\S+", " ", line)
        line = re.sub(r"^[>*+-]\s+", "", line)
        line = re.sub(r"[*_~]", "", line)
        line = re.sub(r"`([^`]*)`", r"\1", line)
        line = " ".join(line.split()).strip()
        words = re.findall(r"\w+", line)
        if not words or (len(words) <= 5 and re.search(r"[\U0001F000-\U0001FAFF]", line)):
            continue
        normalized = line.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(line)
        prose_started = True

    prose = " ".join(lines)
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", prose) if part.strip()]


_TAGLINE_WORDS = {"from", "foundation", "to", "application", "tagline", "slogan", "slogans", "motto",
                  "empower", "empowers", "empowering", "revolutionize", "revolutionizes", "revolutionizing",
                  "unlock", "unlocking", "unlockthe", "unlockingthe", "leverage", "leveraging", "cutting",
                  "edge", "next", "gen", "generation", "gpt", "chatgpt", "ai", "the"}


def _looks_like_heading_or_tagline(sentences):
    """Flag a single short candidate that reads as a heading or tagline, not a description."""
    if not sentences:
        return False
    first = sentences[0]
    tokens = re.findall(r"\w+", first)
    token_count = len(tokens)
    if token_count <= 5:
        ratio = sum(1 for tok in tokens if tok.lower() in _TAGLINE_WORDS) / max(token_count, 1)
        return ratio >= 0.6
    if re.search(r"\A[^.!?]*[:;][^.!?]*$", first):
        return True
    return False


def _is_huggingface_model(row: dict[str, Any]) -> bool:
    """Identify a Hugging Face model result without relying on its display title."""
    return bool(str(row.get("model_id") or "").strip()) and (
        str(row.get("type") or "").casefold() == "huggingface" or
        "hugging face" in str(row.get("source_type") or "").casefold() or
        solution_scout.classify(str(row.get("url") or "")) == "huggingface"
    )


def _huggingface_model_id(row: dict[str, Any]) -> str:
    """Return the exact selected model id and reject values that could change the URL shape."""
    model_id = str(row.get("model_id") or "").strip()
    if not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*", model_id):
        raise ValueError("The selected Hugging Face model_id is missing or invalid.")
    return model_id


def _first_markdown_prose_paragraph(markdown: str) -> str:
    """Extract the first substantive author paragraph from a model card, without paraphrasing."""
    source = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    if source.startswith("---\n"):
        end = source.find("\n---", 4)
        if end >= 0:
            source = source[end + 4:]
    source = re.sub(r"```.*?```|~~~.*?~~~", "\n", source, flags=re.DOTALL)
    source = re.sub(r"<!--.*?-->", "\n", source, flags=re.DOTALL)
    for block in re.split(r"\n\s*\n", source):
        original = html.unescape(block).strip()
        if not original:
            continue
        lines = [line.strip() for line in original.splitlines() if line.strip()]
        if not lines or any(re.match(r"^#{1,6}\s+", line) for line in lines):
            continue
        if all(re.match(r"^(?:[|]|[-*+]\s|<|!\[|\[!\[)", line) for line in lines):
            continue
        text = " ".join(lines)
        if (re.search(r"(?:shields\.io|badge)", text, re.I) or
                re.match(r"^\[[^]]+\]:\s*\S+", text) or
                _README_COMMAND.match(text)):
            continue
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", text)
        text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"^[>*+-]\s+", "", text)
        text = re.sub(r"[*_~]", "", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = " ".join(text.split()).strip()
        word_count = len(re.findall(r"\w+", text))
        cjk_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))
        if word_count >= 6 or cjk_count >= 10:
            return text
    return ""


def read_huggingface_model_card(row: dict[str, Any]) -> dict[str, str]:
    """Read README.md for the exact selected Hugging Face model and classify the outcome."""
    try:
        model_id = _huggingface_model_id(row)
    except Exception as exc:
        return {"status": HF_CARD_FETCH_FAILED, "model_id": "", "source_url": "",
                "introduction": "", "error": f"{type(exc).__name__}: {exc}"}
    encoded_id = quote(model_id, safe="/")
    source_url = f"https://huggingface.co/{encoded_id}/blob/main/README.md"
    raw_url = f"https://huggingface.co/{encoded_id}/resolve/main/README.md"
    try:
        markdown = solution_scout.fetch(
            raw_url, accept="text/markdown,text/plain;q=0.9,*/*;q=0.1", timeout=20
        ).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = HF_CARD_NO_CONTENT if exc.code == 404 else HF_CARD_FETCH_FAILED
        return {"status": status, "model_id": model_id, "source_url": source_url,
                "introduction": "", "error": "" if status == HF_CARD_NO_CONTENT else
                f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"status": HF_CARD_FETCH_FAILED, "model_id": model_id,
                "source_url": source_url, "introduction": "",
                "error": f"{type(exc).__name__}: {exc}"}
    introduction = _first_markdown_prose_paragraph(markdown)
    return {"status": HF_CARD_READ_SUCCESS if introduction else HF_CARD_NO_CONTENT,
            "model_id": model_id, "source_url": source_url,
            "introduction": introduction, "error": ""}


def extract_introduction(row: dict[str, Any]) -> str:
    """Return source-provided prose, preferring substantive README evidence."""
    keys = (("readme_intro", "readme") if _is_huggingface_model(row) else
            ("readme_intro", "readme", "official_description", "description", "summary"))
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value.upper() in {"UNKNOWN", "NONE", "N/A"}:
            continue
        sentences = _source_sentences(value)
        if not sentences:
            continue
        if _looks_like_heading_or_tagline(sentences):
            sentences = sentences[1:]
        if sentences:
            return " ".join(sentences)
    return INTRO_FALLBACK


def _propagate_enriched_introduction(row: dict[str, Any],
                                     documents: list[dict[str, str]]) -> None:
    """Copy usable official source prose into fields consumed by the detail view."""
    fields = {"OFFICIAL_DESCRIPTION": "official_description", "README": "readme_intro"}
    for document in documents:
        field = fields.get(str(document.get("evidence_type") or ""))
        text = str(document.get("text") or "").strip()
        if not field or not text or row.get(field):
            continue
        if extract_introduction({field: text}) != INTRO_FALLBACK:
            row[field] = text


def _purpose_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Return only official source evidence permitted for purpose summarization."""
    if _is_huggingface_model(row):
        records = []
        for item in row.get("purpose_source_evidence") or []:
            if not isinstance(item, dict):
                continue
            passage = str(item.get("text") or "").strip()
            if passage:
                records.append({
                    "source_kind": "MODEL_CARD_INTRODUCTION",
                    "source_url": str(item.get("source_url") or ""),
                    "text": passage[:1200],
                })
        return {"source_evidence": records} if records else {}
    evidence: dict[str, Any] = {}
    limits = {"official_description": 1200, "readme_intro": 8000}
    for key in ("official_description", "readme_intro"):
        value = str(row.get(key) or "").strip()
        if value and extract_introduction({key: value}) != INTRO_FALLBACK:
            evidence[key] = value[:limits[key]]
    if row.get("first_party"):
        for key in ("contentText", "content_text", "content", "summary"):
            value = str(row.get(key) or "").strip()
            introduction = extract_introduction({"summary": value}) if value else INTRO_FALLBACK
            if introduction != INTRO_FALLBACK:
                evidence["first_party_source_prose"] = introduction[:1200]
                break
    records = []
    for item in row.get("source_evidence") or []:
        if not isinstance(item, dict):
            continue
        passage = str(item.get("text") or item.get("short_extracted_evidence") or "").strip()
        if passage:
            records.append({
                "source_kind": str(item.get("source_kind") or item.get("evidence_type") or ""),
                "text": passage[:1200],
            })
    if records:
        evidence["source_evidence"] = records
    if row.get("capability_verification"):
        evidence["requested_capability_verification"] = row["capability_verification"]
    return evidence


def plain_language_explanation(row: dict[str, Any], summarizer: Any = None,
                               fallback: str = PLAIN_FALLBACK,
                               source_read_failed: str = PLAIN_FALLBACK,
                               model_unavailable: str = PLAIN_FALLBACK,
                               invalid_output: str = PLAIN_FALLBACK,
                               on_error: Any = None) -> str:
    """Summarize official evidence without copying the extractive introduction."""
    def fail(kind: str, reason: str, message: str) -> str:
        if callable(on_error):
            on_error(kind, reason)
        return message

    if row.get("detail_enrichment_error"):
        return fail("SOURCE_READ_FAILED", str(row["detail_enrichment_error"]), source_read_failed)
    evidence = _purpose_evidence(row)
    if not evidence:
        return fail("SOURCE_INSUFFICIENT", "No usable official source evidence.", fallback)
    if not callable(summarizer):
        return fail("MODEL_UNAVAILABLE", "Purpose summarizer is not callable.", model_unavailable)
    try:
        raw = summarizer(evidence)
    except query_planner.LocalModelInvalidResponseError as exc:
        return fail("INVALID_OUTPUT", f"{type(exc).__name__}: {exc}", invalid_output)
    except Exception as exc:
        return fail("MODEL_UNAVAILABLE", f"{type(exc).__name__}: {exc}", model_unavailable)
    try:
        explanation = query_planner.extract_final_answer(raw)
        explanation = " ".join(explanation.split()).strip()
    except Exception as exc:
        return fail("INVALID_OUTPUT", f"Purpose response parsing failed: {type(exc).__name__}: {exc}",
                    invalid_output)
    if not explanation:
        return fail("INVALID_OUTPUT", "Purpose response was empty.", invalid_output)
    introduction = extract_introduction(row)
    word_count = len(re.findall(r"\w+", explanation))
    cjk_character_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", explanation))
    sentences = [part for part in re.split(
        r"(?:(?<=[.!?])\s+|(?<=[。！？])\s*)", explanation) if part.strip()]
    if ((word_count < 3 and cjk_character_count < 6) or
            explanation.casefold() == introduction.casefold() or
            not 1 <= len(sentences) <= 2):
        return fail("INVALID_OUTPUT",
                    f"Purpose response failed validation: words={word_count}, "
                    f"cjk={cjk_character_count}, sentences={len(sentences)}, "
                    f"copied_source={explanation.casefold() == introduction.casefold()}.",
                    invalid_output)
    return explanation


def _human_step(step: dict[str, Any]) -> str:
    labels = {
        "COPY_FILE": "Download required files",
        "DOWNLOAD_FILE": "Download required files",
        "VERIFY_FILE": "Verify installed files",
        "CHECK_MODEL": "Verify model",
        "CHECK_PORT": "Check availability",
        "VERIFY_PACKAGE": "Verify package",
        "USER_ACTION_REQUIRED": "Complete required action",
    }
    return labels.get(str(step.get("type", "")), "Complete installation operation")


_REVIEW_REASON_KEYS = {
    "conflicting dependency declarations": "plan.review_reason.conflicting_dependencies",
    "runtime-sensitive package requires manual review": "plan.review_reason.runtime_sensitive",
    "GPU-specific package requires manual review": "plan.review_reason.gpu_specific",
    "custom or unsupported package source": "plan.review_reason.unsupported_source",
    "no authoritative manifest or exact safe primary-install evidence": "plan.review_reason.no_authority",
    "unsupported version specifier": "plan.review_reason.unsupported_version",
    "optional or non-mandatory dependency": "plan.review_reason.optional_dependency",
    "PyTorch runtime lacks exact documented CUDA evidence": "plan.review_reason.pytorch_cuda_missing",
    "PyTorch runtime evidence is missing": "plan.review_reason.pytorch_missing",
    "PyTorch runtime evidence is ambiguous or conflicting": "plan.review_reason.pytorch_ambiguous",
    "Model asset lacks an exact supported HTTPS URL": "plan.review_reason.model_https_missing",
    "Model asset filename is missing or cannot be derived safely": "plan.review_reason.model_filename_missing",
    "Model asset lacks an approved project asset destination": "plan.review_reason.model_destination_missing",
    "Model asset destination is unsafe": "plan.review_reason.model_destination_unsafe",
    "Model asset destination filename does not match the evidenced asset": "plan.review_reason.model_filename_mismatch",
    "Model asset lacks an original evidence location": "plan.review_reason.model_evidence_missing",
    "Model asset has malformed documented SHA-256 evidence": "plan.review_reason.model_hash_malformed",
    "Model asset has malformed documented size evidence": "plan.review_reason.model_size_malformed",
    "Model asset has unsupported revision-kind evidence": "plan.review_reason.model_revision_unsupported",
    "Model asset lacks an immutable documented revision or publisher-documented SHA-256":
        "plan.review_reason.model_identity_missing",
    "Model asset destination escapes the approved asset root": "plan.review_reason.model_destination_escape",
}


def _localized_review_action(action: Any, tr: Any) -> str:
    """Translate only planner-authored reasons while preserving evidence and commands verbatim."""
    text = str(action)
    unsupported_name = "Python dependency has an unsupported package name: "
    if text.startswith(unsupported_name):
        return tr("plan.review_reason.unsupported_package_name", name=text[len(unsupported_name):])

    scoped = re.fullmatch(
        r"(.+): (CORE|OPTIONAL|DEMO|BENCHMARK|DEV_TEST|UNKNOWN_SCOPE) "
        r"dependency is not a core installation dependency",
        text,
    )
    if scoped:
        return f"{scoped.group(1)}: " + tr(
            "plan.review_reason.non_core_scope",
            scope=tr(f"plan.scope.{scoped.group(2).lower()}"),
        )

    conflict = re.fullmatch(r"(.+): conflicting dependency declarations \((.+)\)", text)
    if conflict:
        return f"{conflict.group(1)}: " + tr(
            "plan.review_reason.conflicting_dependencies_detail", details=conflict.group(2))

    for reason, key in _REVIEW_REASON_KEYS.items():
        if text == reason:
            return tr(key)
        suffix = f": {reason}"
        if text.endswith(suffix):
            return text[:-len(reason)] + tr(key)
    return text


INSTALLATION_STEP_LABELS = {
        "CREATE_PYTHON_ENV": "plan.step.create_env",
        "INSTALL_PYTHON_PACKAGES": "plan.step.install_packages",
        "INSTALL_PYTORCH_RUNTIME": "plan.step.install_runtime",
        "INSTALL_LOCAL_PYTHON_PROJECT": "plan.step.install_project",
        "INSTALL_LOCAL_EDITABLE_PROJECT": "plan.step.install_editable_project",
        "DOWNLOAD_MODEL_ASSET": "plan.step.download_model",
        "RUN_FIXED_VERIFICATION_COMMAND": "plan.step.verify",
        "REVIEW_DOCUMENTED_ACTION": "plan.step.review_documented",
}


def installation_plan_review_text(plan: dict[str, Any], research: dict[str, Any], tr: Any) -> str:
    """Render the product plan without exposing schema, hashes, IDs, or raw JSON."""
    executable = [step for step in plan.get("steps", [])
                  if step.get("type") in installation_plan.EXECUTABLE_TYPES]
    review_only = [step for step in plan.get("steps", [])
                   if step.get("type") not in installation_plan.EXECUTABLE_TYPES]

    def evidence_line(step: dict[str, Any]) -> str:
        items = []
        for item in step.get("evidence") or []:
            if isinstance(item, dict):
                location = " · ".join(str(item.get(key) or "") for key in ("path", "heading")
                                      if item.get(key))
                quote = str(item.get("exact_text") or "").strip()
                text = " — ".join(value for value in (location, quote) if value)
            else:
                text = str(item).strip()
            if text and text not in items:
                items.append(text)
        return items[0] if items else tr("plan.evidence_unavailable")

    def step_lines(step: dict[str, Any], review: bool = False) -> list[str]:
        title = tr(INSTALLATION_STEP_LABELS.get(str(step.get("type")), "plan.step.other"))
        rows = [f"• {title}", f"  {tr('plan.target')}: {step.get('target') or tr('install.unknown')}"]
        if review:
            actions = (step.get("inputs") or {}).get("documented_actions") or []
            rows.extend(f"  - {_localized_review_action(action, tr)}" for action in actions)
        else:
            rows.extend([f"  {tr('plan.reason')}: {evidence_line(step)}",
                         f"  {tr('plan.approval_required')}: {tr('common.yes')}"])
        return rows

    status_key = "plan.status.review_required" if plan.get("status") == "REVIEW_REQUIRED" else (
        "plan.status.ready" if plan.get("status") == "READY_FOR_APPROVAL" else "plan.status.blocked")
    models = [row for row in research.get("models") or [] if isinstance(row, dict) and row]
    model_downloads = [step for step in executable if step.get("type") == "DOWNLOAD_MODEL_ASSET"]
    model_text = (tr("plan.models.executable") if model_downloads else
                  tr("plan.models.review") if models else tr("plan.models.none"))
    lines = [f"{tr('plan.status')}: {tr(status_key)}",
             f"{tr('plan.project_location')}: {(plan.get('source_identity') or {}).get('local_path') or tr('install.unknown')}",
             f"{tr('plan.python_environment')}: {(plan.get('environment') or {}).get('path') or tr('install.unknown')}",
             f"{tr('plan.models')}: {model_text}", "", tr("plan.executable_heading")]
    if executable:
        for step in executable:
            lines.extend(step_lines(step))
    else:
        lines.append(f"• {tr('install.nothing')}")
    lines.extend(["", tr("plan.review_only_heading")])
    if review_only:
        for step in review_only:
            lines.extend(step_lines(step, review=True))
    else:
        lines.append(f"• {tr('install.nothing')}")
    return "\n".join(lines)


def installation_cloud_ai_report(plan: dict[str, Any], research: dict[str, Any], tr: Any) -> str:
    """Prepare a copyable review request using only persisted plan and research facts."""
    source = plan.get("source_identity") or {}
    environment = plan.get("environment") or {}
    repository = str(source.get("repository") or source.get("repo") or tr("install.unknown"))
    methods = [row for row in research.get("install_methods") or [] if isinstance(row, dict)]
    evidence_rows: list[tuple[str, str, dict[str, Any]]] = []
    for category in ("install_methods", "dependencies", "system_dependencies", "models",
                     "python_environment", "gpu_runtime", "launch", "proposed_plan"):
        values = research.get(category)
        for row in values if isinstance(values, list) else [values]:
            if not isinstance(row, dict):
                continue
            owner = str(row.get("name") or row.get("package") or row.get("description") or category)
            for evidence in row.get("evidence") or []:
                if isinstance(evidence, dict) and evidence.get("exact_text"):
                    evidence_rows.append((category, owner, evidence))

    def evidence_text(item: dict[str, Any]) -> str:
        path = str(item.get("path") or tr("install.unknown"))
        heading = str(item.get("heading") or "")
        location = f"{path} · {heading}" if heading else path
        return f"{location}: {item['exact_text']}"

    def matching_evidence(issue: str) -> list[str]:
        subject = issue.split(":", 1)[0].strip().lower()
        model_issue = issue.lower().startswith("model asset")
        matches: list[str] = []
        for category, owner, item in evidence_rows:
            quote = str(item["exact_text"])
            if ((model_issue and category == "models")
                    or (not model_issue and subject and
                        (subject in owner.lower() or subject in quote.lower()
                         or quote.strip().lower() in issue.lower()))):
                rendered = evidence_text(item)
                if rendered not in matches:
                    matches.append(rendered)
            if len(matches) >= 3:
                break
        return matches

    lines = [tr("plan.cloud.report_title"), "",
             f"{tr('plan.cloud.project')}: {methods[0].get('name') if methods else repository}",
             f"{tr('plan.cloud.repository')}: {repository}",
             f"{tr('plan.cloud.commit')}: {source.get('exact_commit') or source.get('commit') or tr('install.unknown')}",
             "", tr("plan.cloud.environment"),
             f"- {tr('plan.cloud.host')}: {platform.system()} {platform.release()} ({platform.machine()})",
             f"- {tr('plan.cloud.manager')}: {environment.get('manager') or tr('install.unknown')}",
             f"- Python: {environment.get('python') or tr('install.unknown')}",
             f"- {tr('plan.cloud.environment_path')}: {environment.get('path') or tr('install.unknown')}",
             "", tr("plan.cloud.original_advice")]
    commands = list(dict.fromkeys(str(command) for method in methods
                                  for command in method.get("exact_commands") or [] if command))
    lines.extend(f"- {command}" for command in commands)
    if not commands:
        lines.append(f"- {tr('plan.cloud.none_recorded')}")
    lines.extend(["", tr("plan.cloud.confirmed")])
    executable = [step for step in plan.get("steps") or []
                  if isinstance(step, dict) and step.get("type") in installation_plan.EXECUTABLE_TYPES]
    lines.extend(f"- {step.get('type')}: {step.get('target') or tr('install.unknown')}"
                 for step in executable)
    if not executable:
        lines.append(f"- {tr('plan.cloud.none_recorded')}")
    lines.extend(["", tr("plan.cloud.unresolved")])
    for index, action in enumerate(plan.get("unsupported_actions") or [], 1):
        issue = str(action)
        lines.extend([f"{index}. {issue}",
                      f"   {tr('plan.cloud.question')}: {tr('plan.cloud.question_text', issue=issue)}",
                      f"   {tr('plan.cloud.evidence')}:"])
        matches = matching_evidence(issue)
        lines.extend(f"   - {match}" for match in matches)
        if not matches:
            lines.append(f"   - {tr('plan.cloud.no_direct_evidence')}")
    if not plan.get("unsupported_actions"):
        lines.append(f"- {tr('plan.cloud.none_recorded')}")
    return "\n".join(lines)


def installation_lifecycle_text(result: dict[str, Any], tr: Any) -> str:
    """Render persisted post-research lifecycle state without internal metadata."""
    state = result.get("execution_state") if isinstance(result.get("execution_state"), dict) else result
    status = installation_plan.execution_status(state)
    labels = {
        "NEEDS_APPROVAL": "plan.lifecycle.needs_approval",
        "STOPPED": "plan.lifecycle.stopped",
        "VERIFIED": "plan.lifecycle.verified",
        "FAILED": "plan.lifecycle.failed",
        "RUNNING": "plan.lifecycle.running",
        "APPROVED": "plan.status.approved",
        "WAITING_FOR_APPROVAL": "plan.lifecycle.needs_approval",
    }
    lines = [f"{tr('plan.lifecycle.status')}: {tr(labels.get(status, 'plan.lifecycle.unknown'))}"]
    if status == "STOPPED":
        lines.append(tr("plan.lifecycle.resume_available"))
    elif status == "NEEDS_APPROVAL":
        lines.append(tr("plan.lifecycle.corrective_approval_required"))
    if state.get("stop_reason") or state.get("reason"):
        lines.append(f"{tr('plan.lifecycle.reason')}: {state.get('stop_reason') or state.get('reason')}")
    lines.extend(["", tr("plan.lifecycle.steps")])
    step_labels = {"SUCCESS": "plan.lifecycle.step_success", "FAILED": "plan.lifecycle.failed",
                   "STOPPED": "plan.lifecycle.stopped", "RUNNING": "plan.lifecycle.running",
                   "PENDING": "plan.lifecycle.pending", "WAITING_FOR_USER": "plan.lifecycle.waiting"}
    for index, step in enumerate(state.get("steps") or [], 1):
        title = tr(INSTALLATION_STEP_LABELS.get(str(step.get("type")), "plan.step.other"))
        step_status = installation_plan.execution_step_status(step)
        lines.extend([f"{index}. {title} — {tr(step_labels.get(step_status, 'plan.lifecycle.unknown'))}",
                      f"  {tr('plan.target')}: {step.get('target') or tr('install.unknown')}"])
        reason = step.get("reason") or step.get("stop_reason")
        if reason:
            lines.append(f"  {tr('plan.lifecycle.reason')}: {reason}")
        execution = step.get("execution") or {}
        if execution.get("exit_code") is not None:
            lines.append(f"  {tr('plan.lifecycle.exit_code')}: {execution['exit_code']}")
        if step.get("result") is not None:
            outcome = step["result"]
            rendered = (tr("plan.lifecycle.step_success") if outcome == {"completed": True}
                        else json.dumps(outcome, ensure_ascii=False))
            lines.append(f"  {tr('plan.lifecycle.result')}: "
                         + rendered)
        for name in ("stdout", "stderr"):
            if execution.get(name):
                lines.append(f"  {tr('plan.lifecycle.' + name)}:\n{execution[name]}")
    if not state.get("steps"):
        lines.append(tr("plan.lifecycle.no_steps"))
    return "\n".join(lines)


def source_execution_lifecycle_details(
    job_id: str, state: dict[str, Any], translate: Any,
) -> list[str]:
    """Return the persisted source-execution facts shown by the existing lifecycle page."""
    stopped_step = next(
        (
            step for step in state.get("steps", [])
            if str(step.get("status", "")).upper() in {"STOPPED", "FAILED"}
        ),
        {},
    )
    stage = (
        state.get("current_step")
        or state.get("current_step_id")
        or stopped_step.get("step_id")
    )
    reason = state.get("stop_reason") or stopped_step.get("reason")
    details = [translate("source_execution.job", job_id=job_id)]
    details.append(
        translate("source_execution.stage", stage=stage)
        if stage else translate("source_execution.stage_unknown")
    )
    if state.get("cancel_requested"):
        details.append(translate(
            "source_execution.cancel_requested",
            when=state.get("cancel_requested_at") or translate("source_execution.yes"),
        ))
    details.append(translate(
        "source_execution.reason",
        reason=reason or translate("source_execution.missing_reason"),
    ))
    return details


class NormalFlow:
    """Headless normal-mode view model; it contains no repair mechanism."""

    public_fields = {"name", "preview", "number", "source_name", "role"}

    def __init__(self, controller: Any, planner: Any = None,
                 curated_catalog_path: Path | str | None = None,
                 candidate_enricher: Any = None,
                 purpose_summarizer: Any = None,
                 translator: Any = None,
                 reference_reader: Any = None,
                 huggingface_card_reader: Any = None) -> None:
        self.controller = controller
        self.planner = planner
        self.curated_catalog_path = curated_catalog_path
        self.candidate_enricher = candidate_enricher or fetch_official_candidate_evidence
        self.purpose_summarizer = purpose_summarizer
        self.translator = translator
        self.reference_reader = reference_reader or read_reference_project_links
        self.huggingface_card_reader = huggingface_card_reader or read_huggingface_model_card
        self._reference_cache: dict[str, tuple[list[str], str]] = {}
        self._enrichment_cache: dict[str, tuple[list[dict[str, str]], str]] = {}
        self._huggingface_card_cache: dict[str, dict[str, str]] = {}
        self._purpose_cache: dict[tuple[str, str, str], str] = {}
        self._translation_cache: dict[tuple[str, str, str], str | None] = {}
        self.results: list[dict[str, Any]] = []
        self._sources: dict[int, dict[str, Any]] = {}
        self.search_context: dict[str, Any] = {}
        self.zero_result_message = ZERO_RESULT_MESSAGE
        self.search_errors: list[dict[str, Any]] = []

    def search(self, goal: str, selected_hints: list[str] | None = None) -> list[dict[str, Any]]:
        context = build_search_context(goal, selected_hints)
        self.search_context = context
        self.search_errors = []
        if context["outside_supported_scope"]:
            self._sources, self.results = {}, []
            self.zero_result_message = OUTSIDE_SCOPE_MESSAGE
            return []
        exact_request = (solution_scout.exact_github_repository_request(context["original_query"])
                         or solution_scout.named_github_repository_request(context["original_query"]))
        original_rows: list[dict[str, Any]] | None = None
        if exact_request:
            original_rows = self.controller.search(
                f"{exact_request[0]}/{exact_request[1]}", per_query=10)
            task = getattr(self.controller, "task", None)
            if isinstance(task, dict) and task.get("exact_repository_navigation"):
                context["exact_repository_navigation"] = True
            context["exact_repository_identity"] = (
                f"{exact_request[0]}/{exact_request[1]}".casefold())
        # Search the configured first-party lane alongside the established public Scout path.
        first_party_search = getattr(self.controller, "search_first_party", None)
        first_party_queries = ([f"{exact_request[0]}/{exact_request[1]}"] if exact_request else
                               youtube_search_queries(context))
        with ThreadPoolExecutor(max_workers=2) as executor:
            public_future = (executor.submit(
                self.controller.search, context["original_query"], per_query=10)
                if original_rows is None else None)
            first_party_future = (executor.submit(
                first_party_search, first_party_queries, per_query=5)
                if callable(first_party_search) else None)
            if public_future is not None:
                original_rows = public_future.result()
            try:
                first_party_rows = first_party_future.result() if first_party_future else []
            except Exception:
                first_party_rows = []
        original_rows = [first_party_identity.normalize_content_identity(row)
                         for row in (original_rows or [])]
        first_party_rows = [first_party_identity.normalize_content_identity(row)
                            for row in first_party_rows]
        first_party_rows = [row for row in first_party_rows if row.get("first_party")]
        if exact_request:
            target = str(context["exact_repository_identity"])
            first_party_rows = [
                row for row in first_party_rows
                if target in _candidate_repository_identities(row)
            ]
            discovery_rows = self._merge_rows(original_rows, first_party_rows)
        else:
            discovery_rows = self._merge_rows(first_party_rows, original_rows)
        classified = self._classify(discovery_rows, context)
        classified = self._enrich_entity_matched_weak(classified, context)
        discovery_rows = classified
        # Preserve verified direct matches. Deterministic aliases/task translations may add recall.
        if not any(row["relevance_class"] == "RELEVANT" for row in classified):
            seen_queries = {solution_scout.github_search_terms(context["original_query"]).casefold()}
            fallback_queries = []
            for query in context["expanded_queries"]:
                key = solution_scout.github_search_terms(query).casefold()
                if key not in seen_queries:
                    seen_queries.add(key)
                    fallback_queries.append(query)
            supplemental_search = getattr(self.controller, "search_supplemental", None)
            if fallback_queries and callable(supplemental_search):
                supplemental = supplemental_search(fallback_queries[:2], per_query=10)
                discovery_rows = self._merge_rows(discovery_rows, supplemental)
                classified = self._classify(discovery_rows, context)
        classified = self._enrich_entity_matched_weak(classified, context)
        if not any(row["relevance_class"] == "RELEVANT" for row in classified):
            planned = self._plan_fallback(context)
            if planned["planner_status"] == "OK":
                context.update(planned)
                supplemental_queries = [
                    query for query in planned["expanded_queries"]
                    if query.strip() != context["original_query"].strip()
                ]
                if supplemental_queries:
                    supplemental = [first_party_identity.normalize_content_identity(row) for row in
                                    self.controller.search_supplemental(supplemental_queries, per_query=10)]
                    classified = self._classify(
                        self._merge_rows(discovery_rows, supplemental), context)
            else:
                # Planner failure must leave the original results/classification unchanged.
                context.update(planned)
        youtube_queries: list[str] = []
        youtube_search = getattr(self.controller, "search_youtube", None)
        if should_route_youtube(context) and callable(youtube_search):
            youtube_queries = youtube_search_queries(context)
            youtube_rows = [first_party_identity.normalize_content_identity(row) for row in
                            youtube_search(youtube_queries, per_query=5)]
            classified = self._classify(self._merge_rows(classified, youtube_rows), context)
        context["youtube_queries"] = youtube_queries
        context["first_party_queries"] = first_party_queries
        classified = self._enrich_entity_matched_weak(classified, context)
        classified = self._prefer_relevant_first_party(classified, context)
        relevant = [row for row in classified if row["relevance_class"] == "RELEVANT"]
        catalog = curated_catalog.load_catalog(self.curated_catalog_path)
        curated = curated_catalog.matched_records(
            catalog, context["original_query"], context["selected_hints"], relevant)
        classified, visible = self._merge_curated(classified, relevant, curated)
        if not exact_request:
            visible = self._group_project_sources(visible)
        visible = visible[:10]
        self._sources = {int(row["number"]): row for row in visible}
        self.results = [self.card(row) for row in visible]
        task = getattr(self.controller, "task", None)
        self.search_errors = list(task.get("search_errors", [])) if isinstance(task, dict) else []
        for row in classified:
            enrichment = row.get("enrichment") or {}
            if enrichment.get("evidence_type") == "FETCH_FAILED":
                issue = {"query": str(row.get("url") or ""), "error": str(enrichment.get("error") or "")}
                if issue not in self.search_errors:
                    self.search_errors.append(issue)
        if isinstance(task, dict):
            task["search_errors"] = self.search_errors
        self.zero_result_message = (SEARCH_FAILED_MESSAGE
                                    if not self.results and self.search_errors
                                    else ZERO_RESULT_MESSAGE)
        if isinstance(task, dict):
            task.update(context)
            task["goal"] = context["original_query"]
            task["candidates"] = classified
            persist = getattr(self.controller, "persist_search_results", None)
            if callable(persist):
                persist(classified)
            else:
                save = getattr(self.controller, "_save_task", None)
                if callable(save):
                    save()
        return self.results

    @staticmethod
    def _merge_curated(classified: list[dict[str, Any]], relevant: list[dict[str, Any]],
                       curated: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Attach catalog data by project identity and add conservative curated-only matches."""
        by_identity = {curated_catalog.project_identity(row["original_url"]): row for row in curated}
        all_rows = [dict(row) for row in classified]
        visible: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in all_rows:
            identity = curated_catalog.project_identity(str(row.get("url") or ""))
            if identity in by_identity:
                row["curated"] = by_identity[identity]
            if row.get("relevance_class") == "RELEVANT":
                visible.append(row)
                if identity:
                    seen.add(identity)
        next_number = max((int(row.get("number") or 0) for row in all_rows), default=0) + 1
        for record in curated:
            identity = curated_catalog.project_identity(record["original_url"])
            if identity in seen:
                continue
            candidate = curated_catalog.as_candidate(record, next_number)
            next_number += 1
            all_rows.append(candidate)
            visible.append(candidate)
            seen.add(identity)
        visible.sort(key=lambda row: 0 if row.get("curated") else 1)
        return all_rows, visible

    @staticmethod
    def _group_project_sources(visible: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Present verified first-party projects once, with matching results as references."""
        primary_by_identity: dict[str, dict[str, Any]] = {}
        for row in visible:
            if row.get("source_type") != "First-party Website" or not row.get("first_party"):
                continue
            resolution = _resolve_explicit_original_project(row)
            if resolution.get("status") == "RESOLVED" and resolution.get("source_type") == "github_repository":
                primary_by_identity.setdefault(str(resolution["repository"]).casefold(), row)
        if not primary_by_identity:
            return visible

        primary_rows = {id(row): dict(row, presentation_role="installable", related_sources=[])
                        for row in primary_by_identity.values()}
        grouped: list[dict[str, Any]] = []
        for row in visible:
            if id(row) in primary_rows:
                grouped.append(primary_rows[id(row)])
                continue
            matches = _candidate_repository_identities(row) & primary_by_identity.keys()
            if not matches and str(row.get("source_type") or "") in {
                    "YouTube", "YouTube Post", "documentation", "official_documentation",
                    "First-party Website"}:
                title = re.sub(r"[^a-z0-9]", "", str(row.get("title") or "").casefold())
                matches = {identity for identity in primary_by_identity
                           if len(identity.split("/", 1)[-1]) >= 5 and
                           re.sub(r"[^a-z0-9]", "", identity.split("/", 1)[-1]) in title}
            if len(matches) != 1:
                grouped.append(row)
                continue
            identity = next(iter(matches))
            primary = primary_rows[id(primary_by_identity[identity])]
            url = str(row.get("url") or "")
            if url and url != primary.get("url") and not any(
                    source["url"] == url for source in primary["related_sources"]):
                primary["related_sources"].append({
                    "title": str(row.get("title") or ""), "url": url,
                    "role": (row.get("presentation_role") or
                             ("official_project" if solution_scout.classify(url) == "github_repository"
                              else "hf_item_unverified" if solution_scout.classify(url) == "huggingface"
                              else "reference"))})
        return sorted(grouped, key=lambda row: 0 if row.get("presentation_role") == "installable" else 1)

    @staticmethod
    def _project_identity(row: dict[str, Any]) -> str:
        links = list(row.get("project_links") or [])
        project_url = str(links[0] if links else row.get("url") or "")
        return curated_catalog.project_identity(project_url)

    @classmethod
    def _prefer_relevant_first_party(cls, rows: list[dict[str, Any]],
                                     context: dict[str, Any]) -> list[dict[str, Any]]:
        """Rank by target identity, completeness, evidence, then local popularity."""
        query = solution_scout._normalized_search_text(str(context.get("original_query") or ""))
        query_repositories = {
            value.casefold() for value in solution_scout.extract_repository_identities(query)
        }
        exact_repository_identity = str(context.get("exact_repository_identity") or "").casefold()

        def identity_key(value: Any) -> str:
            normalized = solution_scout._normalized_search_text(str(value or ""))
            return query_planner.separator_equivalent_key(normalized)

        target_keys = {
            identity_key(value) for value in context.get("entities") or [] if identity_key(value)
        }
        project_anchor = context.get("project_anchor")
        if project_anchor:
            target_keys.add(identity_key(project_anchor))
        repository_targets = set(query_repositories)
        if exact_repository_identity:
            repository_targets.add(exact_repository_identity)
        for repository in tuple(repository_targets):
            target_keys.add(identity_key(repository))
            target_keys.add(identity_key(repository.rsplit("/", 1)[-1]))
        target_keys.discard("")
        if not target_keys and context.get("task") in (None, "", "UNKNOWN"):
            target_keys.add(identity_key(query))
            target_keys.discard("")

        completeness_terms = (
            "install", "setup", "usage", "quickstart", "example", "demo", "tutorial",
            "workflow", "run ", "command", "documentation", "guide", "安装", "使用",
            "示例", "演示", "教程", "工作流", "运行", "命令", "说明",
        )

        def provider(row: dict[str, Any]) -> str:
            provenance = row.get("provenance") or {}
            provider_name = str(provenance.get("provider") if isinstance(provenance, dict) else "")
            source = " ".join((
                str(row.get("source_type") or ""), provider_name,
                str(row.get("url") or ""), str(row.get("model_id") or ""),
            )).casefold()
            if "huggingface" in source or "hugging face" in source:
                return "huggingface"
            if "github" in source:
                return "github"
            return "other"

        def metric(row: dict[str, Any], key: str) -> int | None:
            value = row.get(key)
            if value is None or isinstance(value, bool):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def popularity(row: dict[str, Any], source: str) -> tuple[int, ...]:
            if source == "github":
                stars = metric(row, "stargazers_count")
                return (int(stars is not None), stars if stars is not None else -1)
            if source == "huggingface":
                likes = metric(row, "likes")
                downloads = metric(row, "downloads_30d")
                return (int(likes is not None), likes if likes is not None else -1,
                        int(downloads is not None), downloads if downloads is not None else -1)
            return (0,)

        def semantic_score(row: dict[str, Any]) -> tuple[int, int, int]:
            title = str(row.get("title") or row.get("name") or "")
            title_key = identity_key(title)
            repositories = {str(value).casefold() for value in row.get("repository_identities") or []}
            repositories.update(
                value.casefold()
                for value in solution_scout.extract_repository_identities(_candidate_text(row))
            )
            structured_keys = {identity_key(title), identity_key(row.get("model_id"))}
            for repository in repositories:
                structured_keys.add(identity_key(repository))
                structured_keys.add(identity_key(repository.rsplit("/", 1)[-1]))
            structured_keys.discard("")
            first_party = bool(row.get("first_party") or row.get("authority") == "FIRST_PARTY")
            if repository_targets and repository_targets & repositories:
                target_match = 6
            elif target_keys & structured_keys:
                target_match = 5 if first_party else 4
            elif any(key and key in title_key for key in target_keys):
                target_match = 3
            elif any(key and key in identity_key(_candidate_text(row)) for key in target_keys):
                target_match = 1
            else:
                target_match = 0

            content = solution_scout._normalized_search_text(_candidate_text(row)).casefold()
            completeness = sum(1 for term in completeness_terms if term in content)
            evidence_type = str((row.get("enrichment") or {}).get("evidence_type") or "").upper()
            verification = str(row.get("capability_verification") or "").upper()
            if verification == "VERIFIED":
                evidence = 2
            elif evidence_type == "FETCH_FAILED":
                evidence = 0
            else:
                evidence = 1
            return target_match, completeness, evidence

        Entry = tuple[tuple[int, int, int], int, dict[str, Any], str, tuple[int, ...]]
        best_by_project: dict[str, Entry] = {}
        ungrouped: list[Entry] = []
        trailing: list[tuple[int, dict[str, Any]]] = []
        for position, original in enumerate(rows):
            row = dict(original)
            if row.get("relevance_class") != "RELEVANT":
                trailing.append((position, row))
                continue
            semantic = semantic_score(row)
            row["ranking_score"] = semantic[0] * 10000 + semantic[1] * 100 + semantic[2]
            row_provider = provider(row)
            row_popularity = popularity(row, row_provider)
            identity = cls._project_identity(row)
            if (row.get("source_type") == "First-party Website" or
                    (exact_repository_identity and row.get("first_party") and
                     exact_repository_identity in _candidate_repository_identities(row))):
                document_identity = str(
                    row.get("result_identity") or row.get("canonical_page_url") or row.get("url") or "")
                if document_identity:
                    identity = f"{identity}|first-party:{document_identity.casefold()}"
            entry = (semantic, position, row, row_provider, row_popularity)
            if not identity:
                ungrouped.append(entry)
                continue
            current = best_by_project.get(identity)
            if (current is None or semantic > current[0] or
                    (semantic == current[0] and row_provider == current[3]
                     and row_popularity > current[4])):
                best_by_project[identity] = entry
        ranked = [*best_by_project.values(), *ungrouped]
        ranked.sort(key=lambda item: tuple(-value for value in item[0]) + (item[1],))

        # Popularity is comparable only within one source and only after the
        # semantic tiers tie.  Refill that source's existing slots so GitHub
        # stars can never be numerically combined with Hugging Face metrics.
        start = 0
        while start < len(ranked):
            end = start + 1
            while end < len(ranked) and ranked[end][0] == ranked[start][0]:
                end += 1
            group = ranked[start:end]
            for source in ("github", "huggingface"):
                slots = [index for index, entry in enumerate(group) if entry[3] == source]
                source_entries = sorted(
                    (group[index] for index in slots), key=lambda entry: (entry[4], -entry[1]),
                    reverse=True,
                )
                for index, entry in zip(slots, source_entries):
                    group[index] = entry
            ranked[start:end] = group
            start = end
        ordered = [item[2] for item in ranked]
        if exact_repository_identity:
            authoritative = next((
                index for index, row in enumerate(ordered)
                if row.get("exact_repository_request") and
                exact_repository_identity in _candidate_repository_identities(row)
            ), None)
            if authoritative is not None and authoritative:
                ordered.insert(0, ordered.pop(authoritative))
        ordered.extend(row for _, row in trailing)
        return [dict(row, number=index, rank=index) for index, row in enumerate(ordered, 1)]

    def _plan_fallback(self, context: dict[str, Any]) -> dict[str, Any]:
        if self.planner is not None:
            return self.planner(context["original_query"], context["selected_hints"])
        if (getattr(self.controller, "dry_run", False) or
                not hasattr(self.controller, "tasks_root")):
            return query_planner._fallback(
                context["original_query"], context["selected_hints"], "UNAVAILABLE")
        return query_planner.plan_query(
            context["original_query"], context["selected_hints"],
            audit_log=Path(self.controller.tasks_root) / "query-planner-audit.jsonl",
        )

    @staticmethod
    def _classify(rows: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
        classified = []
        for row in rows:
            annotated = dict(row)
            annotated.update(classify_candidate(row, context))
            classified.append(annotated)
        return classified

    def _enrich_entity_matched_weak(self, rows: list[dict[str, Any]],
                                    context: dict[str, Any]) -> list[dict[str, Any]]:
        """Verify bounded README matches through the existing evidence retriever."""
        if not context.get("entity_required") and context.get("task") in (None, "", "UNKNOWN"):
            return rows
        requires_source_passage = context.get("task") not in (None, "", "UNKNOWN")
        required_entities = list(context.get("entities", []))
        updated = [dict(row) for row in rows]
        eligible = []
        for index, row in enumerate(updated):
            if not _github_repository(str(row.get("url") or "")):
                continue
            provenance = row.get("provenance") or {}
            readme_search = (isinstance(provenance, dict)
                             and provenance.get("provider") == "github_repository_search_api"
                             and "readme" in provenance.get("search_fields", []))
            if readme_search and requires_source_passage:
                records = list((row.get("enrichment") or {}).get("source_evidence", []))
                if records:
                    _apply_source_capability_verdict(row, context, records)
                if not row.get("enrichment"):
                    eligible.append(index)
                continue
            if row.get("relevance_class") == "RELEVANT":
                continue
            if readme_search or (row.get("relevance_class") == "WEAK"
                                 and not row.get("missing_entities")
                                 and not row.get("task_evidence")):
                eligible.append(index)
        for index in eligible[:MAX_SECOND_STAGE_CANDIDATES]:
            row = updated[index]
            documents, error = self._official_documents(row)
            if error:
                row["enrichment"] = {"evidence_type": "FETCH_FAILED", "source_evidence": [],
                                     "error": error}
                if requires_source_passage and row.get("relevance_class") == "RELEVANT":
                    row["capability_verification"] = "UNVERIFIED"
                    row["capability_notice_key"] = (
                        "results.source_read_failed_capability_unverified")
                continue
            evaluated = evaluate_enriched_evidence(documents, context)
            row["enrichment"] = evaluated
            row["source_evidence"] = evaluated["source_evidence"]
            _propagate_enriched_introduction(row, documents)
            evidence_row = dict(row, readme="\n".join(str(doc.get("text") or "") for doc in documents))
            row.update(classify_candidate(evidence_row, context))
            if requires_source_passage:
                _apply_source_capability_verdict(row, context, evaluated["source_evidence"])
        return updated

    def _official_documents(self, row: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
        """Fetch official evidence once per repository for this flow."""
        key = solution_scout.canonical_url(str(row.get("url") or ""))
        if key in self._enrichment_cache:
            return self._enrichment_cache[key]
        try:
            result = (list(self.candidate_enricher(row)), "")
        except Exception as exc:
            result = ([], f"{type(exc).__name__}: {exc}")
        self._enrichment_cache[key] = result
        return result

    def _ensure_detail_enrichment(self, row: dict[str, Any]) -> None:
        """Lazily add official source evidence to the selected result."""
        if _is_huggingface_model(row):
            try:
                model_id = _huggingface_model_id(row)
            except Exception as exc:
                result = {"status": HF_CARD_FETCH_FAILED, "model_id": "",
                          "source_url": "", "introduction": "",
                          "error": f"{type(exc).__name__}: {exc}"}
            else:
                result = self._huggingface_card_cache.get(model_id, {})
                if not result:
                    try:
                        result = dict(self.huggingface_card_reader(row))
                    except Exception as exc:
                        result = {"status": HF_CARD_FETCH_FAILED, "model_id": model_id,
                                  "source_url": "", "introduction": "",
                                  "error": f"{type(exc).__name__}: {exc}"}
                    if result.get("status") in {HF_CARD_READ_SUCCESS, HF_CARD_NO_CONTENT}:
                        self._huggingface_card_cache[model_id] = dict(result)
            status = str(result.get("status") or HF_CARD_FETCH_FAILED)
            row["huggingface_card_status"] = status
            row["huggingface_card_source_url"] = str(result.get("source_url") or "")
            row.pop("readme_intro", None)
            row.pop("purpose_source_evidence", None)
            row.pop("detail_enrichment_error", None)
            if status == HF_CARD_READ_SUCCESS:
                introduction = str(result.get("introduction") or "").strip()
                row["readme_intro"] = introduction
                row["purpose_source_evidence"] = [{
                    "source_kind": "MODEL_CARD_INTRODUCTION",
                    "source_url": str(result.get("source_url") or ""),
                    "text": introduction,
                }]
            elif status == HF_CARD_FETCH_FAILED:
                row["detail_enrichment_error"] = str(result.get("error") or
                                                       "Hugging Face Model Card read failed.")
            return
        if not _github_repository(str(row.get("url") or "")):
            if row.get("first_party"):
                return
            source_type = str(row.get("source_type") or "").casefold()
            role = str(row.get("role") or row.get("presentation_role") or "").casefold()
            if source_type not in {"youtube", "youtube post", "tutorial", "reference"} and not (
                    "tutorial" in role or "reference" in role):
                return
            if getattr(self.controller, "dry_run", False) and self.reference_reader is read_reference_project_links:
                return
            source_url = str(row.get("canonical_page_url") or row.get("url") or "")
            key = solution_scout.canonical_url(source_url)
            if key not in self._reference_cache:
                try:
                    self._reference_cache[key] = (self.reference_reader(row), "")
                except Exception as exc:
                    self._reference_cache[key] = ([], str(exc))
            links, error = self._reference_cache[key]
            if error:
                row["detail_enrichment_error"] = error
            else:
                row["project_links"] = list(dict.fromkeys([*(row.get("project_links") or []), *links]))
            return
        documents, error = self._official_documents(row)
        if error:
            row.setdefault("detail_enrichment_error", error)
            return
        _propagate_enriched_introduction(row, documents)
        if not row.get("source_evidence"):
            row["source_evidence"] = [dict(document) for document in documents]

    @staticmethod
    def _localized(language: str, key: str) -> str:
        return localization.Localizer(language).tr(key)

    @staticmethod
    def _call_language_worker(worker: Any, value: Any, language: str) -> str:
        """Support existing one-argument test doubles and language-aware workers."""
        try:
            parameters = inspect.signature(worker).parameters.values()
            positional = [item for item in parameters if item.kind in (
                inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            if len(positional) >= 2 or any(
                    item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters):
                return str(worker(value, language))
        except (TypeError, ValueError):
            pass
        return str(worker(value))

    def _local_purpose(self, evidence: dict[str, Any], language: str = "en") -> str:
        if self.purpose_summarizer is not None:
            return query_planner.extract_final_answer(
                self._call_language_worker(self.purpose_summarizer, evidence, language))
        if (getattr(self.controller, "dry_run", False) or
                not hasattr(self.controller, "tasks_root")):
            raise RuntimeError("Local purpose summarizer is unavailable.")
        prompt = (
            f"Write in {localization.SUPPORTED_LANGUAGES[language]}. Write a 1-2 sentence "
            "plain-language purpose for a normal user using only the "
            "official source evidence in the JSON below. Do not copy the source introduction "
            "verbatim. Do not add unsupported features, installation commands, or hardware "
            "requirements. If requested_capability_verification is "
            "UNVERIFIED, do not describe the requested capability as confirmed. Return only the purpose.\n\n" +
            json.dumps(evidence, ensure_ascii=False)
        )
        return query_planner.extract_final_answer(query_planner.invoke_local_model(
            prompt,
            audit_log=Path(self.controller.tasks_root) / "result-purpose-audit.jsonl",
            timeout=60,
            stage="RESULT_PURPOSE",
            think=False,
        ))

    def _purpose(self, row: dict[str, Any], language: str = "en") -> str:
        identity = solution_scout.canonical_url(str(row.get("url") or "")) or str(row.get("number") or "")
        evidence_hash = hashlib.sha256(json.dumps(
            _purpose_evidence(row), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        key = (identity, evidence_hash, language)
        if key not in self._purpose_cache:
            fallback = self._localized(language, "fallback.purpose")
            source_read_failed = self._localized(language, "fallback.purpose_source_read_failed")
            model_unavailable = self._localized(language, "fallback.purpose_model_unavailable")
            invalid_output = self._localized(language, "fallback.purpose_invalid_output")
            def log_failure(kind: str, reason: str) -> None:
                tasks_root = getattr(self.controller, "tasks_root", None)
                if not tasks_root:
                    return
                record = {
                    "timestamp": solution_scout.now(),
                    "stage": "RESULT_PURPOSE",
                    "status": kind,
                    "url": str(row.get("url") or ""),
                    "reason": reason,
                }
                path = Path(tasks_root) / "result-purpose-validation-audit.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._purpose_cache[key] = plain_language_explanation(
                row, lambda evidence: self._local_purpose(evidence, language), fallback,
                source_read_failed, model_unavailable, invalid_output, log_failure)
        return self._purpose_cache[key]

    def _local_translation(self, text: str, language: str) -> str:
        if self.translator is not None:
            return query_planner.extract_final_answer(
                self._call_language_worker(self.translator, text, language))
        if (getattr(self.controller, "dry_run", False) or
                not hasattr(self.controller, "tasks_root")):
            raise RuntimeError("Local translation is unavailable.")
        prompt = (
            f"Translate the source prose faithfully into {localization.SUPPORTED_LANGUAGES[language]}. "
            "Do not summarize, shorten, paraphrase, or add claims. Preserve paragraph structure and "
            "keep technical terms, model names, filenames, commands, URLs, version numbers, repository "
            "names, and product names unchanged where appropriate. Return only the translation.\n\n" + text
        )
        return query_planner.extract_final_answer(query_planner.invoke_local_model(
            prompt,
            audit_log=Path(self.controller.tasks_root) / "result-translation-audit.jsonl",
            timeout=90,
            stage="RESULT_TRANSLATION",
            think=False,
        ))

    def _translated_introduction(self, row: dict[str, Any], original: str,
                                 language: str) -> tuple[str, bool]:
        if language == "en":
            return original, False
        if original == INTRO_FALLBACK:
            return self._localized(language, "fallback.introduction"), False
        identity = solution_scout.canonical_url(str(row.get("url") or "")) or str(row.get("number") or "")
        source_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        key = (identity, source_hash, language)
        if key not in self._translation_cache:
            try:
                translated = self._local_translation(original, language).strip()
                word_count = len(re.findall(r"\w+", translated))
                cjk_character_count = len(re.findall(
                    r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", translated))
                # CJK translations are routinely much shorter in characters than
                # equivalent English prose. Validate intrinsic substance instead
                # of comparing output length with the source language.
                if (not translated or
                        translated.casefold() == original.casefold() or
                        (word_count < 3 and cjk_character_count < 8)):
                    translated = None
            except Exception:
                translated = None
            self._translation_cache[key] = translated
        translated = self._translation_cache[key]
        return (translated, True) if translated else (original, False)

    @staticmethod
    def _merge_rows(original: list[dict[str, Any]], supplemental: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen_groups: dict[str, int] = {}
        seen_urls: dict[str, int] = {}

        def source_records(row: dict[str, Any]) -> list[dict[str, Any]]:
            records = row.get("provenance_records") or row.get("discovery_sources") or []
            if isinstance(records, list) and records:
                return [dict(record) for record in records if isinstance(record, dict)]
            return [{
                "provider": str(row.get("source_type") or row.get("type") or "unknown"),
                "url": str(row.get("url") or ""),
            }]

        for row in [*original, *supplemental]:
            group_id = str(row.get("group_id") or "")
            url = solution_scout.canonical_url(str(row.get("url") or ""))
            duplicate_index = (seen_groups.get(group_id) if group_id else None)
            if duplicate_index is None and url:
                duplicate_index = seen_urls.get(url)
            if duplicate_index is not None:
                retained = merged[duplicate_index]
                records = list(retained.get("provenance_records") or source_records(retained))
                for record in source_records(row):
                    if record not in records:
                        records.append(record)
                retained["provenance_records"] = records
                retained["discovery_sources"] = records
                continue
            candidate = dict(row, number=len(merged) + 1)
            records = source_records(candidate)
            candidate["provenance_records"] = records
            candidate["discovery_sources"] = records
            index = len(merged)
            if group_id:
                seen_groups[group_id] = index
            if url:
                seen_urls[url] = index
            merged.append(candidate)
        return merged

    def card(self, row: dict[str, Any]) -> dict[str, Any]:
        role = row.get("presentation_role")
        if not role:
            source_kind = solution_scout.classify(str(row.get("url") or ""))
            is_github = source_kind == "github_repository"
            verified_official = bool(row.get("first_party") or
                                     str(row.get("authority") or "").upper() == "FIRST_PARTY")
            role = ("hf_item_unverified" if source_kind == "huggingface" else
                    "official_project" if is_github and verified_official else
                    "github_project" if is_github else "reference")
        return {
            "number": row.get("number"), "name": row.get("title") or "Unnamed solution",
            "preview": " ".join(_source_sentences(extract_introduction(row))[:1])[:240],
            "source_name": configured_first_party_website_name(row),
            "role": role,
            "source_platform": row.get("source_platform"),
            "stars": row.get("stars"), "stars_status": row.get("stars_status"),
            "likes": row.get("likes"), "likes_status": row.get("likes_status"),
            "downloads_30d": row.get("downloads_30d"),
            "downloads_30d_status": row.get("downloads_30d_status"),
        }

    def result_snapshot(self, number: int) -> dict[str, Any]:
        """Return the immutable result identity displayed for one result card."""
        row = self._sources.get(int(number))
        if not row:
            raise ValueError("That search result is no longer available.")
        return first_party_identity.normalize_content_identity(deepcopy(row))

    def solution_summary(self, number: int, language: str = "en", enrich: bool = True,
                         selected_result: dict[str, Any] | None = None) -> dict[str, Any]:
        row = first_party_identity.normalize_content_identity(
            deepcopy(selected_result) if selected_result is not None else self.result_snapshot(number))
        if enrich:
            self._ensure_detail_enrichment(row)
        curated = row.get("curated") if isinstance(row.get("curated"), dict) else {}
        project_links = list(row.get("project_links") or [])
        source_type = str(row.get("source_type") or "")
        is_youtube = source_type in {"YouTube", "YouTube Post"}
        is_website = source_type == "First-party Website"
        is_first_party = bool(row.get("first_party"))
        tutorial_url = str(row.get("tutorial_url") or row.get("video_url") or
                           curated.get("video_url") or "")
        publication_url = str(row.get("canonical_page_url") or row.get("url") or "")
        first_party_reference_url = str(row.get("first_party_reference_url") or
                                        ((tutorial_url or publication_url) if is_first_party else ""))
        project_resolution = _resolve_explicit_original_project(row)
        project_url = _resolved_install_project_url({"selected_result": row})
        source_url = str(row.get("canonical_page_url") or row.get("url") or "") if is_website else project_url
        source_labels = {
            "github_repository": "GitHub", "official_repository": "GitHub",
            "huggingface": "Hugging Face", "civitai": "Civitai",
            "documentation": "Documentation", "first_party_website": "First-party website",
            "web": "Original project",
        }
        project_source_label = source_labels.get(
            solution_scout.classify(project_url) if project_url else "", "Original project")
        language = localization.normalize_language(language)
        localizer = localization.Localizer(language)
        website_source_name = configured_first_party_website_name(row)
        website_source_label = localizer.tr(
            "results.first_party_website_source", source=website_source_name,
        ) if website_source_name else "First-party / Website"
        introduction_status = str(row.get("huggingface_card_status") or "")
        if introduction_status == HF_CARD_FETCH_FAILED:
            original_introduction = self._localized(
                language, "fallback.introduction_source_read_failed")
            display_introduction, translation_available = original_introduction, False
        else:
            original_introduction = extract_introduction(row)
            display_introduction, translation_available = self._translated_introduction(
                row, original_introduction, language)
        if curated and language == "en":
            purpose = curated.get("plain_language_description")
        else:
            purpose = self._purpose(row, language)
        notice_key = str(row.get("capability_notice_key") or "")
        if notice_key:
            notice = self._localized(language, notice_key)
            if notice and notice not in str(purpose or ""):
                purpose = f"{str(purpose or '').rstrip()} {notice}".strip()
        return {"number": number, "name": row.get("title") or "Unnamed solution",
                "selected_result": deepcopy(row),
                "related_sources": deepcopy(row.get("related_sources") or []),
                "plain_explanation": purpose,
                "introduction": original_introduction,
                "display_introduction": display_introduction,
                "introduction_status": introduction_status,
                "introduction_source_url": str(row.get("huggingface_card_source_url") or ""),
                "translation_available": translation_available,
                "translation_unavailable": (language != "en" and
                                            introduction_status != HF_CARD_FETCH_FAILED and
                                            original_introduction != INTRO_FALLBACK and
                                            not translation_available),
                "source_url": source_url,
                "project_url": project_url, "project_source_label": project_source_label,
                "project_resolution": project_resolution,
                "tutorial_url": tutorial_url,
                "first_party_reference_url": first_party_reference_url,
                "first_party_title": str(row.get("title") or "") if is_first_party else "",
                "curated_available": bool(curated),
                "video_url": tutorial_url,
                "video_title": str(row.get("title") if is_youtube else curated.get("video_title") or ""),
                "source_name": website_source_name or str(row.get("source_name") or ""),
                "source_platform": row.get("source_platform"),
                "content_role": row.get("content_role"),
                "stars": row.get("stars"), "stars_status": row.get("stars_status"),
                "likes": row.get("likes"), "likes_status": row.get("likes_status"),
                "downloads_30d": row.get("downloads_30d"),
                "downloads_30d_status": row.get("downloads_30d_status"),
                "popularity_lines": popularity_display_lines(row, localizer.tr),
                "source_label": (website_source_label if is_website else
                    "First-party / YouTube") if is_first_party else (
                    "YouTube" if is_youtube else
                    source_labels.get(str(row.get("type") or ""), "Original project")),
                "channel": str(row.get("channel") or ""), "project_links": project_links}

    def open_original_url(self, number: int) -> bool:
        """Open the cached source externally without changing browsing or job state."""
        return bool(webbrowser.open(self.solution_summary(number)["source_url"]))

    def open_curated_video(self, number: int) -> bool:
        video_url = self.solution_summary(number)["video_url"]
        return bool(video_url and webbrowser.open(video_url))

    def reset_browsing(self) -> None:
        self.results = []
        self._sources = {}
        self.search_context = {}
        self.zero_result_message = ZERO_RESULT_MESSAGE

    def prepare_install(self, displayed_selection: dict[str, Any]) -> dict[str, Any]:
        """Run only source reading, inventory/preflight, and planning."""
        summary = deepcopy(displayed_selection)
        row = deepcopy(summary.get("selected_result")) if isinstance(
            summary.get("selected_result"), dict) else {}
        row = first_party_identity.normalize_content_identity(row)
        reference_url = summary.get("first_party_reference_url") or summary.get("tutorial_url")
        entry = get_install_entry_state(summary)
        if entry.status != "AVAILABLE":
            return {"blocked": True, "issues": [entry.reason]}
        project_url = _resolved_install_project_url(summary)
        installable_types = {"github_repository", "huggingface", "civitai"}
        resolution = summary.get("project_resolution") if isinstance(
            summary.get("project_resolution"), dict) else {}
        project_type = solution_scout.classify(project_url) if project_url else ""
        selected_url = str(row.get("url") or "")
        if ((project_type in installable_types and not row) or
                (solution_scout.classify(selected_url) in installable_types and not project_url)):
            return {"blocked": True, "issues": [
                "Internal handoff error: the selected installable result lost its canonical source; "
                "no job was created."]}
        owner, repo = solution_scout.github_identity(project_url)
        repository = f"{owner}/{repo}" if owner else str(
            resolution.get("repository") or row.get("repository") or row.get("repo") or "UNKNOWN")
        revision = next((str(row.get(key)) for key in ("commit", "exact_revision", "revision", "tag")
                         if row.get(key) not in (None, "", "UNKNOWN")), "UNKNOWN")
        selected_reference = ({
            "url": str(reference_url),
            "canonical_url": solution_scout.canonical_url(str(reference_url)),
            "title": str(summary.get("first_party_title") or summary.get("video_title") or
                         row.get("title") or ""),
            "publication_identity": str(row.get("publication_identity") or
                                        row.get("result_identity") or
                                        solution_scout.canonical_url(str(reference_url))),
            "source_type": str(row.get("source_type") or ""),
            "source_name": str(summary.get("source_name") or row.get("source_name") or
                               row.get("site_name") or ""),
            "provenance": dict(row.get("provenance") or {}),
        } if reference_url else None)
        install_source = {
            "url": project_url,
            "canonical_url": solution_scout.canonical_url(project_url),
            "repository_identity": repository,
            "source_type": project_type,
            "document_type": project_type,
            "exact_revision": revision,
            "resolution_evidence_field": str(resolution.get("evidence_field") or ""),
        }
        install_target = dict(row)
        install_target.update({
            "url": project_url,
            "canonical_url": project_url,
            "canonical_source_url": solution_scout.canonical_url(project_url),
            "repository": repository,
            "repo": repository,
            "source_type": project_type,
            "document_type": project_type,
            "verified_original_project_url": project_url,
            "exact_revision": revision,
            "result_identity": solution_scout.canonical_url(project_url),
            "provenance": dict(row.get("provenance") or {}),
            "selected_reference": selected_reference,
            "reference_source": selected_reference,
            "install_source": install_source,
        })
        job_id = getattr(self.controller, "task_id", None)
        jobs_root = getattr(self.controller, "jobs_root", None)
        job_path = (Path(jobs_root) / str(job_id) / "job.json"
                    if jobs_root is not None and job_id is not None else None)
        handoff_debug.trace(
            "STRUCTURED_TARGET",
            job_id=job_id,
            job_path=str(job_path) if job_path else None,
            job_already_exists=bool(job_path and job_path.exists()),
            structured_install_target=install_target,
        )
        if project_type in installable_types and not install_target["canonical_source_url"]:
            return {"blocked": True, "issues": [
                "Internal handoff error: the selected installable result lost its canonical source; "
                "no job was created."]}
        self.controller.select_result(install_target)
        persisted_after_selection = (product_cli.read_json(job_path)
                                     if job_path and job_path.exists() else {})
        handoff_debug.trace(
            "JOB_CREATED",
            job_id=job_id,
            job_path=str(job_path) if job_path else None,
            job_exists=bool(job_path and job_path.exists()),
            selected_sources=persisted_after_selection.get("selected_sources"),
        )
        records = self.controller.read_selected()
        source = records[0] if records else {}
        if (source.get("fetch_error") or not source.get("url") or
                source.get("commit") in (None, "", "UNKNOWN")):
            return {"blocked": True, "issues": ["The original source or exact revision could not be verified."]}
        self.controller.preflight()
        persisted_job = product_cli.read_json(job_path) if job_path and job_path.exists() else {}
        verification = (persisted_job.get("preflight") or {}).get("source_verification") or {}
        handoff_debug.trace(
            "PREFLIGHT_RESULT",
            job_id=job_id,
            persisted_url=verification.get("url"),
            persisted_repo=verification.get("repository"),
            persisted_revision=verification.get("exact_revision"),
            selected_sources=persisted_job.get("selected_sources"),
            verified_source=persisted_job.get("verified_source"),
            job_state=persisted_job.get("state"),
        )
        plan = self.controller.build_plan()
        return self.confirmation(plan)

    @staticmethod
    def _planned_write_targets(plan: dict[str, Any]) -> list[str]:
        targets: list[str] = []
        for row in plan.get("planned_downloads", []):
            value = row.get("destination")
            if value:
                targets.append(str(value))
        for row in plan.get("planned_modifications", []):
            value = row.get("path") or row.get("destination")
            if value:
                targets.append(str(value))
        write_fields = {
            "DOWNLOAD": "destination", "COPY_FILE": "destination",
            "INSTALL_SOURCE": "destination", "CREATE_DIRECTORY": "path",
            "ACQUIRE_GITHUB_SNAPSHOT": "destination",
        }
        for step in plan.get("steps", []):
            field = write_fields.get(str(step.get("type") or ""))
            value = (step.get("inputs") or {}).get(field) if field else None
            if value:
                targets.append(str(value))
        return list(dict.fromkeys(targets))

    @staticmethod
    def _unknown_write_destinations(plan: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for index, row in enumerate(plan.get("planned_downloads", []), 1):
            if not row.get("destination"):
                missing.append(f"planned download {index}")
        for index, row in enumerate(plan.get("planned_modifications", []), 1):
            if not (row.get("path") or row.get("destination")):
                missing.append(f"planned modification {index}")
        write_fields = {
            "DOWNLOAD": "destination", "COPY_FILE": "destination",
            "INSTALL_SOURCE": "destination", "CREATE_DIRECTORY": "path",
            "ACQUIRE_GITHUB_SNAPSHOT": "destination",
        }
        for step in plan.get("steps", []):
            field = write_fields.get(str(step.get("type") or ""))
            if field and not (step.get("inputs") or {}).get(field):
                missing.append(str(step.get("step_id") or step.get("type") or "write step"))
        return missing

    @staticmethod
    def _inside(path: str | Path, root: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError):
            return False

    def select_destination(self, destination: str | Path) -> dict[str, Any]:
        """Rebase all new writes into an explicitly selected existing folder."""
        selected = Path(destination).resolve()
        if not selected.is_absolute() or not selected.is_dir():
            raise ValueError("Choose an existing destination folder.")
        store = product_cli.JobStore(self.controller.jobs_root, self.controller.task_id)
        plan = store.load("plan.json", {})
        if not plan:
            raise ValueError("Prepare the installation before choosing a destination.")
        state = store.load("execution_state.json", {})
        if any(step.get("status") == "SUCCESS" for step in state.get("steps", [])):
            raise ValueError("The destination cannot change after installation has started.")
        repository_names: set[str] = set()
        for step in plan.get("steps", []):
            if step.get("type") != "ACQUIRE_GITHUB_SNAPSHOT":
                continue
            name = str((step.get("inputs") or {}).get("destination_name") or "")
            if not name or Path(name).name != name:
                raise ValueError("The repository destination name is invalid.")
            repository_names.add(name)
        if any(selected.name.casefold() == name.casefold() for name in repository_names):
            raise ValueError(
                f"Choose the parent installation root, not the repository folder '{selected.name}'.")
        for step in plan.get("steps", []):
            if step.get("type") != "ACQUIRE_GITHUB_SNAPSHOT":
                continue
            inputs = step.get("inputs") or {}
            if inputs.get("destination"):
                continue
            name = str(inputs.get("destination_name") or "")
            if not name or Path(name).name != name:
                raise ValueError("The repository destination name is invalid.")
            target = str((selected / name).resolve())
            inputs["destination"] = target
            step["expected_result"]["destination"] = target
            if isinstance(step.get("rollback"), dict):
                step["rollback"]["delete_created_directory"] = target
            for row in plan.get("planned_downloads", []):
                if (row.get("type") == "repository_source"
                        and row.get("repository") == inputs.get("repository")):
                    row["destination"] = target
                    row["expected_new_directories"] = [target]
                    row["expected_new_files"] = [f"verified snapshot contents under {target}"]
        targets = self._planned_write_targets(plan)
        if self._unknown_write_destinations(plan) or not targets:
            raise ValueError("Every planned new write must have a known destination.")
        paths = [Path(value).resolve() for value in targets]
        common_parent = Path(os.path.commonpath([str(path.parent) for path in paths]))
        mapping: dict[str, str] = {}
        used: set[str] = set()
        for old in paths:
            try:
                relative = old.relative_to(common_parent)
            except ValueError as exc:
                raise ValueError("A planned destination cannot be safely rebased.") from exc
            new = (selected / relative).resolve()
            if not self._inside(new, selected):
                raise ValueError("A planned destination escapes the selected folder.")
            key = os.path.normcase(str(old))
            if os.path.normcase(str(new)) in used and mapping.get(key) != str(new):
                raise ValueError("Two planned writes resolve to the same destination.")
            mapping[key] = str(new)
            used.add(os.path.normcase(str(new)))

        def replace(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, str):
                return mapping.get(os.path.normcase(str(Path(value).resolve())), value)
            return value

        plan = replace(plan)
        plan["normal_ui_destination"] = str(selected)
        plan["plan_version"] = int(plan.get("plan_version") or 0) + 1
        for step in plan.get("steps", []):
            step["approved"] = False
            if step.get("status") != "SUCCESS":
                step["status"] = "PENDING"
        unresolved = [value for value in self._planned_write_targets(plan)
                      if not self._inside(value, selected)]
        if unresolved:
            raise ValueError("Every planned new write must resolve under the selected folder.")
        store.save("plan.json", plan)
        job = store.job()
        job["approval"] = None
        job["state"] = "WAITING_FOR_APPROVAL"
        store.save_job(job)
        self.controller.plan = plan
        self.controller.approval = None
        return self.confirmation(plan)

    def confirmation(self, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        plan = plan or self.controller.plan or {}
        completed_acquisition = install_research.find_completed_acquisition(
            Path(self.controller.jobs_root), plan, exclude_job_id=self.controller.task_id)
        if not completed_acquisition:
            completed_acquisition = install_research.find_completed_acquisition(
                Path(self.controller.jobs_root), plan)
        acquisition_ambiguous = bool(completed_acquisition and
                                     completed_acquisition.get("error_code") == "MULTIPLE_COMPLETED_ACQUISITIONS")
        acquisition_complete = bool(completed_acquisition and not acquisition_ambiguous)
        downloads = plan.get("planned_downloads", [])
        sizes = [x.get("size") for x in downloads]
        total = sum(sizes) if all(isinstance(x, int) and x >= 0 for x in sizes) else None
        selected = str(plan.get("normal_ui_destination") or "")
        destinations = [selected] if selected else []
        blocking_issues = [str(x) for x in plan.get("block_reasons", [])]
        blocking_issues.extend(self._plain_conflict(x) for x in plan.get("cross_project_conflicts", []))
        issues = list(blocking_issues)
        if acquisition_ambiguous:
            choices = ", ".join(row["local_path"] for row in completed_acquisition.get("candidates", []))
            issue = "Multiple verified source acquisitions were found. Choose the installation folder to analyze"
            issues.append(issue + (f": {choices}" if choices else "."))
            blocking_issues.append("MULTIPLE_COMPLETED_ACQUISITIONS")
        if self._unknown_write_destinations(plan):
            issue = "The exact destination for a required download is unknown."
            issues.append(issue)
            if selected:
                blocking_issues.append(issue)
        write_targets = self._planned_write_targets(plan)
        if selected and any(not self._inside(value, selected) for value in write_targets):
            issue = "A planned write is outside the selected destination."
            issues.append(issue)
            blocking_issues.append(issue)
        if not acquisition_complete:
            for step in plan.get("steps", []):
                if step.get("type") != "ACQUIRE_GITHUB_SNAPSHOT":
                    continue
                repository_destination = str((step.get("inputs") or {}).get("destination") or "")
                if repository_destination and Path(repository_destination).exists():
                    issue = "The resolved repository destination already exists; choose another installation root."
                    issues.append(issue)
                    blocking_issues.append(issue)
                    break
        if plan.get("status") != "READY_FOR_APPROVAL" and not issues:
            issue = "Installation is not ready for automatic approval."
            issues.append(issue)
            blocking_issues.append(issue)
        free_space = None
        if selected:
            try:
                free_space = shutil.disk_usage(selected).free
            except OSError:
                issue = "Available disk space could not be determined."
                issues.append(issue)
                blocking_issues.append(issue)
        insufficient = total is not None and free_space is not None and total > free_space
        if insufficient:
            issue = "The selected destination does not have enough free space."
            issues.append(issue)
            blocking_issues.append(issue)
        destination_ready = bool(selected and write_targets and not any(
            not self._inside(value, selected) for value in write_targets))
        displayed_downloads = []
        for download in downloads:
            label = download.get("name") or download.get("identifier") or "Unknown download"
            displayed_downloads.append(label)
        return {
            "blocked": bool(blocking_issues), "issues": issues,
            "downloads": displayed_downloads,
            "download_size": total, "download_size_text": "Unknown" if total is None else self.format_size(total),
            "locations": destinations, "models": [x.get("name") or x.get("identifier") for x in downloads if x.get("model")],
            "write_locations": write_targets,
            "reused_count": len(plan.get("local_assets_to_reuse", [])),
            "reused_assets": [str(x.get("path") or x.get("identifier") or "") for x in plan.get("local_assets_to_reuse", [])],
            "selected_destination": selected, "destination_selected": bool(selected),
            "drive": Path(selected).drive if selected else "",
            "free_space": free_space,
            "free_space_text": self.format_size(free_space) if free_space is not None else "Unknown",
            "insufficient_space": insufficient,
            "source_acquisition_complete": acquisition_complete,
            "acquisition_ambiguous": acquisition_ambiguous,
            "acquisition_candidates": ((completed_acquisition or {}).get("candidates", [])
                                       if acquisition_ambiguous else []),
            "acquired_source_path": (completed_acquisition or {}).get("local_path") if acquisition_complete else None,
            "acquisition_job_id": (completed_acquisition or {}).get("job_id") if acquisition_complete else None,
            "start_allowed": destination_ready and not issues and not completed_acquisition,
            "analysis_allowed": acquisition_complete or (
                destination_ready and not issues and not completed_acquisition),
        }

    @staticmethod
    def _destination_for(download: dict[str, Any], plan: dict[str, Any]) -> str | None:
        ident = download.get("identifier")
        for row in plan.get("planned_modifications", []):
            path = row.get("path") or row.get("destination")
            if path and (not ident or Path(path).name == ident):
                return str(path)
        return None

    @staticmethod
    def _plain_conflict(row: Any) -> str:
        if isinstance(row, dict):
            kind = str(row.get("type", "conflict")).replace("_", " ")
            detail = row.get("service") or row.get("name") or row.get("port") or row.get("detail")
            return f"A {kind} was found" + (f": {detail}." if detail else ".")
        return str(row)

    @staticmethod
    def format_size(value: int) -> str:
        units = ("B", "KB", "MB", "GB", "TB")
        size = float(value)
        unit = units[0]
        for unit in units:
            if size < 1024 or unit == units[-1]:
                break
            size /= 1024
        return f"{int(size)} {unit}" if size.is_integer() else f"{size:.1f} {unit}"

    def analyze_installation(self, progress: Any = None, model_invoke: Any = None) -> dict[str, Any]:
        """Research the acquired source without executing repository content."""
        store = product_cli.JobStore(self.controller.jobs_root, self.controller.task_id)
        completed = install_research.find_completed_acquisition(
            Path(self.controller.jobs_root), store.load("plan.json", {}),
            exclude_job_id=self.controller.task_id)
        if not completed:
            completed = install_research.find_completed_acquisition(
                Path(self.controller.jobs_root), store.load("plan.json", {}))
        if not completed:
            raise install_research.InstallResearchError(
                "SOURCE_ACQUISITION_INCOMPLETE", "Verified source acquisition has not completed.")
        if completed.get("error_code") == "MULTIPLE_COMPLETED_ACQUISITIONS":
            raise install_research.InstallResearchError(
                "MULTIPLE_COMPLETED_ACQUISITIONS",
                "Multiple verified source acquisitions were found; choose the installation folder to analyze.")
        options: dict[str, Any] = {"progress": progress}
        if model_invoke is not None:
            options["model_invoke"] = model_invoke
        return install_research.analyze_job(
            Path(self.controller.jobs_root), self.controller.task_id,
            acquisition_job_id=completed["job_id"],
            expected_source_path=completed["local_path"], **options)

    def installation_research(self) -> dict[str, Any]:
        return install_research.load_research(Path(self.controller.jobs_root), self.controller.task_id)

    def confirm_research_confidence(self, choices: dict[str, str]) -> dict[str, Any]:
        return install_research.confirm_confidence_review(
            Path(self.controller.jobs_root), self.controller.task_id, choices)

    def build_installation_plan(self) -> dict[str, Any]:
        return installation_plan.build_job_plan(Path(self.controller.jobs_root), self.controller.task_id)

    def installation_plan(self) -> dict[str, Any]:
        return installation_plan.load_job_plan(Path(self.controller.jobs_root), self.controller.task_id)

    def installation_plan_approval(self) -> dict[str, Any] | None:
        return self.controller.installation_plan_approval()

    def approve_installation_plan(self) -> dict[str, Any]:
        return self.controller.approve_installation_plan()

    def execute_installation_plan(self, handlers: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.controller.execute_installation_plan(handlers=handlers)

    def resume_installation_plan(self, approve_deviation: bool = False,
                                 handlers: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.controller.resume_installation_plan(
            approve_deviation=approve_deviation, handlers=handlers)

class NormalProductApp:
    """Small Tk UI that reveals technical detail only on demand."""

    RESULT_LIST_WEIGHT = 2
    RESULT_PREVIEW_WEIGHT = 3
    RESULT_LIST_WIDTH = 340
    _JOB_STATE_RENDERERS = {
        JobLifecycleState.SOURCE_SELECTED: "_render_source_selected",
        JobLifecycleState.SOURCE_VERIFIED: "_render_source_verified",
        JobLifecycleState.DESTINATION_SELECTED: "_render_destination_selected",
        JobLifecycleState.SOURCE_ACQUIRED: "_render_source_acquired",
        JobLifecycleState.RESEARCH_COMPLETE: "_render_research_complete",
        JobLifecycleState.PLAN_REVIEW: "_render_plan_review",
        JobLifecycleState.APPROVED: "_render_approved",
        JobLifecycleState.EXECUTING: "_render_executing",
        JobLifecycleState.STOPPED: "_render_stopped",
        JobLifecycleState.RESUMABLE: "_render_resumable",
        JobLifecycleState.VERIFIED: "_render_verified",
    }

    def __init__(self, root: Any, controller: Any, developer_app: Any,
                 locale_service: localization.Localizer | None = None,
                 developer_enabled: bool | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.root, self.controller, self.flow, self.developer_app = root, controller, NormalFlow(controller), developer_app
        if developer_enabled is None:
            developer_enabled = bool(getattr(controller, "_developer_enabled", False))
        self.developer_enabled = bool(developer_enabled)
        self.localizer = locale_service or localization.Localizer()
        self.tk, self.ttk = tk, ttk
        width = min(1480, max(960, root.winfo_screenwidth() - 80))
        height = min(900, max(640, root.winfo_screenheight() - 100))
        root.geometry(f"{width}x{height}")
        root.minsize(min(1000, width), min(680, height))
        root.title(f"{product_config.APP_NAME} — {self.localizer.tr('app.subtitle')}")
        self._configure_styles()
        self._build_shell()
        self.selected_number: int | None = None
        self._detail_request_id = 0
        self._show_original_introductions: set[tuple[int, str]] = set()
        self.result_rows: dict[int, Any] = {}
        self.preview_panel: Any | None = None
        self.confirmation_data: dict[str, Any] | None = None
        self.selected_hints: list[str] = []
        self.hint_buttons: dict[str, Any] = {}
        self.hint_status: Any | None = None
        self.search_text = ""
        self.current_summary_data: dict[str, Any] | None = None
        self._restored_summary: dict[str, Any] | None = None
        self.current_view: tuple[str, Any] = ("home", None)
        root.title(f"{product_config.APP_NAME} — {self.tr('app.subtitle')}")
        self.home(reset=False)

    def render_job_state(self, job_id: str) -> JobContext | None:
        """Open one persisted job and render solely from its canonical lifecycle state."""
        try:
            context = self.controller.open_job(job_id)
        except JobContextValidationError as exc:
            self.show_pre_execution_restore_failure(
                job_id, str(exc), self.controller.failure_handoff() or {})
            return None
        renderer_name = self._JOB_STATE_RENDERERS.get(context.state)
        if renderer_name is None:
            raise ValueError(f"Persisted job state cannot be rendered here: {context.state.value}")
        self._active_job_context = context
        getattr(self, renderer_name)(context)
        return context

    def _render_source_selected(self, context: JobContext) -> None:
        self.show_persisted_source(context)

    def _render_source_verified(self, _context: JobContext) -> None:
        self.confirmation_data = self.flow.confirmation()
        self.show_confirmation()

    def _render_destination_selected(self, _context: JobContext) -> None:
        self.confirmation_data = self.flow.confirmation()
        self.show_confirmation()

    def _render_source_acquired(self, _context: JobContext) -> None:
        research = self.flow.installation_research()
        if research.get("status") == "BLOCKED":
            self.show_installation_research_blocked(research)
        elif research.get("status") == "NEEDS_REVIEW":
            self.show_installation_confidence_review(research)
        else:
            self.success()

    def _render_research_complete(self, _context: JobContext) -> None:
        self.show_installation_research()

    def _render_plan_review(self, _context: JobContext) -> None:
        self.show_installation_plan()

    def _render_approved(self, _context: JobContext) -> None:
        self.show_approved_installation_plan()

    def _render_executing(self, context: JobContext) -> None:
        if "installation_execution_state.json" not in context.artifact_names:
            self.show_source_execution_lifecycle(context)
        else:
            self.show_installation_lifecycle(self._persisted_execution_result(context))

    def _persisted_execution_result(self, context: JobContext) -> dict[str, Any]:
        store = product_cli.JobStore(self.controller.jobs_root, context.job_id)
        state = (store.load("installation_execution_state.json", {})
                 or store.load("execution_state.json", {}))
        result = dict(store.load("result.json", {}))
        result["execution_state"] = dict(state)
        return result

    def _render_stopped(self, context: JobContext) -> None:
        if "installation_execution_state.json" not in context.artifact_names:
            self.show_source_execution_lifecycle(context)
        else:
            self.show_installation_lifecycle(self._persisted_execution_result(context))

    def _render_resumable(self, context: JobContext) -> None:
        self.show_installation_lifecycle(self._persisted_execution_result(context))

    def _render_verified(self, context: JobContext) -> None:
        if "installation_execution_state.json" in context.artifact_names:
            self.show_installation_lifecycle(self._persisted_execution_result(context))
        else:
            self.success()

    def show_source_execution_lifecycle(self, context: JobContext) -> None:
        state = product_cli.JobStore(self.controller.jobs_root, context.job_id).load(
            "execution_state.json", {})
        stopped = context.state is JobLifecycleState.STOPPED
        self.current_view = ("source_execution_lifecycle", context.state.value)
        self.clear(); t = self.ttk
        self.nav(lambda: self.home(reset=False))
        title = self.tr("plan.lifecycle.stopped") if stopped else self.tr("install.preparing")
        t.Label(self.container, text=title, style="PageTitle.TLabel").pack(anchor="w")
        details = source_execution_lifecycle_details(context.job_id, state, self.tr)
        t.Label(self.container, text="\n".join(details), justify="left",
                style="Muted.TLabel").pack(anchor="w", pady=(12, 0))
        if not stopped and not state.get("cancel_requested"):
            t.Button(self.container, text=self.tr("install.stop"),
                     command=self.stop).pack(anchor="w", pady=(12, 0))

    def _current_job_id(self) -> str:
        job_id = getattr(self.controller, "task_id", None)
        if not job_id:
            raise SystemExit("No persisted job is open.")
        return str(job_id)

    def _reload_job_state(self) -> JobContext | None:
        return self.render_job_state(self._current_job_id())

    def show_persisted_source(self, context: JobContext) -> None:
        """Present durable source identity without reconstructing a search result row."""
        self.current_view = ("persisted_source", context.job_id)
        self.clear(); t = self.ttk
        self.nav(lambda: self.home(reset=False))
        reference = context.selected_reference or {}
        source = context.install_source or {}
        title = reference.get("title") or reference.get("publication_identity") or context.job_id
        url = reference.get("canonical_url") or source.get("canonical_url") or ""
        repository = context.repository_identity or ""
        revision = context.exact_revision or ""
        t.Label(self.container, text=title, style="PageTitle.TLabel").pack(anchor="w")
        details = "\n".join(value for value in (url, repository, revision) if value)
        t.Label(self.container, text=details, justify="left", wraplength=760,
                style="Muted.TLabel").pack(anchor="w", pady=(12, 0))

    def tr(self, key: str, **values: Any) -> str:
        return self.localizer.tr(key, **values)

    def language_selector(self, parent: Any) -> None:
        selector = self.ttk.Combobox(
            parent, state="readonly", width=13,
            values=list(localization.SUPPORTED_LANGUAGES.values()),
        )
        selector.set(self.localizer.display_name)
        selector.bind("<<ComboboxSelected>>", lambda _event, widget=selector: self.change_language(
            localization.Localizer.code_for_display_name(widget.get())))
        selector.pack(side="right", padx=(8, 0))

    def change_language(self, language: str) -> None:
        """Persist language and redraw only the current Normal Mode page."""
        self.localizer.set_language(language)
        self.sidebar_home.configure(text=self.tr("search.button"))
        self.sidebar_about.configure(text=self.tr("header.about"))
        self.root.title(f"{product_config.APP_NAME} — {self.tr('app.subtitle')}")
        view, value = self.current_view
        if view == "results":
            self.results()
        elif view == "summary" and value is not None:
            self.summary(int(value))
        elif view == "confirmation":
            self.show_confirmation()
        elif view == "installing":
            self.installing()
        elif view == "success":
            self.success()
        elif view in {"pre_execution_operation_failure", "installation_step_failure"}:
            self._reload_job_state()
        elif view in {"installation_plan_lifecycle", "source_execution_lifecycle", "persisted_source"}:
            self._reload_job_state()
        elif view == "job_history":
            selection = self.history_jobs.selection()
            self.show_job_history(selection[0] if selection else None)
        elif view == "pre_execution_state_restore_failure":
            self.show_pre_execution_restore_failure(
                str(value), self.pre_execution_failure.get("error", ""), self.pre_execution_failure)
        elif view == "installation_research_result":
            self.show_installation_research()
        elif view == "installation_confidence_review":
            self.show_installation_confidence_review(self.flow.installation_research())
        elif view == "installation_plan_review":
            self.show_installation_plan()
        elif view == "installation_cloud_report":
            self.show_installation_cloud_report()
        elif view == "installation_plan_approved":
            self.show_approved_installation_plan()
        else:
            self.home(reset=False)

    def _configure_styles(self) -> None:
        self.ui_colors = win11_theme.apply(self.root)

    def _build_shell(self) -> None:
        t = self.ttk
        self.shell = t.Frame(self.root, style="Shell.TFrame", padding=(12, 12, 16, 16))
        self.shell.pack(fill="both", expand=True)
        self.shell.columnconfigure(1, weight=1)
        self.shell.rowconfigure(0, weight=1)
        self.sidebar = t.Frame(self.shell, style="Shell.TFrame", width=196, padding=(8, 12, 16, 8))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(3, weight=1)
        t.Label(self.sidebar, text="Local AI", style="Brand.Shell.TLabel").grid(
            row=0, column=0, sticky="w", padx=12, pady=(4, 0))
        t.Label(self.sidebar, text="Companion", style="Muted.Shell.TLabel").grid(
            row=1, column=0, sticky="w", padx=12, pady=(2, 28))
        self.sidebar_home = t.Button(self.sidebar, text=self.tr("search.button"),
                                    style="Selected.Nav.TButton", command=self.home)
        self.sidebar_home.grid(row=2, column=0, sticky="ew")
        self.sidebar_about = t.Button(self.sidebar, text=self.tr("header.about"),
                                     style="Nav.TButton", command=self.about)
        self.sidebar_about.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        t.Label(self.sidebar, text=f"v{product_config.APP_VERSION}",
                style="Muted.Shell.TLabel").grid(row=5, column=0, sticky="w", padx=12)
        self.container = t.Frame(self.shell, style="Card.TFrame", padding=(24, 20, 24, 20))
        self.container.grid(row=0, column=1, sticky="nsew")
        self._sidebar_visible = True
        self.root.bind("<Configure>", self._resize_shell, add="+")

    def _resize_shell(self, event: Any) -> None:
        if event.widget is not self.root or not self.sidebar.winfo_exists():
            return
        visible = event.width >= 1200
        if visible != self._sidebar_visible:
            self._sidebar_visible = visible
            if visible:
                self.sidebar.grid()
            else:
                self.sidebar.grid_remove()

    def _fit_wrapped_labels(self, parent: Any) -> None:
        """Keep prose inside the pane at small window widths and high scaling."""
        previous = getattr(parent, "_ornith_wrap_handler", None)
        if previous is not None:
            parent.after_idle(previous)
            return

        def resize(event: Any = None) -> None:
            if not parent.winfo_exists() or (event is not None and event.widget is not parent):
                return
            # Leave room for pane padding and a nested vertical scrollbar.
            width = max(120, parent.winfo_width() - 40)
            pending = list(parent.winfo_children())
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if isinstance(widget, self.ttk.Label):
                    current = int(widget.cget("wraplength") or 0)
                    if current and current != width:
                        widget.configure(wraplength=width)
        parent._ornith_wrap_handler = resize
        parent.bind("<Configure>", resize, add="+")
        parent.after_idle(resize)

    def clear(self) -> None:
        for child in self.container.winfo_children(): child.destroy()

    def _scrollable_content(self, parent: Any) -> Any:
        """Give a page its remaining space while keeping long content reachable."""
        canvas = self.tk.Canvas(parent, highlightthickness=0, borderwidth=0, background=self.ui_colors["surface"])
        scrollbar = self.ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = self.ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        def wheel(event: Any) -> str | None:
            first, last = canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return None
            units = (-1 if event.num == 4 else 1) if event.num in (4, 5) else (
                -1 if event.delta > 0 else 1)
            canvas.yview_scroll(units, "units")
            return "break"
        def bind_children() -> None:
            if not content.winfo_exists():
                return
            pending = [canvas, content]
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if isinstance(widget, (self.tk.Text, self.ttk.Combobox)):
                    continue
                for event_name in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    widget.bind(event_name, wheel, add="+")
        content.after_idle(bind_children)
        return content

    def home(self, reset: bool = True) -> None:
        self.current_view = ("home", None)
        if reset:
            self.flow.reset_browsing()
            self.selected_number = None
            self.selected_hints = []
            self.search_text = ""
        self.clear(); t = self.ttk
        self.header(home_button=False)
        viewport = t.Frame(self.container)
        viewport.pack(fill="both", expand=True)
        content = self._scrollable_content(viewport)
        self._fit_wrapped_labels(content)
        t.Label(content, text=self.tr("home.title"), style="Title.TLabel", wraplength=900, justify="left").pack(anchor="w", pady=(12, 0))
        t.Label(content, text=self.tr("home.subtitle"),
                style="Subtitle.TLabel", wraplength=900, justify="left").pack(anchor="w", pady=(8, 24))
        if self.controller.dry_run:
            t.Label(content, text=self.tr("home.demo"), style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        search_row = t.Frame(content)
        search_row.pack(fill="x")
        self.goal = t.Entry(search_row, font=(self.ui_colors["font"], 12)); self.goal.pack(side="left", fill="x", expand=True, ipady=2)
        if self.search_text:
            self.goal.insert(0, self.search_text)
        self.search_button = t.Button(search_row, text=self.tr("search.button"),
                                      style="Primary.TButton", command=self.search)
        self.search_button.pack(side="left", padx=(10, 0), ipady=1)
        self.history_button = t.Button(search_row, text=self.tr("history.title"),
                                       command=self.show_job_history)
        self.history_button.pack(side="left", padx=(10, 0), ipady=1)
        t.Label(content, text=self.tr("hints.label"), style="Section.TLabel").pack(anchor="w", pady=(24, 10))
        hint_area = t.Frame(content); hint_area.pack(fill="x")
        self.hint_buttons = {}
        for index, hint in enumerate(SEARCH_HINTS):
            button = t.Button(hint_area, text=self.tr(SEARCH_HINT_KEYS[hint]), style="Chip.TButton",
                              command=lambda h=hint: self.toggle_hint(h))
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 8), pady=4)
            self.hint_buttons[hint] = button
        for column in range(3):
            hint_area.columnconfigure(column, weight=1, uniform="hints")
        self.goal.bind("<Return>", lambda _event: self.search()
                       if not self.search_button.instate(["disabled"]) else None)
        self.hint_status = t.Label(content, text=self.tr("hints.none"), style="Muted.TLabel")
        self.hint_status.pack(anchor="w", pady=(6, 0))
        self._refresh_hint_buttons()
        self.source_directory(content)
        footer = t.Frame(self.container)
        footer.pack(fill="x", side="bottom", pady=(8, 0))
        if self.developer_enabled:
            t.Button(footer, text=self.tr("header.developer_mode"), style="Footer.TButton",
                     command=self.developer).pack(side="right")

    def show_job_history(self, selected_job_id: str | None = None) -> None:
        """List saved directories only; opening uses the canonical read-only restore path."""
        self.current_view = ("job_history", None)
        self.clear(); t = self.ttk
        self.nav(lambda: self.home(reset=False))
        t.Label(self.container, text=self.tr("history.title"), style="PageTitle.TLabel").pack(anchor="w")
        t.Label(self.container, text=self.tr("history.select"), style="Muted.TLabel").pack(
            anchor="w", pady=(8, 12))
        area = t.Frame(self.container)
        area.pack(fill="both", expand=True)
        self.history_jobs = t.Treeview(area, show="tree", selectmode="browse")
        scrollbar = t.Scrollbar(area, orient="vertical", command=self.history_jobs.yview)
        self.history_jobs.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.history_jobs.pack(side="left", fill="both", expand=True)
        jobs_root = Path(self.controller.jobs_root)
        job_ids = sorted((path.name for path in jobs_root.iterdir() if path.is_dir()),
                         reverse=True) if jobs_root.is_dir() else []
        for job_id in job_ids:
            self.history_jobs.insert("", "end", iid=job_id, text=job_id)
        if not job_ids:
            t.Label(self.container, text=self.tr("history.empty"), style="Muted.TLabel").pack(
                anchor="w", pady=(8, 0))
        self.history_open_button = t.Button(self.container, text=self.tr("history.open"),
                                            style="Primary.TButton", state="disabled",
                                            command=self._open_selected_history_job)
        self.history_open_button.pack(anchor="e", pady=(12, 0))
        self.history_jobs.bind("<<TreeviewSelect>>", lambda _event: self.history_open_button.configure(
            state="normal" if self.history_jobs.selection() else "disabled"))
        if selected_job_id in job_ids:
            self.history_jobs.selection_set(selected_job_id)
            self.history_jobs.see(selected_job_id)
            self.history_open_button.configure(state="normal")

    def _open_selected_history_job(self) -> None:
        selection = self.history_jobs.selection()
        if selection:
            self.render_job_state(selection[0])

    def focus_primary_input(self) -> None:
        """Focus the home search box after the first complete Tk layout pass."""
        goal = getattr(self, "goal", None)
        if goal is not None and bool(goal.winfo_exists()):
            goal.focus_set()
            goal.icursor("end")

    def header(self, home_button: bool = True) -> None:
        bar = self.ttk.Frame(self.container)
        bar.pack(fill="x", pady=(0, 18))
        self.ttk.Label(bar, text=product_config.APP_NAME, style="Section.TLabel").pack(side="left")
        self.language_selector(bar)
        if home_button:
            self.ttk.Button(bar, text=self.tr("header.home"), command=self.home).pack(side="right", padx=6)
        self.ttk.Button(bar, text=self.tr("header.about"), command=self.about).pack(side="right", padx=6)
        website = self.ttk.Button(bar, text=self.tr("header.official_website"), command=self.open_product_website)
        website.pack(side="right")
        if not product_config.PRODUCT_WEBSITE_URL:
            website.configure(state="disabled")

    def source_directory(self, parent: Any | None = None) -> None:
        area = self.ttk.Frame(parent or self.container)
        area.pack(fill="x", pady=(24, 0))
        for label_key, group in (("sources.useful", "primary"), ("sources.more", "secondary")):
            self.ttk.Label(area, text=self.tr(label_key), style="Section.TLabel").pack(
                anchor="w", pady=(0, 8))
            links = self.ttk.Frame(area)
            links.pack(fill="x", pady=(0, 14))
            for index, source in enumerate(product_config.PUBLIC_SOURCES[group]):
                self.ttk.Button(links, text=source["name"], style="Source.TButton",
                    command=lambda url=source["url"]: self.open_public_source(url)).grid(
                        row=index // 4, column=index % 4, sticky="ew", padx=(0, 8), pady=(0, 6))
            for column in range(4):
                links.columnconfigure(column, weight=1, uniform="sources")

    @staticmethod
    def open_public_source(url: str) -> bool:
        return bool(url and webbrowser.open(url))

    def open_product_website(self) -> bool:
        return self.open_public_source(product_config.PRODUCT_WEBSITE_URL)

    def about(self) -> None:
        from tkinter import messagebox
        info = build_identity.read_build_info()
        messagebox.showinfo(self.tr("about.title"), "\n".join((
            self.tr("about.version", app=product_config.APP_NAME, version=product_config.APP_VERSION),
            f"Build ID: {info.get('build_id', '')}",
            f"Build time (UTC): {info.get('build_timestamp_utc', '')}",
            f"Source fingerprint: {info.get('source_fingerprint', '')}",
        )))

    def toggle_hint(self, hint: str) -> bool:
        if hint in self.selected_hints:
            self.selected_hints.remove(hint)
            accepted = True
        elif len(self.selected_hints) < 2:
            self.selected_hints.append(hint)
            accepted = True
        else:
            accepted = False
        self._refresh_hint_buttons(limit_reached=not accepted)
        return accepted

    def _refresh_hint_buttons(self, limit_reached: bool = False) -> None:
        for hint, button in self.hint_buttons.items():
            button.configure(
                state="disabled" if len(self.selected_hints) >= 2 and hint not in self.selected_hints else "normal",
                style="Selected.Chip.TButton" if hint in self.selected_hints else "Chip.TButton",
            )
        if self.hint_status is not None:
            labels = [self.tr(SEARCH_HINT_KEYS[hint]) for hint in self.selected_hints]
            text = self.tr("hints.limit") if limit_reached else (
                self.tr("hints.selected", items=", ".join(labels)) if labels else self.tr("hints.none"))
            self.hint_status.configure(text=text)

    def search(self) -> None:
        self.search_text = self.goal.get()
        self.search_button.configure(text=self.tr("search.searching"), state="disabled")
        self.root.update_idletasks()
        goal = self.goal.get()
        selected_hints = list(self.selected_hints)

        def run_search() -> None:
            try:
                self.flow.search(goal, selected_hints)
            except BaseException as exc:
                self.root.after(0, lambda error=exc: self._finish_search_failure(error))
                return
            self.root.after(0, self.results)

        threading.Thread(target=run_search, daemon=True).start()

    def _finish_search_failure(self, error: BaseException) -> None:
        self.search_button.configure(text=self.tr("search.button"), state="normal")
        self.message(self.tr("message.search_failed"), str(error))

    def nav(self, back: Any) -> None:
        bar = self.ttk.Frame(self.container); bar.pack(fill="x", pady=(0, 16))
        self.ttk.Button(bar, text=self.tr("header.back"), command=back).pack(side="left")
        self.ttk.Button(bar, text=self.tr("header.home"), command=self.home).pack(side="left", padx=(6, 0))
        self.ttk.Label(bar, text=f"{product_config.APP_NAME}  v{product_config.APP_VERSION}",
                       style="Muted.TLabel").pack(side="right")
        self.language_selector(bar)

    def results(self, enrich: bool = True) -> None:
        self.current_view = ("results", None)
        cards = self.flow.results
        self.clear(); t = self.ttk
        self.nav(self.home)
        t.Label(self.container, text=self.tr("results.title"), style="PageTitle.TLabel").pack(anchor="w", pady=(0, 12))
        if not cards:
            if self.flow.zero_result_message == SEARCH_FAILED_MESSAGE:
                message = self.tr("results.search_failed")
                details = "\n".join(
                    f"{item.get('query', '')}: {item.get('error', '')}"
                    for item in self.flow.search_errors[:3])
                if details:
                    message += "\n\n" + details
            else:
                message = (self.tr("results.none")
                           if self.flow.zero_result_message == ZERO_RESULT_MESSAGE
                           else self.flow.zero_result_message)
            t.Label(self.container, text=message, wraplength=950, justify="left").pack(anchor="w")
            return

        panes = t.Panedwindow(self.container, orient="horizontal")
        panes.pack(fill="both", expand=True)
        result_list = t.Frame(panes, width=self.RESULT_LIST_WIDTH, padding=(0, 2, 10, 2))
        self.preview_panel = t.Frame(panes, padding=(18, 2, 4, 2))
        panes.add(result_list, weight=self.RESULT_LIST_WEIGHT)
        panes.add(self.preview_panel, weight=self.RESULT_PREVIEW_WEIGHT)
        list_content = self._scrollable_content(result_list)

        self.result_rows = {}
        displayed_results = {}
        for card in cards:
            number = int(card["number"])
            displayed_result = self.flow.result_snapshot(number)
            card = dict(card,
                        role_label=self.tr("results.role." + str(card["role"])),
                        popularity_lines=popularity_display_lines(displayed_result, self.tr))
            displayed_results[number] = displayed_result
            row = self.tk.Label(
                list_content,
                text=result_card_text(card, line_width=44),
                anchor="w",
                justify="left",
                font=(self.ui_colors["font"], 10),
                bg=self.ui_colors["surface"],
                fg=self.ui_colors["text"],
                highlightthickness=1,
                highlightbackground=self.ui_colors["line"],
                highlightcolor=self.ui_colors["accent"],
                takefocus=True,
                borderwidth=0,
                padx=14,
                pady=14,
                cursor="hand2",
            )
            row.pack(fill="x", padx=(0, 4), pady=(0, 8))
            row.bind(
                "<Button-1>",
                lambda _event, n=number, selected=displayed_result: self.select_result(
                    n, selected_result=selected),
            )
            row.bind("<Configure>", lambda event, widget=row: widget.configure(
                wraplength=max(100, event.width - 32)))
            row.bind("<Return>", lambda _event, n=number, selected=displayed_result:
                     self.select_result(n, selected_result=selected))
            row.bind("<space>", lambda _event, n=number, selected=displayed_result:
                     self.select_result(n, selected_result=selected))
            row.bind("<Enter>", lambda _event, n=number, widget=row: widget.configure(
                bg=self.ui_colors["selected"] if self.selected_number == n else self.ui_colors["hover"]))
            row.bind("<Leave>", lambda _event, n=number, widget=row: widget.configure(
                bg=self.ui_colors["selected"] if self.selected_number == n else self.ui_colors["surface"]))
            self.result_rows[number] = row

        available = {int(card["number"]) for card in cards}
        selected = self.selected_number if self.selected_number in available else int(cards[0]["number"])
        self.select_result(selected, enrich=enrich, selected_result=displayed_results[selected])

    def select_result(self, number: int, enrich: bool = True,
                      selected_result: dict[str, Any] | None = None) -> None:
        """Update only the result highlight and the adjacent read-only preview."""
        self._detail_request_id += 1
        request_id = self._detail_request_id
        self.selected_number = number
        self._update_result_highlight(number)

        restored = self._restored_summary
        if (not enrich and isinstance(restored, dict)
                and int(restored.get("number", -1)) == number):
            self._finish_result_selection(request_id, number, dict(restored))
            return

        selected = (dict(selected_result) if isinstance(selected_result, dict) else
                    self.flow.result_snapshot(number))
        if enrich and _is_huggingface_model(selected):
            def load_detail() -> None:
                try:
                    data = self.flow.solution_summary(
                        number, self.localizer.language, enrich=True,
                        selected_result=selected)
                except BaseException as exc:
                    self.root.after(0, lambda error=exc: self._finish_result_selection(
                        request_id, number, error=error))
                    return
                self.root.after(0, lambda result=data: self._finish_result_selection(
                    request_id, number, result))

            threading.Thread(target=load_detail, daemon=True,
                             name="huggingface-model-card-detail").start()
            return
        try:
            data = self.flow.solution_summary(
                number, self.localizer.language, enrich=enrich,
                selected_result=selected)
        except BaseException as exc:
            return self.message(self.tr("message.summary_failed"), str(exc))
        self._finish_result_selection(request_id, number, data)

    def _update_result_highlight(self, number: int) -> None:
        for row_number, row in self.result_rows.items():
            selected = row_number == number
            row.configure(
                bg=self.ui_colors["selected"] if selected else self.ui_colors["surface"],
                fg=self.ui_colors["text"],
                highlightbackground=self.ui_colors["accent"] if selected else self.ui_colors["line"],
                relief="flat",
                borderwidth=0,
            )

    def _finish_result_selection(self, request_id: int, number: int,
                                 data: dict[str, Any] | None = None,
                                 error: BaseException | None = None) -> None:
        if request_id != self._detail_request_id or number != self.selected_number:
            return
        if error is not None:
            self.message(self.tr("message.summary_failed"), str(error))
            return
        if data is None:
            return
        self.current_summary_data = dict(data)
        self.render_result_preview(data)

    def _displayed_introduction(self, data: dict[str, Any]) -> str:
        key = (int(data["number"]), self.localizer.language)
        if key in self._show_original_introductions:
            return str(data["introduction"])
        return str(data.get("display_introduction") or data["introduction"])

    def _copy_to_clipboard(self, text: str) -> None:
        """Copy the exact displayed detail text without changing its stored value."""
        self.root.clipboard_clear()
        self.root.clipboard_append(str(text))

    def _copy_selected_detail_text(self, event: Any) -> str:
        return self._copy_detail_text_selection(event.widget)

    def _copy_detail_text_selection(self, widget: Any) -> str:
        try:
            selected = widget.get("sel.first", "sel.last")
        except self.tk.TclError:
            return "break"
        self._copy_to_clipboard(selected)
        return "break"

    @staticmethod
    def _select_all_detail_text(event: Any) -> str:
        event.widget.tag_add("sel", "1.0", "end-1c")
        event.widget.mark_set("insert", "1.0")
        event.widget.see("1.0")
        return "break"

    @staticmethod
    def _show_detail_text_menu(event: Any, menu: Any) -> str:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _readonly_detail_text(self, parent: Any, text: str, width: int = 80) -> Any:
        """Render long detail prose as selectable, read-only text."""
        value = str(text)
        visual_lines = sum(max(1, (len(line) + width - 1) // width)
                           for line in (value.splitlines() or [""]))
        widget = self.tk.Text(
            parent,
            wrap="word",
            width=1,
            height=max(2, min(10, visual_lines)),
            font=(self.ui_colors["font"], 10),
            background=self.ui_colors["surface"],
            foreground=self.ui_colors["text"],
            selectbackground=self.ui_colors["selected"],
            selectforeground=self.ui_colors["text"],
            spacing1=3,
            spacing3=3,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            cursor="xterm",
            takefocus=True,
        )
        widget.insert("1.0", value)
        widget.configure(state="disabled")
        widget.bind("<Control-c>", self._copy_selected_detail_text)
        widget.bind("<Control-C>", self._copy_selected_detail_text)
        widget.bind("<Control-a>", self._select_all_detail_text)
        widget.bind("<Control-A>", self._select_all_detail_text)
        context_menu = self.tk.Menu(widget, tearoff=0)
        context_menu.add_command(
            label=self.tr("action.copy"), command=lambda target=widget: self._copy_detail_text_selection(target))
        context_menu.add_command(
            label=self.tr("action.select_all"),
            command=lambda target=widget: self._select_all_detail_text(
                type("DetailTextEvent", (), {"widget": target})()),
        )
        widget.bind(
            "<Button-3>",
            lambda event, menu=context_menu: self._show_detail_text_menu(event, menu),
        )
        return widget

    def _scrollable_detail(self, parent: Any, text: str) -> Any:
        viewport = self.ttk.Frame(parent)
        detail = self._readonly_detail_text(viewport, text, width=90)
        scrollbar = self.ttk.Scrollbar(viewport, orient="vertical", command=detail.yview)
        detail.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        detail.pack(side="left", fill="both", expand=True)
        return viewport

    def _copy_action(self, parent: Any, label: str, text: str) -> None:
        self.ttk.Button(parent, text=self.tr(label),
                        command=lambda value=str(text): self._copy_to_clipboard(value)).pack(
                            anchor="w", pady=(0, 8))

    def toggle_introduction_language(self, number: int) -> None:
        key = (int(number), self.localizer.language)
        if key in self._show_original_introductions:
            self._show_original_introductions.remove(key)
        else:
            self._show_original_introductions.add(key)
        if self.current_view[0] == "summary":
            self.summary(number, displayed_selection=self.current_summary_data)
        else:
            selected_result = self.current_summary_data.get("selected_result")
            self.select_result(number, selected_result=selected_result)

    def _introduction_action(self, parent: Any, data: dict[str, Any]) -> None:
        if data.get("translation_available"):
            key = (int(data["number"]), self.localizer.language)
            label = ("action.show_translation" if key in self._show_original_introductions
                     else "action.view_original")
            self.ttk.Button(parent, text=self.tr(label), command=lambda: self.toggle_introduction_language(
                int(data["number"]))).pack(anchor="w", pady=(0, 12))
        elif data.get("translation_unavailable"):
            self.ttk.Label(parent, text=self.tr("message.translation_unavailable"),
                           style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

    def _render_provenance(self, parent: Any, data: dict[str, Any], wraplength: int,
                           original_project_label: str | None = None) -> None:
        """Show discovery provenance without conflating it with the install source."""
        t = self.ttk
        original_project_label = (original_project_label or
                                  self.tr("results.original_project"))
        reference_url = str(data.get("first_party_reference_url") or "")
        project_url = str(data.get("project_url") or "")
        if reference_url:
            t.Label(parent, text=self.tr("results.reference_source"),
                    style="Section.TLabel").pack(anchor="w", pady=(12, 4))
            if data.get("first_party_title"):
                t.Label(parent, text=str(data["first_party_title"]), wraplength=wraplength,
                        justify="left").pack(anchor="w", pady=(0, 2))
            if data.get("channel"):
                t.Label(parent, text=self.tr("results.channel", channel=data["channel"])).pack(
                    anchor="w", pady=(0, 2))
            t.Label(parent, text=self.tr("results.reference_url", url=reference_url),
                    wraplength=wraplength, justify="left").pack(anchor="w", pady=(0, 6))
        if project_url:
            t.Label(parent, text=original_project_label,
                    style="Section.TLabel").pack(anchor="w", pady=(12, 4))
            if reference_url:
                t.Label(parent, text=self.tr(
                    "results.source", source=data["project_source_label"])).pack(anchor="w")
            t.Label(parent, text=project_url, style="Muted.TLabel", wraplength=wraplength,
                    justify="left", cursor="hand2").pack(anchor="w")
        elif not reference_url:
            t.Label(parent, text=original_project_label,
                    style="Section.TLabel").pack(anchor="w", pady=(12, 4))
            t.Label(parent, text=self.tr("results.no_project_url"), style="Muted.TLabel",
                    wraplength=wraplength, justify="left").pack(anchor="w")
        related_sources = data.get("related_sources") or []
        if related_sources:
            t.Label(parent, text=self.tr("results.related_sources"),
                    style="Section.TLabel").pack(anchor="w", pady=(12, 4))
            for source in related_sources:
                label = self.tr("results.role." + str(source["role"]))
                t.Label(parent, text=f"{label}: {source['title']}\n{source['url']}",
                        style="Muted.TLabel", wraplength=wraplength,
                        justify="left").pack(anchor="w", pady=(0, 6))

    def _solution_intro_line(self, data: dict[str, Any]) -> str:
        purpose = " ".join(str(data.get("plain_explanation") or "").split())
        sentence = re.split(r"(?<=[。！？!?])\s*|(?<=[.!?])\s+", purpose, maxsplit=1)[0]
        return sentence if len(sentence) <= 200 else sentence[:197].rstrip() + "…"

    def _solution_overview(self, parent: Any, data: dict[str, Any]) -> Any:
        """Keep the decision and its limits visible above the optional source detail."""
        t = self.ttk
        t.Label(parent, text=data["name"], style="PageTitle.TLabel", wraplength=680,
                justify="left").pack(anchor="w", pady=(0, 8))
        t.Label(parent, text=self._solution_intro_line(data), wraplength=680,
                justify="left").pack(anchor="w", pady=(0, 12))
        for line in data.get("popularity_lines") or []:
            t.Label(parent, text=line, style="Muted.TLabel", wraplength=680,
                    justify="left").pack(anchor="w", pady=(0, 3))
        facts = (
            "overview.source_review",
            "overview.windows_unknown",
            "overview.gpu_unknown",
            "overview.approval_required",
        )
        for key in facts:
            t.Label(parent, text="• " + self.tr(key), style="Muted.TLabel",
                    wraplength=680, justify="left").pack(anchor="w", pady=(0, 3))
        t.Label(parent, text=self.tr("overview.review_reason"),
                wraplength=680, justify="left").pack(anchor="w", pady=(10, 4))
        actions = t.Frame(parent)
        actions.pack(fill="x", pady=(4, 12))
        entry = get_install_entry_state(data)
        if entry.status == "UNAVAILABLE":
            t.Label(actions, text=self.tr("overview.original_project_unverified"),
                    style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w")
        elif entry.status == "REVIEW_REQUIRED":
            t.Label(actions, text=self.tr("overview.choose_project_source"),
                    style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w")
            resolution = data.get("project_resolution") or {}
            for candidate in resolution.get("candidates", []):
                t.Button(actions, text=candidate,
                         command=lambda url=candidate: self._select_reference_project(data, url)
                         ).pack(anchor="w", pady=(4, 0))
        else:
            self._install_button(actions, data).pack(side="left")
        return actions

    def _select_reference_project(self, data: dict[str, Any], url: str) -> None:
        selected = deepcopy(data["selected_result"])
        selected["user_selected_project_url"] = url
        self.summary(int(data["number"]), displayed_selection={"selected_result": selected})

    def _collapsible_solution_details(self, parent: Any) -> Any:
        """Create a scrollable details area that is hidden until requested."""
        t = self.ttk
        host = t.Frame(parent)
        expanded = False

        def toggle() -> None:
            nonlocal expanded
            expanded = not expanded
            if expanded:
                host.pack(fill="both", expand=True)
            else:
                host.pack_forget()
            button.configure(text=self.tr("details.collapse" if expanded else "details.expand"))

        button = t.Button(parent, text=self.tr("details.expand"), command=toggle)
        button.pack(anchor="w", pady=(2, 8))
        return self._scrollable_content(host)

    def render_result_preview(self, data: dict[str, Any]) -> None:
        panel = self.preview_panel
        if panel is None:
            return
        for child in panel.winfo_children():
            child.destroy()
        t = self.ttk
        number = int(data["number"])
        self._fit_wrapped_labels(panel)
        actions = self._solution_overview(panel, data)
        displayed_snapshot = deepcopy(data)
        t.Button(actions, text=self.tr("action.details"),
                 command=lambda: self.summary(number, displayed_selection=displayed_snapshot)).pack(
                     side="left", padx=(8, 0))
        panel = self._collapsible_solution_details(panel)
        t.Label(panel, text=self.tr("results.source", source=data["source_label"]),
                style="Muted.TLabel").pack(anchor="w")
        t.Label(panel, text=self.tr("results.what_it_does"), style="Section.TLabel").pack(anchor="w")
        purpose = str(data["plain_explanation"])
        self._scrollable_detail(panel, purpose).pack(fill="x", pady=(4, 4))
        self._copy_action(panel, "action.copy_purpose", purpose)
        t.Label(panel, text=self.tr("results.from_source"), style="Section.TLabel").pack(anchor="w")
        introduction = self._displayed_introduction(data)
        self._scrollable_detail(panel, introduction).pack(fill="x", pady=(4, 4))
        self._copy_action(panel, "action.copy_introduction", introduction)
        self._introduction_action(panel, data)
        if data["source_label"] == "YouTube":
            if data["channel"]:
                t.Label(panel, text=self.tr("results.channel", channel=data["channel"])).pack(anchor="w", pady=(2, 6))
            t.Label(panel, text=self.tr("results.video_url", url=data["video_url"]), wraplength=680, justify="left").pack(anchor="w", pady=(0, 6))
        if data["curated_available"]:
            t.Label(panel, text=self.tr("results.curated"), font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))
        self._render_provenance(
            panel, data, 640, self.tr("results.original_project"))
        if data["source_url"]:
            t.Button(panel, text=self.tr("action.open_original"), command=lambda: self.flow.open_original_url(number)).pack(anchor="w", pady=(8, 0))
        if data["video_url"]:
            t.Button(panel, text=self.tr("action.watch_intro"), command=lambda: self.flow.open_curated_video(number)).pack(anchor="w", pady=(8, 0))

    def summary(self, number: int, displayed_selection: dict[str, Any] | None = None) -> None:
        self.current_view = ("summary", number)
        selected_result = (displayed_selection or {}).get("selected_result")
        try: data = self.flow.solution_summary(
            number, self.localizer.language, selected_result=selected_result)
        except BaseException as exc: return self.message(self.tr("message.summary_failed"), str(exc))
        self.selected_number = number
        self.current_summary_data = dict(data)
        self.clear(); t = self.ttk
        self.nav(self.results)
        self._solution_overview(self.container, data)
        content = self._collapsible_solution_details(self.container)
        if data.get("source_name"):
            t.Label(content, text=self.tr(
                "results.source", source=data["source_label"]),
                style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        t.Label(content, text=self.tr("results.what_it_does"), style="Section.TLabel").pack(
            anchor="w", pady=(22, 6))
        purpose = str(data["plain_explanation"])
        self._scrollable_detail(content, purpose).pack(fill="x", pady=(0, 4))
        self._copy_action(content, "action.copy_purpose", purpose)
        t.Label(content, text=self.tr("details.title"), style="Section.TLabel").pack(anchor="w", pady=(22, 6))
        introduction = self._displayed_introduction(data)
        self._scrollable_detail(content, introduction).pack(fill="x", pady=(0, 4))
        self._copy_action(content, "action.copy_introduction", introduction)
        self._introduction_action(content, data)
        if data["source_label"] == "YouTube":
            t.Label(content, text="\n".join((self.tr("results.source", source="YouTube"),
                    self.tr("results.channel", channel=data["channel"]), self.tr("results.video_url", url=data["video_url"]))),
                    wraplength=730, justify="left").pack(anchor="w", pady=(12, 0))
        if data["curated_available"]:
            t.Label(content, text=self.tr("results.curated"), font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 0))
        self._render_provenance(content, data, 730)
        if data["source_url"]:
            t.Button(content, text=self.tr("action.open_original"), command=lambda: self.flow.open_original_url(number)).pack(anchor="w", pady=(8, 22))
        if data["video_url"]:
            t.Button(content, text=self.tr("action.watch_intro"), command=lambda: self.flow.open_curated_video(number)).pack(anchor="w", pady=(0, 12))

    def _install_button(self, parent: Any, displayed_selection: dict[str, Any]) -> Any:
        """Bind the visible install action to the exact result currently being displayed."""
        displayed_snapshot = deepcopy(displayed_selection)

        def callback() -> None:
            probe_path = runtime_path("logs", "install_probe.log")
            probe_path.parent.mkdir(parents=True, exist_ok=True)
            with open(probe_path, "a", encoding="utf-8") as f:
                f.write(f"BUTTON_CALLBACK_REACHED {displayed_snapshot!r}\n")
            self.prepare(displayed_snapshot)

        handoff_debug.trace(
            "visible_install_button_bound",
            callback_function=f"{type(self).__module__}.{type(self).__qualname__}.prepare",
            callback_code_file=inspect.getsourcefile(type(self).prepare),
            displayed_selection=displayed_selection,
        )
        return self.ttk.Button(parent, text=self.tr("action.review_before_install"),
                               style="Primary.TButton", command=callback)

    def prepare(self, displayed_selection: dict[str, Any]) -> None:
        probe_path = runtime_path("logs", "install_probe.log")
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        with open(probe_path, "a", encoding="utf-8") as f:
            f.write(f"PREPARE_REACHED {displayed_selection!r}\n")
        selected = displayed_selection.get("selected_result")
        selected = selected if isinstance(selected, dict) else {}
        handoff_debug.trace(
            "BUTTON_CLICK",
            python_executable=sys.executable,
            app_path=str((Path(__file__).resolve().parent / "app.py").resolve()),
            normal_ui_file=str(Path(__file__).resolve()),
            cwd=os.getcwd(),
            callback_function=f"{type(self).__module__}.{type(self).__qualname__}.prepare",
            selected_display_title=(displayed_selection.get("name") or displayed_selection.get("title")
                                    or selected.get("title")),
            selected_canonical_url=selected.get("canonical_url") or selected.get("url"),
            selected_repo=selected.get("repo"),
            selected_repository=selected.get("repository"),
            selected_source_type=selected.get("source_type"),
            selected_identity=selected.get("result_identity") or selected.get("identity"),
            selected_provenance=selected.get("provenance"),
            prepare_argument=displayed_selection,
        )
        handoff_debug.trace(
            "PREPARE_INPUT",
            callback_function=f"{type(self).__module__}.{type(self).__qualname__}.prepare",
            prepare_argument=displayed_selection,
        )
        try:
            probe_path.parent.mkdir(parents=True, exist_ok=True)
            with open(probe_path, "a", encoding="utf-8") as f:
                f.write(f"PREPARE_INSTALL_CALL {displayed_selection!r}\n")
            data = self.flow.prepare_install(displayed_selection)
        except JobContextValidationError as exc:
            return self._route_lifecycle_error(exc)
        except BaseException as exc:
            return self.show_pre_execution_operation_failure(str(exc))
        if data["blocked"]:
            return self.show_pre_execution_operation_failure(" ".join(data["issues"]))
        self._reload_job_state()

    def show_confirmation(self) -> None:
        self.current_view = ("confirmation", None)
        data = self.confirmation_data
        if data is None:
            raise ValueError("Persisted confirmation data is unavailable.")
        self.clear(); t = self.ttk
        self.nav(lambda: self.home(reset=False))
        t.Label(self.container, text=self.tr("install.confirm_title"), style="PageTitle.TLabel").pack(anchor="w")
        actions = t.Frame(self.container)
        actions.pack(fill="x", side="bottom")
        content = self._scrollable_content(self.container)
        destination = t.Frame(content)
        destination.pack(fill="x", pady=(16, 4))
        t.Label(destination, text=self.tr("destination.title"), style="Section.TLabel").pack(side="left")
        t.Button(destination, text=self.tr("destination.browse"), command=self.browse_destination).pack(side="left", padx=10)
        if data["destination_selected"]:
            location_text = "\n".join((
                self.tr("destination.path", path=data["selected_destination"]),
                *data.get("write_locations", []),
                self.tr("destination.drive", drive=data["drive"] or self.tr("install.unknown")),
                self.tr("destination.free", size=data["free_space_text"]),
                self.tr("destination.required", size=data["download_size_text"]),
            ))
        else:
            location_text = self.tr("destination.required_prompt")
        t.Label(content, text=location_text, justify="left", wraplength=760,
                style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        text = self.tr("install.downloads") + "\n" + self.tr("install.software") + "\n" + "\n".join(
            f"• {x}" for x in data["downloads"] or [self.tr("install.nothing")])
        text += "\n\n" + self.tr("install.models") + "\n" + "\n".join(
            f"• {x}" for x in data["models"] or [self.tr("install.no_models")])
        text += f"\n\n{self.tr('install.size')}\n{self.tr('install.total_size')}\n{data['download_size_text']}"
        text += "\n\n" + self.tr("install.reused") + "\n" + (
            "\n".join(f"• {item}" for item in data["reused_assets"]) or f"• {self.tr('install.nothing')}")
        t.Label(content, text=text, justify="left", wraplength=760).pack(anchor="w", pady=(8, 18))
        if data["issues"]:
            t.Label(content, text="\n".join(data["issues"]), foreground="#9b1c1c",
                    wraplength=760, justify="left").pack(anchor="w", pady=(0, 10))
        self.start_button = t.Button(
            actions, text=self.tr("research.analyze"), style="Primary.TButton",
            command=self.analyze_installation,
            state="normal" if data["analysis_allowed"] else "disabled")
        self.start_button.pack(side="left")

    def browse_destination(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=self.root, mustexist=True,
                                         title=self.tr("destination.picker_title"))
        if not chosen:
            return
        try:
            self.flow.select_destination(chosen)
        except JobContextValidationError as exc:
            return self._route_lifecycle_error(exc)
        except (OSError, ValueError) as exc:
            return self.message(self.tr("destination.invalid"), str(exc))
        self._reload_job_state()

    def installing(self) -> None:
        self.current_view = ("installing", None)
        self.clear(); t = self.ttk
        self.nav(self._reload_job_state)
        t.Label(self.container, text=self.tr("install.preparing"), style="PageTitle.TLabel").pack(anchor="w")
        self.progress_label = t.Label(self.container, text=self.tr("install.preparing_safe"), justify="left"); self.progress_label.pack(anchor="w", pady=(16, 22))
        self.stop_button = t.Button(self.container, text=self.tr("install.stop"), command=self.stop); self.stop_button.pack(anchor="w")
    def stop(self) -> None:
        try:
            context = self.controller.open_job(self._current_job_id())
            if "installation_execution_state.json" in context.artifact_names:
                self.controller.request_installation_cancel()
            else:
                self.controller.request_cancel()
            self._reload_job_state()
        except BaseException as exc:
            self._route_lifecycle_error(exc)

    def success(self) -> None:
        self.current_view = ("success", None)
        info = self.flow.confirmation(); self.clear(); t = self.ttk
        self.nav(self._reload_job_state)
        t.Label(self.container, text=self.tr("source.completed"), style="PageTitle.TLabel").pack(anchor="w")
        t.Label(self.container, text=self.tr("source.completed_detail",
                downloads=", ".join(info["downloads"]) or self.tr("install.nothing"),
                locations=", ".join(info["locations"]) or self.tr("install.unknown"),
                reused=info["reused_count"]), justify="left").pack(anchor="w", pady=24)
        actions = t.Frame(self.container); actions.pack(fill="x")
        t.Button(actions, text=self.tr("research.analyze"), style="Primary.TButton",
                 command=self.analyze_installation).pack(side="left")
        t.Button(actions, text=self.tr("install.done"), command=self.home).pack(side="left", padx=10)
        self._external_asset_action(actions)

    def analyze_installation(self) -> None:
        try:
            context = self.controller.open_job(self._current_job_id())
        except JobContextValidationError as exc:
            self.show_pre_execution_restore_failure(
                self._current_job_id(), str(exc), self.controller.failure_handoff() or {})
            return
        if context.state not in {JobLifecycleState.DESTINATION_SELECTED,
                                 JobLifecycleState.SOURCE_ACQUIRED}:
            self.render_job_state(context.job_id)
            return
        self.current_view = ("installation_research", None)
        self.clear(); t = self.ttk
        self.nav(self.success)
        t.Label(self.container, text=self.tr("research.title"), style="PageTitle.TLabel").pack(anchor="w")
        t.Label(self.container, text=self.tr("research.safety"), style="Subtitle.TLabel",
                wraplength=760, justify="left").pack(anchor="w", pady=(4, 16))
        self.research_progress_label = t.Label(self.container, text=self.tr("research.reading"),
                                               wraplength=760, justify="left")
        self.research_progress_label.pack(anchor="w")
        target = (self._run_source_acquisition
                  if context.state is JobLifecycleState.DESTINATION_SELECTED
                  else self._run_installation_research)
        threading.Thread(target=target, daemon=True).start()

    def _run_source_acquisition(self) -> None:
        """Run only the persisted source-acquisition plan, then dispatch persisted state."""
        try:
            context = self.controller.open_job(self._current_job_id())
            if context.state is not JobLifecycleState.DESTINATION_SELECTED:
                self.root.after(0, self._reload_job_state)
                return
            plan = product_cli.JobStore(self.controller.jobs_root, context.job_id).load(
                "plan.json", {})
            step_ids = [str(step["step_id"]) for step in plan.get("steps", [])]
            if not step_ids:
                raise SystemExit("The persisted source-acquisition plan has no executable steps.")
            self.controller.approve(step_ids)
            self.controller.execute()
            if product_cli.JobStore(self.controller.jobs_root, context.job_id).job().get("state") == "VERIFIED":
                self.controller.close()
            self.root.after(0, self._reload_job_state)
        except BaseException as exc:
            self.root.after(0, lambda error=exc: self._route_lifecycle_error(error))

    def _research_progress(self, stage: str) -> None:
        key = {
            "READING_PROJECT_DOCUMENTATION": "research.reading",
            "PARSING_MANIFESTS": "research.parsing",
            "CONSULTING_LOCAL_ORNITH": "research.consulting",
            "VALIDATING_EVIDENCE": "research.validating",
        }.get(stage, "research.reading")
        self.root.after(0, lambda: self.research_progress_label.configure(text=self.tr(key)))

    def _run_installation_research(self) -> None:
        try:
            context = self.controller.open_job(self._current_job_id())
            if context.state is not JobLifecycleState.SOURCE_ACQUIRED:
                self.root.after(0, self._reload_job_state)
                return
            result = self.flow.analyze_installation(progress=self._research_progress)
            if result.get("status") == "BLOCKED":
                self.root.after(0, lambda: self.show_installation_research_blocked(result))
            else:
                self.root.after(0, self._reload_job_state)
        except BaseException as exc:
            self.root.after(0, lambda error=exc: self._route_lifecycle_error(error))

    def show_installation_confidence_review(self, result: dict[str, Any]) -> None:
        self.current_view = ("installation_confidence_review", None)
        self.clear(); t = self.ttk
        self.nav(self.success)
        t.Label(self.container, text=self.tr("research.confidence_title"),
                style="PageTitle.TLabel").pack(anchor="w")
        t.Label(self.container, text=self.tr("research.confidence_instruction"),
                justify="left", wraplength=980).pack(anchor="w", pady=(10, 14))
        choices: dict[str, Any] = {}
        actions = t.Frame(self.container); actions.pack(fill="x", side="bottom", pady=(12, 0))
        content = self._scrollable_content(self.container)
        for issue in result.get("confidence_review", {}).get("issues", []):
            row = t.Frame(content); row.pack(fill="x", pady=(0, 16))
            t.Label(row, text=f"{issue['field']}: {issue['claim']}",
                    style="Section.TLabel").pack(anchor="w")
            t.Label(row, text=f"{issue.get('path') or ''}: {issue.get('exact_text') or ''}",
                    justify="left", wraplength=940).pack(anchor="w")
            value = self.tk.StringVar(value="")
            t.Combobox(row, textvariable=value, values=install_research.CONFIDENCE_CHOICES,
                       state="readonly", width=12).pack(anchor="w", pady=(4, 0))
            choices[issue["id"]] = value

        def confirm() -> None:
            selected = {key: value.get() for key, value in choices.items()}
            if not selected or any(value not in install_research.CONFIDENCE_CHOICES
                                   for value in selected.values()):
                self.message(self.tr("research.confidence_title"),
                             self.tr("research.confidence_required"))
                return
            try:
                self.flow.confirm_research_confidence(selected)
            except install_research.InstallResearchError as exc:
                self.message(self.tr("research.confidence_title"), str(exc))
                return
            self._reload_job_state()

        t.Button(actions, text=self.tr("research.confidence_confirm"),
                 style="Primary.TButton", command=confirm).pack(side="left")

    def show_installation_research_blocked(self, result: dict[str, Any]) -> None:
        self.current_view = ("installation_research_blocked", None)
        self.clear(); t = self.ttk
        self.nav(self.success)
        local_ai_error = str(result.get("error_code") or "").startswith("LOCAL_AI_")
        title = "research.local_ai_failed" if local_ai_error else "research.blocked_title"
        t.Label(self.container, text=self.tr(title), style="PageTitle.TLabel").pack(anchor="w")
        backend = result.get("backend_error") or {}
        response = str(backend.get("response_body") or "")
        try:
            response = str(json.loads(response).get("error") or response)
        except (ValueError, AttributeError):
            pass
        diagnosis = "\n".join(value for value in (
            str(result.get("error_code") or ""),
            str(result.get("error") or ""),
            f"HTTP {backend['http_status']}" if backend.get("http_status") else "",
            response,
        ) if value)
        t.Label(self.container, text=diagnosis, justify="left", wraplength=980).pack(
            anchor="w", pady=(12, 0))
        t.Label(self.container, text=self.tr("research.source_reusable"),
                style="Muted.TLabel", wraplength=980, justify="left").pack(anchor="w", pady=(12, 0))
        actions = t.Frame(self.container); actions.pack(fill="x", pady=(20, 0))
        t.Button(actions, text=self.tr("research.retry"), style="Primary.TButton",
                 command=self.analyze_installation).pack(side="left")
        t.Button(actions, text=self.tr("failure.details"),
                 command=lambda: self.show_installation_research_blocked_details(result)).pack(
                     side="left", padx=(8, 0))
        t.Button(actions, text=self.tr("header.back"), command=self.success).pack(
            side="left", padx=(8, 0))

    def show_installation_research_blocked_details(self, result: dict[str, Any]) -> None:
        self.current_view = ("installation_research_blocked_details", None)
        self.clear(); t = self.ttk
        self.nav(lambda: self.show_installation_research_blocked(result))
        t.Label(self.container, text=self.tr("failure.details"),
                style="PageTitle.TLabel").pack(anchor="w")
        details = install_research.review_text(
            result, self.tr("research.safety"), self.localizer.language)
        details += "\n\nbackend_error:\n" + json.dumps(
            result.get("backend_error") or {}, ensure_ascii=False, indent=2)
        self._scrollable_detail(self.container, details).pack(
            fill="both", expand=True, pady=(12, 0))

    def show_installation_research(self, result: dict[str, Any] | None = None) -> None:
        result = result or self.flow.installation_research()
        self.current_view = ("installation_research_result", None)
        self.clear(); t = self.ttk
        self.nav(self.success)
        t.Label(self.container, text=self.tr("research.review_title"), style="PageTitle.TLabel").pack(anchor="w")
        review = install_research.review_text(
            result, self.tr("research.safety"), self.localizer.language)
        actions = t.Frame(self.container)
        actions.pack(fill="x", side="bottom", pady=(12, 0))
        t.Button(actions, text=self.tr("install.done"), command=self.home).pack(anchor="w")
        if result.get("status") in {"COMPLETE", "REVIEW_REQUIRED"}:
            t.Button(actions, text=self.tr("plan.build"), style="Primary.TButton",
                     command=self.build_installation_plan).pack(side="right")
        self._external_asset_action(actions)
        viewport = t.Frame(self.container)
        viewport.pack(fill="both", expand=True, pady=(12, 0))
        research_text = self._readonly_detail_text(viewport, review, width=90)
        scrollbar = t.Scrollbar(viewport, orient="vertical", command=research_text.yview)
        research_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        research_text.pack(side="left", fill="both", expand=True)

    def build_installation_plan(self) -> None:
        """Persist and display a deterministic plan; this action never executes it."""
        try:
            self.flow.build_installation_plan()
            self._reload_job_state()
        except BaseException as exc:
            self._route_lifecycle_error(exc)

    def show_installation_plan(self, plan: dict[str, Any] | None = None) -> None:
        plan = plan or self.flow.installation_plan()
        research = self.flow.installation_research()
        self.current_view = ("installation_plan_review", None)
        self.clear(); t = self.ttk
        self.nav(self.show_installation_research)
        if plan.get("status") == "REVIEW_REQUIRED":
            t.Label(self.container, text=self.tr("plan.risk.title"),
                    style="PageTitle.TLabel").pack(anchor="w")
            t.Label(self.container, text=self.tr("plan.risk.message"),
                    justify="left", wraplength=760).pack(anchor="w", pady=(12, 20))
            actions = t.Frame(self.container)
            actions.pack(fill="x")
            t.Button(actions, text=self.tr("plan.risk.ask_cloud"),
                     command=self.show_installation_cloud_report).pack(side="left", padx=(0, 8))
            t.Button(actions, text=self.tr("plan.risk.continue"), style="Primary.TButton",
                     command=lambda: self.accept_unresolved_installation_risk(plan)).pack(side="left", padx=(0, 8))
            t.Button(actions, text=self.tr("plan.risk.cancel"), command=self.home).pack(side="left")
            self._external_asset_action(actions)
            self._installation_advanced_details(plan, research)
            return
        t.Label(self.container, text=self.tr("plan.review_title"), style="PageTitle.TLabel").pack(anchor="w")
        actions = t.Frame(self.container)
        actions.pack(fill="x", side="bottom", pady=(12, 0))
        t.Button(actions, text=self.tr("install.done"), command=self.home).pack(anchor="w")
        if plan.get("status") in {"REVIEW_REQUIRED", "READY_FOR_APPROVAL"}:
            t.Button(actions, text=self.tr("plan.approve"), style="Primary.TButton",
                     command=self.approve_reviewed_plan).pack(side="right")
        self._external_asset_action(actions)
        viewport = t.Frame(self.container)
        viewport.pack(fill="both", expand=True, pady=(12, 0))
        review = installation_plan_review_text(plan, research, self.tr)
        plan_text = self._readonly_detail_text(viewport, review, width=90)
        scrollbar = t.Scrollbar(viewport, orient="vertical", command=plan_text.yview)
        plan_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        plan_text.pack(side="left", fill="both", expand=True)

    def _installation_advanced_details(self, plan: dict[str, Any], research: dict[str, Any]) -> None:
        """Keep technical actions available without showing them by default."""
        t = self.ttk
        viewport = t.Frame(self.container)
        technical = installation_plan_review_text(plan, research, self.tr)
        unsupported = plan.get("unsupported_actions") or []
        if unsupported:
            technical += "\n\n" + self.tr("plan.risk.advanced")
            technical += "\n" + "\n".join(f"- {action}" for action in unsupported)
        detail = self._readonly_detail_text(
            viewport, technical, width=90)
        scrollbar = t.Scrollbar(viewport, orient="vertical", command=detail.yview)
        detail.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        detail.pack(side="left", fill="both", expand=True)

        def toggle() -> None:
            if viewport.winfo_ismapped():
                viewport.pack_forget()
            else:
                viewport.pack(fill="both", expand=True, pady=(8, 0))

        t.Button(self.container, text=self.tr("plan.risk.advanced"),
                 command=toggle).pack(anchor="w", pady=(16, 0))

    def show_installation_cloud_report(self) -> None:
        plan = self.flow.installation_plan()
        research = self.flow.installation_research()
        report = installation_cloud_ai_report(plan, research, self.tr)
        self.current_view = ("installation_cloud_report", None)
        self.clear(); t = self.ttk
        self.nav(self.show_installation_plan)
        t.Label(self.container, text=self.tr("plan.cloud.report_title"),
                style="PageTitle.TLabel").pack(anchor="w")
        actions = t.Frame(self.container)
        actions.pack(fill="x", pady=(12, 0))
        t.Button(actions, text=self.tr("plan.cloud.copy"),
                 command=lambda: self._copy_to_clipboard(report)).pack(side="left", padx=(0, 8))
        t.Button(actions, text=self.tr("plan.cloud.back"),
                 command=self.show_installation_plan).pack(side="left")
        viewport = t.Frame(self.container)
        viewport.pack(fill="both", expand=True, pady=(12, 0))
        detail = self._readonly_detail_text(viewport, report, width=90)
        scrollbar = t.Scrollbar(viewport, orient="vertical", command=detail.yview)
        detail.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        detail.pack(side="left", fill="both", expand=True)

    def accept_unresolved_installation_risk(self, displayed_plan: dict[str, Any]) -> None:
        """Record the explicit user choice before using the existing approval path."""
        try:
            store = product_cli.JobStore(self.controller.jobs_root, self._current_job_id())
            persisted = store.load("installation_plan.json")
            if (not isinstance(persisted, dict) or persisted.get("status") != "REVIEW_REQUIRED"
                    or product_cli.plan_hash(persisted) != product_cli.plan_hash(displayed_plan)):
                raise ValueError("Installation plan changed during risk confirmation.")
            product_cli._audit(store, product_cli.INSTALLATION_PLAN_KIND).append(
                "USER_ACCEPTED_UNRESOLVED_RISK", "UI_REVIEW_REQUIRED", "APPROVED",
                metadata={"plan_hash": product_cli.plan_hash(persisted),
                          "source_identity": persisted.get("source_identity"),
                          "unresolved_action_count": len(persisted.get("unsupported_actions") or [])})
            self.approve_reviewed_plan()
        except BaseException as exc:
            self._route_lifecycle_error(exc)

    def approve_reviewed_plan(self) -> None:
        try:
            self.controller.approve_installation_plan()
            self._reload_job_state()
        except BaseException as exc:
            self._route_lifecycle_error(exc)

    def _external_asset_action(self, actions: Any) -> None:
        """Expose the persisted external plan without preparing a new one."""
        jobs_root = getattr(self.controller, "jobs_root", None)
        if not jobs_root:
            return
        store = product_cli.JobStore(jobs_root, self._current_job_id())
        plan = store.load(external_asset_execution.PLAN_NAME)
        if isinstance(plan, dict) and external_asset_execution._ready(plan):
            self.ttk.Button(actions, text=self.tr("external.open"),
                            command=self.show_external_asset_plan).pack(side="right", padx=(0, 8))

    def _external_asset_report(self, plan: dict[str, Any], state: dict[str, Any]) -> str:
        identity = plan.get("source_identity") or {}
        steps = plan.get("steps") or []
        model = next((step for step in steps if step.get("type") == "DOWNLOAD_MODEL_ASSET"), {})
        nodes = [step for step in steps if step.get("type") == "ACQUIRE_CUSTOM_NODE_REPOSITORY"]
        completed = [step for step in state.get("steps") or [] if step.get("status") == "SUCCESS"]
        outcomes = {step.get("step_id"): step.get("outcome") for step in completed}
        report = {
            "job_id": self._current_job_id(),
            "build_id": build_identity.read_build_info().get("build_id"),
            "source_repository": identity.get("repository") or identity.get("repo") or plan.get("verified_source"),
            "revision": identity.get("commit") or identity.get("revision") or identity.get("exact_commit"),
            "external_plan_hash": plan.get("plan_hash"),
            "failed_step": state.get("failed_step_id"),
            "error": state.get("error"),
            "completed_steps": completed,
            "executor_result": state,
            "model_target": model.get("target_path"),
            "custom_node_status": [
                {"repository": step.get("repository"), "revision": step.get("revision"),
                 "target": step.get("target"), "outcome": outcomes.get(step.get("step_id"))}
                for step in nodes],
        }
        return json.dumps(report, ensure_ascii=False, indent=2)

    def show_external_asset_plan(self, outcome: dict[str, Any] | None = None) -> None:
        store = product_cli.JobStore(self.controller.jobs_root, self._current_job_id())
        plan = store.load(external_asset_execution.PLAN_NAME)
        if not isinstance(plan, dict):
            return
        saved = store.load("external_asset_execution_state.json", {})
        state = outcome or (saved if saved.get("plan_hash") == plan.get("plan_hash") else {})
        if state.get("status") not in {"REVIEW_REQUIRED", "FAILED", "BLOCKED", "COMPLETE", "RUNNING"}:
            state = {}
        approved = False
        if external_asset_execution._ready(plan):
            try:
                external_asset_execution._approved(store, plan)
                approved = True
            except external_asset_execution.ExternalAssetExecutionError:
                pass
        status = state.get("status") or ("APPROVED" if approved else plan.get("status"))
        self.current_view = ("external_asset_plan", status)
        self.clear(); t = self.ttk
        self.nav(self._reload_job_state)
        t.Label(self.container, text=self.tr("external.title"), style="PageTitle.TLabel").pack(anchor="w")
        t.Label(self.container, text=self.tr("external.status", status=status),
                justify="left", wraplength=760).pack(anchor="w", pady=(12, 8))
        actions = t.Frame(self.container); actions.pack(fill="x", pady=(8, 0))
        displayed_hash = plan.get("plan_hash")
        if status == "READY_FOR_APPROVAL" and external_asset_execution._ready(plan):
            t.Button(actions, text=self.tr("external.approve"), style="Primary.TButton",
                     command=lambda: self.approve_external_asset_plan(displayed_hash)).pack(side="left")
        elif status == "APPROVED":
            t.Button(actions, text=self.tr("external.execute"), style="Primary.TButton",
                     command=lambda: self.execute_external_asset_plan(displayed_hash)).pack(side="left")
        elif status in {"REVIEW_REQUIRED", "BLOCKED"}:
            t.Label(self.container, text=self.tr("external.review"),
                    wraplength=760, justify="left").pack(anchor="w", pady=(12, 0))
        elif status == "FAILED":
            t.Label(self.container, text=self.tr("external.failed"),
                    wraplength=760, justify="left").pack(anchor="w", pady=(12, 0))
        elif status == "COMPLETE":
            t.Label(self.container, text=self.tr("external.complete"),
                    wraplength=760, justify="left").pack(anchor="w", pady=(12, 0))
        if status in {"REVIEW_REQUIRED", "BLOCKED", "FAILED"}:
            report = self._external_asset_report(plan, state)
            t.Button(actions, text=self.tr("failure.copy_report"),
                     command=lambda: self._copy_to_clipboard(report)).pack(side="left", padx=(8, 0))
        t.Button(actions, text=self.tr("install.done"), command=self.home).pack(side="right")
        detail = json.dumps({"plan_hash": displayed_hash, "status": status,
                             "model_target": next((s.get("target_path") for s in plan.get("steps", [])
                                                   if s.get("type") == "DOWNLOAD_MODEL_ASSET"), None),
                             "executor_result": state}, ensure_ascii=False, indent=2)
        self._scrollable_detail(self.container, detail).pack(fill="both", expand=True, pady=(12, 0))

    def approve_external_asset_plan(self, displayed_hash: str) -> None:
        try:
            external_asset_execution.approve_external_plan(
                self.controller.jobs_root, self._current_job_id(), displayed_hash)
            self.show_external_asset_plan()
        except external_asset_execution.ExternalAssetExecutionError as exc:
            self.show_external_asset_plan({"status": "REVIEW_REQUIRED", "error": str(exc)})

    def execute_external_asset_plan(self, displayed_hash: str) -> None:
        store = product_cli.JobStore(self.controller.jobs_root, self._current_job_id())
        plan = store.load(external_asset_execution.PLAN_NAME)
        try:
            if (not isinstance(plan, dict) or displayed_hash != plan.get("plan_hash")
                    or displayed_hash != product_cli.plan_hash(plan)):
                raise external_asset_execution.ExternalAssetExecutionError("Displayed external plan changed")
            external_asset_execution._approved(store, plan)
        except external_asset_execution.ExternalAssetExecutionError as exc:
            self.show_external_asset_plan({"status": "REVIEW_REQUIRED", "error": str(exc)})
            return
        job_id = self._current_job_id()
        self.show_external_asset_plan({"status": "RUNNING", "plan_hash": displayed_hash})

        def run() -> None:
            try:
                result = external_asset_execution.execute_external_plan(self.controller.jobs_root, job_id)
            except Exception as exc:
                persisted = product_cli.JobStore(self.controller.jobs_root, job_id).load(
                    "external_asset_execution_state.json", {})
                result = (persisted if persisted.get("plan_hash") == displayed_hash and
                          persisted.get("status") in {"FAILED", "BLOCKED", "REVIEW_REQUIRED"} else
                          {"status": "REVIEW_REQUIRED" if "REVIEW_REQUIRED" in str(exc) else "FAILED",
                           "error": str(exc), "plan_hash": displayed_hash, "steps": []})
            self.root.after(0, lambda: self.show_external_asset_plan(result))

        threading.Thread(target=run, daemon=True).start()

    def show_approved_installation_plan(self, plan: dict[str, Any] | None = None) -> None:
        """Show the separately approved state where canonical execution becomes available."""
        plan = plan or self.flow.installation_plan()
        research = self.flow.installation_research()
        self.current_view = ("installation_plan_approved", None)
        self.clear(); t = self.ttk
        self.nav(self.show_installation_research)
        t.Label(self.container, text=self.tr("plan.approved_title"),
                style="PageTitle.TLabel").pack(anchor="w")
        actions = t.Frame(self.container)
        actions.pack(fill="x", side="bottom", pady=(12, 0))
        t.Button(actions, text=self.tr("install.done"), command=self.home).pack(anchor="w")
        t.Button(actions, text=self.tr("plan.execute"), style="Primary.TButton",
                 command=self.execute_reviewed_plan).pack(side="right")
        self._external_asset_action(actions)
        if plan.get("unsupported_actions"):
            t.Label(self.container, text=self.tr("plan.risk.accepted_message"),
                    justify="left", wraplength=760).pack(anchor="w", pady=(12, 0))
            self._installation_advanced_details(plan, research)
            return
        viewport = t.Frame(self.container)
        viewport.pack(fill="both", expand=True, pady=(12, 0))
        review = (f"{self.tr('plan.lifecycle.status')}: {self.tr('plan.status.approved')}\n\n"
                  + installation_plan_review_text(plan, research, self.tr))
        plan_text = self._readonly_detail_text(viewport, review, width=90)
        scrollbar = t.Scrollbar(viewport, orient="vertical", command=plan_text.yview)
        plan_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        plan_text.pack(side="left", fill="both", expand=True)

    def execute_reviewed_plan(self) -> None:
        """Execution is reachable only after exact persisted-plan approval."""
        try:
            context = self.controller.open_job(self._current_job_id())
        except JobContextValidationError as exc:
            self._route_lifecycle_error(exc)
            return
        if context.state is not JobLifecycleState.APPROVED:
            self.render_job_state(context.job_id)
            return
        self.installing()
        threading.Thread(target=self._execute_reviewed_plan, daemon=True).start()

    def _execute_reviewed_plan(self) -> None:
        try:
            result = self.controller.execute_installation_plan()
            if result.get("status") == "PRECONDITION_MISSING_CONDA":
                self.root.after(0, self.show_conda_precondition)
            else:
                self.root.after(0, self._reload_job_state)
        except BaseException as exc:
            self.root.after(0, lambda error=str(exc): self.show_installation_step_failure(error))

    def show_conda_precondition(self, error: str | None = None) -> None:
        self.current_view = ("conda_precondition", "PRECONDITION_MISSING_CONDA")
        self.clear(); t = self.ttk
        t.Label(self.container, text=self.tr("conda.missing_title"),
                style="PageTitle.TLabel").pack(anchor="w")
        t.Label(self.container, text=self.tr("conda.missing_message"),
                justify="left", wraplength=760).pack(anchor="w", pady=(16, 8))
        if error:
            t.Label(self.container, text=error, justify="left",
                    wraplength=760).pack(anchor="w", pady=(0, 8))
        actions = t.Frame(self.container)
        actions.pack(fill="x", pady=(16, 0))
        t.Button(actions, text=self.tr("conda.auto_install"), style="Primary.TButton",
                 command=self.auto_install_conda).pack(side="left")
        t.Button(actions, text=self.tr("conda.recheck"),
                 command=self.recheck_conda).pack(side="left", padx=10)
        t.Button(actions, text=self.tr("conda.cancel"),
                 command=self.show_approved_installation_plan).pack(side="left")

    def _show_conda_progress(self) -> None:
        self.current_view = ("conda_bootstrap", None)
        self.clear(); t = self.ttk
        t.Label(self.container, text=self.tr("conda.installing"),
                style="PageTitle.TLabel").pack(anchor="w")

    def recheck_conda(self) -> None:
        self._show_conda_progress()
        threading.Thread(target=self._recheck_conda, daemon=True).start()

    def _recheck_conda(self) -> None:
        try:
            result = self.controller.check_conda_precondition()
            if result.get("status") == "AVAILABLE":
                self.root.after(0, self.show_approved_installation_plan)
            else:
                self.root.after(0, self.show_conda_precondition)
        except BaseException as exc:
            self.root.after(0, lambda error=str(exc): self.show_conda_precondition(error))

    def auto_install_conda(self) -> None:
        self._show_conda_progress()
        threading.Thread(target=self._auto_install_conda, daemon=True).start()

    def _auto_install_conda(self) -> None:
        try:
            result = self.controller.install_recommended_conda()
            if result.get("status") != "AVAILABLE":
                raise RuntimeError("Miniforge installation did not provide Conda")
            self.root.after(0, self.show_approved_installation_plan)
        except BaseException as exc:
            self.root.after(0, lambda error=str(exc): self.show_conda_precondition(error))

    def show_installation_lifecycle(self, result: dict[str, Any]) -> None:
        state = result.get("execution_state") if isinstance(result.get("execution_state"), dict) else result
        status = installation_plan.execution_status(state)
        self.current_view = ("installation_plan_lifecycle", status)
        self.clear(); t = self.ttk
        self.nav(self.show_approved_installation_plan)
        title = "install.completed" if status == "VERIFIED" else "plan.lifecycle.title"
        t.Label(self.container, text=self.tr(title),
                style="PageTitle.TLabel").pack(anchor="w")
        detail = self._scrollable_detail(
            self.container, installation_lifecycle_text(result, self.tr))
        detail.pack(fill="both", expand=True, pady=(12, 0))
        actions = t.Frame(self.container)
        actions.pack(fill="x", side="bottom", pady=(12, 0))
        t.Button(actions, text=self.tr("install.done"), command=self.home).pack(anchor="w")
        lifecycle = getattr(getattr(self, "_active_job_context", None), "state", None)
        if lifecycle is JobLifecycleState.RESUMABLE:
            t.Button(actions, text=self.tr("plan.lifecycle.resume"),
                     command=self.resume_reviewed_plan).pack(side="right")
        elif lifecycle is JobLifecycleState.STOPPED:
            t.Button(actions, text=self.tr("plan.lifecycle.approve_and_resume"),
                     command=self.approve_corrective_resume).pack(side="right")

    def resume_reviewed_plan(self) -> None:
        self.installing()
        threading.Thread(target=self._resume_reviewed_plan, args=(False,), daemon=True).start()

    def approve_corrective_resume(self) -> None:
        self.installing()
        threading.Thread(target=self._resume_reviewed_plan, args=(True,), daemon=True).start()

    def _resume_reviewed_plan(self, approve_deviation: bool) -> None:
        try:
            context = self.controller.open_job(self._current_job_id())
            expected = JobLifecycleState.STOPPED if approve_deviation else JobLifecycleState.RESUMABLE
            if context.state is not expected:
                self.root.after(0, self._reload_job_state)
                return
            deviation_id = self._required_installation_deviation(context) if approve_deviation else None
            self.controller.resume_installation_plan(deviation_id=deviation_id)
            self.root.after(0, self._reload_job_state)
        except BaseException as exc:
            self.root.after(0, lambda error=str(exc): self.show_installation_step_failure(error))

    def _required_installation_deviation(self, context: JobContext) -> str:
        state = product_cli.JobStore(self.controller.jobs_root, context.job_id).load(
            "installation_execution_state.json", {}
        )
        if state.get("cancel_requested"):
            return "USER_CANCELLED"
        for step in state.get("steps") or []:
            if str(step.get("status") or "").upper() == "STOPPED":
                deviation_id = step.get("deviation_id") or step.get("reason") or step.get("stop_reason")
                if deviation_id:
                    return str(deviation_id)
        deviation_id = state.get("deviation_id") or state.get("stop_reason")
        if deviation_id:
            return str(deviation_id)
        raise JobContextValidationError(JobContextValidationFailure(
            code="MISSING_DEVIATION_IDENTITY",
            message="stopped installation has no persisted deviation identity",
            artifact="installation_execution_state.json",
        ))

    def _route_lifecycle_error(self, error: BaseException) -> None:
        if isinstance(error, JobContextValidationError):
            handoff_provider = getattr(self.controller, "failure_handoff", None)
            handoff = handoff_provider() if callable(handoff_provider) else {}
            self.show_pre_execution_restore_failure(self._current_job_id(), str(error), handoff)
            return
        self.show_pre_execution_operation_failure(str(error))

    def show_pre_execution_operation_failure(self, issue: str) -> None:
        self.current_view = ("pre_execution_operation_failure", issue)
        self.clear(); t = self.ttk
        self.nav(self.home)
        t.Label(
            self.container,
            text=self.tr("failure.operation_title"),
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        self._scrollable_detail(self.container, issue).pack(
            fill="both", expand=True, pady=(12, 0)
        )

    def show_installation_step_failure(self, issue: str) -> None:
        self.current_view = ("installation_step_failure", issue)
        self.clear(); t = self.ttk
        self.nav(self._reload_job_state)
        t.Label(
            self.container,
            text=self.tr("failure.installation_step_title"),
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        self._scrollable_detail(self.container, issue).pack(
            fill="both", expand=True, pady=(12, 0)
        )

    def show_pre_execution_restore_failure(
        self,
        job_id: str,
        issue: str,
        handoff: dict[str, Any] | None = None,
    ) -> None:
        """Render persisted-state validation failures separately from execution failures."""
        details = dict(handoff or {})
        details.setdefault("classification", "PRE-EXECUTION STATE RESTORE FAILURE")
        details.setdefault("job_id", job_id)
        details.setdefault("error", issue)
        self.pre_execution_failure = details
        self.current_view = ("pre_execution_state_restore_failure", job_id)
        self.clear(); t = self.ttk
        self.nav(self.home)
        t.Label(
            self.container,
            text=self.tr("failure.restore_title"),
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        identity_keys = (
            "selected_reference",
            "install_source",
            "repository_identity",
            "canonical_repo_url",
            "exact_revision",
            "destination_root",
            "resolved_project_destination",
        )
        preserved = "\n".join(
            f"{key}: {details[key]}" for key in identity_keys if details.get(key) not in (None, "")
        )
        body = f"job_id: {job_id}\n{issue}"
        if preserved:
            body += f"\n\n{preserved}"
        view = self._scrollable_detail(self.container, body)
        view.pack(fill="both", expand=True, pady=(12, 0))

    def message(self, title: str, text: str) -> None:
        from tkinter import messagebox
        messagebox.showerror(title, text)

    def developer(self) -> None:
        if not self.developer_enabled:
            return
        for child in self.root.winfo_children():
            child.destroy()
        self.developer_app(self.root, self.controller)
