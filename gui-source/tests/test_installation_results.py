"""Offline execution/persistence/UI regression; never installs candidate software."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import executor
import installation_plan
import localization
import normal_ui
import product_cli


class InstallationResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tr = localization.Localizer('zh-CN', preferences_path=self.root / 'prefs.json').tr

    def execute(self, outcome):
        store = product_cli.JobStore(self.root, 'result-test')
        steps = [installation_plan._step(i, 'CREATE_PYTHON_ENV', 'test environment',
                 str(self.root / f'env-{i}'), {'manager': 'conda', 'python_version': '3.10',
                 'environment_path': str(self.root / f'env-{i}')}, []) for i in range(1, 4)]
        plan = {'schema_version': 1, 'status': 'READY_FOR_APPROVAL', 'steps': steps}
        store.save('installation_plan.json', plan)
        store.job('offline result test')
        product_cli.approve_installation_plan(self.root, store.job_id)
        calls = []

        def handler(step):
            calls.append(step['step_id'])
            if outcome == 'cancel_final' and step['step_id'] == 'install-003':
                product_cli.request_installation_cancel(self.root, store.job_id)
            if step['step_id'] == 'install-002':
                if outcome == 'failure':
                    step['execution'] = {'exit_code': 7, 'stderr': 'controlled command failure',
                                         'verification_result': {'status': 'FAILED'}}
                    raise RuntimeError('controlled command failure')
                if outcome == 'guard':
                    raise RuntimeError('snapshot mismatch; command not executed')
                if outcome == 'cancel':
                    product_cli.request_installation_cancel(self.root, store.job_id)
            # Real reversible local I/O, substituted for Conda in this offline test.
            Path(step['target']).mkdir()
            return {'completed': True}

        state = product_cli.execute_installation_plan(
            self.root, store.job_id, handlers={'CREATE_PYTHON_ENV': handler})
        saved = store.load('installation_execution_state.json')
        self.assertEqual(state, saved)
        self.assertEqual(store.job()['installation_plan_state'], state['status'])
        app = normal_ui.NormalProductApp.__new__(normal_ui.NormalProductApp)
        app.controller = SimpleNamespace(jobs_root=self.root)
        reopened = app._persisted_execution_result(SimpleNamespace(job_id=store.job_id))
        text = normal_ui.installation_lifecycle_text(reopened, self.tr)
        self.assertNotIn(self.tr('plan.lifecycle.unknown'), text)
        self.assertIn('各步结果', text)
        return state, text, calls

    def test_success_is_persisted_and_reopened(self):
        state, text, calls = self.execute('success')
        self.assertEqual(state['status'], 'VERIFIED')
        self.assertEqual(len(calls), 3)
        self.assertIn('安装成功', text)
        self.assertIn('结果: 成功', text)

    def test_conda_command_failure_keeps_actual_evidence(self):
        step = installation_plan._step(1, 'CREATE_PYTHON_ENV', 'create environment',
            str(self.root/'env'), {'manager': 'conda', 'python_version': '3.10',
            'environment_path': str(self.root/'env')}, [])
        step['approved'] = True
        worker = executor.PlanExecutor({'status': 'READY_FOR_APPROVAL', 'steps': [step]},
                                       conda_executable='controlled-conda')
        with patch('executor.subprocess.run', return_value=SimpleNamespace(
                returncode=9, stdout='conda output', stderr='conda failure')) as runner:
            worker.run()
        self.assertEqual(runner.call_args.kwargs['shell'], False)
        self.assertEqual(step['execution']['exit_code'], 9)
        self.assertEqual(installation_plan.execution_status({'steps': [step]}), 'FAILED')
        text = normal_ui.installation_lifecycle_text({'steps': [step]}, self.tr)
        self.assertIn('退出码: 9', text)
        self.assertIn('conda failure', text)

    def test_failure_displays_exit_code_and_pending_step(self):
        state, text, calls = self.execute('failure')
        self.assertEqual(state['status'], 'FAILED')
        self.assertEqual(len(calls), 2)
        self.assertIn('退出码: 7', text)
        self.assertIn('controlled command failure', text)
        self.assertIn('未执行', text)

    def test_guard_stop_is_not_command_failure(self):
        state, text, calls = self.execute('guard')
        self.assertEqual(state['status'], 'STOPPED')
        self.assertEqual(len(calls), 2)
        self.assertIn('已停止', text)
        self.assertIn('snapshot mismatch', text)
        self.assertIn('未执行', text)

    def test_cancel_preserves_completed_steps(self):
        state, text, calls = self.execute('cancel')
        self.assertEqual(state['status'], 'STOPPED')
        self.assertEqual(state['stop_reason'], 'USER_CANCELLED')
        self.assertEqual([s['status'] for s in state['steps']], ['SUCCESS', 'SUCCESS', 'PENDING'])
        self.assertEqual(len(calls), 2)
        self.assertIn('USER_CANCELLED', text)

    def test_cancel_after_final_success_is_not_reported_as_stopped(self):
        state, text, calls = self.execute('cancel_final')
        self.assertEqual(state['status'], 'VERIFIED')
        self.assertIsNone(state['stop_reason'])
        self.assertEqual(len(calls), 3)
        self.assertIn('安装成功', text)
        self.assertNotIn('USER_CANCELLED', text)

    def test_legacy_records_do_not_inherit_acquisition_success(self):
        result = {'final_status': 'VERIFIED', 'execution_state': {'steps': [
            {'type': 'INSTALL_LOCAL_PYTHON_PROJECT', 'status': 'STOPPED', 'reason': 'guard'},
            {'type': 'RUN_FIXED_VERIFICATION_COMMAND', 'status': 'PENDING'}]}}
        text = normal_ui.installation_lifecycle_text(result, self.tr)
        self.assertIn('已停止', text)
        self.assertNotIn('安装成功', text)
        self.assertEqual(installation_plan.execution_status({'steps': []}), '')
        self.assertEqual(installation_plan.execution_status({'status': 'VERIFIED',
            'steps': [{'status': 'SUCCESS'}, {'status': 'PENDING'}]}), 'RUNNING')
        self.assertEqual(installation_plan.execution_status({'cancel_requested': True,
            'steps': [{'status': 'SUCCESS'}]}), 'VERIFIED')
        for status in ('SUCCESS', 'FAILED', 'STOPPED'):
            with self.subTest(status=status):
                state = {'steps': [{'type': 'INSTALL_LOCAL_PYTHON_PROJECT', 'status': status}]}
                self.assertNotIn(self.tr('plan.lifecycle.unknown'),
                                 normal_ui.installation_lifecycle_text(state, self.tr))

    def test_project_labels_and_results_in_all_languages(self):
        plan = {'status': 'READY_FOR_APPROVAL', 'steps': [
            {'type': 'INSTALL_LOCAL_PYTHON_PROJECT', 'target': 'source'},
            {'type': 'INSTALL_LOCAL_EDITABLE_PROJECT', 'target': 'source'}]}
        for language in localization.SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                tr = localization.Localizer(language, preferences_path=self.root/'prefs.json').tr
                text = normal_ui.installation_plan_review_text(plan, {}, tr)
                self.assertIn(tr('plan.step.install_project'), text)
                self.assertIn(tr('plan.step.install_editable_project'), text)
                self.assertNotIn(tr('plan.step.other'), text)
                result = {'steps': [{**plan['steps'][0], 'status': 'SUCCESS',
                                    'result': {'completed': True}, 'execution': {'exit_code': 0}}]}
                rendered = normal_ui.installation_lifecycle_text(result, tr)
                self.assertIn(tr('plan.lifecycle.verified'), rendered)
                self.assertIn(tr('plan.lifecycle.exit_code') + ': 0', rendered)
                self.assertNotIn('plan.lifecycle.', rendered)


if __name__ == '__main__':
    unittest.main()
