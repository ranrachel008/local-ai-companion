import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import executor


class ExecutorRequirementTests(unittest.TestCase):
    def test_complete_pep508_requirement_is_single_pip_argument(self):
        requirement = 'yt-dlp[default]>=2026.07.04; python_version >= "3.10"'
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "approved-env"
            environment.mkdir()
            python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
            package = {
                "name": "yt-dlp",
                "canonical_name": "yt-dlp",
                "extras": "[default]",
                "specifier": ">=2026.07.04",
                "marker": 'python_version >= "3.10"',
                "requirement": requirement,
                "scope": "CORE",
                "mandatory": True,
                "optional": False,
                "executable": True,
                "source_manifests": ["pyproject.toml"],
                "evidence": [{"path": "pyproject.toml", "exact_text": requirement}],
            }
            inputs = {
                "environment_path": str(environment),
                "target_environment": str(environment),
                "python_executable": str(python),
                "packages": [package],
                "source_manifests": ["pyproject.toml"],
            }
            plan = {"status": "READY_FOR_APPROVAL", "environment": {"path": str(environment)}}
            step = {
                "step_id": "install-001",
                "type": "INSTALL_PYTHON_PACKAGES",
                "inputs": inputs,
                "expected_result": {"completed": True},
                "approved": True,
                "timeout": 30,
                "status": "PENDING",
            }

            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(executor.subprocess, "run", return_value=completed) as runner:
                result = executor.PlanExecutor(plan).execute_step(step)

            self.assertEqual(result["status"], "SUCCESS")
            argv = runner.call_args.args[0]
            self.assertEqual(argv, [str(python), "-m", "pip", "install", requirement])
            self.assertIs(argv[-1], requirement)
            self.assertFalse(runner.call_args.kwargs["shell"])

            for invalid_requirement in (
                    'yt-dlp[default]>=2026.07.04; python_version < "3.10"',
                    "yt-dlp @ https://example.invalid/yt-dlp.whl"):
                invalid_step = copy.deepcopy(step)
                invalid_step["status"] = "PENDING"
                invalid_step["inputs"]["packages"][0]["requirement"] = invalid_requirement
                with self.subTest(requirement=invalid_requirement):
                    with patch.object(executor.subprocess, "run") as invalid_runner:
                        invalid_result = executor.PlanExecutor(plan).execute_step(invalid_step)
                    self.assertEqual(invalid_result["status"], "STOPPED")
                    invalid_runner.assert_not_called()

    def test_conditional_requirements_and_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "approved-env"
            environment.mkdir()
            python = environment / ("python.exe" if sys.platform == "win32" else "bin/python")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
            packages = []
            for marker in ('python_version < "3.10"', 'python_version >= "3.10"'):
                requirement = f"yt-dlp[default]>=2026.07.04; {marker}"
                packages.append({
                    "name": "yt-dlp", "canonical_name": "yt-dlp", "extras": "[default]",
                    "specifier": ">=2026.07.04", "marker": marker, "requirement": requirement,
                    "scope": "CORE", "mandatory": True, "optional": False, "executable": True,
                    "source_manifests": ["pyproject.toml"],
                    "evidence": [{"path": "pyproject.toml", "exact_text": requirement}],
                })
            plan = {"status": "READY_FOR_APPROVAL", "environment": {"path": str(environment)}}
            step = {
                "step_id": "install-001", "type": "INSTALL_PYTHON_PACKAGES",
                "inputs": {
                    "environment_path": str(environment), "target_environment": str(environment),
                    "python_executable": str(python), "packages": packages,
                    "source_manifests": ["pyproject.toml"],
                },
                "expected_result": {"completed": True}, "approved": True,
                "timeout": 30, "status": "PENDING",
            }
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            extras_variant = copy.deepcopy(packages[0])
            extras_variant["extras"] = ""
            extras_variant["requirement"] = f"yt-dlp>=2026.07.04; {extras_variant['marker']}"
            extras_variant["evidence"][0]["exact_text"] = extras_variant["requirement"]
            for identity_field, approved_packages in (
                    ("marker", packages), ("extras", [packages[0], extras_variant])):
                with self.subTest(identity_field=identity_field):
                    valid_step = copy.deepcopy(step)
                    valid_step["inputs"]["packages"] = approved_packages
                    with patch.object(executor.subprocess, "run", return_value=completed) as runner:
                        result = executor.PlanExecutor(plan).execute_step(valid_step)
                    self.assertEqual(result["status"], "SUCCESS")
                    runner.assert_called_once()
                    self.assertEqual(runner.call_args.args[0], [
                        str(python), "-m", "pip", "install",
                        *[package["requirement"] for package in approved_packages],
                    ])
                    self.assertFalse(runner.call_args.kwargs["shell"])

            for invalid_case in ("duplicate", "url", "specifier_mismatch"):
                with self.subTest(invalid_case=invalid_case):
                    invalid_step = copy.deepcopy(step)
                    invalid_packages = invalid_step["inputs"]["packages"]
                    if invalid_case == "duplicate":
                        invalid_packages[1] = copy.deepcopy(invalid_packages[0])
                    elif invalid_case == "url":
                        invalid_packages[0]["requirement"] = "yt-dlp @ https://example.invalid/yt-dlp.whl"
                    else:
                        invalid_packages[0]["specifier"] = "==2026.07.04"
                    with patch.object(executor.subprocess, "run") as invalid_runner:
                        result = executor.PlanExecutor(plan).execute_step(invalid_step)
                    self.assertEqual(result["status"], "STOPPED")
                    invalid_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
