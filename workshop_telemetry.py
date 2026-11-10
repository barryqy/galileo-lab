"""Real SDK spans exported through the Workshop JSON API, rather than OTLP."""

import copy
import json
import math
import struct
import time
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import uuid

from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from galileo_client import GalileoApiError
from observability_tests import TOKEN_FIELDS, _pii_labels

RUN_LABELS = ('first', 'traced', 'context', 'privacy')


class PendingRecords(ValueError):
    pass


def duration_matches(sdk_value, stored_value):
    if (type(sdk_value) is not int or sdk_value < 0 or type(stored_value) not in (int, float)
            or not math.isfinite(stored_value) or stored_value < 0):
        return False
    if sdk_value == stored_value:
        return True
    try:
        # The native metrics store uses Float32; SDK nanoseconds stay exact in the receipt.
        normalized = struct.unpack('!f', struct.pack('!f', sdk_value))[0]
    except (OverflowError, struct.error):
        return False
    return stored_value == normalized


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_labels(state_dir):
    path = Path(state_dir) / 'otel/labels.json'
    if not path.exists():
        return {}
    labels = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(labels, dict) or any(key not in RUN_LABELS or not isinstance(value, str)
            for key, value in labels.items()):
        raise ValueError('The saved run labels are invalid; inspect otel/labels.json')
    return labels


def label_run(state_dir, label, run_id):
    if label not in RUN_LABELS:
        raise ValueError('Use a workshop label: ' + ', '.join(RUN_LABELS))
    labels = load_labels(state_dir)
    labels[label] = run_id
    write_json(Path(state_dir) / 'otel/labels.json', labels)


def finish_reason(receipt):
    if isinstance(receipt.get('finish_reason'), str):
        return receipt['finish_reason']
    for span in reversed(receipt['spans']):
        if span['type'] == 'llm':
            return span['attributes'].get('provider.finish_reason')
    return None


def completion_warning(receipt):
    if finish_reason(receipt) == 'length':
        return 'The model reached its output token limit; the answer may be incomplete. Read the saved answer before comparing quality.'
    return None


def retrieval_summary(receipt):
    requested = receipt['request'].get('trace_retrieval')
    setting = 'enabled' if requested is True else 'disabled' if requested is False else 'unknown (legacy run)'
    called = receipt['request'].get('search_called')
    if called is None:
        if receipt['retrieval_status'] == 'instrumented' or receipt['request'].get('document_count'):
            called = True
    if called is False:
        search = 'not called by the model'
    elif receipt['retrieval_status'] == 'instrumented':
        search = 'recorded in a retrieval span'
    elif called:
        search = 'called without a retrieval span'
    else:
        search = 'unknown (legacy run)'
    return f'Retrieval tracing: {setting} | Search: {search}'


def _iso(nanoseconds):
    return datetime.fromtimestamp(nanoseconds / 1_000_000_000, timezone.utc).isoformat().replace('+00:00', 'Z')


def _metadata_value(value):
    return value if isinstance(value, str) else json.dumps(value)


def _token_count(value):
    return value if type(value) is int and value >= 0 else None


def _sum_usage(models, field):
    values = [_token_count(item['attributes'].get('provider.' + field)) for item in models]
    return sum(values) if values and all(value is not None for value in values) else None


def record_completion(span, llm, answer):
    span.set_attribute('workshop.output', answer)
    span.set_attribute('gen_ai.response.model', llm.model)
    for name, value in (getattr(llm, 'last_completion_metadata', {}) or {}).items():
        if isinstance(value, (str, bool, int, float)):
            span.set_attribute('provider.' + name, value)
    span.set_status(StatusCode.OK)


class TrackSpans(SpanProcessor):
    """SimpleSpanProcessor sees ended spans; this also tracks unfinished children."""

    def __init__(self, exporter):
        self.exporter = exporter

    def on_start(self, span, parent_context=None):
        context = span.get_span_context()
        self.exporter.active.setdefault(context.trace_id, set()).add(context.span_id)


