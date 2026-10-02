"""Strict local-only OpenAI-compatible model gate for Solution Scout."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from product_config import ALLOW_CLOUD_AI, LOCAL_AI_BASE_URL, LOCAL_AI_MODEL, LOCAL_AI_PROVIDER

MODEL_PROVIDER = LOCAL_AI_PROVIDER
MODEL_NAME = LOCAL_AI_MODEL
MODEL_ENDPOINT = LOCAL_AI_BASE_URL.rstrip("/")
# Compatibility alias for callers reconstructed from the earlier Ollama build.
OLLAMA_ENDPOINT = MODEL_ENDPOINT

if ALLOW_CLOUD_AI:
    raise RuntimeError("Cloud AI must remain disabled for Local AI Companion")


class LocalOnlyError(RuntimeError):
    """The mandatory local model path cannot be used."""


class LocalModelUnavailableError(LocalOnlyError):
    """The local model service cannot be reached."""


class LocalModelNotFoundError(LocalOnlyError):
    """The service is reachable but the configured model is absent."""


class LocalModelTimeoutError(LocalOnlyError):
    """The local model service did not respond before the timeout."""


class LocalModelInvalidResponseError(LocalOnlyError):
    """The service responded, but the response cannot be safely consumed."""


class LocalModelTruncatedResponseError(LocalModelInvalidResponseError):
    """The service explicitly stopped because the output token limit was reached."""

    def __init__(self, message: str, partial_response: str = "") -> None:
        super().__init__(message)
        self.partial_response = partial_response


class LocalModelHTTPError(LocalOnlyError):
    """HTTP failure with response details retained for auditing."""

    def __init__(self, status: int, response_body: str, endpoint: str) -> None:
        super().__init__(f"Local model HTTP failure: status={status}")
        self.status = status
        self.response_body = response_body
        self.endpoint = endpoint


# Public compatibility names retained for existing first-party callers.
OllamaUnavailableError = LocalModelUnavailableError
OllamaTimeoutError = LocalModelTimeoutError
OllamaInvalidResponseError = LocalModelInvalidResponseError
OllamaHTTPError = LocalModelHTTPError

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)
_FINAL_LABEL = re.compile(r"(?im)^\s*(?:final answer|final translation|translation|translated text|purpose)\s*:\s*")
_MODEL_META = re.compile(
    r"(?im)^\s*(?:the user (?:wants|asked|is asking|has asked)|"
    r"i (?:need|should|must|will|have to|am going to|was asked)|let me(?:\s|\b)|"
    r"(?:the )?(?:prompt|instructions?) (?:asks?|requires?|says?|wants?)|"
    r"(?:my|the) task is(?:\s|\b))"
)


def extract_final_answer(payload: Any) -> str:
    """Return presentation-safe final content while discarding model reasoning."""
    value: Any = payload
    if isinstance(payload, dict):
        message = payload.get("message")
        candidates = [
            payload.get("final"),
            message.get("content") if isinstance(message, dict) else None,
            payload.get("content"), payload.get("response"), payload.get("output"),
        ]
        value = next((item for item in candidates if isinstance(item, str) and item.strip()), "")
    if not isinstance(value, str):
        raise LocalModelInvalidResponseError("Local model final response field is not text")
    text = _THINK_BLOCK.sub("", value).strip()
    if _THINK_CLOSE.search(text):
        text = _THINK_CLOSE.split(text)[-1].strip()
    opening = _THINK_OPEN.search(text)
    if opening:
        text = text[:opening.start()].strip()
    text = _THINK_OPEN.sub("", _THINK_CLOSE.sub("", text)).strip()
    final_label = _FINAL_LABEL.search(text)
    if final_label:
        text = text[final_label.end():].strip()
    if not text or _MODEL_META.search(text):
        raise LocalModelInvalidResponseError("Local model returned reasoning/meta output without a safe final answer")
    return text


def _request_json(request: urllib.request.Request, timeout: float) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise LocalModelHTTPError(int(exc.code), response_body, request.full_url) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise LocalModelTimeoutError(f"Local model timed out at {MODEL_ENDPOINT}: {exc}") from exc
        raise LocalModelUnavailableError(f"Local model unreachable at {MODEL_ENDPOINT}: {exc}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise LocalModelTimeoutError(f"Local model timed out at {MODEL_ENDPOINT}: {exc}") from exc
    except OSError as exc:
        raise LocalModelUnavailableError(f"Local model unreachable at {MODEL_ENDPOINT}: {exc}") from exc
    if not 200 <= status < 300:
        raise LocalModelHTTPError(status, body.decode("utf-8", errors="replace"), request.full_url)
    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalModelInvalidResponseError("Local model returned invalid JSON") from exc


def verify_local_model(timeout: float = 5.0) -> dict[str, Any]:
    endpoint = f"{MODEL_ENDPOINT}/models"
    status, payload = _request_json(urllib.request.Request(endpoint, method="GET"), timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise LocalModelInvalidResponseError("OpenAI-compatible model list is malformed")
    available = {item.get("id") for item in payload["data"] if isinstance(item, dict)}
    if MODEL_NAME not in available:
        raise LocalModelNotFoundError(f"Required local model is not listed: {MODEL_NAME}")
    return {"provider": MODEL_PROVIDER, "model": MODEL_NAME, "endpoint": MODEL_ENDPOINT,
            "models_endpoint": endpoint, "http_status": status}


def _append_audit(log_path: Path, record: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def invoke_local_model(
    prompt: str,
    audit_log: Path,
    timeout: float = 300.0,
    stage: str = "UNSPECIFIED",
    think: bool | None = None,
    diagnostic: dict[str, Any] | None = None,
    num_ctx: int = 8192,
    max_output_tokens: int = 1024,
) -> str:
    """Invoke Strata using OpenAI chat completions.

    ``think`` and ``num_ctx`` remain accepted for source compatibility. They are
    intentionally not sent because they are Ollama-only request fields. Strata's
    supported ``reasoning_effort`` switch and tool switch keep this text-only
    inference path free of hidden reasoning and tool execution.
    """
    if not prompt.strip():
        raise LocalOnlyError("Prompt is empty")
    if not isinstance(max_output_tokens, int) or max_output_tokens < 1:
        raise LocalOnlyError("max_output_tokens must be a positive integer")
    started = time.perf_counter()
    timestamp = datetime.now(timezone.utc).isoformat()
    endpoint = f"{MODEL_ENDPOINT}/chat/completions"
    status: int | None = None
    response_text = ""
    error: str | None = None
    http_error_body: str | None = None
    compatibility = {"think": {"value": think, "behavior": "accepted_not_sent"},
                     "num_ctx": {"value": num_ctx, "behavior": "accepted_not_sent"}}
    if diagnostic is not None:
        diagnostic.update({"endpoint": endpoint, "compatibility_parameters": compatibility,
                           "tools_enabled": False, "reasoning_effort": "none",
                           "max_output_tokens": max_output_tokens})
    try:
        verify_local_model(timeout=min(timeout, 5.0))
        request_payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "reasoning_effort": "none",
            "strata_mcp": False,
        }
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
        status, payload = _request_json(request, timeout)
        choices = payload.get("choices") if isinstance(payload, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        message = choice.get("message") if isinstance(choice, dict) else None
        raw_response = message.get("content") if isinstance(message, dict) else None
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        reasoning_response = message.get("reasoning_content") if isinstance(message, dict) else None
        response_text = raw_response if isinstance(raw_response, str) else ""
        metadata = {
            "finish_reason": finish_reason,
            "has_reasoning_content": isinstance(reasoning_response, str) and bool(reasoning_response),
            "reasoning_content_length": len(reasoning_response) if isinstance(reasoning_response, str) else 0,
        }
        if isinstance(payload, dict):
            for key in ("id", "created", "usage"):
                if key in payload:
                    metadata[key] = payload[key]
        if diagnostic is not None:
            diagnostic.update({"http_status": status, "raw_response": response_text,
                               "response_metadata": metadata})
        if finish_reason == "length":
            raise LocalModelTruncatedResponseError(
                "Local model output was truncated because the output limit was reached", response_text)
        if not isinstance(raw_response, str):
            raise LocalModelInvalidResponseError("OpenAI-compatible response lacks choices[0].message.content text")
        sanitized = extract_final_answer(response_text)
        if diagnostic is not None:
            diagnostic["sanitized_response"] = sanitized
        return sanitized
    except LocalOnlyError as exc:
        error = str(exc)
        if isinstance(exc, LocalModelHTTPError):
            status = exc.status
            http_error_body = exc.response_body
        raise
    finally:
        record = {
            "timestamp": timestamp, "provider": MODEL_PROVIDER, "local": True,
            "model": MODEL_NAME, "endpoint": endpoint, "stage": stage,
            "compatibility_parameters": compatibility, "tools_enabled": False,
            "reasoning_effort": "none",
            "max_output_tokens": max_output_tokens,
            "success": error is None, "no_cloud_fallback": True,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "response_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
            "response_length": len(response_text),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3), "http_status": status,
        }
        if error is not None:
            record["error"] = error
        if http_error_body is not None:
            record["http_error_body"] = http_error_body
        _append_audit(audit_log, record)


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict local-only OpenAI-compatible model gate")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--audit-log", type=Path)
    parser.add_argument("--stage", default="UNSPECIFIED")
    args = parser.parse_args()
    try:
        if args.health_check:
            print(json.dumps(verify_local_model(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.prompt_file is None or args.audit_log is None:
            parser.error("use --health-check or provide --prompt-file and --audit-log")
        print(invoke_local_model(args.prompt_file.read_text(encoding="utf-8"), args.audit_log, stage=args.stage))
        return 0
    except (LocalOnlyError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
