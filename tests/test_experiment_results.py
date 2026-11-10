import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

import galileo_lab as lab
from galileo_client import GalileoClient


PROJECT = '6b0d57da-418c-4c2e-b823-536a38e20984'
BASELINE = '2e664092-9405-4895-adc4-a44ea42777fb'
CANDIDATE = '73da4886-9705-4b9e-8192-5aa317c5c1a0'
API_BASE = 'https://api.example.test'


class ExperimentResultsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = {'api_base_url': API_BASE, 'project_id': PROJECT,
            'baseline_experiment_id': BASELINE, 'candidate_experiment_id': CANDIDATE}
        self.report = {'group': 'Saved comparison', 'variants': {}}
        self.navigation = {'experiment': {
            'destination': {'api_base_url': API_BASE, 'project_id': PROJECT},
            'variants': {'baseline': {'experiment_id': BASELINE},
                'candidate': {'experiment_id': CANDIDATE}},
        }}
        self.records = {}
        for variant, experiment_id in [('baseline', BASELINE), ('candidate', CANDIDATE)]:
            trace_ids = []
            for index, case in enumerate(lab.SUPPORT_CASES, 1):
                trace_id = f'00000000-0000-4000-8000-{index + (4 if variant == "candidate" else 0):012d}'
                trace_ids.append(trace_id)
                self.records[trace_id] = {
                    'id': trace_id, 'type': 'trace', 'trace_id': trace_id,
                    'session_id': trace_id, 'project_id': PROJECT, 'run_id': experiment_id,
                    'created_at': '2026-11-03T12:00:00Z', 'name': f'eval-{case["case_id"]}-{variant}',
                    'input': case['input'], 'dataset_input': case['input'],
                    'dataset_output': case['expected'], 'dataset_metadata': {'case_id': case['case_id']},
                    'output': f'Stored {variant} answer for {case["case_id"]}',
                    'user_metadata': {'response_source': 'deterministic', 'tool_execution': 'simulated'},
                    'metrics': {'action_completion': 0.0 if variant == 'baseline' else 1.0,
                        'policy_compliance': 0.25 if variant == 'baseline' else 1.0,
                        'tool_selection_quality': 0.5 if variant == 'baseline' else 1.0,
                        'tool_errors': 1.0 if variant == 'baseline' else 0.0},
                    'is_complete': True, 'spans': [],
                }
            self.report['variants'][variant] = {'experiment_id': experiment_id, 'trace_ids': trace_ids,
                'metrics': {'action_completion': 12345}, 'gate': {'passed': True}}
        self.reads = []
        self.output = io.StringIO()

    def request(self, method, url, **kwargs):
        self.assertEqual(method.upper(), 'GET', 'Results must not create experiments, traces, or model requests')
        prefix = f'/v2/projects/{PROJECT}/traces/'
        path = urlsplit(url).path
        self.assertTrue(path.startswith(prefix), path)
        trace_id = path[len(prefix):]
        self.reads.append(trace_id)
        record = self.records[trace_id]
        status = record if isinstance(record, int) else 200
        body = {'detail': 'pending'} if isinstance(record, int) else record
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.headers['Content-Type'] = 'application/json'
        response.url = url
        return response

    def invoke(self):
        (self.root / 'state.json').write_text(json.dumps(self.state))
        (self.root / 'evaluation.json').write_text(json.dumps(self.report))
        if self.navigation is not None:
            (self.root / 'navigation.json').write_text(json.dumps(self.navigation))
        client = GalileoClient(api_key='offline-test-key', base_url=API_BASE)
        with patch.object(lab, 'STATE_DIR', self.root), patch.object(lab, 'STATE_FILE', self.root / 'state.json'):
            with patch.object(lab, 'EVALUATION_FILE', self.root / 'evaluation.json'):
                with patch.object(lab, 'client_or_exit', return_value=client), patch('requests.request', side_effect=self.request):
                    with patch.object(lab, 'ensure_project_and_stream', side_effect=AssertionError('Results must not create a workspace')):
                        with patch.object(lab, 'run_case', side_effect=AssertionError('Results must not rerun author samples')):
                            with patch.object(lab.DevNetLLM, 'from_env', side_effect=AssertionError('Results must not call a model')):
                                with contextlib.redirect_stdout(self.output):
                                    try:
                                        args = lab.build_parser().parse_args(['experiment-results', '--guided'])
                                    except SystemExit:
                                        self.fail('The results-only experiment command is missing')
                                    args.func(args)
        return self.output.getvalue()

    def test_reads_actual_case_outputs_and_metrics_from_saved_exact_ids(self):
        output = self.invoke()
        self.assertEqual(len(self.reads), 8)
        self.assertIn('Stored baseline answer for refund-auth', output)
        self.assertIn('Stored candidate answer for refund-auth', output)
        self.assertIn('policy compliance: 25% -> 100%', output)
        self.assertIn('tool errors: 1 -> 0', output)
        self.assertIn('Matched cases: 4/4', output)
        self.assertNotIn('12345', output)
        self.assertNotIn('https://', output)

    def test_pending_records_keep_the_available_variant_and_offer_a_results_only_retry(self):
        candidate_ids = self.report['variants']['candidate']['trace_ids']
        self.records[candidate_ids[0]] = 404
        self.records[candidate_ids[1]] = 503
        self.records[candidate_ids[2]] = {'type': 'stub_trace', 'id': candidate_ids[2],
            'created_at': '2026-11-03T12:00:00Z', 'spans': []}
        self.records[candidate_ids[3]]['is_complete'] = False
        with self.assertRaisesRegex(RuntimeError, 'Retry experiment-results --guided'):
            self.invoke()
        output = self.output.getvalue()
        self.assertIn('Matched cases: 0/4', output)
        self.assertIn('Stored baseline answer for refund-auth', output)
        self.assertIn('candidate output: unavailable (trace pending or unavailable)', output)
        self.assertNotIn('Stored candidate answer', output)
        self.assertEqual(len(self.reads), 8)

    def test_missing_output_and_non_numeric_metrics_are_unavailable(self):
        first = next(iter(self.records.values()))
        first['output'] = None
        first['metrics'] = {'action_completion': None, 'policy_compliance': True,
            'tool_selection_quality': '0.5', 'tool_errors': float('nan')}
        output = self.invoke()
        auth = output.split('Case: refund-auth\n', 1)[1].split('Case:', 1)[0]
        self.assertIn('baseline output: unavailable', auth)
        self.assertIn('action completion: unavailable -> 100%', auth)
        self.assertIn('policy compliance: unavailable -> 100%', auth)
        self.assertIn('tool selection quality: unavailable -> 100%', auth)
        self.assertIn('tool errors: unavailable -> 0', auth)

    def test_pending_metric_status_is_not_printed_as_a_computed_value(self):
        first = next(iter(self.records.values()))
        first['metric_info'] = {'action_completion': {'status_type': 'pending'}}
        output = self.invoke()
        auth = output.split('Case: refund-auth\n', 1)[1].split('Case:', 1)[0]
        self.assertIn('action completion: unavailable (pending) -> 100%', auth)

    def test_computed_metric_info_value_is_available_when_metrics_omits_it(self):
        first = next(iter(self.records.values()))
        del first['metrics']['action_completion']
        first['metric_info'] = {'action_completion': {'status_type': 'success', 'value': 0.5}}
        output = self.invoke()
        self.assertIn('action completion: 50% -> 100%', output)

    def test_record_must_belong_to_the_saved_experiment_and_project(self):
        first_id = next(iter(self.records))
        original = copy.deepcopy(self.records[first_id])
        for field, wrong in [('id', CANDIDATE), ('project_id', CANDIDATE), ('run_id', CANDIDATE)]:
            with self.subTest(field=field):
                self.records[first_id] = dict(original, **{field: wrong})
                with self.assertRaisesRegex(ValueError, 'trace ID|project and experiment'):
                    self.invoke()
                self.assertNotIn('Stored baseline answer', self.output.getvalue())

    def test_ambiguous_case_ids_are_not_silently_overwritten(self):
        trace_ids = self.report['variants']['baseline']['trace_ids']
        first = copy.deepcopy(self.records[trace_ids[0]])
        first.update(id=trace_ids[1], trace_id=trace_ids[1], session_id=trace_ids[1])
        self.records[trace_ids[1]] = first
        with self.assertRaisesRegex(ValueError, 'multiple saved traces'):
            self.invoke()

    def test_dataset_case_id_cannot_relabel_a_different_question(self):
        first = next(iter(self.records.values()))
        first['dataset_metadata']['case_id'] = 'refund-valid'
        with self.assertRaisesRegex(ValueError, 'input does not match'):
            self.invoke()

    def test_saved_destination_change_is_rejected_before_any_read(self):
        self.navigation['experiment']['destination']['api_base_url'] = 'https://other.example.test'
        with self.assertRaisesRegex(ValueError, 'destination differs'):
            self.invoke()
        self.assertEqual(self.reads, [])

    def test_mutable_workspace_project_cannot_redirect_saved_experiment_reads(self):
        self.state['project_id'] = '7ff9d2f0-a6e3-4de5-9571-5e5a0f319083'
        output = self.invoke()
        self.assertIn('Matched cases: 4/4', output)
        self.assertEqual(len(self.reads), 8)

    def test_legacy_report_without_its_matching_destination_snapshot_is_explicitly_unverifiable(self):
        self.navigation = None
        with self.assertRaisesRegex(ValueError, 'Cannot verify.*experiment destination'):
            self.invoke()
        self.assertEqual(self.reads, [])
        self.assertNotIn('indexing', self.output.getvalue())

    def test_destination_snapshot_must_match_both_saved_experiments(self):
        self.navigation['experiment']['variants']['candidate']['experiment_id'] = BASELINE
        with self.assertRaisesRegex(ValueError, 'Cannot verify.*experiment destination'):
            self.invoke()
        self.assertEqual(self.reads, [])

    def test_both_saved_experiment_ids_are_checked_before_any_read(self):
        self.state['candidate_experiment_id'] = BASELINE
        with self.assertRaisesRegex(ValueError, 'candidate experiment ID differs'):
            self.invoke()
        self.assertEqual(self.reads, [])

    def test_authentication_errors_do_not_claim_indexing_is_pending(self):
        self.records[next(iter(self.records))] = 401
        with self.assertRaises(lab.GalileoApiError) as error:
            self.invoke()
        self.assertEqual(error.exception.status_code, 401)
        self.assertNotIn('indexing', self.output.getvalue())

    def test_malformed_saved_comparison_is_rejected_without_api_reads(self):
        original = copy.deepcopy(self.report)
        malformed = [[], dict(original, variants=[]),
            dict(original, variants={'baseline': [], 'candidate': original['variants']['candidate']})]
        invalid_ids = copy.deepcopy(original)
        invalid_ids['variants']['baseline']['trace_ids'] = [None, {}]
        malformed.append(invalid_ids)
        for report in malformed:
            with self.subTest(report=report):
                self.report = report
                with self.assertRaises(ValueError):
                    self.invoke()
        self.assertEqual(self.reads, [])


if __name__ == '__main__':
    unittest.main()