class WorkshopExporter(SpanExporter):
    def __init__(self, client, destination, state_dir, *, run_label=None):
        self.client = client
        self.destination = dict(destination)
        self.state_dir = Path(state_dir)
        self.active = {}
        self.waiting = {}
        self.receipts = []
        self.run_label = run_label

    def export(self, spans):
        result = SpanExportResult.SUCCESS
        for span in spans:
            trace_id = span.context.trace_id
            self.active.setdefault(trace_id, set()).discard(span.context.span_id)
            self.waiting.setdefault(trace_id, []).append(span)
            if self.active[trace_id]:
                continue
            ended = self.waiting.pop(trace_id)
            self.active.pop(trace_id)
            roots = [item for item in ended if item.parent is None]
            ids = {item.context.span_id for item in ended}
            if len(roots) != 1 or any(item.parent and item.parent.span_id not in ids for item in ended):
                self.waiting[trace_id] = ended
                result = SpanExportResult.FAILURE
                continue
            receipt, trace = self._convert(roots[0], ended)
            receipt['upload_payload'] = {'log_stream_id': self.destination['log_stream_id'], 'traces': [trace],
                'reliable': True, 'include_trace_ids': True}
            self._save(receipt)
            self._deliver(receipt, trace)
            if self.run_label:
                receipt['label'] = self.run_label
                try:
                    label_run(self.state_dir, self.run_label, receipt['run_id'])
                    receipt['label_status'] = 'saved'
                except (OSError, ValueError) as exc:
                    receipt.update(label_status='failed', label_error=f'{type(exc).__name__}: {exc}')
            self._save(receipt)
            self.receipts.append(receipt)
            if receipt['upload_status'] != 'accepted' or receipt['readback_status'] == 'failed':
                result = SpanExportResult.FAILURE
        return result

    def force_flush(self, timeout_millis=30000):
        return not self.waiting and not any(self.active.values())

    def shutdown(self):
        self.force_flush()

    def _save(self, receipt):
        write_json(self.state_dir / 'otel/runs' / (receipt['run_id'] + '.json'), receipt)
        write_json(self.state_dir / 'otel/latest.json', {'run_id': receipt['run_id']})

    def _convert(self, root, spans):
        run_id = str(uuid.uuid4())
        native_ids = {span.context.span_id: str(uuid.uuid4()) for span in spans}
        trace_id = native_ids[root.context.span_id]
        otel_trace = format(root.context.trace_id, '032x')
        ordered = sorted(spans, key=lambda span: span.start_time)
        records = {}
        saved_spans = []
        for span in ordered:
            attrs = dict(span.attributes or {})
            parent = span.parent.span_id if span.parent else None
            context = {'otel.trace_id': otel_trace, 'otel.span_id': format(span.context.span_id, '016x'),
                'otel.parent_span_id': format(parent, '016x') if parent else '',
                'otel.status': span.status.status_code.name,
                'otel.status_description': span.status.description or '',
                'service.name': str(span.resource.attributes.get('service.name', 'unknown')),
                'run_id': run_id, 'transport': 'custom-workshop-json',
                'workflow': 'model-selected-support-search', **attrs}
            events = [{'name': event.name, 'timestamp_ns': event.timestamp,
                'attributes': dict(event.attributes or {})} for event in span.events]
            context['otel.events'] = events
            kind = attrs.get('workshop.kind', 'workflow')
            record = {'id': native_ids[span.context.span_id], 'trace_id': trace_id,
                'name': span.name, 'created_at': _iso(span.start_time),
                'user_metadata': {key: _metadata_value(value) for key, value in context.items()},
                'metrics': {'duration_ns': span.end_time - span.start_time}, 'spans': []}
            if parent:
                record['parent_id'] = native_ids[parent]
                record['type'] = {'model': 'llm', 'retrieval': 'tool', 'agent': 'agent'}.get(kind, 'workflow')
            if span.status.status_code == StatusCode.ERROR:
                record['status_code'] = 500
            elif span.status.status_code == StatusCode.OK:
                record['status_code'] = 200
            if kind == 'model':
                record['input'] = json.loads(attrs.get('workshop.messages', '[]'))
                if 'workshop.output' in attrs:
                    record['output'] = {'role': 'assistant', 'content': attrs['workshop.output']}
                record['model'] = attrs.get('provider.model') or attrs.get('gen_ai.response.model') or attrs.get('gen_ai.request.model')
                for field in TOKEN_FIELDS.values():
                    count = attrs.get('provider.' + field)
                    if type(count) is int and count >= 0:
                        record['metrics'][field] = count
                if isinstance(attrs.get('provider.finish_reason'), str):
                    record['finish_reason'] = attrs['provider.finish_reason']
            else:
                record['input'] = attrs.get('workshop.question', '')
                if 'workshop.output' in attrs:
                    record['output'] = attrs['workshop.output']
            records[span.context.span_id] = record
            saved_spans.append({'name': span.name, 'type': record.get('type', 'trace'),
                'native_id': record['id'], 'native_parent_id': record.get('parent_id'),
                'otel_span_id': context['otel.span_id'], 'otel_parent_span_id': context['otel.parent_span_id'],
                'start_time_ns': span.start_time, 'end_time_ns': span.end_time,
                'duration_ns': span.end_time - span.start_time, 'status': span.status.status_code.name,
                'attributes': attrs, 'events': events})
        for span in ordered:
            if span.parent:
                records[span.parent.span_id]['spans'].append(records[span.context.span_id])
        trace = records[root.context.span_id]
        trace.update(log_stream_id=self.destination['log_stream_id'],
            tags=['devnet-workshop-otel', str(root.resource.attributes.get('service.name', 'unknown')), run_id])
        attrs = dict(root.attributes or {})
        models = [item for item in saved_spans if item['type'] == 'llm']
        final_models = [item for item in models if item['attributes'].get('workshop.response_kind') == 'final-answer']
        model = final_models[-1] if final_models else models[-1] if models else None
        model_attrs = model['attributes'] if model else {}
        model_status = ('error' if model['status'] == 'ERROR' else 'ok' if final_models else 'unavailable') if model else 'not-called'
        answer = attrs.get('workshop.output')
        receipt = {'schema_version': 1, 'run_id': run_id, 'created_at': _iso(root.start_time),
            'destination': self.destination, 'service_name': str(root.resource.attributes.get('service.name', 'unknown')),
            'otel_trace_id': otel_trace, 'otel_root_span_id': format(root.context.span_id, '016x'),
            'native_trace_id': trace_id, 'spans': saved_spans, 'answer': answer,
            'status': 'error' if root.status.status_code == StatusCode.ERROR else 'ok',
            'model_status': model_status, 'finish_reason': model_attrs.get('provider.finish_reason'),
            'duration_ns': root.end_time - root.start_time,
            'request': {'question': attrs.get('workshop.question'), 'top_k': attrs.get('workshop.top_k'),
                'max_tokens': attrs.get('gen_ai.request.max_tokens'), 'requested_model': attrs.get('gen_ai.request.model'),
                'model': model_attrs.get('provider.model') or model_attrs.get('gen_ai.response.model'),
                'model_endpoint': attrs.get('workshop.model_endpoint'),
                'trace_retrieval': attrs.get('workshop.trace_retrieval'),
                'search_called': attrs.get('workshop.search_called'),
                'document_count': attrs.get('workshop.document_count'),
                'documents': json.loads(attrs.get('workshop.documents', '[]'))},
            'usage': {name: _sum_usage(models, field) for name, field in TOKEN_FIELDS.items()},
            'privacy': {'input_labels': _pii_labels(attrs.get('workshop.question', '')),
                'output_labels': _pii_labels(answer) if isinstance(answer, str) else None,
                'source': 'local-email-phone-pattern-v1'},
            'retrieval_status': ('instrumented' if any(item['name'] == 'retrieve-documents' for item in saved_spans)
                else 'enabled-no-search' if attrs.get('workshop.trace_retrieval') is True
                and attrs.get('workshop.search_called') is False else 'not-instrumented'),
            'upload_status': 'not-attempted', 'readback_status': 'not-attempted'}
        return receipt, trace

    def _deliver(self, receipt, trace):
        try:
            response = self.client.post(f"/v2/projects/{self.destination['project_id']}/traces",
                json=receipt['upload_payload'])
        except Exception as exc:
            receipt.update(upload_status='failed', delivery_error=f'{type(exc).__name__}: {exc}')
            return
        if (not isinstance(response, dict) or response.get('trace_ids') != [trace['id']]
                or ('traces_count' in response and response['traces_count'] != 1)):
            receipt.update(upload_status='unconfirmed', delivery_error='Upload did not confirm this exact native trace ID; inspect before repeating the write.')
            return
        receipt['upload_status'] = 'accepted'
        try:
            trace = read_back(self.client, receipt)
            receipt['readback_status'] = 'verified'
            receipt['session_id'] = trace.get('session_id')
        except PendingRecords as exc:
            receipt.update(readback_status='pending', delivery_error=str(exc))
        except GalileoApiError as exc:
            receipt['readback_status'] = 'pending' if exc.status_code in (404, 503) else 'failed'
            receipt['delivery_error'] = 'Trace is still indexing; run workshop_results.py inspect without calling the model again.' if receipt['readback_status'] == 'pending' else str(exc)
        except Exception as exc:
            receipt.update(readback_status='failed', delivery_error=f'{type(exc).__name__}: {exc}')


