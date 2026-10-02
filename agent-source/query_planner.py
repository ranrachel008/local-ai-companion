"""Conservative, local-only search-query planning for Normal User mode."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from local_only_test import (LocalOnlyError, LocalModelInvalidResponseError, MODEL_NAME,
                             extract_final_answer, invoke_local_model)


MAX_EXPANDED_QUERIES = 6
_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
_MODEL_FIELDS = {
    "original_query", "selected_hints", "intent_summary",
    "entities", "task", "constraints", "domain", "preferred_sources",
    "entity_required", "expanded_queries", "confidence", "ambiguities",
}

_NON_ENTITY_WORDS = {
    "a", "an", "and", "best", "build", "camera", "clone", "cloning", "code",
    "control", "create", "cuda", "cpu", "do", "find", "for", "formatting", "from",
    "generation", "github", "how", "http", "image", "install", "installation",
    "instructions", "invoices", "library", "local", "manager", "model", "nvidia",
    "official", "on", "open", "optional", "package", "project", "python", "rag",
    "remove", "replacement", "repo", "repository", "run", "source", "speech",
    "speed", "subtitle", "terminal", "the", "to", "tool", "transcription",
    "translate", "translation", "tutorial", "use", "using", "video", "voice",
    "windows", "with", "workflow",
}
_TASK_RULES = (
    ("face replacement", ("換人臉", "换人脸", "face swap", "face replacement", "face refinement", "identity replacement")),
    ("background replacement", ("換背景", "换背景", "background replacement", "replace background", "background removal")),
    ("acceleration", (
        "提高 token/s", "提升 token/s", "提高 tokens/s", "提升 tokens/s",
        "生成速度提升", "提升生成速度", "提高生成速度", "降低延遲", "降低延迟",
        "推理提速", "推理加速", "提升吞吐", "提高吞吐", "性能優化", "性能优化",
        "效能優化", "效能优化", "加速", "speed up", "acceleration", "faster",
        "turbo", "speedup", "faster inference", "inference acceleration",
        "inference speed", "performance optimization", "performance optimisation",
        "performance improvement", "token/s", "tokens/s", "tokens per second",
        "tok/s", "lower latency", "reduced latency", "latency reduction",
        "higher throughput", "throughput", "generation speed", "faster generation",
    )),
    ("camera control", ("控制攝影機", "控制摄影机", "控制相機", "控制相机", "camera control", "control camera", "camera automation")),
    ("voice cloning", ("克隆聲音", "克隆声音", "聲音克隆", "声音克隆", "語音克隆", "语音克隆", "voice clone", "voice cloning")),
    ("retrieval-augmented generation", ("做rag", "做 rag", "rag", "retrieval augmented generation", "retrieval-augmented generation")),
    ("subtitle transcription and translation", ("字幕翻譯", "字幕翻译", "subtitle translation", "transcribe and translate", "transcription translation")),
    ("motion LoRA", ("動作lora", "动作lora", "motion lora", "action lora")),
    ("workflow", ("workflow", "工作流")),
    ("local deployment", ("本地部署", "local deployment", "deploy locally", "local install")),
)
_TASK_EXPANSIONS = {
    "face replacement": ("face replacement", "face swap", "face refinement", "identity replacement"),
    "background replacement": ("background replacement", "replace background"),
    "acceleration": ("acceleration", "faster inference", "performance optimization",
                     "higher throughput", "lower latency", "generation speed"),
    "camera control": ("camera control", "camera automation", "camera tool"),
    "voice cloning": ("voice cloning", "voice clone", "speech synthesis cloning"),
    "retrieval-augmented generation": ("RAG", "retrieval augmented generation", "local RAG"),
    "subtitle transcription and translation": ("subtitle translation", "transcription translation", "translated subtitles"),
    "motion LoRA": ("motion LoRA", "action LoRA", "movement LoRA"),
    "workflow": ("workflow", "implementation", "tool"),
    "local deployment": ("local deployment", "local setup", "self hosted"),
}
_CONSTRAINT_TERMS = {
    "local": ("本地", "local", "offline"),
    "Windows": ("windows",),
    "open source": ("open source", "開源", "开源"),
}

_HF_PIPELINE_MARKERS = (
    ("text-to-speech", ("tts", "text-to-speech", "text to speech",
                         "speech synthesis", "語音合成", "语音合成")),
    ("automatic-speech-recognition", ("asr", "automatic speech recognition",
                                       "speech recognition", "語音辨識", "语音识别")),
    ("translation", ("translation model", "translate model", "翻譯模型", "翻译模型")),
)
_HF_FORMAT_MARKERS = (
    ("gguf", ("gguf",)),
    ("lora", ("lora",)),
    ("checkpoint", ("checkpoint",)),
)
_HF_GENERIC_MARKERS = ("model", "models", "模型", "hugging face", "huggingface")
_HF_COMMAND_WORDS = {
    "find", "locate", "show", "search", "download", "use", "using", "for",
    "the", "a", "an", "please", "official", "public", "local",
}
_LANGUAGE_MARKERS = (
    "kazakh", "qazaq", "chinese", "mandarin", "cantonese", "english",
    "japanese", "korean", "french", "german", "spanish", "portuguese",
    "russian", "arabic", "hindi", "vietnamese", "thai", "indonesian",
    "哈薩克語", "哈萨克语", "中文", "普通話", "普通话", "粵語", "粤语",
    "英語", "英语", "日語", "日语", "韓語", "韩语", "法語", "法语",
    "德語", "德语", "西班牙語", "西班牙语", "葡萄牙語", "葡萄牙语",
    "俄語", "俄语", "阿拉伯語", "阿拉伯语", "越南語", "越南语", "泰語", "泰语",
)


def _contains(text: str, phrase: str) -> bool:
    if phrase.isascii() and phrase.replace("-", "").replace(" ", "").isalnum():
        return bool(re.search(rf"(?<![A-Za-z0-9-]){re.escape(phrase)}(?![A-Za-z0-9-])", text, re.I))
    return phrase.casefold() in text.casefold()


def extract_task(original_query: str) -> tuple[str, list[str]]:
    matches = [(task, phrases) for task, phrases in _TASK_RULES
               if any(_contains(original_query, phrase) for phrase in phrases)]
    if not matches:
        return "UNKNOWN", []
    task = matches[0][0]
    terms = list(dict.fromkeys(term for name, phrases in matches
                               for term in _TASK_EXPANSIONS.get(name, phrases)))
    return task, terms


def normalize_search_query(query: str) -> str:
    """Normalize a verified product alias without rewriting explicit repo paths."""
    return re.sub(r"(?<![A-Za-z0-9_./:-])chatgpt[-_ ]oss(?![A-Za-z0-9_])",
                  "gpt-oss", str(query), flags=re.IGNORECASE)


def _compact_query(query: str) -> str:
    return " ".join(str(query).split())


def separator_equivalent_key(value: str) -> str:
    """Normalize only word separators used in project names for matching."""
    compact = _compact_query(value).casefold()
    return re.sub(r"(?<=[a-z0-9])(?:[ -]+)(?=[a-z0-9])", " ", compact)


def _separator_name_parts(value: str) -> list[str]:
    """Return a conservative whole-query project name, never command prose/URLs."""
    compact = _compact_query(value)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[ -]+[A-Za-z0-9]+){1,3}", compact):
        return []
    parts = re.split(r"[ -]+", compact)
    if any(part.casefold() in _NON_ENTITY_WORDS for part in parts):
        return []
    return parts


def separator_query_variants(value: str) -> list[str]:
    """Add bounded space/hyphen spellings for a whole project name."""
    compact = _compact_query(value)
    parts = _separator_name_parts(compact)
    variants = [compact]
    if parts:
        variants.extend((" ".join(parts), "-".join(parts)))
    return list(dict.fromkeys(variant for variant in variants if variant))


def _append_query(queries: list[str], query: str) -> None:
    """Append a query once while keeping the first spelling and spacing untouched."""
    if not query or not query.strip():
        return
    key = _compact_query(query).casefold()
    if key not in {_compact_query(item).casefold() for item in queries}:
        queries.append(query)


def _task_phrases(task: str) -> list[str]:
    return list(next((phrases for name, phrases in _TASK_RULES if name == task), ()))


def _explicit_qualifiers(query: str) -> list[str]:
    """Return explicit version/language qualifiers which supplemental queries must retain."""
    qualifiers = [marker for marker in _LANGUAGE_MARKERS if _contains(query, marker)]
    qualifiers.extend(re.findall(
        r"(?<![A-Za-z0-9])(?:v?\d+(?:\.\d+)+(?:[-._][A-Za-z0-9]+)*|\d+(?:\.\d+)?[bBkKmM])(?![A-Za-z0-9])",
        query,
    ))
    return list(dict.fromkeys(qualifiers))


def _retains_markers(query: str, markers: list[str]) -> bool:
    comparable = normalize_search_query(_compact_query(query)).casefold()
    return all(normalize_search_query(_compact_query(marker)).casefold() in comparable
               for marker in markers)


def _without_search_phrases(value: str, phrases: list[str]) -> str:
    """Remove intent words while preserving an adjacent product/model identifier."""
    result = value
    for phrase in sorted(set(phrases), key=len, reverse=True):
        if phrase.isascii():
            pattern = rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])"
            result = re.sub(pattern, " ", result, flags=re.IGNORECASE)
        else:
            result = re.sub(re.escape(phrase), " ", result, flags=re.IGNORECASE)
    return " ".join(result.split())


def huggingface_search_spec(original_query: str) -> dict[str, Any] | None:
    """Return a bounded HF model-search specification when the query warrants it.

    This is intentionally shared by planning and retrieval so alias normalization and
    intent stripping cannot diverge.  It does not classify an explicit GitHub repository
    URL as a model search, and a task-only acceleration query has no usable HF target.
    """
    raw_query = str(original_query)
    normalized = _compact_query(raw_query)
    alias_normalized = _compact_query(normalize_search_query(normalized))
    if not normalized or re.search(r"(?:https?://)?(?:www\.)?github\.com/", normalized,
                                   re.IGNORECASE):
        return None

    pipeline_tag = ""
    marker_phrases: list[str] = []
    for pipeline, phrases in _HF_PIPELINE_MARKERS:
        if any(_contains(normalized, phrase) for phrase in phrases):
            pipeline_tag = pipeline
            marker_phrases.extend(phrases)
            break

    filter_tag = ""
    for candidate_filter, phrases in _HF_FORMAT_MARKERS:
        if any(_contains(normalized, phrase) for phrase in phrases):
            filter_tag = candidate_filter
            marker_phrases.extend(phrases)
            break

    has_generic_marker = any(_contains(normalized, phrase) for phrase in _HF_GENERIC_MARKERS)
    if has_generic_marker:
        marker_phrases.extend(_HF_GENERIC_MARKERS)

    task, _terms = extract_task(normalized)
    if task != "UNKNOWN":
        marker_phrases.extend(next((phrases for name, phrases in _TASK_RULES if name == task), ()))
    search_text = _without_search_phrases(normalized, marker_phrases)
    search_tokens = re.findall(
        r"[A-Za-z0-9][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)*|[\u3400-\u9fff]+",
        search_text,
    )
    search_tokens = [token for token in search_tokens
                     if token.casefold() not in _HF_COMMAND_WORDS]
    if search_tokens:
        # Preserve spelling and separators in names (for example, gpt-oss), but remove
        # command prose which is not useful to the public model API.
        search_text = " ".join(search_tokens)
    else:
        search_text = ""

    explicit_hf_intent = bool(pipeline_tag or filter_tag or has_generic_marker)
    concise_model_name = (bool(search_tokens) and len(search_tokens) <= 4 and
                          len(normalized) <= 96)
    named_task_target = task != "UNKNOWN" and bool(search_tokens)
    if not (explicit_hf_intent or concise_model_name or named_task_target):
        return None
    if not search_text and not (pipeline_tag or filter_tag):
        return None
    return {
        "original_query": raw_query,
        "normalized_query": normalized,
        "alias_normalized_query": alias_normalized,
        "search": search_text,
        "filter": filter_tag,
        "pipeline_tag": pipeline_tag,
    }


def extract_entities(original_query: str) -> list[str]:
    """Conservatively retain technical-looking Latin tokens without a brand whitelist."""
    entities: list[str] = []
    task, _ = extract_task(original_query)
    named_project = project_anchor(original_query)
    if named_project:
        entities.append(named_project)
    separator_name = _compact_query(original_query) if _separator_name_parts(original_query) else ""
    if separator_name and separator_name.casefold() not in {x.casefold() for x in entities}:
        entities.append(separator_name)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)*", original_query):
        token = match.group(0)
        folded = token.casefold()
        if folded in _NON_ENTITY_WORDS or len(token) < 2:
            continue
        technical_form = (any(char.isdigit() for char in token) or
                          any(char in "._+-" for char in token) or
                          token != token.lower())
        if task == "UNKNOWN" and not technical_form:
            continue
        if folded not in {x.casefold() for x in entities}:
            entities.append(token)
    return entities


def project_anchor(original_query: str) -> str:
    """Keep a project named immediately after a find request, including lowercase names."""
    match = re.match(
        r"\s*(?:find|locate|show)\s+(?:the\s+)?(?:official\s+)?"
        r"([A-Za-z][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)*)",
        original_query, re.I,
    )
    if not match or match.group(1).casefold() in _NON_ENTITY_WORDS:
        return ""
    return match.group(1)


def _source_preferences(original_query: str, entities: list[str], task: str) -> list[str]:
    text = original_query.casefold()
    if any(term in text for term in ("error", "issue", "troubleshoot", "錯誤", "报错", "故障")):
        return ["GitHub Issues", "Reddit"]
    hf_spec = huggingface_search_spec(original_query)
    if hf_spec:
        sources = ["Hugging Face"]
        if hf_spec.get("filter") in {"lora", "gguf", "checkpoint"}:
            sources.append("Civitai")
        sources.append("GitHub")
        return sources
    if task == "workflow" or any(term in text for term in ("tutorial", "教學", "教程")):
        return ["YouTube", "ComfyUI workflow sources"]
    return ["GitHub"] if entities else []


def deterministic_intent(original_query: str, selected_hints: list[str] | None = None) -> dict[str, Any]:
    raw_query = str(original_query)
    search_query = _compact_query(raw_query)
    alias_query = _compact_query(normalize_search_query(search_query))
    entities = extract_entities(search_query)
    task, task_terms = extract_task(search_query)
    constraints = [name for name, terms in _CONSTRAINT_TERMS.items()
                   if any(_contains(original_query, term) for term in terms)]
    hints = list(selected_hints or [])
    domain = hints[0] if len(hints) == 1 else (" / ".join(hints) if hints else "UNKNOWN")
    anchor = _without_search_phrases(search_query, _task_phrases(task)) if task != "UNKNOWN" else search_query
    if not anchor:
        anchor = " ".join(entities)
    expansions: list[str] = []
    _append_query(expansions, raw_query)
    separator_keys = set()
    for variant in separator_query_variants(search_query):
        _append_query(expansions, variant)
        if _compact_query(variant).casefold() != search_query.casefold():
            separator_keys.add(_compact_query(variant).casefold())
    if alias_query.casefold() != search_query.casefold():
        _append_query(expansions, alias_query)
    if task != "UNKNOWN":
        for phrase in _TASK_EXPANSIONS.get(task, (task,)):
            query = " ".join(part for part in (anchor, phrase) if part).strip()
            _append_query(expansions, query)
            if len(expansions) >= MAX_EXPANDED_QUERIES:
                break
    expansions = expansions[:MAX_EXPANDED_QUERIES]
    alias_suggestions = ([{
        "original": search_query,
        "alias": alias_query,
        "kind": "verified_search_alias",
    }] if alias_query.casefold() != search_query.casefold() else [])
    expanded_query_metadata = [{
        "query": query,
        "kind": ("original" if index == 0 else
                 "separator_supplement" if _compact_query(query).casefold() in separator_keys else
                 "alias_supplement" if _compact_query(query).casefold() == alias_query.casefold()
                 and alias_suggestions else "intent_supplement"),
        "source": "user_input" if index == 0 else "deterministic_planner",
    } for index, query in enumerate(expansions)]
    return {
        "original_query": raw_query,
        "normalized_query": search_query,
        "alias_normalized_query": alias_query,
        "alias_suggestions": alias_suggestions,
        "selected_hints": hints,
        "entities": entities,
        "project_anchor": named_project if (named_project := project_anchor(search_query)) else "",
        "task": task,
        "task_terms": task_terms,
        "constraints": constraints,
        "domain": domain,
        "preferred_sources": _source_preferences(original_query, entities, task),
        "entity_required": bool(entities),
        "expanded_queries": expansions,
        "expanded_query_metadata": expanded_query_metadata,
    }


def build_prompt(original_query: str, selected_hints: list[str]) -> str:
    compact_query = _compact_query(original_query)
    alias_query = _compact_query(normalize_search_query(compact_query))
    payload = json.dumps(
        {"original_query": original_query, "selected_hints": selected_hints,
         "normalized_query": compact_query,
         "alias_search_suggestion": alias_query if alias_query.casefold() != compact_query.casefold() else ""},
        ensure_ascii=False,
    )
    return f"""You are a conservative search-query planner for local AI software and workflows.
