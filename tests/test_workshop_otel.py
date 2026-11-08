import contextlib
import copy
import io
import json
import struct
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from galileo_client import GalileoApiError, api_payload

try:
    import workshop_app as app
    import workshop_results as evidence
    import workshop_telemetry as telemetry
except ImportError:
    app = evidence = telemetry = None


class MemoryClient:
    base_url = 'https://api.example.test'

    def __init__(self):
        self.traces = {}
        self.payloads = []
        self.pending = False
        self.fail_upload = False
        self.wrong_ack = False

    def post(self, path, *, json):
        if self.fail_upload:
            raise RuntimeError('upload unavailable')
        payload = api_payload(json)
        self.payloads.append(payload)
        for trace in payload['traces']:
            self.traces[trace['id']] = copy.deepcopy(trace)
        return {'trace_ids': ['wrong'] if self.wrong_ack else list(self.traces)[-1:], 'traces_count': 1}

    def get(self, path):
        if self.pending:
            raise GalileoApiError('GET', path, 404, 'pending')
        return copy.deepcopy(self.traces[path.rsplit('/', 1)[-1]])


class Model:
    model = 'test-model'
    base_url = 'https://llm.example.test/v1'

    def __init__(self, *, usage=True, fail=False):
        self.usage = usage
        self.fail = fail
        self.last_completion_metadata = {}
        self.calls = []

    def complete(self, messages, *, max_tokens):
        self.calls.append((copy.deepcopy(messages), max_tokens))
        if self.fail:
            raise RuntimeError('model unavailable')
        self.last_completion_metadata = {'model': self.model, 'request_count': 1}
        if self.usage:
            self.last_completion_metadata.update(num_input_tokens=40, num_output_tokens=12, num_total_tokens=52)
        return 'Check the timeline, provider usage, and errors.'

    def chat_step(self, messages, *, tools, max_tokens):
        answer = self.complete(messages, max_tokens=max_tokens)
        if not any(message['role'] == 'tool' for message in messages):
            return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'search-1', 'type': 'function',
                'function': {'name': 'search_support_notes', 'arguments': json.dumps({'query': messages[-1]['content']})}}]}
        return {'role': 'assistant', 'content': answer}


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(app, 'The visible SDK app and exporter are missing')
        pause = patch('workshop_telemetry.time.sleep')
        pause.start()
        self.addCleanup(pause.stop)
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.docs = self.state / 'docs'
        self.docs.mkdir()
        (self.docs / 'latency.md').write_text('# Latency\nCheck model latency, usage, and errors in the timeline.')
        (self.docs / 'privacy.md').write_text('# Privacy\nContact customer information needs protection. Use fictional email and phone data.')
        (self.docs / 'release.md').write_text('# Release\nCompare the same questions before changing an assistant.')
        self.client = MemoryClient()
        self.destination = {'api_base_url': self.client.base_url, 'project_id': str(uuid.uuid4()),
            'log_stream_id': str(uuid.uuid4()), 'console_url': 'https://console.example.test/team'}
        self.exporter = telemetry.WorkshopExporter(self.client, self.destination, self.state)
        self.provider = TracerProvider(resource=Resource.create({'service.name': 'test-support'}))
        self.provider.add_span_processor(telemetry.TrackSpans(self.exporter))
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer('workshop-tests')
        self.addCleanup(self.provider.shutdown)

    def run_question(self, *, model=None, question='Which model latency usage and errors should I check?', top_k=3):
        result = app.answer_question(self.tracer, model or Model(), question, top_k, 120, self.docs)
        self.assertTrue(self.provider.force_flush())
        return result, self.exporter.receipts[-1]

    def test_real_sdk_round_trip_retains_context_and_uuid4_mapping(self):
        answer, receipt = self.run_question()
        trace = self.client.traces[receipt['native_trace_id']]
        self.assertEqual(answer, trace['output'])
        self.assertEqual(receipt['upload_status'], 'accepted')
        self.assertEqual(receipt['readback_status'], 'verified')
        self.assertEqual(len(receipt['otel_trace_id']), 32)
        self.assertEqual(len(receipt['otel_root_span_id']), 16)
        self.assertEqual(uuid.UUID(trace['id']).version, 4)
        self.assertNotEqual(trace['id'].replace('-', ''), receipt['otel_trace_id'])
        self.assertEqual(trace['user_metadata']['otel.trace_id'], receipt['otel_trace_id'])
        self.assertEqual(trace['user_metadata']['otel.span_id'], receipt['otel_root_span_id'])
        model = trace['spans'][0]['spans'][-1]
        self.assertEqual(model['type'], 'llm')
        self.assertEqual(model['parent_id'], trace['spans'][0]['id'])
        self.assertEqual(model['trace_id'], trace['id'])
        self.assertEqual(uuid.UUID(model['id']).version, 4)
        self.assertEqual(model['user_metadata']['otel.parent_span_id'], receipt['spans'][1]['otel_span_id'])
        self.assertGreater(model['metrics']['duration_ns'], 0)
        self.assertGreaterEqual(trace['metrics']['duration_ns'], model['metrics']['duration_ns'])
        self.assertEqual(model['metrics']['num_total_tokens'], 52)
        self.assertEqual(model['user_metadata']['service.name'], 'test-support')
        saved = json.loads((self.state / 'otel/runs' / (receipt['run_id'] + '.json')).read_text())
        self.assertEqual(saved, receipt)
        self.assertTrue(self.client.payloads[0]['include_trace_ids'])
        self.assertTrue(self.client.payloads[0]['reliable'])

    def test_missing_learner_decorator_does_not_synthesize_retrieval_span(self):
        _, receipt = self.run_question()
        self.assertEqual([s['name'] for s in receipt['spans']], ['answer-question', 'support-agent', 'support-model-step', 'support-answer'])
        self.assertEqual(receipt['retrieval_status'], 'not-instrumented')

    def test_uncommenting_decorator_instruments_actual_retrieval_body(self):
        source = Path(app.__file__).read_text().replace('# @tracer.start_as_current_span("retrieve-documents")',
            '@tracer.start_as_current_span("retrieve-documents")')
        namespace = {'__file__': app.__file__, '__name__': 'edited_workshop'}
        exec(compile(source, str(app.__file__), 'exec'), namespace)
        model = Model()
        namespace['answer_question'](self.tracer, model, 'latency usage errors', 1, 120, self.docs)
        receipt = self.exporter.receipts[-1]
        self.assertEqual(receipt['retrieval_status'], 'instrumented')
        self.assertEqual([s['name'] for s in receipt['spans']], ['answer-question', 'support-agent', 'support-model-step', 'retrieve-documents', 'support-answer'])
        self.assertIn('latency.md', model.calls[-1][0][-1]['content'])
        self.assertNotIn('privacy.md', model.calls[-1][0][-1]['content'])
        for span in receipt['spans'][2:]:
            self.assertEqual(span['otel_parent_span_id'], receipt['spans'][1]['otel_span_id'])

    def test_real_search_changes_prompt_with_top_k(self):
        model = Model()
        self.run_question(model=model, top_k=1)
        self.run_question(model=model, top_k=3)
        self.assertLess(len(model.calls[1][0][-1]['content']), len(model.calls[3][0][-1]['content']))

    def test_missing_provider_usage_stays_unavailable(self):
        _, receipt = self.run_question(model=Model(usage=False))
        self.assertIsNone(receipt['usage']['total_tokens'])
        model = self.client.traces[receipt['native_trace_id']]['spans'][0]['spans'][-1]
        self.assertNotIn('num_total_tokens', model['metrics'])

    def test_model_error_records_error_without_successful_answer(self):
        with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
            self.run_question(model=Model(fail=True))
        receipt = self.exporter.receipts[-1]
        self.assertEqual(receipt['model_status'], 'error')
        self.assertEqual(receipt['status'], 'error')
        self.assertIsNone(receipt['answer'])
        self.assertIsNone(receipt['usage']['total_tokens'])
        self.assertEqual(receipt['spans'][-1]['status'], 'ERROR')

    def test_empty_documents_stops_before_model(self):
        for file in self.docs.glob('*.md'):
            file.unlink()
        model = Model()
        with self.assertRaisesRegex(ValueError, 'documents'):
            self.run_question(model=model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.exporter.receipts[-1]['model_status'], 'unavailable')

    def test_upload_error_is_saved_as_failure(self):
        self.client.fail_upload = True
        _, receipt = self.run_question()
        self.assertEqual(receipt['upload_status'], 'failed')
        self.assertEqual(receipt['readback_status'], 'not-attempted')
        self.assertIn('upload unavailable', receipt['delivery_error'])

    def test_wrong_upload_acknowledgement_is_not_accepted(self):
        self.client.wrong_ack = True
        _, receipt = self.run_question()
        self.assertEqual(receipt['upload_status'], 'unconfirmed')

    def test_pending_indexing_preserves_successful_model_and_pending_delivery(self):
        self.client.pending = True
        _, receipt = self.run_question()
        self.assertEqual(receipt['model_status'], 'ok')
        self.assertEqual(receipt['readback_status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'indexing'):
            evidence.open_view(self.client, receipt, 'response')
        self.assertEqual(len(self.client.payloads), 1)

    def test_children_buffer_until_root_ends(self):
        with self.tracer.start_as_current_span('answer-question'):
            with self.tracer.start_as_current_span('child'):
                pass
            self.assertEqual(self.client.payloads, [])
        self.assertEqual(len(self.client.payloads), 1)

    def test_open_child_never_uploads_partial_root(self):
        root = self.tracer.start_span('answer-question')
        from opentelemetry.trace import set_span_in_context
        child = self.tracer.start_span('child', context=set_span_in_context(root))
        root.end()
        self.assertEqual(self.client.payloads, [])
        self.assertFalse(self.exporter.force_flush())
        child.end()
        self.assertEqual(len(self.client.payloads), 1)
        self.assertTrue(self.exporter.force_flush())

    def test_remote_readback_rejects_changed_context(self):
        _, receipt = self.run_question()
        self.client.traces[receipt['native_trace_id']]['user_metadata']['otel.trace_id'] = '0' * 32
        with self.assertRaisesRegex(ValueError, 'context'):
            evidence.inspect_run(self.client, receipt)

    def test_remote_readback_rejects_missing_child(self):
        _, receipt = self.run_question()
        self.client.traces[receipt['native_trace_id']]['spans'] = []
        with self.assertRaisesRegex(ValueError, 'span'):
            evidence.inspect_run(self.client, receipt)

    def test_open_prints_one_literal_bold_cyan_exact_trace_url(self):
        _, receipt = self.run_question()
        self.client.traces[receipt['native_trace_id']]['session_id'] = str(uuid.uuid4())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            evidence.open_view(self.client, receipt, 'tokens')
        text = output.getvalue()
        self.assertEqual(text.count('https://'), 1)
        self.assertIn('\033[1;36mhttps://', text)
        self.assertIn('traceId=' + receipt['native_trace_id'], text)
        self.assertIn('spanId=' + receipt['spans'][-1]['native_id'], text)

    def test_privacy_view_requires_contact_pattern_evidence(self):
        _, receipt = self.run_question()
        with self.assertRaisesRegex(ValueError, 'contact'):
            evidence.open_view(self.client, receipt, 'privacy')
        _, contact = self.run_question(question='Privacy contact fictional alex@example.com +1 202-555-0147')
        self.assertEqual(contact['privacy']['input_labels'], ['email', 'phone'])
        with contextlib.redirect_stdout(io.StringIO()):
            evidence.open_view(self.client, contact, 'privacy')

    def test_compare_matching_runs_preserves_measurements(self):
        _, before = self.run_question(top_k=3)
        _, after = self.run_question(top_k=1)
        report = evidence.compare_runs(self.client, before, after)
        self.assertEqual(report['before']['usage']['total_tokens'], 104)
        self.assertEqual(report['after']['top_k'], 1)
        self.assertGreater(report['before']['duration_ns'], 0)

    def test_compare_rejects_mismatch_and_unchanged_top_k(self):
        _, before = self.run_question(top_k=3)
        _, after = self.run_question(top_k=1)
        for field, value in [('question', 'other question'), ('max_tokens', 50), ('requested_model', 'other'),
                ('model', 'other'), ('model_endpoint', 'https://other.test')]:
            bad = copy.deepcopy(after)
            bad['request'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evidence.compare_runs(self.client, before, bad)
        bad = copy.deepcopy(after)
        bad['destination']['log_stream_id'] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            evidence.compare_runs(self.client, before, bad)
        with self.assertRaisesRegex(ValueError, 'TOP_K'):
            evidence.compare_runs(self.client, before, before)

    def test_selection_uses_successful_runs_and_privacy_filter(self):
        _, before = self.run_question()
        _, privacy = self.run_question(question='Contact privacy alex@example.com')
        selected = evidence.load_run(self.state, view='response')
        self.assertEqual(selected['run_id'], privacy['run_id'])
        selected = evidence.load_run(self.state, view='privacy')
        self.assertEqual(selected['run_id'], privacy['run_id'])
        with self.assertRaises(ValueError):
            evidence.load_run(self.state, run_id='../escape')

    def test_remote_measurements_cannot_drift_from_exported_sdk_values(self):
        _, receipt = self.run_question()
        trace = self.client.traces[receipt['native_trace_id']]
        trace['spans'][0]['spans'][0]['metrics']['num_total_tokens'] = 999
        with self.assertRaisesRegex(ValueError, 'measurement'):
            evidence.inspect_run(self.client, receipt)

    def test_readback_rejects_changed_model_output(self):
        _, receipt = self.run_question()
        self.client.traces[receipt['native_trace_id']]['spans'][0]['spans'][-1]['output']['content'] = 'different answer'
        with self.assertRaisesRegex(ValueError, 'output'):
            evidence.inspect_run(self.client, receipt)

    def test_invalid_provider_token_values_stay_unavailable(self):
        model = Model(usage=False)
        original = model.complete
        def complete(messages, *, max_tokens):
            answer = original(messages, max_tokens=max_tokens)
            model.last_completion_metadata.update(num_input_tokens=True, num_output_tokens=-1, num_total_tokens='52')
            return answer
        model.complete = complete
        _, receipt = self.run_question(model=model)
        self.assertEqual(receipt['usage'], {'input_tokens': None, 'output_tokens': None, 'total_tokens': None})

    def test_compare_does_not_turn_backend_inferred_tokens_into_provider_usage(self):
        _, before = self.run_question(model=Model(usage=False), top_k=3)
        _, after = self.run_question(model=Model(usage=False), top_k=1)
        for saved in (before, after):
            self.client.traces[saved['native_trace_id']]['spans'][0]['spans'][-1]['metrics']['num_total_tokens'] = 999
        report = evidence.compare_runs(self.client, before, after)
        self.assertIsNone(report['before']['usage']['total_tokens'])

    def test_cli_json_reads_actual_saved_payload_without_client_or_model(self):
        _, receipt = self.run_question()
        with patch.object(evidence, 'STATE_DIR', self.state), patch.object(evidence, 'client_or_exit', side_effect=AssertionError('network')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(evidence.main(['json', '--run', receipt['run_id']]), 0)
        self.assertEqual(json.loads(output.getvalue()), self.client.payloads[0])
        self.assertNotIn('api_key', output.getvalue())

    def test_default_comparison_selects_latest_matching_changed_setting(self):
        _, before = self.run_question(top_k=3)
        _, after = self.run_question(top_k=1)
        self.run_question(question='Contact privacy alex@example.com', top_k=1)
        selected = evidence.comparison_pair(self.state)
        self.assertEqual([run['run_id'] for run in selected], [before['run_id'], after['run_id']])

    def test_cli_local_inspect_has_no_network_or_new_model_call(self):
        _, receipt = self.run_question()
        with patch.object(evidence, 'STATE_DIR', self.state), patch.object(evidence, 'client_or_exit', side_effect=AssertionError('network')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(evidence.main(['inspect', '--local', '--run', receipt['run_id']]), 0)
        self.assertIn(receipt['otel_trace_id'], output.getvalue())
        self.assertIn('support-agent', output.getvalue())

    def test_root_privacy_metadata_and_document_counts_are_actual(self):
        _, receipt = self.run_question(question='Privacy contact alex@example.com +1 202-555-0147', top_k=1)
        root = self.client.traces[receipt['native_trace_id']]
        self.assertEqual(root['user_metadata'].get('pii_input'), 'email,phone')
        self.assertEqual(root['user_metadata'].get('pii_output'), 'none')
        self.assertEqual(root['user_metadata'].get('pii_detector'), 'local-email-phone-pattern-v1')
        self.assertEqual(receipt['request']['document_count'], 1)
        self.assertEqual(receipt['request']['documents'], ['privacy.md'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            evidence.print_results(receipt)
        self.assertIn('pii_input=email,phone', output.getvalue())

    def test_model_selects_tool_and_receives_actual_search_results(self):
        model = Model()
        self.run_question(model=model, top_k=1)
        self.assertEqual(len(model.calls), 2)
        final_messages = model.calls[-1][0]
        self.assertEqual(final_messages[-1]['role'], 'tool')
        self.assertIn('latency.md', final_messages[-1]['content'])
        self.assertEqual(final_messages[-1]['tool_call_id'], 'search-1')
        self.assertEqual(self.exporter.receipts[-1]['usage']['total_tokens'], 104)

    def test_invalid_tool_name_stops_without_canned_answer(self):
        model = Model()
        model.chat_step = lambda *args, **kwargs: {'role': 'assistant', 'tool_calls': [{'id': 'bad',
            'function': {'name': 'unknown', 'arguments': '{}'}}]}
        with self.assertRaisesRegex(ValueError, 'tool'):
            self.run_question(model=model)
        self.assertIsNone(self.exporter.receipts[-1]['answer'])

    def test_tool_loop_is_bounded_without_fabricated_final_answer(self):
        model = Model()
        def never_finishes(messages, *, tools, max_tokens):
            model.complete(messages, max_tokens=max_tokens)
            return {'role': 'assistant', 'tool_calls': [{'id': 'repeat', 'type': 'function',
                'function': {'name': 'search_support_notes', 'arguments': '{"query":"latency"}'}}]}
        model.chat_step = never_finishes
        with self.assertRaisesRegex(ValueError, 'three'):
            self.run_question(model=model)
        self.assertEqual(len(model.calls), 3)
        self.assertIsNone(self.exporter.receipts[-1]['answer'])

    def test_devnet_chat_step_preserves_actual_tool_response_and_usage(self):
        from barrybot import DevNetLLM
        llm = DevNetLLM('https://llm.test/v1', 'fake-only', 'test-model', 'test', ['test-model'])
        self.assertTrue(callable(getattr(llm, 'chat_step', None)), 'The actual tools API call is missing')
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-1', 'type': 'function',
            'function': {'name': 'search_support_notes', 'arguments': '{"query":"latency"}'}}]}
        response = Mock()
        response.json.return_value = {'model': 'served-model', 'choices': [{'message': message, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}}
        with patch('barrybot.requests.post', return_value=response) as post:
            actual = llm.chat_step([{'role': 'user', 'content': 'latency'}], tools=[{'type': 'function'}], max_tokens=120)
        self.assertEqual(actual, message)
        self.assertEqual(llm.last_completion_metadata['num_total_tokens'], 20)
        self.assertEqual(llm.last_completion_metadata['model'], 'served-model')
        self.assertEqual(post.call_args.kwargs['json']['tool_choice'], 'auto')

    def test_tools_call_retries_only_transient_http_statuses(self):
        import requests
        from barrybot import DevNetLLM
        llm = DevNetLLM('https://llm.test/v1', 'fake-only', 'test-model', 'test', ['test-model'])
        limited = Mock(status_code=429, headers={'Retry-After': '2'})
        limited.raise_for_status.side_effect = requests.HTTPError(response=limited)
        success = Mock()
        success.json.return_value = {'choices': [{'message': {'role': 'assistant', 'content': 'actual response'}}]}
        with patch('barrybot.requests.post', side_effect=[limited, success]) as post, patch('barrybot.time.sleep') as sleep:
            self.assertEqual(llm.chat_step([], tools=[], max_tokens=120)['content'], 'actual response')
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertEqual(llm.last_completion_metadata['request_count'], 2)
        denied = Mock(status_code=403, headers={})
        denied.raise_for_status.side_effect = requests.HTTPError(response=denied)
        with patch('barrybot.requests.post', return_value=denied) as post, patch('barrybot.time.sleep') as sleep:
            with self.assertRaises(requests.HTTPError):
                llm.chat_step([], tools=[], max_tokens=120)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_agent_graph_opens_actual_shared_stream_graph_route(self):
        _, receipt = self.run_question()
        self.client.traces[receipt['native_trace_id']]['session_id'] = str(uuid.uuid4())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            url = evidence.open_view(self.client, receipt, 'agent')
        self.assertTrue(url.endswith('/agent-streams/' + self.destination['log_stream_id'] + '?view=graph'))
        self.assertNotIn('/sessions/', url)
        self.assertIn('shared stream', output.getvalue())

    def test_partly_indexed_children_remain_pending_instead_of_failed(self):
        original = self.client.get
        def partial(path):
            record = original(path)
            record['spans'] = []
            return record
        with patch.object(self.client, 'get', side_effect=partial):
            _, receipt = self.run_question()
        self.assertEqual(receipt['readback_status'], 'pending')
        self.assertIn('indexing', receipt['delivery_error'])
        self.assertEqual(len(self.client.payloads), 1)
        self.assertEqual(evidence.inspect_run(self.client, receipt)['readback_status'], 'verified')

    def test_native_float32_duration_storage_preserves_exact_sdk_receipt(self):
        _, receipt = self.run_question()
        original = receipt['spans'][0]['duration_ns']
        for record in telemetry._flatten(self.client.traces[receipt['native_trace_id']]).values():
            value = record['metrics']['duration_ns']
            record['metrics']['duration_ns'] = int(struct.unpack('f', struct.pack('f', value))[0])
        self.assertEqual(evidence.inspect_run(self.client, receipt)['duration_ns'], original)
        for saved, stored in ((3541307570, 3541307648), (3540659178, 3540659200), (1624403837, 1624403840), (1915116139, 1915116160)):
            self.assertTrue(telemetry.duration_matches(saved, stored))
            self.assertFalse(telemetry.duration_matches(saved, stored + 1024))
        self.assertFalse(telemetry.duration_matches(original, None))
        self.assertFalse(telemetry.duration_matches(original, True))

    def test_native_tool_message_normalization_is_narrow(self):
        _, receipt = self.run_question()
        model = self.client.traces[receipt['native_trace_id']]['spans'][0]['spans'][-1]
        assistant = model['input'][2]
        self.assertIsNone(assistant['content'])
        assistant['content'] = ''
        assistant['tool_calls'][0].pop('type')
        self.assertEqual(evidence.inspect_run(self.client, receipt)['run_id'], receipt['run_id'])
        assistant['tool_calls'][0]['function']['arguments'] = '{"query":"changed"}'
        with self.assertRaisesRegex(ValueError, 'input'):
            evidence.inspect_run(self.client, receipt)

    def test_json_outline_uses_same_actual_tree_without_model_messages(self):
        _, receipt = self.run_question()
        with patch.object(evidence, 'STATE_DIR', self.state), patch.object(evidence, 'client_or_exit', side_effect=AssertionError('network')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(evidence.main(['json', '--outline', '--run', receipt['run_id']]), 0)
        outline = json.loads(output.getvalue())
        actual = receipt['upload_payload']['traces'][0]
        root = outline['traces'][0]
        self.assertEqual(root['id'], actual['id'])
        self.assertEqual(root['input'], actual['input'])
        self.assertEqual(root['output'], actual['output'])
        self.assertEqual(root['metrics'], actual['metrics'])
        model = root['spans'][0]['spans'][-1]
        self.assertEqual(model['name'], 'support-answer')
        self.assertNotIn('input', model)
        self.assertNotIn('output', model)
        self.assertEqual(model['metrics'], actual['spans'][0]['spans'][-1]['metrics'])

    def test_local_inspection_never_calls_network(self):
        _, receipt = self.run_question()
        with patch.object(self.client, 'get', side_effect=AssertionError('network')):
            actual = evidence.inspect_run(self.client, receipt, local=True)
        self.assertEqual(actual['run_id'], receipt['run_id'])


if __name__ == '__main__':
    unittest.main()