def _flatten(trace):
    result = {trace.get('id'): trace}
    for span in trace.get('spans') or []:
        result.update(_flatten(span))
    return result


def read_back(client, receipt):
    for attempt in range(3):
        try:
            return _read_once(client, receipt)
        except PendingRecords:
            if attempt == 2:
                raise
        except GalileoApiError as exc:
            if exc.status_code not in (404, 503) or attempt == 2:
                raise
        time.sleep(0.2 * (attempt + 1))


def _message_matches(original, stored):
    if not isinstance(original, dict) or not isinstance(stored, dict):
        return original == stored
    calls = original.get('tool_calls')
    if original.get('role') != 'assistant' or not isinstance(calls, list) or not calls:
        return original == stored
    expected = copy.deepcopy(original)
    # Native messages use empty content and omit the standard function discriminator.
    if expected.get('content') is None and stored.get('content') == '':
        expected['content'] = ''
    native_calls = stored.get('tool_calls')
    if isinstance(native_calls, list) and len(calls) == len(native_calls):
        for call, native in zip(expected['tool_calls'], native_calls):
            if isinstance(call, dict) and isinstance(native, dict) and call.get('type') == 'function' and 'type' not in native:
                call.pop('type')
    return expected == stored


def _input_matches(original, stored):
    if not isinstance(original, list) or not isinstance(stored, list):
        return original == stored
    return len(original) == len(stored) and all(_message_matches(want, got) for want, got in zip(original, stored))


