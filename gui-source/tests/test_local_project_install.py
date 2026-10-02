"""Offline coverage of evidence-bound ordinary local project installation."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import executor
import installation_plan


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path(
    r"C:\Users\ASUS\AppData\Local\Ornith-Solution-Scout\jobs"
    r"\ui-20261001-182446-12eafd\install_research.json"
)


class LocalProjectInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix=".local-project-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "Example-Project"
        self.source.mkdir()
        self.repository = "Example/Example-Project"
        self.url = "https://github.com/Example/Example-Project"
        self.commit = "a" * 40
        self.command = f"pip install {self.url}/archive/main.zip"
        self.readme = f"# Quick start\n```sh\n{self.command}\n```\n"
        (self.source / "README.md").write_text(self.readme, encoding="utf-8")
        (self.source / "pyproject.toml").write_text(
            '[project]\nname = "example-project"\nversion = "1.0"\n'
            'requires-python = ">=3.10"\ndependencies = ["requests>=2.28"]\n',
            encoding="utf-8",
        )
        self.manifest = executor.build_extracted_tree_manifest(
            self.source, self.repository, self.commit, "b" * 64
        )
        self.job = self.root / "jobs" / "acquired"
        self.job.mkdir(parents=True)
        (self.job / "execution_state.json").write_text(json.dumps({"steps": [{
            "step_id": "acquire", "status": "SUCCESS",
            "inputs": {"repository": self.repository, "canonical_url": self.url,
                       "exact_commit": self.commit, "destination": str(self.source)},
            "acquisition_evidence": {"extracted_tree_manifest": self.manifest},
        }]}), encoding="utf-8")
        self.context = SimpleNamespace(
            job_dir=self.job, state=SimpleNamespace(value="SOURCE_ACQUIRED"),
            resolved_project_destination=str(self.source),
            repository_identity=self.repository, canonical_repo_url=self.url,
            exact_revision=self.commit,
        )
        self.identity = {
            "repository": self.repository, "canonical_url": self.url,
            "exact_commit": self.commit, "local_path": str(self.source),
            "acquisition_verified": True, "acquisition_step_id": "acquire",
        }
        self.evidence = {"path": "README.md", "evidence_type": "code_block",
                         "exact_text": self.command}
        tool = {"name": "Example CLI", "version": "UNKNOWN", "mandatory": "UNKNOWN",
                "evidence": [{"path": "README.md", "exact_text": "Example CLI prerequisite"}]}
        self.research = {
            "status": "COMPLETE", "source_identity": self.identity,
            "python_environment": {"manager": "UNKNOWN", "version": ">=3.10",
                "evidence": [{"path": "pyproject.toml", "exact_text": 'requires-python = ">=3.10"'}]},
            "dependencies": [
                {"package": "example-project", "version": "UNKNOWN",
                 "mandatory": "UNKNOWN", "evidence": [self.evidence]},
                {"package": "requests", "version": ">=2.28", "mandatory": True,
                 "source": "index", "evidence": [{"path": "pyproject.toml",
                                                   "exact_text": '"requests>=2.28"'}]},
                {"package": "Example CLI", "version": "UNKNOWN", "mandatory": "UNKNOWN",
                 "evidence": tool["evidence"]},
            ],
            "system_dependencies": [tool], "models": [],
            "diagnostics": {"deterministic_manifests": [{"path": "pyproject.toml"}]},
            "install_methods": [
                {"command": "npx skills add example/skill", "evidence": [{
                    "path": "README.md", "exact_text": "npx skills add example/skill"}]},
                {"command": "pip install --upgrade example-project", "evidence": [{
                    "path": "README.md", "exact_text": "pip install --upgrade example-project"}]},
            ],
        }
        self.plan = installation_plan.build_plan(self.research, "offline", self.context)

    def ready(self, plan: dict | None = None) -> tuple[dict, dict]:
        plan = copy.deepcopy(self.plan if plan is None else plan)
        body = next(step for step in plan["steps"] if step["type"] == "INSTALL_LOCAL_PYTHON_PROJECT")
        environment = next(step for step in plan["steps"] if step["type"] == "CREATE_PYTHON_ENV")
        environment.update(approved=True, status="SUCCESS")
        body.update(approved=True, status="PENDING")
        plan["status"] = "READY_FOR_APPROVAL"
        python = Path(body["inputs"]["python_executable"])
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"fake python; never executed")
        return plan, body

    def test_bound_snapshot_uses_fixed_isolated_python_argv(self) -> None:
        plan, body = self.ready()
        self.assertEqual(body["inputs"]["source_tree_manifest"], self.manifest)
        self.assertEqual(body["inputs"]["author_command"], self.command)
        self.assertEqual(body["inputs"]["execution_strategy"], "VERIFIED_FIXED_SNAPSHOT")
        packages = [row["requirement"] for step in plan["steps"]
                    if step["type"] == "INSTALL_PYTHON_PACKAGES" for row in step["inputs"]["packages"]]
        self.assertEqual(packages, ["requests>=2.28"])
        self.assertTrue(plan["system_prerequisites"])
        review = next(step for step in plan["steps"] if step["type"] == "REVIEW_DOCUMENTED_ACTION")
        self.assertTrue(review["inputs"]["system_prerequisites"])
        self.assertNotIn(self.command, str(review["inputs"]))
        self.assertIn("npx skills add example/skill", str(review["inputs"]))
        self.assertIn("pip install --upgrade example-project", str(review["inputs"]))
        with patch("executor.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="mock installation", stderr="")) as runner:
            result = executor.PlanExecutor(
                plan, job_context=self.context, review_risk_accepted=True
            ).execute_step(body)
        self.assertEqual(result["status"], "SUCCESS", result)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[0], [
            body["inputs"]["python_executable"], "-m", "pip", "install", str(self.source)])
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertEqual(Path(runner.call_args.kwargs["cwd"]), self.source)
        print("FAKE_RUNNER: isolated Python -m pip install <exact snapshot>; no -e, shell=False")

    def test_identity_or_snapshot_mismatch_stops_before_runner(self) -> None:
        for kind in ("repository", "commit", "source_path", "manifest", "changed", "added", "missing"):
            with self.subTest(kind=kind):
                plan, body = self.ready()
                if kind == "repository":
                    plan["source_identity"]["repository"] = "Other/Repository"
                elif kind == "commit":
                    plan["source_identity"]["exact_commit"] = "c" * 40
                elif kind == "source_path":
                    body["inputs"]["source_path"] = str(self.root / "other")
                elif kind == "manifest":
                    body["inputs"]["source_tree_manifest"]["files"][0]["sha256"] = "d" * 64
                elif kind == "changed":
                    (self.source / "README.md").write_text("changed", encoding="utf-8")
                elif kind == "added":
                    (self.source / "unexpected.txt").write_text("added", encoding="utf-8")
                else:
                    (self.source / "README.md").unlink()
                with patch("executor.subprocess.run") as runner:
                    result = executor.PlanExecutor(
                        plan, job_context=self.context, review_risk_accepted=True
                    ).execute_step(body)
                self.assertEqual(result["status"], "STOPPED", result)
                runner.assert_not_called()
                (self.source / "README.md").write_text(self.readme, encoding="utf-8")
                (self.source / "unexpected.txt").unlink(missing_ok=True)
                print(f"STOP_BEFORE_PIP: {kind}: {result.get('reason') or result.get('error')}")

    def test_actual_research_copy_through_build_job_plan(self) -> None:
        if not RESEARCH.is_file():
            self.skipTest(f"UNVERIFIED: actual research is missing: {RESEARCH}")
        jobs = self.root / "actual-jobs"
        job = jobs / RESEARCH.parent.name
        job.mkdir(parents=True)
        shutil.copy2(RESEARCH, job / "install_research.json")
        research = json.loads((job / "install_research.json").read_text(encoding="utf-8"))
        identity = research["source_identity"]
        source_job = research["source_acquisition_job_id"]
        acquired = jobs / source_job
        acquired.mkdir()
        for name in ("job.json", "session.json", "preflight.json", "plan.json", "execution_state.json",
                     "result.json", "install_research.json", "installation_plan.json",
                     "installation_execution_state.json"):
            original = RESEARCH.parent.parent / source_job / name
            if original.is_file():
                shutil.copy2(original, acquired / name)
        plan = installation_plan.build_job_plan(jobs, job.name)
        body = next(step for step in plan["steps"] if step["type"] == "INSTALL_LOCAL_PYTHON_PROJECT")
        binding = body["inputs"]
        self.assertEqual(binding["source_path"], identity["local_path"])
        self.assertEqual(binding["repository"].casefold(), identity["repository"].casefold())
        self.assertEqual(binding["exact_commit"], identity["exact_commit"])
        self.assertEqual(binding["source_tree_manifest"]["file_count"], 122)
        self.assertEqual(binding["source_tree_sha256"],
                         "b3d214501861d15edd5ca28648e2b35a6398a9ca6f67e896923a5404be54b1cd")
        persisted = json.loads((job / "installation_plan.json").read_text(encoding="utf-8"))
        saved_body = next(step for step in persisted["steps"] if step["type"] == "INSTALL_LOCAL_PYTHON_PROJECT")
        self.assertEqual(saved_body["inputs"], binding)
        self.assertTrue(plan["system_prerequisites"])
        packages = [row["name"].casefold() for step in plan["steps"]
                    if step["type"] == "INSTALL_PYTHON_PACKAGES" for row in step["inputs"]["packages"]]
        self.assertFalse({"node.js", "gh cli", "mcporter", "agent-reach"}.intersection(packages))
        self.assertEqual(plan["source_acquisition_job_id"], source_job)
        self.assertFalse(any("VERIFIED_PROJECT_SOURCE_INVALID" in row for row in plan["block_reasons"]))
        print(f"ACTUAL_BUILD_JOB_PLAN: {binding['repository']}@{binding['exact_commit']}; "
              f"files={binding['source_tree_manifest']['file_count']}; SHA-256={binding['source_tree_sha256']}")


if __name__ == "__main__":
    unittest.main()
