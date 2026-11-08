#!/usr/bin/env python3
"""Inspect saved workshop results without calling the model again."""

import argparse
import copy
import json
from pathlib import Path
import sys
import uuid
from urllib.parse import quote, urlencode

from galileo_client import GalileoApiError
from galileo_lab import STATE_DIR, client_or_exit, load_local_env
from guided_navigation import _stream_url
from workshop_telemetry import read_back, write_json

VIEWS = ('response', 'trace', 'agent', 'tokens', 'latency', 'privacy')


def _run_id(value):
    try:
        result = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError('Use a saved run UUID from workshop_app.py') from exc
    if result.version != 4:
        raise ValueError('Use a saved run UUID from workshop_app.py')
    return str(result)


def load_run(state_dir, run_id=None, view=None):
    folder = Path(state_dir) / 'otel/runs'
    if run_id:
        path = folder / (_run_id(run_id) + '.json')
        if not path.exists():
            raise ValueError('That workshop run is not saved; run workshop_app.py first')
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('run_id') != path.stem or saved.get('schema_version') != 1:
            raise ValueError('The saved run does not match its results file')
        return saved
    candidates = []
    for path in folder.glob('*.json'):
        saved = load_run(state_dir, path.stem)
        if view and (saved.get('upload_status') != 'accepted' or saved.get('model_status') != 'ok'):
            continue
        if view == 'privacy' and not saved.get('privacy', {}).get('input_labels'):
            continue
        candidates.append(saved)
    if not candidates:
        action = 'a question containing fictional contact details' if view == 'privacy' else 'workshop_app.py'
        raise ValueError(f'No matching saved workshop results. Run {action} first')
    return max(candidates, key=lambda item: (item['created_at'], item['run_id']))


def inspect_run(client, receipt, *, local=False):
    if local:
        return copy.deepcopy(receipt)
    if receipt['upload_status'] != 'accepted':
        raise ValueError('The saved upload is not confirmed. Inspect --local before deciding whether to repeat it')
    result = copy.deepcopy(receipt)
    try:
        trace = read_back(client, receipt)
    except GalileoApiError as exc:
        if exc.status_code in (404, 503):
            raise ValueError('These records are still indexing; retry inspect without rerunning the app') from exc
        raise
    result['readback_status'] = 'verified'
    result['session_id'] = trace.get('session_id')
    result['remote_trace'] = trace
    return result


def _successful(receipt):
    if receipt['model_status'] != 'ok' or receipt['status'] != 'ok':
        raise ValueError('This request has no successful model result; inspect it before opening a response')


def open_view(client, receipt, view):
    _successful(receipt)
    if view == 'privacy' and not receipt['privacy']['input_labels']:
        raise ValueError('This view needs a saved request containing fictional contact details')
    result = inspect_run(client, receipt)
    trace = result['remote_trace']
    model = next((span for span in reversed(receipt['spans']) if span['type'] == 'llm'
        and span['attributes'].get('workshop.response_kind') == 'final-answer'), None)
    if view in ('response', 'tokens', 'latency') and model is None:
        raise ValueError('The saved model span is missing; inspect the request before opening this view')
    url = _stream_url(receipt['destination'])
    query = {'traceId': receipt['native_trace_id'], 'tabView': 'conversation'}
    if view in ('response', 'tokens'):
        query['spanId'] = model['native_id']
    if view == 'agent':
        url += '?view=graph'
    elif trace.get('session_id'):
        url += '/sessions/' + quote(str(trace['session_id']), safe='') + '?' + urlencode(query)
    else:
        url += '?' + urlencode(query)
    print(f'View: {view} | Request: {receipt["request"]["question"]}')
    print('OTel trace ID: ' + receipt['otel_trace_id'])
    print('Native trace UUID: ' + receipt['native_trace_id'])
    instructions = {'response': 'Read the question and actual model answer.',
        'trace': 'Inspect the root, agent, model, and any instrumented retrieval span in the timeline.',
        'agent': 'Explore Agent graph for this shared stream. The model selects search_support_notes and receives its actual results; open Traces for one request.',
        'tokens': 'Select the model span and inspect Tokens. Missing provider usage is unavailable.',
        'latency': 'Compare the SDK timeline durations. Retrieval appears only after adding its decorator.',
        'privacy': 'Inspect recorded input/output and metadata; the contact pattern check is local application code.'}
    print(instructions[view])
    if not trace.get('session_id') and view != 'agent':
        print('If the console opens the stream, select Tracing → Traces and this native trace UUID.')
    print(f'\033[1;36m{url}\033[0m')
    return url


def compare_runs(client, before, after):
    _successful(before)
    _successful(after)
    fields = ('question', 'max_tokens', 'requested_model', 'model', 'model_endpoint')
    if (before['destination'] != after['destination'] or before['service_name'] != after['service_name']
            or any(before['request'].get(key) != after['request'].get(key) for key in fields)):
        raise ValueError('Comparison needs the same question, destination, service, model and maximum output tokens')
    if before['request']['top_k'] == after['request']['top_k']:
        raise ValueError('Change only TOP_K before running the second request')
    rows = []
    for saved in (before, after):
        inspect_run(client, saved)
        retrieval = [span for span in saved['spans'] if span['name'] == 'retrieve-documents']
        rows.append({'run_id': saved['run_id'], 'top_k': saved['request']['top_k'], 'answer': saved['answer'],
            'duration_ns': saved['duration_ns'],
            'model_duration_ns': sum(span['duration_ns'] for span in saved['spans'] if span['type'] == 'llm'),
            'document_count': saved['request'].get('document_count'), 'documents': saved['request'].get('documents'),
            'retrieval_duration_ns': sum(span['duration_ns'] for span in retrieval) if retrieval else None,
            'usage': dict(saved['usage'])})
    return {'before': rows[0], 'after': rows[1]}


