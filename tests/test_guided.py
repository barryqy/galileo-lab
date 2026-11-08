import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import galileo_lab as lab
import observability_tests as workflow
from test_observability_tests import MemoryClient, TestLLM


class GuidedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = MemoryClient()
        self.ids = {'project_id': 'project', 'log_stream_id': 'stream'}
        self.output = io.StringIO()
        self.pause = patch.object(workflow.time, 'sleep')
        self.pause.start()
        self.addCleanup(self.pause.stop)

    def run_suite(self, suite):
        with contextlib.redirect_stdout(io.StringIO()):
            return workflow.run_suite(self.client, self.ids, TestLLM(), suite,
                state_dir=self.root, console_url='https://console.example.test/saved-tenant')

    def invoke(self, *args, tty=False):
        self.output = io.StringIO()
        with patch.object(lab, 'STATE_DIR', self.root), patch.object(lab, 'client_or_exit', return_value=self.client):
            with patch.object(self.output, 'isatty', return_value=tty), contextlib.redirect_stdout(self.output):
                try:
                    parsed = lab.build_parser().parse_args(list(args))
                except SystemExit as exc:
                    self.fail(f'CLI rejected required arguments {args}: {exc}')
                parsed.func(parsed)
        return self.output.getvalue()

    def navigation(self, feature, **values):
        try:
            from guided_navigation import save_navigation
        except ImportError:
            self.fail('Saved feature navigation is missing')
        save_navigation(self.root, feature, self.client, self.ids,
            console_url='https://console.example.test/saved-tenant', **values)

    def test_guided_run_has_values_and_no_automatic_urls(self):
        with patch.object(lab, 'ensure_project_and_stream', return_value=self.ids):
            with patch.object(lab.DevNetLLM, 'from_env', return_value=TestLLM()):
                output = self.invoke('run-tests', '--guided')
        self.assertNotIn('https://', output)
        self.assertIn('Purpose:', output)
        self.assertIn('Readiness:', output)
        self.assertIn('Tokens:', output)
        self.assertIn('Exact traces read back: 3/3', output)

    def test_guided_findings_retains_missing_values_and_warnings(self):
        report = self.run_suite('baseline')
        trace = self.client.traces[report['trace_ids'][0]]
        trace['spans'][-1]['finish_reason'] = 'length'
        trace['spans'][-1]['user_metadata']['request_count'] = '2'
        output = self.invoke('findings', '--guided')
        self.assertNotIn('https://', output)
        self.assertIn('unavailable', output)
        self.assertIn('Warning: response reached output limit', output)
        self.assertIn('Retries occurred', output)

    def test_guided_compare_suppresses_both_link_sets_and_keeps_caveat(self):
        self.run_suite('baseline')
        self.run_suite('efficient')
        output = self.invoke('compare-tests', '--guided')
        self.assertNotIn('https://', output)
        self.assertIn('does not establish equal answer quality', output)

    def test_default_findings_and_open_ui_preserve_links(self):
        report = self.run_suite('baseline')
        self.assertIn('Splunk Observability UI: https://', self.invoke('findings'))
        self.assertIn(report['run_id'], self.invoke('open-ui', '--run', report['run_id']))

    def test_baseline_checkpoint_ignores_latest_privacy_run_and_uses_saved_tenant(self):
        baseline = self.run_suite('baseline')
        self.run_suite('privacy')
        trace = self.client.traces[baseline['trace_ids'][0]]
        trace['session_id'] = 'baseline-session'
        output = self.invoke('open-ui', '--checkpoint', 'baseline-tokens')
        urls = [line for line in output.splitlines() if line.startswith('https://')]
        self.assertEqual(len(urls), 1)
        self.assertIn('/saved-tenant/project/project/agent-streams/stream/sessions/baseline-session?', urls[0])
        self.assertIn('spanId=' + trace['spans'][-1]['id'], urls[0])
        self.assertIn('Tokens', output)
        self.assertIn(baseline['run_id'], output)

    def test_checkpoint_tty_emphasizes_entire_literal_url_on_own_line(self):
        self.run_suite('baseline')
        with patch.dict('os.environ', {'TERM': 'xterm'}, clear=True):
            output = self.invoke('open-ui', '--checkpoint', 'baseline-response', tty=True)
        url_lines = [line for line in output.splitlines() if 'https://' in line]
        self.assertEqual(len(url_lines), 1)
        self.assertTrue(url_lines[0].startswith('\033[1;36mhttps://'))
        self.assertTrue(url_lines[0].endswith('\033[0m'))

    def test_wrong_suite_run_is_rejected_before_api_read(self):
        report = self.run_suite('privacy')
        self.client.reads.clear()
        with self.assertRaisesRegex(ValueError, 'baseline'):
            self.invoke('open-ui', '--checkpoint', 'baseline-response', '--run', report['run_id'])
        self.assertEqual(self.client.reads, [])

    def test_optional_checkpoint_rejects_batch_run(self):
        with self.assertRaisesRegex(ValueError, '--run'):
            self.invoke('open-ui', '--checkpoint', 'dataset', '--run', 'unrelated')

    def test_missing_optional_evidence_never_falls_back_to_latest_batch(self):
        self.run_suite('baseline')
        with self.assertRaisesRegex(ValueError, 'dataset'):
            self.invoke('open-ui', '--checkpoint', 'dataset')
        self.assertNotIn('https://', self.output.getvalue())

    def test_pending_batch_is_truthful_and_keeps_one_entry_link(self):
        report = self.run_suite('baseline')
        for trace_id in report['trace_ids']:
            self.client.pending[trace_id] = 20
        output = self.invoke('open-ui', '--checkpoint', 'baseline-latency')
        self.assertIn('indexing', output)
        self.assertEqual(output.count('https://'), 1)
        self.assertNotIn('/sessions/', output)

    def test_privacy_metadata_selects_fictional_contact_case(self):
        report = self.run_suite('privacy')
        output = self.invoke('open-ui', '--checkpoint', 'privacy-metadata')
        self.assertIn(report['cases'][0]['trace_id'], output)
        self.assertIn('local', output)
        self.assertIn('pii_input', output)

    def test_optional_entry_uses_verified_feature_routes_and_saved_ids(self):
        for feature, checkpoint, values, wanted, entry_url in [
            ('dataset', 'dataset', {'dataset_id': 'data-17', 'name': 'Refund cases'}, 'data-17', 'https://console.example.test/saved-tenant/datasets'),
            ('experiment', 'experiment-candidate', {'variants': {'candidate': {'experiment_id': 'exp-9', 'name': 'Candidate'}}}, 'exp-9', 'https://console.example.test/saved-tenant/project/project/experiments'),
            ('dashboard', 'dashboard', {'section_id': 'section-6', 'name': 'BarryBot release readiness'}, 'section-6', 'https://console.example.test/saved-tenant/project/project/agent-streams/stream'),
        ]:
            with self.subTest(checkpoint=checkpoint):
                self.navigation(feature, **values)
                output = self.invoke('open-ui', '--checkpoint', checkpoint)
                self.assertEqual(output.count('https://'), 1)
                self.assertIn(entry_url, output)
                self.assertIn(wanted, output)
                self.assertNotIn('/experiments/', output)
                self.assertNotIn('/datasets/', output)
                self.assertNotIn('/dashboard', output)

    def test_control_denied_selects_denied_trace_and_has_no_model_span(self):
        self.navigation('guardrail', cases={'allowed': {'trace_id': 'allow', 'decision': 'ALLOW'},
            'denied': {'trace_id': 'deny', 'decision': 'DENY'}})
        self.client.traces['deny'] = {'id': 'deny', 'session_id': 'session-deny',
            'user_metadata': {'decision': 'DENY', 'protected_calls': 0},
            'spans': [{'id': 'policy', 'type': 'tool'}]}
        output = self.invoke('open-ui', '--checkpoint', 'control-denied')
        self.assertIn('traceId=deny', output)
        self.assertNotIn('spanId=', output)
        self.assertIn('no model span', output)
        self.assertEqual(self.client.reads, ['deny'])

    def test_control_denied_refuses_mismatched_decision(self):
        self.navigation('guardrail', cases={'denied': {'trace_id': 'wrong', 'decision': 'ALLOW'}})
        with self.assertRaisesRegex(ValueError, 'DENY'):
            self.invoke('open-ui', '--checkpoint', 'control-denied')

    def test_dataset_command_saves_exact_destination_and_reconciled_id(self):
        self.client.post = lambda *a, **kw: {'id': 'dataset-42', 'name': 'Refund cases', 'num_rows': 4}
        with patch.object(lab, 'ensure_project_and_stream', return_value=self.ids):
            with patch.object(lab, 'dataset_list', return_value=[]), patch.object(lab, 'save_state'):
                with patch('dataset_sync.reconcile_dataset', return_value={'id': 'dataset-42', 'name': 'Refund cases', 'num_rows': 4}):
                    self.invoke('dataset', '--guided')
        saved = json.loads((self.root / 'navigation.json').read_text())['dataset']
        self.assertEqual(saved['dataset_id'], 'dataset-42')
        self.assertEqual(saved['destination']['api_base_url'], 'https://api.example.test')
        self.assertEqual(saved['destination']['log_stream_id'], 'stream')

    def test_refund_candidate_navigation_uses_saved_candidate_not_latest_batch(self):
        baseline = self.run_suite('baseline')
        self.navigation('agent-demo', session_id='refund-session', cases={
            'baseline': {'trace_id': 'refund-old'}, 'candidate': {'trace_id': 'refund-new'}})
        self.client.traces['refund-new'] = {'id': 'refund-new', 'spans': [], 'user_metadata': {}}
        output = self.invoke('open-ui', '--checkpoint', 'refund-candidate')
        self.assertIn('traceId=refund-new', output)
        self.assertNotIn(baseline['run_id'], output)
        self.assertIn('refund-session', output)

    def test_deny_checkpoint_rejects_model_execution_even_if_decision_says_deny(self):
        self.navigation('guardrail', cases={'denied': {'trace_id': 'deny', 'decision': 'DENY'}})
        self.client.traces['deny'] = {'id': 'deny', 'user_metadata': {'decision': 'DENY', 'protected_calls': 0},
            'spans': [{'id': 'bad-model', 'type': 'llm'}]}
        with self.assertRaisesRegex(ValueError, 'model execution'):
            self.invoke('open-ui', '--checkpoint', 'control-denied')

    def test_optional_guided_readout_has_purpose_and_preserves_gate_exit(self):
        report = {'variants': {'baseline': {'gate': {'passed': False, 'checks': {
            name: {'value': 0, 'passed': False} for name in lab.RELEASE_THRESHOLDS}}}}}
        evaluation = self.root / 'evaluation.json'
        evaluation.write_text(json.dumps(report))
        with patch.object(lab, 'EVALUATION_FILE', evaluation):
            with self.assertRaises(SystemExit) as stopped:
                self.invoke('release-gate', '--variant', 'baseline', '--guided')
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn('Purpose:', self.output.getvalue())
        self.assertIn('BASELINE: FAIL', self.output.getvalue())

    def test_missing_suite_evidence_does_not_open_different_batch(self):
        self.run_suite('privacy')
        with self.assertRaisesRegex(ValueError, 'baseline'):
            self.invoke('open-ui', '--checkpoint', 'baseline-response')
        self.assertNotIn('https://', self.output.getvalue())

    def test_saved_destination_mismatch_is_explicit_without_link(self):
        self.navigation('dataset', dataset_id='data', name='Cases')
        self.client.base_url = 'https://different.example.test'
        with self.assertRaisesRegex(ValueError, 'destination differs'):
            self.invoke('open-ui', '--checkpoint', 'dataset')
        self.assertNotIn('https://', self.output.getvalue())

    def test_every_checkpoint_opens_saved_evidence(self):
        for suite in ('baseline', 'efficient', 'privacy'):
            self.run_suite(suite)
        self.navigation('trends')
        self.navigation('dataset', dataset_id='data', name='Cases')
        self.navigation('dashboard', section_id='section', name='BarryBot release readiness')
        self.navigation('experiment', variants={variant: {'experiment_id': variant, 'name': variant}
            for variant in ('baseline', 'candidate')})
        cases = {'baseline': {'trace_id': 'baseline-refund'}, 'candidate': {'trace_id': 'candidate-refund'}}
        self.navigation('agent-demo', cases=cases)
        self.navigation('human-workflows', cases=cases)
        self.navigation('guardrail', cases={'allowed': {'trace_id': 'allowed', 'decision': 'ALLOW'},
            'denied': {'trace_id': 'denied', 'decision': 'DENY'}})
        for trace_id in ('baseline-refund', 'candidate-refund', 'allowed', 'denied'):
            self.client.traces[trace_id] = {'id': trace_id, 'spans': [], 'user_metadata': {
                'decision': 'DENY' if trace_id == 'denied' else 'ALLOW', 'protected_calls': 0}}
        for checkpoint in ('baseline-response', 'baseline-tokens', 'baseline-latency', 'efficient-response',
                'privacy-response', 'privacy-metadata', 'trends', 'control-allowed', 'control-denied',
                'refund-baseline', 'refund-candidate', 'dataset', 'experiment-baseline',
                'experiment-candidate', 'sample-review', 'dashboard'):
            with self.subTest(checkpoint=checkpoint):
                output = self.invoke('open-ui', '--checkpoint', checkpoint)
                self.assertEqual(output.count('https://'), 1)
                self.assertIn('Expected view:', output)
                self.assertIn('What to find:', output)

    def test_experiment_saves_variant_ids_without_changing_evaluation_schema(self):
        class EvaluationClient:
            base_url = 'https://api.example.test'

            def __init__(self):
                self.created = 0

            def post(self, path, **kwargs):
                if path.endswith('/experiments'):
                    self.created += 1
                    return {'id': 'experiment-' + str(self.created)}
                return {}

        self.client = EvaluationClient()
        with patch.object(lab, 'ensure_project_and_stream', return_value=self.ids):
            with patch.object(lab, 'load_state', return_value={'dataset_id': 'dataset'}), patch.object(lab, 'save_state'):
                with patch.object(lab, 'EVALUATION_FILE', self.root / 'evaluation.json'):
                    self.invoke('experiment', '--name', 'Comparison', '--guided')
        saved = json.loads((self.root / 'navigation.json').read_text())['experiment']
        self.assertEqual(saved['variants']['baseline']['experiment_id'], 'experiment-1')
        self.assertEqual(saved['variants']['candidate']['experiment_id'], 'experiment-2')
        report = json.loads((self.root / 'evaluation.json').read_text())
        self.assertEqual(set(report), {'group', 'variants'})
        self.assertEqual(set(report['variants']['candidate']), {'experiment_id', 'name', 'trace_ids', 'metrics', 'gate'})

    def test_token_checkpoint_reports_unavailable_usage_without_inventing_zero(self):
        report = self.run_suite('baseline')
        self.client.traces[report['trace_ids'][0]]['spans'][-1]['metrics'] = {}
        output = self.invoke('open-ui', '--checkpoint', 'baseline-tokens')
        self.assertIn('Tokens: input unavailable, output unavailable, total unavailable', output)
        self.assertNotIn('total 0', output)

    def test_catalog_checkpoints_use_verified_routes_and_saved_tenant(self):
        for feature, route in [('scorers', '/evaluators'), ('integrations', '/settings/integrations')]:
            with self.subTest(feature=feature):
                self.navigation(feature, entries=[{'name': 'Example'}])
                output = self.invoke('open-ui', '--checkpoint', feature)
                self.assertEqual(output.count('https://'), 1)
                self.assertIn('https://console.example.test/saved-tenant' + route, output)

    def test_interactive_and_instrumentation_never_open_another_batch(self):
        baseline = self.run_suite('baseline')
        for checkpoint, feature in [('interactive', 'barrybot'), ('instrumentation', 'log-traces')]:
            with self.subTest(checkpoint=checkpoint):
                with self.assertRaisesRegex(ValueError, feature):
                    self.invoke('open-ui', '--checkpoint', checkpoint)
                self.navigation(feature, trace_id='own-' + checkpoint)
                self.client.traces['own-' + checkpoint] = {'id': 'own-' + checkpoint,
                    'session_id': 'own-session', 'spans': [], 'user_metadata': {}}
                output = self.invoke('open-ui', '--checkpoint', checkpoint)
                self.assertIn('traceId=own-' + checkpoint, output)
                self.assertNotIn(baseline['run_id'], output)
                self.assertIn('Tracing', output)
                self.assertNotIn('Logs', output)

    def test_diagnostic_execution_saves_navigation_without_extra_remote_writes(self):
        class CatalogClient:
            base_url = 'https://api.example.test'

            def get(self, path):
                if path == '/v2/integrations/available':
                    return {'integrations': ['OpenAI', 'LangChain']}
                raise AssertionError(path)

        self.client = CatalogClient()
        with patch.object(lab, 'list_scorers', return_value=[{'id': 'scorer', 'name': 'Correctness'}]):
            self.invoke('scorers', '--guided')
        self.invoke('integrations', '--guided')
        saved = json.loads((self.root / 'navigation.json').read_text())
        self.assertEqual(saved['scorers']['entries'][0]['id'], 'scorer')
        self.assertEqual(saved['integrations']['entries'][0]['name'], 'OpenAI')

    def test_interactive_and_instrumentation_execution_save_their_own_ids(self):
        class LocalLLM(TestLLM):
            base_url = 'https://model.example.test'
            model_source = 'test'

        with patch.object(lab, 'ensure_project_and_stream', return_value=self.ids), patch.object(lab, 'save_state'):
            with patch.object(lab.DevNetLLM, 'from_env', return_value=LocalLLM()):
                self.invoke('barrybot', '--ask', 'How should I check usage?', '--guided')
                self.invoke('log-traces', '--guided')
        saved = json.loads((self.root / 'navigation.json').read_text())
        self.assertEqual(saved['barrybot']['trace_id'], self.client.payloads[0]['traces'][0]['id'])
        self.assertEqual(saved['log-traces']['trace_id'], self.client.payloads[1]['traces'][0]['id'])
        self.assertNotEqual(saved['barrybot']['trace_id'], saved['log-traces']['trace_id'])

    def legacy_scorers(self):
        self.output = io.StringIO()
        with patch.object(lab, 'STATE_DIR', self.root), patch.object(lab, 'client_or_exit', return_value=self.client):
            with patch.object(lab, 'list_scorers', return_value=[{'id': 'correctness', 'name': 'Correctness'}]):
                with contextlib.redirect_stdout(self.output):
                    lab.cmd_scorers(argparse.Namespace())
        return self.output.getvalue()

    def test_corrupt_sidecar_preserves_original_and_successful_legacy_output(self):
        path = self.root / 'navigation.json'
        for corrupt in ('{', '[]', 'null'):
            with self.subTest(corrupt=corrupt):
                path.write_text(corrupt)
                output = self.legacy_scorers()
                self.assertIn('Scorers visible to this key: 1', output)
                self.assertIn('Correctness (correctness)', output)
                self.assertIn('Navigation warning:', output)
                self.assertIn('not saved', output)
                self.assertEqual(path.read_text(), corrupt)

    def test_optional_persistence_oserror_keeps_successful_legacy_output(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('storage unavailable')):
            output = self.legacy_scorers()
        self.assertIn('Scorers visible to this key: 1', output)
        self.assertIn('Navigation warning:', output)
        self.assertIn('not saved', output)
        self.assertFalse((self.root / 'navigation.json').exists())

    def test_interrupted_atomic_replace_preserves_previous_navigation(self):
        self.navigation('dataset', dataset_id='kept', name='Previous cases')
        path = self.root / 'navigation.json'
        original = path.read_bytes()
        with patch('os.replace', side_effect=OSError('interrupted publication')):
            output = self.legacy_scorers()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.glob('.navigation-*')), [])
        self.assertIn('Scorers visible to this key: 1', output)
        self.assertIn('Navigation warning:', output)

    def test_atomic_navigation_publishes_complete_private_file(self):
        import os
        self.navigation('dataset', dataset_id='kept', name='Previous cases')
        path = self.root / 'navigation.json'
        original = path.read_bytes()
        real_replace = os.replace
        publications = []

        def observe_replace(source, target):
            self.assertEqual(path.read_bytes(), original)
            payload = json.loads(Path(source).read_text())
            self.assertEqual(payload['dataset']['dataset_id'], 'kept')
            self.assertEqual(payload['scorers']['entries'][0]['id'], 'correctness')
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            publications.append(str(target))
            real_replace(source, target)

        with patch('os.replace', side_effect=observe_replace):
            self.legacy_scorers()
        self.assertEqual(publications, [str(path)])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_privacy_metadata_opens_parent_trace_and_explains_native_metadata_path(self):
        report = self.run_suite('privacy')
        trace = self.client.traces[report['trace_ids'][0]]
        trace['session_id'] = 'privacy-session'
        output = self.invoke('open-ui', '--checkpoint', 'privacy-metadata')
        self.assertIn('traceId=' + trace['id'], output)
        self.assertNotIn('spanId=', output)
        self.assertIn('parent trace', output)
        self.assertIn('Trace Summary → Params → Metadata', output)
        self.assertEqual(output.count('https://'), 1)

    def test_every_guide_execution_command_accepts_guided(self):
        parser = lab.build_parser()
        for command in ('run-tests', 'findings', 'compare-tests', 'guardrail', 'agent-demo',
                'dataset', 'experiment', 'human-workflows', 'dashboard', 'trends', 'release-gate',
                'scorers', 'integrations', 'barrybot', 'log-traces', 'query-traces'):
            with self.subTest(command=command):
                self.assertTrue(parser.parse_args([command, '--guided']).guided)


if __name__ == '__main__':
    unittest.main()
