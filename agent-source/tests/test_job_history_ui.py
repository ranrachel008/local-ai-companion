"""Real Tk event tests for read-only history and in-place lifecycle language changes.

Synthetic records contain presentation evidence only: no candidate commands are run.
The saved successful job is additionally opened read-only, when present.
"""
import hashlib
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import localization
import normal_ui
import product_cli


SAVED_JOB_ID = 'ui-20261001-235857-2d3646'
SAVED_JOBS = Path(r'C:\Users\ASUS\AppData\Local\Ornith-Solution-Scout\jobs')


def file_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file()}


def create_record(jobs_root, outcome):
    """Independent, validly bound display fixture; never a substitute for a real install."""
    job_id = 'ui-acceptance-' + outcome
    folder = jobs_root / job_id
    folder.mkdir(parents=True)
    project = jobs_root.parent / 'fixture-project'
    identity = {'repository_identity': 'example/ui-fixture',
                'canonical_url': 'https://github.com/example/ui-fixture',
                'exact_revision': 'a' * 40, 'destination': str(project)}
    source_step = {'step_id': 'source-001', 'type': 'ACQUIRE_GITHUB_SNAPSHOT',
                   'inputs': identity, 'status': 'SUCCESS'}
    source_plan = {'plan_version': 1, 'steps': [source_step]}
    steps = [
        {'step_id': 'install-001', 'type': 'CREATE_PYTHON_ENV',
         'target': 'synthetic environment', 'status': 'SUCCESS',
         'execution': {'exit_code': 0}, 'result': {'completed': True}},
        {'step_id': 'install-002', 'type': 'INSTALL_LOCAL_PYTHON_PROJECT',
         'target': str(project), 'status': 'SUCCESS',
         'execution': {'exit_code': 0}, 'result': {'completed': True}},
        {'step_id': 'install-003', 'type': 'RUN_FIXED_VERIFICATION_COMMAND',
         'target': 'synthetic verification', 'status': 'SUCCESS',
         'execution': {'exit_code': 0}, 'result': {'completed': True}},
    ]
    install_plan = {'schema_version': 1, 'status': 'READY_FOR_APPROVAL',
                    'source_identity': identity, 'steps': steps}
    source_hash = product_cli.plan_hash(source_plan)
    install_hash = product_cli.plan_hash(install_plan)
    ids = [step['step_id'] for step in steps]
    job = {'job_id': job_id, 'destination_root': str(jobs_root.parent),
           'selected_reference': {'publication_identity': 'github:example/ui-fixture',
                                  'canonical_url': identity['canonical_url']},
           'install_source': identity,
           'approval': {'plan_hash': source_hash, 'plan_version': 1,
                        'approved_step_ids': ['source-001']},
           'installation_plan_approval': {
               'plan_kind': product_cli.INSTALLATION_PLAN_KIND,
               'plan_hash': install_hash, 'plan_version': 1,
               'approved_step_ids': ids, 'reviewed_step_ids': ids, 'review_only_step_ids': []}}
    # Do not change the approved plan when constructing execution outcomes.
    executed = json.loads(json.dumps(steps))
    state = {'plan_kind': product_cli.INSTALLATION_PLAN_KIND,
             'plan_hash': install_hash, 'plan_version': 1,
             'steps': executed, 'status': 'VERIFIED'}
    if outcome != 'success':
        executed[2] = {key: value for key, value in executed[2].items()
                       if key not in {'execution', 'result'}}
        executed[2]['status'] = 'PENDING'
        if outcome == 'failure':
            executed[1].update(status='FAILED', reason='controlled command failure',
                               execution={'exit_code': 7, 'stderr': 'controlled command failure'})
            executed[1].pop('result')
            state.update(status='FAILED', stop_reason='controlled command failure')
        elif outcome == 'stopped':
            executed[1].update(status='STOPPED', reason='snapshot mismatch; command not executed')
            executed[1].pop('execution'); executed[1].pop('result')
            state.update(status='STOPPED', stop_reason='snapshot mismatch; command not executed')
        elif outcome == 'cancelled':
            executed[1].update(status='STOPPED', reason='USER_CANCELLED')
            executed[1].pop('execution'); executed[1].pop('result')
            state.update(status='STOPPED', stop_reason='USER_CANCELLED', cancel_requested=True)
        else:
            raise ValueError(outcome)
    artifacts = {
        'job.json': job, 'plan.json': source_plan,
        'execution_state.json': {'plan_kind': product_cli.SOURCE_PLAN_KIND,
                                 'plan_hash': source_hash, 'plan_version': 1,
                                 'steps': [source_step]},
        'result.json': {'final_status': 'VERIFIED', 'source_verified': True,
                        'install_verified': True},
        'install_research.json': {'status': 'COMPLETE',
                                  'source_identity': dict(identity, acquisition_verified=True)},
        'installation_plan.json': install_plan,
        'installation_execution_state.json': state,
    }
    for name, value in artifacts.items():
        (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return job_id


class JobHistoryUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / 'fixture_jobs'
        self.jobs.mkdir()
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.controller = app.ProductUIController(
            dry_run=False, first_party_index_path=self.base / 'index.json')
        self.controller.jobs_root = self.jobs
        self.ui = normal_ui.NormalProductApp(
            self.root, self.controller, None,
            locale_service=localization.Localizer('zh-TW', preferences_path=self.base / 'prefs.json'),
            developer_enabled=False)
        self.open_job = patch.object(self.controller, 'open_job', wraps=self.controller.open_job).start()
        self.addCleanup(patch.stopall)
        self.no_execute = patch('product_cli.execute_installation_plan',
                                side_effect=AssertionError('history must never execute')).start()
        self.no_approve = patch('product_cli.approve_installation_plan',
                                side_effect=AssertionError('history must never approve')).start()
        self.tick()

    def tick(self):
        self.root.update()
        self.assertEqual(self.callback_errors, [], 'Tk callback raised an exception')

    def widgets(self, parent=None):
        for child in (parent or self.root).winfo_children():
            yield child
            yield from self.widgets(child)

    def text(self):
        return '\n'.join(widget.get('1.0', 'end') for widget in self.widgets()
                         if isinstance(widget, tk.Text))

    def open_from_home(self, job_id):
        self.ui.home(reset=False)
        self.ui.history_button.invoke()
        self.tick()
        self.assertEqual(self.ui.current_view, ('job_history', None))
        self.ui.history_jobs.selection_set(job_id)
        self.ui.history_jobs.event_generate('<<TreeviewSelect>>')
        self.tick()
        self.assertFalse(self.ui.history_open_button.instate(['disabled']))
        self.ui.history_open_button.invoke()
        self.tick()
        self.open_job.assert_called_with(job_id)

    def switch_all_languages(self, job_id, expected_view):
        for code, display_name in localization.SUPPORTED_LANGUAGES.items():
            combo = next(widget for widget in self.widgets()
                         if isinstance(widget, self.ui.ttk.Combobox))
            combo.set(display_name)
            combo.event_generate('<<ComboboxSelected>>')
            self.tick()
            self.assertEqual(self.ui.localizer.language, code)
            self.assertEqual(self.ui.current_view, expected_view)
            self.assertEqual(self.controller.task_id, job_id)
            self.assertEqual(self.ui._active_job_context.job_id, job_id)
            self.assertIn(self.ui.tr('plan.lifecycle.steps'), self.text())
            self.assertIn(self.ui.tr('plan.step.install_project'), self.text())
            status_key = {'VERIFIED': 'verified', 'FAILED': 'failed', 'STOPPED': 'stopped'}[
                expected_view[1]]
            self.assertIn(self.ui.tr('plan.lifecycle.' + status_key), self.text())

    def test_history_button_selection_and_six_language_success(self):
        job_id = create_record(self.jobs, 'success')
        before = file_hashes(self.jobs)
        self.open_from_home(job_id)
        expected = ('installation_plan_lifecycle', 'VERIFIED')
        self.assertEqual(self.ui.current_view, expected)
        self.assertIn(self.ui.tr('plan.lifecycle.verified'), self.text())
        self.assertIn(self.ui.tr('plan.step.install_project'), self.text())
        self.assertEqual(self.text().count(self.ui.tr('plan.lifecycle.exit_code') + ': 0'), 3)
        self.switch_all_languages(job_id, expected)
        self.assertEqual(before, file_hashes(self.jobs))
        self.no_execute.assert_not_called(); self.no_approve.assert_not_called()

    def test_independent_failure_stop_cancel_pending_records(self):
        for outcome in ('failure', 'stopped', 'cancelled'):
            with self.subTest(outcome=outcome):
                job_id = create_record(self.jobs, outcome)
                before = file_hashes(self.jobs)
                self.open_from_home(job_id)
                expected = self.ui.current_view
                self.assertEqual(expected[0], 'installation_plan_lifecycle')
                self.assertIn(self.ui.tr('plan.lifecycle.pending'), self.text())
                if outcome == 'failure':
                    self.assertIn('controlled command failure', self.text())
                    self.assertIn(self.ui.tr('plan.lifecycle.exit_code') + ': 7', self.text())
                elif outcome == 'stopped':
                    self.assertIn('snapshot mismatch; command not executed', self.text())
                else:
                    self.assertIn('USER_CANCELLED', self.text())
                self.switch_all_languages(job_id, expected)
                self.assertEqual(before, file_hashes(self.jobs))
        self.no_execute.assert_not_called(); self.no_approve.assert_not_called()

    def test_missing_and_invalid_history_use_read_only_restore_failure(self):
        for mode in ('missing', 'invalid', 'binding'):
            with self.subTest(mode=mode):
                job_id = 'ui-broken-' + mode
                folder = self.jobs / job_id
                folder.mkdir()
                if mode == 'invalid':
                    (folder / 'job.json').write_text('{broken', encoding='utf-8')
                elif mode == 'binding':
                    (folder / 'job.json').write_text(json.dumps({'job_id': 'other'}), encoding='utf-8')
                before = file_hashes(self.jobs)
                self.open_from_home(job_id)
                self.assertEqual(self.ui.current_view, ('pre_execution_state_restore_failure', job_id))
                self.assertEqual(before, file_hashes(self.jobs))
                if mode == 'missing':
                    self.assertFalse((folder / 'job.json').exists())
        self.no_execute.assert_not_called(); self.no_approve.assert_not_called()

    @unittest.skipUnless((SAVED_JOBS / SAVED_JOB_ID / 'job.json').is_file(),
                         'UNVERIFIED: original successful job is unavailable')
    def test_original_success_via_history_events_is_read_only(self):
        self.controller.jobs_root = SAVED_JOBS
        folder = SAVED_JOBS / SAVED_JOB_ID
        before = file_hashes(folder)
        self.open_from_home(SAVED_JOB_ID)
        expected = ('installation_plan_lifecycle', 'VERIFIED')
        self.assertEqual(self.ui.current_view, expected)
        self.assertIn(self.ui.tr('plan.lifecycle.verified'), self.text())
        self.assertIn(self.ui.tr('plan.step.install_project'), self.text())
        # Only the two saved pip operations carry exit codes; env creation has none.
        self.assertEqual(self.text().count(self.ui.tr('plan.lifecycle.exit_code') + ': 0'), 2)
        self.switch_all_languages(SAVED_JOB_ID, expected)
        self.assertEqual(before, file_hashes(folder))
        self.no_execute.assert_not_called(); self.no_approve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
