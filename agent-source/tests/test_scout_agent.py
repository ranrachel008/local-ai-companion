"""Focused Agent boundary tests; no GUI, LLM, candidate code or installation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import app
import scout_agent as agent
from tests.test_job_history_ui import create_record, file_hashes


class ScoutAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / 'saved_jobs'
        self.jobs.mkdir()
        self.workspace = self.base / 'agent_workspace'
        self.provider_calls = []

        def provider(goal, limit):
            self.provider_calls.append((goal, limit))
            return [{'url': 'https://github.com/example/project', 'title': 'Example',
                     'summary': 'A source-backed candidate', 'date': 'UNKNOWN',
                     'source_type': 'GitHub', 'first_party': True}]

        self.core = app.ProductUIController(search_provider=provider,
                                            first_party_index_path=self.base / 'index.json')
        self.adapter = agent.ScoutAgent(self.workspace, self.jobs, controller=self.core)

    def test_existing_search_delegation_and_private_job(self):
        result = self.adapter.call('scout_search', {'goal': 'Find a subtitle tool', 'limit': 2})
        self.assertTrue(result['ok'], result)
        data = result['data']
        self.assertEqual(self.provider_calls, [('Find a subtitle tool', 2)])
        self.assertEqual(data['candidates'][0]['url'], 'https://github.com/example/project')
        self.assertFalse(data['approval_granted'])
        self.assertFalse(data['execution_started'])
        self.assertTrue((self.workspace / 'jobs' / data['job_id'] / 'job.json').is_file())
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_saved_results_preserved_and_original_jobs_unchanged(self):
        for outcome in ('success', 'failure', 'stopped', 'cancelled'):
            with self.subTest(outcome=outcome):
                job_id = create_record(self.jobs, outcome)
                before = file_hashes(self.jobs / job_id)
                result = self.adapter.call('scout_job', {'job_id': job_id})
                self.assertTrue(result['ok'], result)
                state = result['data']['records']['installation_execution_state']
                self.assertEqual(state['steps'][0]['execution']['exit_code'], 0)
                if outcome == 'success':
                    self.assertEqual(state['status'], 'VERIFIED')
                    self.assertEqual(state['steps'][1]['execution']['exit_code'], 0)
                else:
                    self.assertEqual(state['steps'][2]['status'], 'PENDING')
                    self.assertNotIn('execution', state['steps'][2])
                    if outcome == 'failure':
                        self.assertEqual(state['steps'][1]['execution']['exit_code'], 7)
                    else:
                        self.assertNotIn('execution', state['steps'][1])
                self.assertTrue(result['data']['read_only'])
                self.assertEqual(file_hashes(self.jobs / job_id), before)

    def test_saved_research_plan_and_human_review_preserved(self):
        job_id = create_record(self.jobs, 'success')
        before = file_hashes(self.jobs / job_id)
        research = self.adapter.call('scout_research', {'job_id': job_id})
        self.assertTrue(research['ok'], research)
        self.assertEqual(research['data']['research']['source_identity']['exact_revision'], 'a' * 40)
        plan = self.adapter.call('scout_plan', {'job_id': job_id})
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['data']['plan']['steps'][1]['type'], 'INSTALL_LOCAL_PYTHON_PROJECT')
        self.assertTrue(plan['data']['human_review_required'])
        self.assertFalse(plan['data']['approval_granted'])
        self.assertEqual(file_hashes(self.jobs / job_id), before)

    def test_missing_plan_uses_real_core_entry_on_private_copy(self):
        job_id = create_record(self.jobs, 'success')
        directory = self.jobs / job_id
        (directory / 'installation_plan.json').unlink()
        (directory / 'installation_execution_state.json').unlink()
        job = json.loads((directory / 'job.json').read_text(encoding='utf-8'))
        job.pop('installation_plan_approval')
        (directory / 'job.json').write_text(json.dumps(job), encoding='utf-8')
        before = file_hashes(directory)
        import installation_plan
        with patch.object(installation_plan, 'build_job_plan', wraps=installation_plan.build_job_plan) as build:
            result = self.adapter.call('scout_plan', {'job_id': job_id})
        self.assertTrue(result['ok'], result)
        build.assert_called_once_with(self.workspace / 'jobs', job_id)
        self.assertTrue((self.workspace / 'jobs' / job_id / 'installation_plan.json').is_file())
        self.assertFalse((directory / 'installation_plan.json').exists())
        self.assertEqual(file_hashes(directory), before)

    def test_tampered_plan_stops_at_canonical_validation(self):
        job_id = create_record(self.jobs, 'success')
        path = self.jobs / job_id / 'installation_plan.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['steps'][1]['target'] = 'tampered destination'
        path.write_text(json.dumps(value), encoding='utf-8')
        result = self.adapter.call('scout_job', {'job_id': job_id})
        self.assertFalse(result['ok'], result)
        self.assertNotEqual(result['error']['code'], 'CORE_ERROR')
        self.assertFalse((self.workspace / 'jobs' / job_id).exists())

    def test_model_cannot_confirm_execute_or_supply_paths(self):
        for name, arguments, code in (
            ('scout_execute', {}, 'TOOL_NOT_ALLOWED'),
            ('scout_search', {'goal': 'x', 'approved': True}, 'INVALID_ARGUMENT'),
            ('scout_job', {'job_id': '../original'}, 'INVALID_ARGUMENT'),
            ('scout_search', {'goal': 'x', 'limit': True}, 'INVALID_ARGUMENT'),
            ('scout_research', {'job_id': 'x', 'analyze': 'yes'}, 'INVALID_ARGUMENT'),
        ):
            with self.subTest(name=name, arguments=arguments):
                result = self.adapter.call(name, arguments)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], code)
        self.assertEqual(self.provider_calls, [])

    def test_stdio_protocol_notifications_and_utf8(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'method': 'tools/call',
             'params': {'name': 'scout_search', 'arguments': {'goal': '不得執行'}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'scout_job', 'arguments': {'job_id': 'missing'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'},
        ]
        process = subprocess.run(
            [sys.executable, str(Path(agent.__file__).resolve()), '--workspace', str(self.workspace)],
            input='\n'.join(json.dumps(item, ensure_ascii=False) for item in messages) + '\nnot-json\n',
            text=True, encoding='utf-8', capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([item['id'] for item in replies], [1, 2, 3, 4, None])
        self.assertEqual(len(replies[1]['result']['tools']), 4)
        self.assertEqual(replies[2]['result']['structuredContent']['error']['code'], 'JOB_NOT_FOUND')
        self.assertEqual(replies[-1]['error']['code'], -32700)
        self.assertEqual(list((self.workspace / 'jobs').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
