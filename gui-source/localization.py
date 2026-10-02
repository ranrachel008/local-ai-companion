"""Small JSON-backed localization service for the Normal Mode UI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime_paths import runtime_path


ROOT = Path(__file__).resolve().parent
LOCALES_DIR = ROOT / "locales"
DEFAULT_LANGUAGE = "en"

_LANGUAGE_ALIASES = {
    "english": "en",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "simplified chinese": "zh-CN",
    "简体中文": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "traditional chinese": "zh-TW",
    "繁體中文": "zh-TW",
}


def normalize_language(language: Any) -> str:
    raw = str(language or "").strip()
    if raw in SUPPORTED_LANGUAGES:
        return raw
    display_names = {label: code for code, label in SUPPORTED_LANGUAGES.items()}
    if raw in display_names:
        return display_names[raw]
    return _LANGUAGE_ALIASES.get(raw.replace("_", "-").casefold(), DEFAULT_LANGUAGE)
SUPPORTED_LANGUAGES = {
    "en": "English",
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
    "ja": "日本語",
    "es": "Español",
    "ko": "한국어",
}


def default_preferences_path() -> Path:
    return runtime_path("preferences.json")


class Localizer:
    def __init__(self, language: str | None = None, *, locales_dir: Path | None = None,
                 preferences_path: Path | None = None) -> None:
        self.locales_dir = Path(locales_dir or LOCALES_DIR)
        self.preferences_path = Path(preferences_path or default_preferences_path())
        self._catalogs: dict[str, dict[str, str]] = {}
        self.language = self._valid(language or self._load_preference())

    @staticmethod
    def _valid(language: Any) -> str:
        return normalize_language(language)

    def _load_preference(self) -> str:
        try:
            payload = json.loads(self.preferences_path.read_text(encoding="utf-8"))
            return str(payload.get("language") or DEFAULT_LANGUAGE)
        except (OSError, ValueError, TypeError):
            return DEFAULT_LANGUAGE

    def _catalog(self, language: str) -> dict[str, str]:
        language = self._valid(language)
        if language not in self._catalogs:
            try:
                payload = json.loads((self.locales_dir / f"{language}.json").read_text(encoding="utf-8"))
                self._catalogs[language] = {str(key): str(value) for key, value in payload.items()}
            except (OSError, ValueError, TypeError):
                self._catalogs[language] = {}
        return self._catalogs[language]

    def tr(self, key: str, **values: Any) -> str:
        text = self._catalog(self.language).get(key)
        if text is None:
            text = self._catalog(DEFAULT_LANGUAGE).get(key, key)
        try:
            return text.format(**values)
        except (KeyError, ValueError):
            return text

    def set_language(self, language: str, *, persist: bool = True) -> str:
        self.language = self._valid(language)
        if persist:
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
            self.preferences_path.write_text(
                json.dumps({"language": self.language}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return self.language

    @property
    def display_name(self) -> str:
        return SUPPORTED_LANGUAGES[self.language]

    @staticmethod
    def code_for_display_name(name: str) -> str:
        return normalize_language(name)
