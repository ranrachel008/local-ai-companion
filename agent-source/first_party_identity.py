"""Authority for identifying the configured first-party YouTube channel."""
from __future__ import annotations

from urllib.parse import urlsplit

import product_config


def canonical_channel_url(value: object) -> str:
    raw = str(value or "").strip()
    if raw.casefold() == product_config.FIRST_PARTY_YOUTUBE_HANDLE.casefold():
        return product_config.FIRST_PARTY_YOUTUBE_CHANNEL_URL
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {"youtube.com", "www.youtube.com"}:
        return ""
    if parsed.query or parsed.fragment or parsed.path.rstrip("/").casefold() != "/@ranai-korea":
        return ""
    return product_config.FIRST_PARTY_YOUTUBE_CHANNEL_URL


def is_first_party_youtube(row: dict) -> bool:
    if row.get("source_type") not in {"YouTube", "YouTube Post"}:
        return False
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    urls = [row.get("channel_url"), provenance.get("channel_url")]
    urls = [value for value in urls if value]
    if not urls or not all(canonical_channel_url(value) for value in urls):
        return False
    confirmed_id = product_config.FIRST_PARTY_YOUTUBE_CHANNEL_ID
    ids = [value for value in (row.get("channel_id"), provenance.get("channel_id")) if value]
    return not confirmed_id or (bool(ids) and all(value == confirmed_id for value in ids))


def normalize_content_identity(row: dict) -> dict:
    result = dict(row)
    trusted = is_first_party_youtube(result)
    result["first_party"] = trusted
    if trusted or result.get("content_trust") == "TRUSTED_FIRST_PARTY_CONTENT" or result.get("source_type") in {"YouTube", "YouTube Post", "First-party Website"}:
        result["content_trust"] = "TRUSTED_FIRST_PARTY_CONTENT" if trusted else ""
    if trusted or result.get("authority") == "FIRST_PARTY" or result.get("source_type") in {"YouTube", "YouTube Post", "First-party Website"}:
        result["authority"] = "FIRST_PARTY" if trusted else "UNKNOWN"
        if not trusted:
            result["authority_evidence"] = ""
    return result