def _read_once(client, receipt):
    destination = receipt['destination']
    if client.base_url.rstrip('/') != destination['api_base_url'].rstrip('/'):
        raise ValueError('Current API destination differs from this saved run')
    trace = client.get(f"/v2/projects/{destination['project_id']}/traces/{receipt['native_trace_id']}")
    if not isinstance(trace, dict) or trace.get('id') != receipt['native_trace_id']:
        raise ValueError('Stored record native trace ID does not match saved records')
    if trace.get('log_stream_id') not in (None, destination['log_stream_id']):
        raise ValueError('Stored record log stream differs from saved records')
    if trace.get('project_id') not in (None, destination['project_id']):
        raise ValueError('Stored record project differs from saved records')
    records = _flatten(trace)
    expected = _flatten(receipt['upload_payload']['traces'][0])
    for saved in receipt['spans']:
        record = records.get(saved['native_id'])
        if record is None:
            raise PendingRecords('SDK spans are still indexing; retry inspect without rerunning the app')
        meta = record.get('user_metadata') or {}
        if (meta.get('otel.trace_id') != receipt['otel_trace_id']
                or meta.get('otel.span_id') != saved['otel_span_id']
                or meta.get('otel.parent_span_id') != saved['otel_parent_span_id']
                or meta.get('run_id') != receipt['run_id']):
            raise ValueError('Stored record SDK context does not match saved records')
        if saved['native_parent_id'] and record.get('parent_id') != saved['native_parent_id']:
            raise ValueError('Stored span parent does not match SDK parent mapping')
        original = expected[saved['native_id']]
        for field in ('name', 'type', 'input', 'output', 'model', 'status_code'):
            if field not in original:
                continue
            matches = _input_matches(original[field], record.get(field)) if field == 'input' else record.get(field) == original[field]
            if not matches:
                raise ValueError(f'Stored {field} does not match the actual SDK request')
        for field, value in original['metrics'].items():
            stored = (record.get('metrics') or {}).get(field)
            matches = duration_matches(value, stored) if field == 'duration_ns' else type(stored) in (int, float) and stored == value
            if not matches:
                raise ValueError('Stored measurement does not match the actual SDK/provider value')
    return trace
