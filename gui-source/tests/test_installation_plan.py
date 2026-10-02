import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import installation_plan


class InstallationPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "lingbot-map"
        self.research = {
            "status": "COMPLETE",
            "source_identity": {
                "repository": "Robbyant/lingbot-map",
                "canonical_url": "https://github.com/Robbyant/lingbot-map",
                "exact_commit": "a" * 40,
                "local_path": str(self.source),
            },
            "python_environment": {
                "value": "Python 3.10 in conda",
                "evidence": [{"path": "README.md", "text": "conda create -n lingbot python=3.10"}],
            },
            "dependencies": [],
            "diagnostics": {"deterministic_manifests": [
                {"path": "requirements.txt"}, {"path": "pyproject.toml"},
            ]},
            "models": [],
            "install_methods": [{
                "command": "pip install -r requirements.txt",
                "evidence": [{"path": "README.md", "heading": "Installation"}],
            }],
        }

    def tearDown(self):
        self.temp.cleanup()

    def _set_core_python_research(self, version):
        self.research["python_environment"] = {
            "manager": "UNKNOWN", "version": version,
            "evidence": [{"path": "pyproject.toml", "exact_text": f'requires-python = "{version}"'}],
        }
        self.research["dependencies"] = [
            {"package": "requests", "version": ">=2.28", "mandatory": True,
             "source": "index", "evidence": [{"path": "pyproject.toml", "exact_text": '"requests>=2.28"'}]},
        ]
        self.research["install_methods"] = []

    def test_product_managed_isolation_with_unknown_author_manager(self):
        self._set_core_python_research(">=3.10")
        plan = installation_plan.build_plan(self.research, "offline-compatible-python")

        self.assertNotIn("DOCUMENTED_ISOLATED_ENVIRONMENT_UNKNOWN", plan["block_reasons"])
        env_steps = [step for step in plan["steps"] if step["type"] == "CREATE_PYTHON_ENV"]
        self.assertEqual(len(env_steps), 1)
        self.assertEqual(env_steps[0]["inputs"]["manager"], "conda")
        self.assertEqual(env_steps[0]["inputs"]["python_version"], "3.10")
        policy = next(item for item in env_steps[0]["evidence"]
                      if item.get("policy") == "PRODUCT_MANAGED_ISOLATION")
        self.assertEqual(policy["author_environment_manager"], "UNKNOWN")
        self.assertEqual(policy["supported_install_step_types"], ["INSTALL_PYTHON_PACKAGES"])
        self.assertEqual(policy["documented_pip_commands"], [])
        self.assertIn(self.research["python_environment"]["evidence"][0], env_steps[0]["evidence"])
        self.assertEqual(self.research["python_environment"]["manager"], "UNKNOWN")
        packages = next(step["inputs"]["packages"] for step in plan["steps"]
                        if step["type"] == "INSTALL_PYTHON_PACKAGES")
        self.assertEqual([package["requirement"] for package in packages], ["requests>=2.28"])

    def test_product_managed_isolation_blocks_incompatible_python(self):
        self._set_core_python_research(">=3.11")
        plan = installation_plan.build_plan(self.research, "offline-incompatible-python")

        self.assertEqual(plan["status"], "BLOCKED")
        self.assertTrue(plan["block_reasons"])
        self.assertFalse(any(step["type"] == "CREATE_PYTHON_ENV" for step in plan["steps"]))

    def test_requirement_suffixes_and_system_dependency_routing(self):
        marker = 'python_version >= "3.10"'
        self.research["dependencies"] = [
            {"package": f"yt-dlp[default]>=2026.07.04; {marker}", "version": "UNKNOWN",
             "mandatory": True, "source": "index", "evidence": [{"path": "pyproject.toml",
             "exact_text": f'"yt-dlp[default]>=2026.07.04; {marker}"'}]},
            {"package": "requests", "version": ">=2.28", "mandatory": True,
             "source": "index", "evidence": [{"path": "pyproject.toml",
             "exact_text": '"requests>=2.28"'}]},
        ]
        steps, manual, _ = installation_plan._python_package_steps(self.research, self.root / "env")
        packages = steps[0]["inputs"]["packages"]
        self.assertEqual([item["requirement"] for item in packages], [
            "requests>=2.28", f"yt-dlp[default]>=2026.07.04; {marker}",
        ])
        yt_dlp = next(item for item in packages if item["name"] == "yt-dlp")
        self.assertEqual((yt_dlp["extras"], yt_dlp["specifier"], yt_dlp["marker"]),
                         ("[default]", ">=2026.07.04", marker))
        self.assertEqual(manual, [])

        tool_evidence = {
            name: {"path": "README.md", "heading": "Prerequisites", "exact_text": f"Install {name}"}
            for name in ("Node.js", "gh CLI", "mcporter")
        }
        self.research["dependencies"] = [
            {"package": "yt-dlp", "version": "[default]>=2026.07.04", "mandatory": True,
             "source": "index", "evidence": [{"path": "pyproject.toml",
             "exact_text": '"yt-dlp[default]>=2026.07.04"'}]},
            *({"package": name, "version": "UNKNOWN", "mandatory": True,
               "source": "UNKNOWN", "evidence": [evidence]}
              for name, evidence in tool_evidence.items()),
        ]
        self.research["system_dependencies"] = [
            {"name": name, "evidence": [evidence]} for name, evidence in tool_evidence.items()
        ]
        steps, manual, _ = installation_plan._python_package_steps(self.research, self.root / "env")
        packages = steps[0]["inputs"]["packages"]
        self.assertEqual([item["requirement"] for item in packages],
                         ["yt-dlp[default]>=2026.07.04"])
        self.assertEqual(manual, [])
