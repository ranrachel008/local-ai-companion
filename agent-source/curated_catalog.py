"""Conservative, optional curated metadata for Scout search results."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import solution_scout


DEFAULT_CATALOG_PATH = Path(__file__).with_name("data") / "curated_sources.json"
REQUIRED_FIELDS = {
    "id", "title", "categories", "keywords", "plain_language_description",
    "original_url", "video_url", "video_title", "verified_status",
    "verified_date", "notes",
}


def load_catalog(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return validated records; an absent or malformed optional catalog is empty."""
    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    records: list[dict[str, Any]] = []
    for value in payload:
        if not isinstance(value, dict) or not REQUIRED_FIELDS.issubset(value):
            continue
        if not all(isinstance(value[key], str) for key in REQUIRED_FIELDS - {"categories", "keywords"}):
            continue
        if not all(isinstance(value[key], list) and all(isinstance(x, str) for x in value[key])
                   for key in ("categories", "keywords")):
            continue
        if not value["id"].strip() or not value["title"].strip() or not value["original_url"].strip():
            continue
        records.append(dict(value))
    return records


def project_identity(url: str) -> str:
    """Use repository identity when available, otherwise an exact canonical URL."""
    owner, repo = solution_scout.github_identity(url)
    if owner and repo:
        return f"github:{owner.casefold()}/{repo.casefold()}"
    return solution_scout.canonical_url(url)


def _normalized(value: str) -> str:
    return " ".join(str(value).casefold().split())


def _contains_phrase(text: str, phrase: str) -> bool:
    text, phrase = _normalized(text), _normalized(phrase)
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9_+.-]+", phrase):
        return bool(re.search(rf"(?<![a-z0-9_+.-]){re.escape(phrase)}(?![a-z0-9_+.-])", text))
    return phrase in text


def match_record(record: dict[str, Any], query: str, selected_hints: list[str],
                 scout_rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Classify an entry using exact project identity or explicit catalog phrases."""
    identity = project_identity(record["original_url"])
    if identity and any(project_identity(str(row.get("url") or "")) == identity for row in scout_rows):
        return "CURATED_MATCH", ["original_url"]
    searchable = " ".join([query, *selected_hints])
    matched = [term for term in [record["title"], *record["keywords"], *record["categories"]]
               if _contains_phrase(searchable, term)]
    return ("CURATED_MATCH", matched) if matched else ("NO_CURATED_MATCH", [])


def matched_records(records: list[dict[str, Any]], query: str, selected_hints: list[str],
                    scout_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for record in records:
        classification, evidence = match_record(record, query, selected_hints, scout_rows)
        if classification == "CURATED_MATCH":
            matched.append(dict(record, curated_match_class=classification,
                                curated_match_evidence=evidence))
    return matched


def as_candidate(record: dict[str, Any], number: int) -> dict[str, Any]:
    """Project a curated-only match into the existing Scout candidate shape."""
    return {
        "number": number,
        "title": record["title"],
        "url": record["original_url"],
        "description": record["plain_language_description"],
        "group_id": project_identity(record["original_url"]),
        "discovered_by": "curated catalog",
        "relevance_class": "RELEVANT",
        "domain_evidence": ["curated catalog"],
        "task_evidence": list(record.get("curated_match_evidence") or []),
        "rejection_reason": "",
        "curated": record,
    }