Your job is not to answer the user's question and not to recommend tools. Your only job is
to turn the user's wording into a small set of search queries for an existing search system.

Preserve the original intent, every explicitly named technology/project entity, version and
language, and original_query exactly. Never replace an explicit identity. A supplied alias is a
supplemental search spelling only; keep the user's spelling as the first query. You may translate
intent into useful English search terminology and use common technical synonyms.
Do not invent unsupported goals, compatibility, suitability, or unrelated tools. If ambiguous,
record the ambiguity. Prefer fewer high-quality queries. LOW confidence may return only the
original query. Otherwise return 3 to 6 supplemental queries distinct from the original query.
This applies generically to supported local-AI
work such as image/video, voice/audio, documents/OCR/RAG, automation/agents, local models,
and 3D/Blender/previsualization; do not hardcode any one example.

Separate named entities from the requested task. Every expanded query must retain every explicit
entity; never emit a generic task-only query when an entity was named. Source preferences are
metadata only and must not invent a backend.

Return valid JSON only, with exactly these keys:
original_query, selected_hints, intent_summary, entities, task, constraints, domain,
preferred_sources, entity_required, expanded_queries, confidence, ambiguities.
confidence must be HIGH, MEDIUM, or LOW. expanded_queries and ambiguities must be JSON arrays.
Input JSON:
{payload}"""


def _json_object(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty planner response")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("planner response does not contain a JSON object")


def parse_plan(text: str, original_query: str, selected_hints: list[str]) -> dict[str, Any]:
    value = _json_object(text)
    if set(value) != _MODEL_FIELDS:
        raise ValueError("planner response fields do not match the contract")
    queries = value.get("expanded_queries")
    ambiguities = value.get("ambiguities")
    confidence = value.get("confidence")
    if not isinstance(value.get("intent_summary"), str):
        raise ValueError("intent_summary must be text")
    for field in ("entities", "constraints", "preferred_sources"):
        if not isinstance(value.get(field), list) or any(not isinstance(x, str) for x in value[field]):
            raise ValueError(f"{field} must be a text array")
    if not isinstance(value.get("task"), str) or not isinstance(value.get("domain"), str):
        raise ValueError("task and domain must be text")
    if not isinstance(value.get("entity_required"), bool):
        raise ValueError("entity_required must be boolean")
    if confidence not in _CONFIDENCE:
        raise ValueError("invalid confidence")
    if (not isinstance(queries, list) or not queries or len(queries) > MAX_EXPANDED_QUERIES or
            any(not isinstance(query, str) or not query.strip() for query in queries)):
        raise ValueError("invalid expanded_queries")
    if not isinstance(ambiguities, list) or any(not isinstance(item, str) for item in ambiguities):
        raise ValueError("invalid ambiguities")
    baseline = deterministic_intent(original_query, selected_hints)
    original_folded = normalize_search_query(original_query).casefold()
    model_entities = [x.strip() for x in value["entities"]
                      if x.strip() and normalize_search_query(x.strip()).casefold() in original_folded]
    entities = []
    seen_entities: set[str] = set()
    for entity in [*baseline["entities"], *model_entities]:
        entity_key = normalize_search_query(entity).casefold()
        if entity_key not in seen_entities:
            seen_entities.add(entity_key)
            entities.append(entity)
    required = list(dict.fromkeys([*baseline["entities"], *_explicit_qualifiers(original_query)]))
    model_queries = [_compact_query(query) for query in queries
                     if _retains_markers(query, required)]

    # The user's exact text is authoritative and always executes first. A verified alias
    # spelling and model-planned variants are supplemental searches, never replacements.
    merged_queries: list[str] = []
    _append_query(merged_queries, original_query)
    separator_queries = [
        item["query"] for item in baseline["expanded_query_metadata"]
        if item.get("kind") == "separator_supplement"
    ]
    for query in separator_queries:
        _append_query(merged_queries, query)
    alias_query = baseline["alias_normalized_query"]
    if baseline["alias_suggestions"]:
        _append_query(merged_queries, alias_query)
    for query in model_queries:
        _append_query(merged_queries, query)
    for query in baseline["expanded_queries"][1:]:
        _append_query(merged_queries, query)
    merged_queries = merged_queries[:MAX_EXPANDED_QUERIES]
    supplemental = merged_queries[1:]
    if confidence != "LOW" and not 2 <= len(supplemental) <= MAX_EXPANDED_QUERIES:
        raise ValueError("non-low-confidence plan requires 2 to 6 supplemental queries")
    selected_task = baseline["task"] if baseline["task"] != "UNKNOWN" else (value["task"] or "UNKNOWN")
    expanded_query_metadata = []
    separator_keys = {_compact_query(query).casefold() for query in separator_queries}
    baseline_keys = {_compact_query(query).casefold()
                     for query in baseline["expanded_queries"][1:]}
    for index, query in enumerate(merged_queries):
        compact_key = _compact_query(query).casefold()
        if index == 0:
            kind, source = "original", "user_input"
        elif baseline["alias_suggestions"] and compact_key == alias_query.casefold():
            kind, source = "alias_supplement", "deterministic_planner"
        elif compact_key in separator_keys:
            kind, source = "separator_supplement", "deterministic_planner"
        elif compact_key in baseline_keys:
            kind, source = "intent_supplement", "deterministic_planner"
        else:
            kind, source = "model_supplement", "local_model"
        expanded_query_metadata.append({"query": query, "kind": kind, "source": source})
    # Caller-owned values are authoritative even if a model tries to rewrite them.
    return {
        "original_query": original_query,
        "normalized_query": baseline["normalized_query"],
        "alias_normalized_query": baseline["alias_normalized_query"],
        "alias_suggestions": baseline["alias_suggestions"],
        "selected_hints": list(selected_hints),
        "intent_summary": value["intent_summary"],
        "entities": entities,
        "project_anchor": baseline["project_anchor"],
        "task": selected_task,
        "task_terms": list(dict.fromkeys([
            *baseline["task_terms"],
            *_TASK_EXPANSIONS.get(selected_task, (selected_task,))
        ])) if selected_task != "UNKNOWN" else baseline["task_terms"],
        "constraints": list(dict.fromkeys([*baseline["constraints"], *value["constraints"]])),
        "domain": value["domain"] or baseline["domain"],
        "preferred_sources": list(dict.fromkeys(value["preferred_sources"] or baseline["preferred_sources"])),
        "entity_required": bool(entities),
        "expanded_queries": merged_queries,
        "expanded_query_metadata": expanded_query_metadata,
        "confidence": confidence,
        "ambiguities": ambiguities,
        "planner_model": MODEL_NAME,
        "planner_status": "OK",
    }


def _fallback(original_query: str, selected_hints: list[str], status: str) -> dict[str, Any]:
    intent = deterministic_intent(original_query, selected_hints)
    return {**intent,
        "intent_summary": "",
        "confidence": "LOW",
        "ambiguities": [],
        "planner_model": MODEL_NAME,
        "planner_status": status,
    }


def plan_query(
    original_query: str,
    selected_hints: list[str] | None = None,
    *,
    audit_log: Path,
    timeout: float = 60.0,
    invoke: Callable[..., str] = invoke_local_model,
) -> dict[str, Any]:
    """Plan queries locally; failure never blocks search or invokes another model."""
    hints = list(selected_hints or [])
    try:
        response = invoke(
            build_prompt(original_query, hints), audit_log,
            timeout=timeout, stage="QUERY_PLANNER",
        )
        return parse_plan(response, original_query, hints)
    except LocalOnlyError as exc:
        status = "DEGRADED" if "timeout" in str(exc).casefold() else "UNAVAILABLE"
        return _fallback(original_query, hints, status)
    except Exception:
        return _fallback(original_query, hints, "DEGRADED")