def comparison_pair(state_dir):
    runs = [load_run(state_dir, path.stem) for path in (Path(state_dir) / 'otel/runs').glob('*.json')]
    runs.sort(key=lambda item: (item['created_at'], item['run_id']), reverse=True)
    for after in runs:
        if after['model_status'] != 'ok' or after['upload_status'] != 'accepted':
            continue
        for before in runs:
            if before['created_at'] >= after['created_at'] or before['upload_status'] != 'accepted' or before['model_status'] != 'ok':
                continue
            if (before['destination'] == after['destination'] and before['service_name'] == after['service_name']
                    and all(before['request'].get(field) == after['request'].get(field) for field in
                        ('question', 'max_tokens', 'requested_model', 'model', 'model_endpoint'))
                    and before['request']['top_k'] != after['request']['top_k']):
                return before, after
    raise ValueError('No matching pair. Run the same question twice with only TOP_K changed')


def print_results(result):
    print('Question: ' + str(result['request']['question']))
    print('Answer: ' + str(result['answer'] if result['answer'] is not None else 'unavailable'))
    print(f"Run: {result['run_id']}\nService: {result['service_name']}\nOTel trace ID: {result['otel_trace_id']}\nNative trace UUID: {result['native_trace_id']}")
    print(f"Model result: {result['model_status']} | Upload: {result['upload_status']} | Record lookup: {('available' if result['readback_status'] == 'verified' else result['readback_status'])}")
    print('Provider tokens: ' + ', '.join(f'{name}={value if value is not None else "unavailable"}' for name, value in result['usage'].items()))
    if any(span['attributes'].get('provider.request_count', 0) > 1 for span in result['spans']):
        print('Retries occurred; failed-attempt token usage is unavailable. Counts cover successful replies.')
    for span in result['spans']:
        print(f"  {span['name']} [{span['type']}] {span['duration_ns'] / 1_000_000:.3f} ms | SDK status {span['status']}")
        print(f"    span_id={span['otel_span_id']} parent_span_id={span['otel_parent_span_id'] or '(root)'}")
    print('Retrieval: ' + result['retrieval_status'])
    print(f"Documents: {result['request'].get('document_count', 'unavailable')} | " + ', '.join(result['request'].get('documents') or []))
    privacy = result['privacy']
    print('pii_input=' + (','.join(privacy['input_labels']) or 'none') + ' | pii_output='
        + (','.join(privacy['output_labels']) or 'none' if privacy['output_labels'] is not None else 'unavailable')
        + ' | pii_detector=' + privacy['source'])
    if result.get('delivery_error') and result['readback_status'] != 'verified':
        print(result['delivery_error'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect', help='Read results for a saved request; no model call')
    inspect.add_argument('--run')
    inspect.add_argument('--local', action='store_true', help='Read only the saved local receipt')
    raw = sub.add_parser('json', help='Print the actual JSON trace payload from a saved request')
    raw.add_argument('--run')
    opening = sub.add_parser('open', help='Print one native console URL for saved results')
    opening.add_argument('--view', choices=VIEWS, required=True)
    opening.add_argument('--run')
    compare = sub.add_parser('compare', help='Compare two saved requests with only TOP_K changed')
    compare.add_argument('--before')
    compare.add_argument('--after')
    args = parser.parse_args(argv)
    try:
        load_local_env()
        if args.command == 'json':
            receipt = load_run(STATE_DIR, args.run)
            print(json.dumps(receipt['upload_payload'], indent=2))
            return 0
        if args.command == 'compare':
            if bool(args.before) != bool(args.after):
                raise ValueError('Use both --before and --after, or omit both for the latest matching pair')
            before, after = ((load_run(STATE_DIR, args.before), load_run(STATE_DIR, args.after))
                if args.before else comparison_pair(STATE_DIR))
            report = compare_runs(client_or_exit(), before, after)
            print(json.dumps(report, indent=2))
            print('Does the shorter context still support a useful, accurate answer? Read both answers. '
                'One request does not establish a reliable latency or quality improvement.')
            return 0
        receipt = load_run(STATE_DIR, args.run, view=args.view if args.command == 'open' else None)
        if args.command == 'open':
            open_view(client_or_exit(), receipt, args.view)
        else:
            result = inspect_run(None if args.local else client_or_exit(), receipt, local=args.local)
            if not args.local:
                write_json(STATE_DIR / 'otel/runs' / (receipt['run_id'] + '.json'),
                    {key: value for key, value in result.items() if key != 'remote_trace'})
            print_results(result)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, GalileoApiError) as exc:
        print(f'Workshop results: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
