#!/usr/bin/env python3
"""Local Tkinter alpha for the Scout + Product Core workflow.

The controller is deliberately independent of Tk so lifecycle and safety rules can
be tested without a display.  It calls the existing Python modules directly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURE_ROOT = APP_ROOT.parent.with_name(APP_ROOT.parent.name + ".tmp") / "product-core-ui"
_RUNTIME_IMPORT_LOCK = threading.Lock()


def _load_runtime_modules() -> None:
    """Load the application core on demand without requiring Tk to wait for it."""
    global product_cli, product_config, solution_scout, handoff_debug, build_identity
    global JobContext, JobContextValidationError, JobContextValidationFailure, JobLifecycleState
    if "solution_scout" in globals():
        return
    with _RUNTIME_IMPORT_LOCK:
        if "solution_scout" in globals():
            return
        import build_identity as loaded_build_identity
        import handoff_debug as loaded_handoff_debug
        import product_cli as loaded_product_cli
        import product_config as loaded_product_config
        import solution_scout as loaded_solution_scout
        from job_context import (
            JobContext as LoadedJobContext,
            JobContextValidationError as LoadedJobContextValidationError,
            JobContextValidationFailure as LoadedJobContextValidationFailure,
            JobLifecycleState as LoadedJobLifecycleState,
        )

        build_identity = loaded_build_identity
        handoff_debug = loaded_handoff_debug
        product_cli = loaded_product_cli
        product_config = loaded_product_config
        solution_scout = loaded_solution_scout
        JobContext = LoadedJobContext
        JobContextValidationError = LoadedJobContextValidationError
        JobContextValidationFailure = LoadedJobContextValidationFailure
        JobLifecycleState = LoadedJobLifecycleState


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def friendly_error(error: BaseException) -> str:
    """Translate core failures without concealing their useful detail."""
    text = str(error).strip() or type(error).__name__
    translations = (
        ("explicit approval is required", "Execution is locked until you approve specific plan steps."),
        ("plan changed after approval", "The plan changed after approval. Review it and approve the new revision."),
        ("select a source first", "Select a source before running the local preflight."),
        ("preflight must complete first", "Run Preflight before building a plan."),
        ("verification has not passed", "Close is available only after verification succeeds."),
        ("plan status is BLOCKED", "This plan is blocked by a conflict or unknown. Resolve it before approval."),
        ("one or more valid step IDs", "Select at least one plan step to approve."),
    )
    lowered = text.lower()
    for needle, replacement in translations:
        if needle.lower() in lowered:
            return f"{replacement}\n\nTechnical detail: {text}"
    return text


class ProductUIController:
    """Headless view-model using Product Core as the lifecycle authority."""

    def __init__(self, *, dry_run: bool = False, fixture_root: Path | None = None,
                 search_provider: Callable[[str, int], list[dict[str, Any]]] | None = None,
                 first_party_index_path: Path | None = None,
                 first_party_fetcher: Callable[..., bytes] | None = None,
                 first_party_website_index_path: Path | None = None,
                 first_party_website_fetcher: Callable[..., bytes] | None = None,
                 first_party_refresh_interval_seconds: int | None = None) -> None:
        _load_runtime_modules()
        self.dry_run = dry_run
        self.fixture_root = (fixture_root or DEFAULT_FIXTURE_ROOT).resolve()
        self.tasks_root = (self.fixture_root / "tasks") if dry_run else solution_scout.TASKS
        self.jobs_root = (self.fixture_root / "jobs") if dry_run else solution_scout.JOBS
        self.snapshots_root = (self.fixture_root / "source_snapshots") if dry_run else solution_scout.SNAPSHOTS
        self.environment_root = self.fixture_root / "environment"
        self.source_root = self.fixture_root / "verified-source"
        self.output_root = self.fixture_root / "output"
        self.search_provider = search_provider
        self.first_party_index_path = first_party_index_path or solution_scout.FIRST_PARTY_YOUTUBE_INDEX
        self.first_party_fetcher = first_party_fetcher or solution_scout.fetch
        self.first_party_website_index_path = None
        if not dry_run and self.first_party_index_path == solution_scout.FIRST_PARTY_YOUTUBE_INDEX:
            solution_scout.ensure_first_party_youtube_baseline()
        self.first_party_website_fetcher = first_party_website_fetcher or solution_scout.fetch
        self.first_party_refresh_interval_seconds = (
            product_config.FIRST_PARTY_INDEX_REFRESH_INTERVAL_SECONDS
            if first_party_refresh_interval_seconds is None
            else max(0, first_party_refresh_interval_seconds))
        self._first_party_refresh_thread: threading.Thread | None = None
        self._first_party_refresh_lock = threading.Lock()
        self.task_id: str | None = None
        self.task: dict[str, Any] | None = None
        self.job_context: JobContext | None = None
        self.restore_failure: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None
        self.approval: dict[str, Any] | None = None
        self.last_error = ""
        if dry_run:
            self._prepare_fixture()

    def _inside_fixture(self, path: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.fixture_root)
            return True
        except (OSError, ValueError):
            return False

    def _guard_fixture_plan(self, plan: dict[str, Any]) -> None:
        if not self.dry_run:
            return
        path_keys = {"path", "source", "destination"}
        for step in plan.get("steps", []):
            for key, value in step.get("inputs", {}).items():
                if key in path_keys and value and not self._inside_fixture(value):
                    raise SystemExit(f"Fixture safety blocked a path outside the fixture root: {value}")
            url = step.get("inputs", {}).get("url") or step.get("inputs", {}).get("source_url")
            if url and not str(url).startswith("file:"):
                raise SystemExit("Fixture safety blocked a non-local source URL.")
        for item in plan.get("planned_downloads", []) + plan.get("planned_modifications", []):
            for key in path_keys:
                if item.get(key) and not self._inside_fixture(item[key]):
                    raise SystemExit(f"Fixture safety blocked a path outside the fixture root: {item[key]}")
            if item.get("url") and not str(item["url"]).startswith("file:"):
                raise SystemExit("Fixture safety blocked a non-local source URL.")

    def _prepare_fixture(self) -> None:
        for directory in (self.tasks_root, self.jobs_root, self.snapshots_root,
                          self.environment_root, self.source_root, self.output_root):
            directory.mkdir(parents=True, exist_ok=True)
        source = self.source_root / "fixture-artifact.txt"
        existing = self.environment_root / "compatible-local.txt"
        if not source.exists():
            source.write_bytes(b"Ornith harmless fixture artifact v1\n")
        if not existing.exists():
            existing.write_bytes(b"Ornith compatible local fixture v1\n")

    def _save_task(self) -> None:
        if not self.task:
            return
        self.task["updated_at"] = _now()
        product_cli.write_json(self.tasks_root / self.task["task_id"] / "session.json", self.task)

    def _load_job(self) -> dict[str, Any]:
        if not self.task_id:
            return {"state": "NEW", "approval": None}
        return product_cli.JobStore(self.jobs_root, self.task_id).job()

    def _job_directory(self, job_id: str) -> Path:
        if not job_id or Path(job_id).name != job_id:
            raise JobContextValidationError(JobContextValidationFailure(
                "INVALID_JOB_ID", "The persisted job identifier is not a safe directory name."))
        root = self.jobs_root.resolve()
        directory = (root / job_id).resolve()
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise SystemExit("The requested job is outside the configured jobs directory.") from exc
        return directory

    @staticmethod
    def _known_identity(job: dict[str, Any]) -> dict[str, Any]:
        selected = job.get("selected_sources") or []
        selected_row = selected[0] if len(selected) == 1 and isinstance(selected[0], dict) else {}
        verified = job.get("verified_source") if isinstance(job.get("verified_source"), dict) else {}
        reference = (job.get("selected_reference") or verified.get("selected_reference") or
                     verified.get("reference_source") or selected_row.get("selected_reference"))
        install_source = (job.get("install_source") or verified.get("install_source") or
                          selected_row.get("install_source"))
        install_source = install_source if isinstance(install_source, dict) else {}
        destination = (job.get("destination") if isinstance(job.get("destination"), dict) else {})
        return {
            "selected_reference": reference,
            "install_source": install_source or None,
            "repository_identity": (install_source.get("repository_identity") or
                                    install_source.get("repository") or verified.get("repo")),
            "canonical_repo_url": (install_source.get("canonical_url") or
                                   install_source.get("url") or
                                   verified.get("verified_original_project_url") or
                                   verified.get("url")),
            "exact_revision": (install_source.get("exact_revision") or
                               install_source.get("commit") or verified.get("commit")),
            "destination_root": (job.get("destination_root") or destination.get("root")),
            "resolved_project_destination": (
                job.get("resolved_project_destination") or destination.get("project")),
        }

    def _load_context(self, job_id: str, *, clear_search: bool) -> JobContext:
        directory = self._job_directory(job_id)
        self.task_id = job_id
        if clear_search:
            self.task = None
        try:
            context = JobContext.load(directory)
        except JobContextValidationError as exc:
            try:
                raw_job = product_cli.JobStore(self.jobs_root, job_id).load("job.json", {})
            except Exception:
                raw_job = {}
            self.job_context = None
            self.restore_failure = {
                "classification": "PRE-EXECUTION STATE RESTORE FAILURE",
                "job_id": job_id,
                "error_code": exc.failure.code,
                "error": str(exc),
                **self._known_identity(raw_job),
            }
            self.last_error = str(exc)
            raise
        store = product_cli.JobStore(self.jobs_root, job_id)
        raw_job = store.load("job.json", {})
        self.job_context = context
        self.restore_failure = None
        self.last_error = ""
        # Presentation compatibility only; persisted artifacts and JobContext remain authoritative.
        self.plan = store.load("plan.json", None)
        self.approval = raw_job.get("approval")
        return context

    def _refresh_context(self) -> JobContext:
        if not self.task_id:
            raise SystemExit("No active job.")
        return self._load_context(self.task_id, clear_search=False)

    def _refresh_active_context(self) -> JobContext | None:
        """Refresh only a job already admitted through canonical context validation.

        Older integrations may still call backend compatibility callbacks with a
        task id alone.  Those callbacks must not manufacture lifecycle authority
        from incomplete fixture state; open_job and committed selections remain
        the only ways to establish canonical controller context.
        """
        if self.job_context is None or self.job_context.state is JobLifecycleState.SEARCH:
            return None
        return self._refresh_context()

    def open_job(self, job_id: str) -> JobContext:
        """Open a persisted job without reconstructing selection from Search state."""
        return self._load_context(job_id, clear_search=True)

    def failure_handoff(self) -> dict[str, Any] | None:
        return dict(self.restore_failure) if self.restore_failure else None

    def search(self, goal: str, per_query: int = 5) -> list[dict[str, Any]]:
        return self.search_queries(goal, [goal], per_query=per_query)

    @staticmethod
    def _search_error(source: str, query: str, error: BaseException) -> dict[str, str]:
        """Keep provider failures actionable without discarding another source."""
        provider = str(getattr(error, "provider", "") or source)
        kind = str(getattr(error, "kind", "") or solution_scout._provider_error_kind(error))
        detail = str(getattr(error, "detail", "") or f"{type(error).__name__}: {error}")
        return {"source": provider, "kind": kind, "query": query, "error": detail}

    def _run_public_searches(self, queries: list[str], per_query: int
                             ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Run each public provider independently with a small concurrency bound."""
        unique_queries = list(dict.fromkeys(query for query in queries if query.strip()))
        try:
            bounded_limit = max(1, min(int(per_query), solution_scout.HF_MAX_CANDIDATES))
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 5
        if self.search_provider is not None:
            providers = [("custom", self.search_provider)]
        else:
            providers = [
                ("github", solution_scout.github_repository_search),
                ("huggingface", solution_scout.huggingface_model_search),
            ]

        self.search_diagnostics = []
        def run_provider(source: str, provider: Callable[[str, int], list[dict[str, Any]]]
                         ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
            found: list[dict[str, Any]] = []
            failures: list[dict[str, str]] = []
            for query in unique_queries:
                events = []
                token = solution_scout.SEARCH_TRACE.set(events)
                diagnostic = {"adapter": source, "input_query": query,
                              "limit": bounded_limit, "events": events}
                try:
                    provider_rows = provider(query, bounded_limit)
                    diagnostic.update(status="RETURNED", candidate_count=len(provider_rows),
                                      candidates=provider_rows)
                    found.extend(provider_rows)
                except Exception as exc:
                    failure = self._search_error(source, query, exc)
                    diagnostic.update(status="ERROR", error=failure)
                    failures.append(failure)
                finally:
                    solution_scout.SEARCH_TRACE.reset(token)
                    self.search_diagnostics.append(diagnostic)
            return found, failures

        rows_by_source: dict[str, list[dict[str, Any]]] = {}
        errors_by_source: dict[str, list[dict[str, str]]] = {}
        # One worker per provider avoids an unbounded request fan-out while allowing
        # GitHub and Hugging Face to participate in the same first search round.
        with ThreadPoolExecutor(max_workers=max(1, min(2, len(providers))),
                                thread_name_prefix="public-search") as pool:
            pending = {
                source: pool.submit(run_provider, source, provider)
                for source, provider in providers
            }
            for source, _provider in providers:
                rows_by_source[source], errors_by_source[source] = pending[source].result()

        rows: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for source, _provider in providers:
            rows.extend(rows_by_source[source])
            errors.extend(errors_by_source[source])
        return rows, errors

    def search_queries(self, goal: str, queries: list[str], per_query: int = 5) -> list[dict[str, Any]]:
        original_goal = str(goal)
        if not original_goal.strip():
            raise ValueError("Enter a goal before searching.")
        provider_queries = [original_goal]
        for query in queries:
            value = str(query)
            if value.strip() and value not in provider_queries:
                provider_queries.append(value)
        normalized_goal = original_goal.strip()
        self.task_id = f"ui-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        errors: list[dict[str, str]] = []
        self.search_diagnostics = []
        if self.dry_run:
            raw = [{"title": "Verified Product Core fixture",
                    "url": (self.source_root / "fixture-artifact.txt").as_uri(),
                    "date": "2026-09-16", "summary": "Local, harmless, exact-revision fixture source.",
                    "discovered_by": "fixture catalog",
                    "repository": "ornith/product-core-fixture",
                    "selected_reference": {
                        "publication_identity": "fixture-publication-v1",
                        "canonical_url": "https://example.invalid/ornith/product-core-fixture",
                        "title": "Verified Product Core fixture",
                        "source_type": "fixture",
                    },
                    "install_source": {
                        "repository_identity": "ornith/product-core-fixture",
                        "canonical_url": "https://github.com/ornith/product-core-fixture",
                        "url": "https://github.com/ornith/product-core-fixture",
                        "source_type": "github_repository",
                    }}]
            query_source = "fixture_no_web"
        else:
            raw = []
            query_source = "direct_scout_api"
            exact_request = solution_scout.github_navigation_request(normalized_goal)
            if exact_request:
                events = []
                token = solution_scout.SEARCH_TRACE.set(events)
                diagnostic = {"adapter": "github_repository_api", "input_query": normalized_goal,
                              "events": events}
                try:
                    raw = solution_scout.github_repository_exact(normalized_goal)
                    diagnostic.update(status="RETURNED", candidate_count=len(raw), candidates=raw)
                except Exception as exc:
                    failure = self._search_error("github_repository_api", normalized_goal, exc)
                    errors.append(failure)
                    diagnostic.update(status="ERROR", error=failure)
                finally:
                    solution_scout.SEARCH_TRACE.reset(token)
                    self.search_diagnostics.append(diagnostic)
                query_source = "exact_github_repository"
            else:
                raw, errors = self._run_public_searches(provider_queries, per_query)
                query_source = ("custom_search_provider" if self.search_provider is not None
                                else "github_and_huggingface_public_search")
        # Preserve all bounded provider candidates through classification and
        # ranking.  The result view applies its display limit only at the end.
        candidates = solution_scout.deduplicate(raw)
        deduplicated_count = len(candidates)
        identities: set[str] = set()
        candidates = [item for item in candidates
                      if not (item["group_id"] in identities or identities.add(item["group_id"]))]
        self.task = {
            "schema_version": 1, "task_id": self.task_id, "state": "WAITING_FOR_USER_SELECTION",
            "goal": original_goal, "queries": provider_queries, "query_source": query_source,
            "exact_repository_navigation": bool(
                candidates and candidates[0].get("exact_repository_request")),
            "candidates": candidates, "selected": [], "rejected": [], "search_errors": errors,
            "outcome": "UNKNOWN", "success": False, "created_at": _now(), "updated_at": _now(),
            "history": [{"at": _now(), "event": "SCOUT_COMPLETE"}],
        }
        self._save_task()
        product_cli.write_json(self.tasks_root / self.task_id / "search_diagnostics.json", {
            "schema_version": 1, "task_id": self.task_id, "goal": original_goal,
            "provider_queries": provider_queries, "adapters": self.search_diagnostics,
            "raw_candidate_count": len(raw), "deduplicated_count": deduplicated_count,
            "final_candidate_count": len(candidates),
            "raw_candidates": raw,
            "historical_recovery": False,
        })
        product_cli.sync_scout_job(self.jobs_root, self.task)
        product_cli.write_json(self.tasks_root / self.task_id / "scout_results.json", candidates)
        self.plan = self.approval = None
        self.restore_failure = None
        self._refresh_context()
        return candidates

    def search_supplemental(self, queries: list[str], per_query: int = 5) -> list[dict[str, Any]]:
        """Run additional Scout queries without replacing the original Scout job."""
        if self.dry_run:
            return []
        unique_queries = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
        raw, errors = self._run_public_searches(unique_queries, per_query)
        candidates = solution_scout.deduplicate(raw)
        identities: set[str] = set()
        candidates = [item for item in candidates
                      if not (item["group_id"] in identities or identities.add(item["group_id"]))]
        if self.task:
            self.task["queries"] = list(dict.fromkeys([*self.task.get("queries", []), *unique_queries]))
            self.task.setdefault("search_errors", []).extend(errors)
            self._save_task()
        return candidates

    def search_youtube(self, queries: list[str], per_query: int = 5) -> list[dict[str, Any]]:
        """Search public YouTube metadata without changing the Scout provider architecture."""
        if self.dry_run or self.search_provider is not None:
            return []
        raw: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        unique_queries = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
        for query in unique_queries:
            try:
                raw.extend(solution_scout.youtube_search(query, per_query))
            except Exception as exc:
                errors.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
        candidates = solution_scout.deduplicate(raw)
        if self.task:
            self.task["youtube_queries"] = unique_queries
            self.task.setdefault("search_errors", []).extend(errors)
            self._save_task()
        return candidates

    def search_first_party(self, queries: list[str], per_query: int = 5) -> list[dict[str, Any]]:
        """Refresh the verified channel directly before searching its local index."""
        if self.dry_run:
            return []
        youtube_index = self.refresh_first_party_index()
        merged: dict[str, dict[str, Any]] = {}
        for query in dict.fromkeys(query.strip() for query in queries if query.strip()):
            rows = solution_scout.search_first_party_youtube_index(youtube_index, query, per_query)
            for row in rows:
                identity = str(row.get("result_identity") or row.get("group_id") or row.get("url"))
                if identity not in merged:
                    merged[identity] = dict(row, matched_queries=[query])
                else:
                    current = merged[identity]
                    current["matched_queries"] = list(dict.fromkeys([*current.get("matched_queries", []), query]))
                    current["matched_fields"] = list(dict.fromkeys([
                        *current.get("matched_fields", []), *row.get("matched_fields", [])]))
                    current["retrieval_score"] = max(int(current.get("retrieval_score") or 0),
                                                     int(row.get("retrieval_score") or 0))
        ordered = sorted(merged.values(), key=lambda row: (
            -int(row.get("retrieval_score") or 0), str(row.get("title") or "").casefold()))
        return [dict(row, number=index, rank=index) for index, row in enumerate(ordered, 1)]

    def refresh_first_party_index(self) -> dict[str, Any]:
        with self._first_party_refresh_lock:
            return solution_scout.refresh_first_party_youtube_index(
                product_config.FIRST_PARTY_YOUTUBE_CHANNEL_URL,
                self.first_party_index_path, self.first_party_fetcher)

    def refresh_first_party_website_index(self) -> dict[str, Any]:
        return {"schema_version": 1, "documents": []}

    def refresh_first_party_indexes_if_due(self, *, background: bool = True) -> dict[str, Any]:
        """Refresh stale existing caches; missing caches remain a first-search bootstrap."""
        if self.dry_run:
            return {"status": "disabled", "scheduled": []}
        targets: list[str] = []
        if self.first_party_index_path.exists() and solution_scout.first_party_refresh_due(
                self.first_party_index_path, self.first_party_refresh_interval_seconds):
            targets.append("youtube")
        if not targets:
            return {"status": "fresh", "scheduled": []}
        if self._first_party_refresh_thread and self._first_party_refresh_thread.is_alive():
            return {"status": "already_running", "scheduled": targets}

        results: dict[str, Any] = {}

        def refresh() -> None:
            if "youtube" in targets:
                results["youtube"] = self.refresh_first_party_index()

        if not background:
            refresh()
            return {"status": "completed", "scheduled": targets, "results": results}
        self._first_party_refresh_thread = threading.Thread(
            target=refresh, name="first-party-index-refresh", daemon=True)
        self._first_party_refresh_thread.start()
        return {"status": "scheduled", "scheduled": targets}

    def persist_search_results(self, candidates: list[dict[str, Any]]) -> None:
        if not self.task or not self.task_id:
            return
        self.task["candidates"] = candidates
        self._save_task()
        product_cli.write_json(self.tasks_root / self.task_id / "scout_results.json", candidates)

    @staticmethod
    def _candidate_identity_tokens(candidate: dict[str, Any]) -> set[tuple[str, str]]:
        """Return stable search identity only; display numbers are deliberately excluded."""
        tokens: set[tuple[str, str]] = set()
        reference = candidate.get("selected_reference")
        if not isinstance(reference, dict):
            reference = candidate.get("reference_source")
        reference = reference if isinstance(reference, dict) else {}
        for key in ("result_identity", "group_id", "publication_identity"):
            value = candidate.get(key) or reference.get(key)
            if value:
                tokens.add((key, str(value).strip().casefold()))
        for key in ("canonical_url", "url"):
            value = reference.get(key)
            if value:
                tokens.add(("publication_url", str(value).rstrip("/").casefold()))
        if not reference:
            value = candidate.get("canonical_url") or candidate.get("url")
            if value:
                tokens.add(("candidate_url", str(value).rstrip("/").casefold()))
        return tokens

    def _active_search_candidates(self) -> list[dict[str, Any]]:
        if not self.task:
            return []
        rows = [row for row in self.task.get("candidates", []) if isinstance(row, dict)]
        # Some existing integrations attach enriched install targets to results.
        rows.extend(row for row in self.task.get("results", []) if isinstance(row, dict))
        return rows

    def _resolve_active_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        requested = self._candidate_identity_tokens(candidate)
        for row in self._active_search_candidates():
            if row is candidate or row == candidate:
                return dict(row)
            persisted = self._candidate_identity_tokens(row)
            if requested and persisted and requested.intersection(persisted):
                return dict(row)
        raise SystemExit("Select a result from the current Search results.")

    @staticmethod
    def _selection_identity(selected: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        reference = selected.get("selected_reference") or selected.get("reference_source")
        if not isinstance(reference, dict):
            reference = {}
        if not reference:
            canonical = selected.get("canonical_url") or selected.get("url")
            reference = {
                "publication_identity": (selected.get("publication_identity") or
                                         selected.get("result_identity") or
                                         selected.get("group_id") or canonical),
                "canonical_url": canonical,
                "title": selected.get("title"),
                "source_type": selected.get("source_type"),
            }
        reference = {key: value for key, value in reference.items() if value not in (None, "")}

        install_source = selected.get("install_source")
        if not isinstance(install_source, dict):
            install_source = {}
        def known(value: Any) -> str:
            text = str(value or "").strip()
            return "" if text.casefold() == "unknown" else text

        repository = known(install_source.get("repository_identity") or
                           install_source.get("repository") or selected.get("repository") or
                           selected.get("repo"))
        canonical_repo = (install_source.get("canonical_url") or install_source.get("url") or
                          selected.get("verified_original_project_url"))
        candidate_url = selected.get("canonical_url") or selected.get("url")
        if not canonical_repo and str(candidate_url or "").casefold().startswith("https://github.com/"):
            canonical_repo = candidate_url

        if not repository:
            repositories = {known(value) for value in selected.get("repository_identities", [])}
            repositories.discard("")
            if len(repositories) == 1:
                repository = repositories.pop()

        if not canonical_repo:
            evidenced_urls: list[str] = []
            for key in ("github_urls", "external_urls"):
                evidenced_urls.extend(str(value) for value in selected.get(key, []) if value)
            for link in selected.get("outbound_links", []):
                if isinstance(link, dict):
                    evidenced_urls.append(str(
                        link.get("canonical_url") or link.get("outbound_url") or
                        link.get("url") or ""))
            canonical_urls = {
                solution_scout.canonical_url(value): value
                for value in evidenced_urls
                if solution_scout.repository_identity_from_url(value)
            }
            if len(canonical_urls) == 1:
                canonical_repo = next(iter(canonical_urls))

        canonical_repo = solution_scout.canonical_url(str(canonical_repo or ""))
        repository_from_url = solution_scout.repository_identity_from_url(canonical_repo)
        if canonical_repo and repository and repository_from_url.casefold() != repository.casefold():
            raise SystemExit(
                "The selected result has conflicting repository identity; selection was not saved.")
        if not repository:
            repository = repository_from_url
        if repository or canonical_repo:
            install_source = {
                **install_source,
                "repository_identity": repository,
                "canonical_url": canonical_repo,
                "url": install_source.get("url") or canonical_repo,
                "exact_revision": (install_source.get("exact_revision") or
                                   install_source.get("commit") or selected.get("commit")),
                "source_type": install_source.get("source_type") or "github_repository",
            }
        install_source = {key: value for key, value in install_source.items()
                          if value not in (None, "")}
        if not reference.get("publication_identity"):
            raise SystemExit(
                "The selected result lacks immutable publication identity; selection was not saved.")
        if not repository or not canonical_repo:
            install_source = {}
        return reference, install_source

    def _commit_selection(self, selected: dict[str, Any], choices: list[int]) -> dict[str, Any]:
        if not self.task or not self.task_id:
            raise SystemExit("Selection is available only after Search.")
        reference, install_source = self._selection_identity(selected)
        committed = dict(selected, selected_reference=reference)
        if not committed.get("source_type") and committed.get("type") in {
                "github_repository", "official_repository"}:
            committed["source_type"] = "github_repository"
        if install_source:
            committed["install_source"] = install_source
        valid_numbers = {int(item.get("number") or 0) for item in self.task.get("candidates", [])}
        self.task["selected"] = [committed]
        self.task["rejected"] = sorted(number for number in valid_numbers if number not in choices)
        self.task["state"] = "SELECTED"
        self.task.setdefault("history", []).append({
            "at": _now(), "event": "HUMAN_SELECTION", "choices": choices,
            "result_identity": (selected.get("result_identity") or
                                selected.get("group_id")),
            "publication_identity": reference.get("publication_identity"),
            "canonical_source_url": install_source.get("canonical_url"),
        })
        self._save_task()
        product_cli.record_selection(self.jobs_root, self.task)
        store = product_cli.JobStore(self.jobs_root, self.task_id)
        job = store.job(self.task.get("goal"))
        job["selected_reference"] = reference
        if install_source:
            job["install_source"] = install_source
        else:
            job.pop("install_source", None)
        store.save_job(job)
        if install_source:
            self._refresh_context()
        else:
            self.job_context = None
        return self.task

    def select(self, numbers: list[int]) -> dict[str, Any]:
        if not self.task or self.task.get("state") != "WAITING_FOR_USER_SELECTION":
            raise SystemExit("Selection is available only after Search.")
        valid = {item["number"] for item in self.task["candidates"]}
        choices = sorted(set(numbers))
        if len(choices) != 1 or not set(choices) <= valid:
            raise SystemExit("Select exactly one candidate from the current Search results.")
        selected = next(item for item in self.task["candidates"] if item["number"] == choices[0])
        return self._commit_selection(dict(selected), choices)

    def select_result(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Select the exact Normal UI result/install target, independent of display renumbering."""
        if not self.task or self.task.get("state") != "WAITING_FOR_USER_SELECTION":
            raise SystemExit("Selection is available only after Search.")
        selected = self._resolve_active_candidate(candidate)
        display_number = int(selected.get("number") or 0)
        return self._commit_selection(selected, [display_number])

    def read_selected(self) -> list[dict[str, Any]]:
        context = self.job_context or (self._refresh_context() if self.task_id else None)
        if not context or context.state not in {
                JobLifecycleState.SOURCE_SELECTED, JobLifecycleState.SOURCE_VERIFIED}:
            raise SystemExit("Source Reader is locked until you explicitly select a source.")
        if self.dry_run:
            source = self.source_root / "fixture-artifact.txt"
            records = [{"url": context.canonical_repo_url, "title": "Verified Product Core fixture",
                        "repository": "ornith/product-core-fixture", "commit": "fixture-revision-0001",
                        "tag": "1.0", "updated_at": "2026-09-16", "readme": source.name,
                        "requirements": "none", "workflow": "fixture-only", "model_downloads": "none",
                        "issues": "none", "troubleshooting": "none", "sha256": _sha256(source).split(":", 1)[1],
                        "fetch_error": None,
                        "verified_original_project_url": context.canonical_repo_url,
                        "selected_reference": dict(context.selected_reference or {}),
                        "install_source": {**dict(context.install_source or {}),
                                           "exact_revision": "fixture-revision-0001"}}]
            product_cli.write_json(self.snapshots_root / self.task_id / "snapshot_manifest.json", records)
        else:
            # Existing Source Reader is used directly; only its storage root is scoped here.
            old = solution_scout.SNAPSHOTS
            try:
                solution_scout.SNAPSHOTS = self.snapshots_root
                if self.task:
                    reader_task = self.task
                else:
                    store = product_cli.JobStore(self.jobs_root, self.task_id)
                    reader_task = {
                        "task_id": self.task_id,
                        "state": "SELECTED",
                        "selected": store.job().get("selected_sources", []),
                    }
                # The durable install source may differ from the discovery result.
                # Give Source Reader the verified upstream identity, while retaining
                # the selected article or video as publication evidence.
                reader_task = dict(reader_task, selected=[
                    dict(row,
                         install_source=dict(context.install_source or row.get("install_source") or {}),
                         selected_reference=dict(context.selected_reference or
                                                 row.get("selected_reference") or {}))
                    for row in reader_task.get("selected", [])
                ])
                records = solution_scout.snapshot_selected(reader_task)
            finally:
                solution_scout.SNAPSHOTS = old
        if self.task:
            self.task["state"] = "SOURCES_READ"
            self.task["source_snapshots"] = records
            self.task.setdefault("history", []).append({"at": _now(), "event": "SOURCE_READER_COMPLETE"})
            self._save_task()
        if records:
            first = records[0]
            store = product_cli.JobStore(self.jobs_root, self.task_id)
            job = store.job(self.task.get("goal") if self.task else None)
            selected_reference = dict(context.selected_reference or {})
            install_source = dict(context.install_source or {})
            install_source.update({key: value for key, value in {
                "url": (install_source.get("url") or first.get("verified_original_project_url") or
                        first.get("url")),
                "canonical_url": (install_source.get("canonical_url") or
                                  first.get("verified_original_project_url") or first.get("url")),
                "repository_identity": (install_source.get("repository_identity") or
                                        first.get("repository")),
                "exact_revision": (first.get("commit") if first.get("commit") not in (None, "", "UNKNOWN")
                                   else install_source.get("exact_revision") or "UNKNOWN"),
                "source_type": install_source.get("source_type") or first.get("source_type"),
            }.items() if value not in (None, "")})
            job["selected_reference"] = selected_reference
            job["install_source"] = install_source
            job["verified_source"] = {
                "selected": True, "url": install_source.get("canonical_url"),
                "repo": install_source.get("repository_identity"),
                "commit": first.get("commit"), "package_metadata": {"version": first.get("tag")},
                "source_type": first.get("source_type"),
                "verified_original_project_url": install_source.get("canonical_url"),
                "selected_reference": selected_reference,
                "reference_source": selected_reference,
                "install_source": install_source,
                "result_identity": first.get("result_identity"),
                "provenance": first.get("provenance"),
            }
            store.save_job(job)
        self._refresh_context()
        return records

    @staticmethod
    def _unused_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def preflight(self) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("Start with Search and source selection.")
        spec: dict[str, Any] = {}
        if self.dry_run:
            spec = {"files": [str(self.environment_root / "compatible-local.txt"),
                              str(self.output_root / "fixture-artifact.txt")],
                    "ports": [{"host": "127.0.0.1", "port": self._unused_port()}]}
        result = product_cli.run_preflight(self.jobs_root, self.task_id, spec)
        self._refresh_active_context()
        return result

    def _fixture_request(self) -> dict[str, Any]:
        source = self.source_root / "fixture-artifact.txt"
        existing = self.environment_root / "compatible-local.txt"
        destination = self.output_root / source.name
        return {
            "expected_version": "1.0",
            "downloads": [
                {"identifier": existing.name, "name": existing.name, "hash": _sha256(existing),
                 "url": existing.as_uri(), "size": existing.stat().st_size,
                 "destination": str(existing)},
                {"identifier": source.name, "name": source.name, "hash": _sha256(source),
                 "url": source.as_uri(), "size": source.stat().st_size,
                 "destination": str(destination)},
            ],
            "modifications": [{"path": str(destination), "action": "copy approved fixture artifact"}],
            "verification_criteria": [{"path": str(destination), "sha256": _sha256(source),
                                       "size": source.stat().st_size}],
            "rollback_scope": [str(destination)],
            "steps": [
                {"step_id": "copy-fixture", "type": "COPY_FILE",
                 "inputs": {"source": str(source), "destination": str(destination), "source_url": source.as_uri()},
                 "expected_result": {"sha256": _sha256(source).split(":", 1)[1], "size": source.stat().st_size},
                 "approved": False, "rollback": {"delete": str(destination)}, "timeout": 5, "status": "PENDING"},
                {"step_id": "verify-fixture", "type": "VERIFY_FILE", "inputs": {"path": str(destination)},
                 "expected_result": {"exists": True, "sha256": _sha256(source).split(":", 1)[1],
                                     "size": source.stat().st_size},
                 "approved": False, "rollback": None, "timeout": 5, "status": "PENDING"},
            ],
        }

    def build_plan(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("Open a persisted job or start with Search and source selection.")
        request = self._fixture_request() if self.dry_run and request is None else (request or {})
        self.plan = product_cli.create_plan(self.jobs_root, self.task_id, None, request)
        self._guard_fixture_plan(self.plan)
        self.approval = None
        self._refresh_active_context()
        return self.plan

    def approve(self, step_ids: list[str]) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        self.approval = product_cli.approve_plan(self.jobs_root, self.task_id, step_ids, "listed steps only")
        self._refresh_active_context()
        return self.approval

    def execute(self, handlers: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        current = product_cli.JobStore(self.jobs_root, self.task_id).load("plan.json", {})
        self._guard_fixture_plan(current)
        result = product_cli.execute(self.jobs_root, self.task_id, handlers=handlers)
        self._refresh_active_context()
        return result

    def installation_plan_approval(self) -> dict[str, Any] | None:
        if not self.task_id:
            return None
        return product_cli.installation_plan_approval_status(self.jobs_root, self.task_id)

    def approve_installation_plan(self) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        result = product_cli.approve_installation_plan(self.jobs_root, self.task_id)
        self._refresh_active_context()
        return result

    def execute_installation_plan(self, handlers: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        current = product_cli.JobStore(self.jobs_root, self.task_id).load("installation_plan.json", {})
        self._guard_fixture_plan(current)
        result = product_cli.execute_installation_plan(self.jobs_root, self.task_id, handlers=handlers)
        self._refresh_active_context()
        return result

    def check_conda_precondition(self) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        return product_cli.check_conda_precondition(self.jobs_root, self.task_id)

    def install_recommended_conda(self) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        return product_cli.install_recommended_conda(self.jobs_root, self.task_id)

    def request_cancel(self, requested_by: str = "alpha-ui") -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        result = product_cli.request_cancel(self.jobs_root, self.task_id, requested_by)
        self._refresh_active_context()
        return result

    def request_installation_cancel(self, requested_by: str = "alpha-ui") -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        current = product_cli.JobStore(self.jobs_root, self.task_id).load(
            "installation_plan.json", {})
        self._guard_fixture_plan(current)
        result = product_cli.request_installation_cancel(
            self.jobs_root, self.task_id, requested_by)
        self._refresh_active_context()
        return result

    def resume(self, deviation_id: str | None = None) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        current = product_cli.JobStore(self.jobs_root, self.task_id).load("plan.json", {})
        self._guard_fixture_plan(current)
        result = product_cli.resume_job(self.jobs_root, self.task_id, deviation_id)
        self._refresh_active_context()
        return result

    def resume_installation_plan(
        self,
        deviation_id: str | None = None,
        handlers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        current = product_cli.JobStore(self.jobs_root, self.task_id).load(
            "installation_plan.json", {})
        self._guard_fixture_plan(current)
        result = product_cli.resume_installation_plan(
            self.jobs_root, self.task_id, deviation_id, handlers=handlers)
        self._refresh_active_context()
        return result

    def close(self) -> dict[str, Any]:
        if not self.task_id:
            raise SystemExit("No active job.")
        result = product_cli.close_job(self.jobs_root, self.task_id)
        self._refresh_active_context()
        return result

    def status_text(self) -> str:
        return product_cli.status(self.jobs_root, self.task_id) if self.task_id else "No active job."

    def audit_rows(self) -> list[dict[str, Any]]:
        if not self.task_id:
            return []
        path = product_cli.JobStore(self.jobs_root, self.task_id).path("audit.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def state(self) -> str:
        if not self.task_id:
            return JobLifecycleState.SEARCH.value
        return (self.job_context or self._refresh_context()).state.value

    def approval_state(self) -> str:
        if not self.task_id:
            return "NONE"
        store = product_cli.JobStore(self.jobs_root, self.task_id)
        job, value = store.job(), store.load("plan.json", {})
        approval = job.get("approval")
        if not approval:
            return "NONE"
        return "VALID" if approval.get("plan_hash") == product_cli.plan_hash(value) else "INVALID"

    def control_states(self) -> dict[str, bool]:
        context = (self.job_context or self._refresh_context()) if self.task_id else None
        lifecycle = context.state if context else JobLifecycleState.SEARCH
        store = product_cli.JobStore(self.jobs_root, self.task_id) if self.task_id else None
        source_plan = store.load("plan.json", {}) if store else {}
        source_execution = store.load("execution_state.json", {}) if store else {}
        installation_execution = store.load("installation_execution_state.json", {}) if store else {}
        source_result = store.load("result.json", {}) if store else {}
        artifacts = set(context.artifact_names) if context else set()
        active_execution = (installation_execution if "installation_plan.json" in artifacts
                            else source_execution)
        cancel_pending = bool(active_execution.get("cancel_requested")) and lifecycle not in {
            JobLifecycleState.STOPPED, JobLifecycleState.RESUMABLE}
        plan_ready = source_plan.get("status") == "READY_FOR_APPROVAL"
        source_running = (source_execution.get("state") == "RUNNING" or any(
            step.get("status") == "RUNNING" for step in source_execution.get("steps", [])))
        source_stopped = (source_execution.get("state") in {"STOPPED", "WAITING_FOR_USER", "FAILED"}
                          or any(step.get("status") in {"STOPPED", "WAITING_FOR_USER", "FAILED"}
                                 for step in source_execution.get("steps", [])))
        source_verified = (source_result.get("final_status") == "VERIFIED" and
                           "installation_plan.json" not in artifacts)
        return {
            "search": True,
            "select": bool(self.task and self.task.get("state") == "WAITING_FOR_USER_SELECTION"),
            "read": lifecycle == JobLifecycleState.SOURCE_SELECTED and
                    "snapshot_manifest.json" not in artifacts,
            "preflight": lifecycle == JobLifecycleState.SOURCE_VERIFIED and
                         "preflight.json" not in artifacts,
            "plan": "preflight.json" in artifacts and "plan.json" not in artifacts,
            "approve": bool(plan_ready and self.approval_state() != "VALID" and not cancel_pending),
            "execute": bool(source_plan and self.approval_state() == "VALID" and
                            not source_execution and not cancel_pending),
            "stop": lifecycle == JobLifecycleState.EXECUTING or source_running,
            "resume": (lifecycle == JobLifecycleState.RESUMABLE or source_stopped) and
                      not cancel_pending,
            "close": lifecycle == JobLifecycleState.VERIFIED or source_verified,
            "source_acquisition": lifecycle == JobLifecycleState.DESTINATION_SELECTED,
            "research": lifecycle == JobLifecycleState.DESTINATION_SELECTED,
            "install_research": lifecycle == JobLifecycleState.SOURCE_ACQUIRED,
            "plan_review": lifecycle in {
                JobLifecycleState.RESEARCH_COMPLETE, JobLifecycleState.PLAN_REVIEW},
            "installation_execute": lifecycle == JobLifecycleState.APPROVED and not cancel_pending,
            "corrective_action": lifecycle == JobLifecycleState.STOPPED,
            "complete": lifecycle == JobLifecycleState.VERIFIED,
        }


class ProductCoreApp:
    """Three-panel, local-only Tkinter presentation."""

    def __init__(self, root: Any, controller: ProductUIController) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root, self.controller = tk, ttk, root, controller
        root.title("Local AI Companion — Product Core Alpha")
        root.geometry("1280x820")
        self.goal = tk.StringVar(value="Validate Product Core with a harmless local fixture" if controller.dry_run else "")
        self.status_var = tk.StringVar(value="Ready")
        self.job_var = tk.StringVar(value="Job: —")
        self.approval_var = tk.StringVar(value="Approval: NONE")
        self.dry_var = tk.StringVar(value=f"DRY RUN — writes restricted to {controller.fixture_root}" if controller.dry_run else "LIVE MODE — review every plan path")
        self.ai_var = tk.StringVar(value="Local AI: not used in fixture mode" if controller.dry_run else
                                   "Local AI: search uses the configured Scout provider")
        self.planner_var = tk.StringVar(value="Query planner fallback: NOT USED")
        self.step_vars: dict[str, Any] = {}
        self._buttons: dict[str, Any] = {}
        self._execution_active = False
        self._cancel_pending = False
        self._worker_error: BaseException | None = None
        self._build()
        self.refresh()

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        mode_bar = ttk.Frame(self.root); mode_bar.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(mode_bar, text="← Back to Normal Mode", command=self.back_to_normal).pack(side="left")
        banner = ttk.Label(self.root, textvariable=self.dry_var, anchor="center")
        banner.pack(fill="x", padx=8, pady=(4, 2))
        ai_line = ttk.Frame(self.root); ai_line.pack(fill="x", padx=8)
        ttk.Label(ai_line, textvariable=self.ai_var).pack(side="left")
        ttk.Button(ai_line, text="Advanced details", command=self.show_ai_details).pack(side="right")
        top = ttk.Frame(self.root); top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Goal").pack(side="left")
        ttk.Entry(top, textvariable=self.goal).pack(side="left", fill="x", expand=True, padx=6)
        self._add_button(top, "search", "Search", self.search)
        ttk.Label(top, textvariable=self.job_var).pack(side="left", padx=10)

        workflow = ttk.Frame(self.root); workflow.pack(fill="x", padx=8, pady=(0, 6))
        for key, label, command in (("select", "Select", self.select), ("read", "Read Source", self.read_source),
                                    ("preflight", "Preflight", self.preflight), ("plan", "Build Plan", self.build_plan),
                                    ("approve", "Approve Checked", self.approve), ("execute", "Execute", self.execute),
                                    ("stop", "Stop", self.stop), ("resume", "Resume", self.resume),
                                    ("close", "Close", self.close)):
            self._add_button(workflow, key, label, command)
        ttk.Label(workflow, textvariable=self.approval_var).pack(side="right", padx=8)

        panes = ttk.Panedwindow(self.root, orient="horizontal"); panes.pack(fill="both", expand=True, padx=8)
        self.sources = ttk.Frame(panes); self.plan_panel = ttk.Frame(panes); self.execution = ttk.Frame(panes)
        panes.add(self.sources, weight=1); panes.add(self.plan_panel, weight=1); panes.add(self.execution, weight=1)
        self.candidate_list = tk.Listbox(self.sources, selectmode="extended")
        ttk.Label(self.sources, text="Source selection — raw URLs remain visible").pack(anchor="w")
        ttk.Label(self.sources, textvariable=self.planner_var, justify="left", wraplength=390).pack(fill="x", pady=(2, 5))
        self.candidate_list.pack(fill="both", expand=True)
        ttk.Button(self.sources, text="Open selected source", command=self.open_source).pack(fill="x", pady=4)

        ttk.Label(self.plan_panel, text="Plan review — check only steps you approve").pack(anchor="w")
        self.plan_text = tk.Text(self.plan_panel, wrap="word", state="disabled", height=16)
        self.plan_text.pack(fill="both", expand=True)
        self.steps_frame = ttk.LabelFrame(self.plan_panel, text="Atomic steps")
        self.steps_frame.pack(fill="x", pady=4)

        ttk.Label(self.execution, text="Execution status").pack(anchor="w")
        self.execution_text = tk.Text(self.execution, wrap="word", state="disabled", height=10)
        self.execution_text.pack(fill="x")
        ttk.Label(self.execution, text="Append-only audit — select a row to expand details").pack(anchor="w", pady=(6, 0))
        self.audit_tree = ttk.Treeview(self.execution, columns=("time", "action", "result"), show="headings", height=10)
        for key, label, width in (("time", "Timestamp", 150), ("action", "Action", 105), ("result", "Result", 85)):
            self.audit_tree.heading(key, text=label); self.audit_tree.column(key, width=width, stretch=True)
        self.audit_tree.pack(fill="both", expand=True)
        self.audit_tree.bind("<<TreeviewSelect>>", self.show_audit_detail)
        self.audit_detail = tk.Text(self.execution, wrap="word", state="disabled", height=6)
        self.audit_detail.pack(fill="x")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill="x", padx=8, pady=6)

    def back_to_normal(self) -> None:
        """Return to the normal UI while preserving the current controller state."""
        if self._execution_active:
            from tkinter import messagebox
            messagebox.showinfo("Execution in progress", "Wait for the current safe operation to finish before returning to Normal Mode.")
            return
        for child in self.root.winfo_children():
            child.destroy()
        from normal_ui import NormalProductApp
        NormalProductApp(
            self.root,
            self.controller,
            ProductCoreApp,
        )

    def _add_button(self, parent: Any, key: str, label: str, command: Callable[[], None]) -> None:
        button = self.ttk.Button(parent, text=label, command=command)
        button.pack(side="left", padx=2)
        self._buttons[key] = button

    def _run(self, action: Callable[[], Any]) -> None:
        from tkinter import messagebox
        try:
            action()
            self.status_var.set("Completed. Review the updated state before continuing.")
        except BaseException as exc:
            self.status_var.set(friendly_error(exc).splitlines()[0])
            messagebox.showerror("Action blocked", friendly_error(exc))
        self.refresh()

    def search(self) -> None:
        self._run(lambda: self.controller.search(self.goal.get()))

    def select(self) -> None:
        self._run(lambda: self.controller.select([int(i) + 1 for i in self.candidate_list.curselection()]))

    def read_source(self) -> None: self._run(self.controller.read_selected)
    def preflight(self) -> None: self._run(self.controller.preflight)
    def build_plan(self) -> None: self._run(self.controller.build_plan)
    def approve(self) -> None:
        chosen = [key for key, var in self.step_vars.items() if var.get()]
        self._run(lambda: self.controller.approve(chosen))
    def _start_execution(self, action: Callable[[], Any]) -> None:
        if self._execution_active:
            return
        self._execution_active = True
        self._worker_error = None
        self.status_var.set("Execution in progress...")
        self.refresh()

        def worker() -> None:
            try:
                action()
            except BaseException as exc:
                self._worker_error = exc
            finally:
                self._execution_active = False

        threading.Thread(target=worker, name="product-core-executor", daemon=True).start()
        self.root.after(50, self._poll_execution)

    def _poll_execution(self) -> None:
        from tkinter import messagebox
        self.refresh()
        if self._execution_active:
            self.root.after(50, self._poll_execution)
            return
        if self._worker_error is not None:
            self.status_var.set(friendly_error(self._worker_error).splitlines()[0])
            messagebox.showerror("Action blocked", friendly_error(self._worker_error))
        elif self.controller.state() == "STOPPED" and self.controller.status_text().find("USER_CANCELLED") >= 0:
            self.status_var.set("EXECUTION STOPPED — Reason: User cancelled")
        else:
            self.status_var.set("Completed. Review the updated state before continuing.")
        self._cancel_pending = False
        self.refresh()

    def execute(self) -> None: self._start_execution(self.controller.execute)
    def resume(self) -> None:
        from tkinter import messagebox
        store = product_cli.JobStore(self.controller.jobs_root, self.controller.task_id)
        execution = store.load("execution_state.json", {})
        if execution.get("cancel_requested"):
            if not messagebox.askyesno("Resume cancelled execution",
                                       "Explicitly resume from the next safe pending step?"):
                return
            self._start_execution(lambda: self.controller.resume("USER_CANCELLED"))
            return
        stopped = next((x for x in execution.get("steps", [])
                        if x.get("status") in {"STOPPED", "WAITING_FOR_USER", "FAILED"}), {})
        if stopped and not messagebox.askyesno(
                "Approve continuation",
                "Confirm that the displayed deviation or manual action is resolved and approve continuation."):
            return
        self._run(lambda: self.controller.resume(stopped.get("deviation_id") or stopped.get("reason")))
    def close(self) -> None: self._run(self.controller.close)
    def stop(self) -> None:
        try:
            self.controller.request_cancel()
            self._cancel_pending = True
            self.status_var.set("STOP REQUESTED — Waiting for current safe step to finish...")
            self.refresh()
        except BaseException as exc:
            self.status_var.set(friendly_error(exc).splitlines()[0])

    def show_ai_details(self) -> None:
        from tkinter import messagebox
        detail = ("Fixture mode performs no web research and makes no local-AI calls."
                  if self.controller.dry_run else
                  "Scout search runs first. The Local AI Companion Query Planner is called only when the existing relevance filter finds no useful result. Product Core execution never invokes local AI.")
        messagebox.showinfo("Local AI details", detail)

    def show_audit_detail(self, _event: Any = None) -> None:
        selected = self.audit_tree.selection()
        rows = self.controller.audit_rows()
        if selected:
            index = int(selected[0].removeprefix("audit-"))
            if index < len(rows):
                self._set_text(self.audit_detail, json.dumps(rows[index], indent=2, ensure_ascii=False))

    def open_source(self) -> None:
        if self.controller.task:
            indices = self.candidate_list.curselection()
            if indices:
                webbrowser.open(self.controller.task["candidates"][int(indices[0])]["url"])

    def _set_text(self, widget: Any, value: str) -> None:
        widget.configure(state="normal"); widget.delete("1.0", "end"); widget.insert("1.0", value); widget.configure(state="disabled")

    def refresh(self) -> None:
        tk = self.tk
        self.job_var.set(f"Job: {self.controller.task_id or '—'} | State: {self.controller.state()}")
        approval = self.controller.approval_state()
        record = self.controller._load_job().get("approval") if self.controller.task_id else None
        self.approval_var.set(f"Approval: {approval}" + (f" | {record.get('plan_hash', '')[:22]}…" if record else ""))
        self.candidate_list.delete(0, "end")
        task = self.controller.task or {}
        if "planner_status" not in task:
            self.planner_var.set("Query planner fallback: NOT USED (original Scout path only)")
        else:
            self.planner_var.set(
                "Query planner fallback\n"
                f"ORIGINAL QUERY: {task.get('original_query', '')}\n"
                f"SELECTED HINTS: {task.get('selected_hints', [])}\n"
                f"INTENT SUMMARY: {task.get('intent_summary', '')}\n"
                f"CONFIDENCE: {task.get('confidence', '')}\n"
                f"AMBIGUITIES: {task.get('ambiguities', [])}\n"
                f"EXPANDED QUERIES: {task.get('expanded_queries', [])}\n"
                f"PLANNER STATUS: {task.get('planner_status', '')}\n"
                f"PLANNER MODEL: {task.get('planner_model', '')}"
            )
        for row in task.get("candidates", []):
            diagnostics = ""
            if row.get("relevance_class"):
                diagnostics = (f"\nRelevance: {row['relevance_class']} | domain={row.get('domain_evidence', [])} "
                               f"| task={row.get('task_evidence', [])} | {row.get('rejection_reason', '')}")
            if row.get("curated"):
                curated = row["curated"]
                diagnostics += (f"\nCurated: {curated.get('curated_match_class', 'CURATED_MATCH')} "
                                f"| evidence={curated.get('curated_match_evidence', [])} "
                                f"| verified={curated.get('verified_status', '')} {curated.get('verified_date', '')}")
            self.candidate_list.insert("end", f"{row['number']}. {row['title']}\n{row['url']}{diagnostics}")
        plan = self.controller.plan
        if self.controller.task_id:
            plan = product_cli.JobStore(self.controller.jobs_root, self.controller.task_id).load("plan.json", plan)
            self.controller.plan = plan
        if plan:
            counts = (f"Revision {plan.get('plan_version')} | Hash {product_cli.plan_hash(plan)}\n"
                      f"Reuse {len(plan.get('local_assets_to_reuse', []))} | Download {len(plan.get('planned_downloads', []))} | "
                      f"Modify {len(plan.get('planned_modifications', []))} | Conflicts {len(plan.get('cross_project_conflicts', []))} | "
                      f"Unknown {len(plan.get('unknowns', []))} | Verification {len(plan.get('verification_criteria', []))}\n\n")
            self._set_text(self.plan_text, counts + product_cli.format_plan(plan))
        else:
            self._set_text(self.plan_text, "No plan yet.")
        for child in self.steps_frame.winfo_children(): child.destroy()
        self.step_vars = {}
        for step in (plan or {}).get("steps", []):
            safe_default = step.get("type") in {"VERIFY_FILE", "CHECK_PORT", "CHECK_MODEL", "VERIFY_PACKAGE"}
            var = tk.BooleanVar(value=safe_default); self.step_vars[step["step_id"]] = var
            self.ttk.Checkbutton(self.steps_frame, variable=var,
                                 text=f"{step['step_id']} — {step['type']} — {step.get('status', 'PENDING')}").pack(anchor="w")
        audit = self.controller.audit_rows()
        self._set_text(self.execution_text, self.controller.status_text())
        for item in self.audit_tree.get_children(): self.audit_tree.delete(item)
        for index, row in enumerate(audit):
            self.audit_tree.insert("", "end", iid=f"audit-{index}", values=(
                row.get("timestamp", row.get("at", "")),
                row.get("step_id", row.get("action", "")), row.get("result", "")))
        for key, enabled in self.controller.control_states().items():
            if self._execution_active:
                enabled = key == "stop" and not self._cancel_pending
            self._buttons[key].configure(state="normal" if enabled else "disabled")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local AI Companion Product Core local alpha UI")
    parser.add_argument("--dry-run", action="store_true", help="use an isolated, no-web fixture workspace")
    parser.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    parser.add_argument("--build-info-output", type=Path,
                        help="write this running program's own build identity and exit")
    parser.add_argument(
        "--developer",
        action="store_true",
        help="show the Developer Mode entry in the normal UI",
    )
    args = parser.parse_args(argv)
    if args.build_info_output:
        import build_identity
        args.build_info_output.parent.mkdir(parents=True, exist_ok=True)
        args.build_info_output.write_text(json.dumps({
            "build_info": build_identity.read_build_info(),
            "build_info_path": str(build_identity.build_info_path()),
            "frozen": bool(getattr(sys, "frozen", False)),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    import tkinter as tk

    startup_started = time.perf_counter()
    root = tk.Tk()
    width = min(1480, max(960, root.winfo_screenwidth() - 80))
    height = min(900, max(640, root.winfo_screenheight() - 100))
    root.geometry(f"{width}x{height}")
    root.minsize(min(1000, width), min(680, height))
    root.title("Local AI Companion — 正在載入首頁…")
    root.configure(background="#f3f3f3")

    loading = tk.Frame(root, background="#ffffff", padx=34, pady=30)
    loading.place(relx=0.5, rely=0.5, anchor="center")
    tk.Label(
        loading,
        text="Local AI Companion",
        background="#ffffff",
        foreground="#1a1a1a",
        font=("Segoe UI", 18, "bold"),
    ).pack(anchor="w")
    loading_status = tk.StringVar(value="正在準備首頁，視窗目前可以移動或關閉。")
    tk.Label(
        loading,
        textvariable=loading_status,
        background="#ffffff",
        foreground="#626262",
        font=("Segoe UI", 11),
    ).pack(anchor="w", pady=(10, 0))

    startup_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    runtime: dict[str, Any] = {}

    def load_runtime() -> None:
        """Load non-visual dependencies without blocking Tk's event loop."""
        try:
            _load_runtime_modules()
            controller = ProductUIController(dry_run=args.dry_run, fixture_root=args.fixture_root)
            controller._developer_enabled = bool(args.developer)
            import normal_ui
            startup_results.put(("ready", (controller, normal_ui, time.perf_counter())))
        except BaseException as exc:
            detail = traceback.format_exc()
            try:
                if "handoff_debug" in globals():
                    handoff_debug.trace(
                        "runtime_startup_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        traceback=detail,
                    )
            finally:
                startup_results.put(("error", (exc, detail)))

    def refresh_indexes_later(controller: Any) -> None:
        refresh_indexes = getattr(controller, "refresh_first_party_indexes_if_due", None)
        if callable(refresh_indexes):
            try:
                refresh_indexes()
            except BaseException as exc:
                handoff_debug.trace(
                    "runtime_index_refresh_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    def finish_startup(controller: Any, normal_ui: Any, dependencies_ready: float) -> None:
        loading.destroy()
        runtime["app"] = normal_ui.NormalProductApp(
            root,
            controller,
            ProductCoreApp,
            developer_enabled=bool(args.developer),
        )
        expected_root = Path(__file__).resolve().parent
        module_paths = {
            "app": str(Path(__file__).resolve()),
            "normal_ui": str(Path(normal_ui.__file__).resolve()),
            "solution_scout": str(Path(solution_scout.__file__).resolve()),
            "product_cli": str(Path(product_cli.__file__).resolve()),
        }
        ui_ready = time.perf_counter()
        handoff_debug.trace(
            "runtime_startup",
            **module_paths,
            python_executable=sys.executable,
            cwd=os.getcwd(),
            sys_path=list(sys.path),
            expected_project_root=str(expected_root),
            modules_from_expected_project={
                name: Path(path).parent == expected_root for name, path in module_paths.items()
            },
            debug_log=str(handoff_debug.DEBUG_LOG),
            dependency_load_seconds=round(dependencies_ready - startup_started, 3),
            ui_ready_seconds=round(ui_ready - startup_started, 3),
        )

        def mark_input_ready() -> None:
            app = runtime["app"]
            app.focus_primary_input()
            goal = getattr(app, "goal", None)
            geometry = {}
            if goal is not None and bool(goal.winfo_exists()):
                geometry = {
                    "input_root_x": goal.winfo_rootx(),
                    "input_root_y": goal.winfo_rooty(),
                    "input_width": goal.winfo_width(),
                    "input_height": goal.winfo_height(),
                }
            handoff_debug.trace(
                "runtime_input_ready",
                elapsed_seconds=round(time.perf_counter() - startup_started, 3),
                root_x=root.winfo_rootx(),
                root_y=root.winfo_rooty(),
                root_width=root.winfo_width(),
                root_height=root.winfo_height(),
                **geometry,
            )
            threading.Thread(
                target=refresh_indexes_later,
                args=(controller,),
                name="first-party-index-refresh-scheduler",
                daemon=True,
            ).start()

        # Let Tk finish the first layout/paint pass before focusing the search
        # box or starting optional background maintenance.  This keeps the
        # initial home window interactive and records usable screen geometry.
        root.after(150, mark_input_ready)

    def poll_startup() -> None:
        try:
            state, payload = startup_results.get_nowait()
        except queue.Empty:
            root.after(30, poll_startup)
            return
        if state == "ready":
            finish_startup(*payload)
            return
        exc, _detail = payload
        root.title("Local AI Companion — 首頁載入失敗")
        loading_status.set(f"首頁載入失敗：{type(exc).__name__}: {exc}")

    def start_loading() -> None:
        threading.Thread(
            target=load_runtime,
            name="solution-scout-startup-loader",
            daemon=True,
        ).start()
        poll_startup()

    root.after(0, start_loading)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
