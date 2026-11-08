"""Saved-evidence navigation for the guided lessons."""

import json
import os
import sys
import tempfile
from urllib.parse import quote, urlencode, urlsplit

from galileo_client import GalileoApiError
from observability_tests import _display, _load_manifest, report_findings


CHECKPOINTS = (
    'baseline-response', 'baseline-tokens', 'baseline-latency', 'efficient-response',
    'privacy-response', 'privacy-metadata', 'trends', 'control-allowed', 'control-denied',
    'refund-baseline', 'refund-candidate', 'dataset', 'experiment-baseline',
    'experiment-candidate', 'sample-review', 'dashboard',
    'scorers', 'integrations', 'instrumentation', 'interactive',
)
BATCH_VIEWS = {
    'response': ('Conversation / model response', 'Read the question and actual model answer.'),
    'tokens': ('Model span / Tokens', 'Compare input, output and total tokens. Missing usage is unavailable, never zero.'),
    'latency': ('Trace timeline / Latency', 'Compare controlled reference retrieval, model duration and total duration.'),
    'metadata': ('Parent trace / Metadata', 'Select the parent trace, then Trace Summary → Params → Metadata. Find pii_input, pii_output and pii_detector. These are local email/phone pattern checks, not a native classifier.'),
}


def save_navigation(state_dir, feature, client, ids, *, console_url=None, **values):
    path = state_dir / 'navigation.json'
    tmp_path = None
    try:
        try:
            records = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            records = {}
        if not isinstance(records, dict):
            raise ValueError('existing navigation file must contain an object')
        records[feature] = {'destination': {'api_base_url': client.base_url.rstrip('/'), **ids,
            'console_url': (console_url or os.environ.get('GALILEO_CONSOLE_URL',
                'https://app.galileo.ai/barry-2')).rstrip('/')}, **values}
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        fd, tmp_path = tempfile.mkstemp(prefix='.navigation-', suffix='.json', dir=state_dir)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(records, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except (OSError, ValueError, TypeError) as exc:
        # Optional UI navigation must not invalidate an otherwise successful feature.
        print(f'Navigation warning: {feature} navigation was not saved: {exc}. '
            f'Any existing {path} is preserved; inspect it before retrying the feature.')
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                print(f'Navigation warning: could not remove temporary navigation file {tmp_path}: {exc}')


def _console_url(destination):
    console = destination['console_url']
    parsed = urlsplit(console)
    if (parsed.scheme not in ('https', 'http') or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in console)):
        raise ValueError('Saved console destination is invalid')
    return console.rstrip('/')


def _stream_url(destination):
    console = _console_url(destination)
    return (f"{console.rstrip('/')}/project/{quote(destination['project_id'], safe='')}/"
        f"agent-streams/{quote(destination['log_stream_id'], safe='')}")


def _trace_url(url, row, *, model=True):
    if not row.get('session_id') or (model and not row.get('llm_span_id')):
        return url
    query = {'traceId': row['trace_id'], 'tabView': 'conversation'}
    if model:
        query['spanId'] = row['llm_span_id']
    return f"{url}/sessions/{quote(row['session_id'], safe='')}?{urlencode(query)}"


def _show(url, view, instruction, markers, readiness):
    print(f'Expected view: {view}')
    print(f'Readiness: {readiness}')
    print(markers)
    print(f'What to find: {instruction}')
    if sys.stdout.isatty() and os.environ.get('TERM') != 'dumb' and 'NO_COLOR' not in os.environ:
        print(f'\033[1;36m{url}\033[0m')
    else:
        print(url)


def _batch_checkpoint(client, state_dir, checkpoint, run_id):
    suite, view = checkpoint.split('-', 1)
    if run_id:
        manifest = _load_manifest(state_dir, run_id)
        if manifest.get('suite') != suite:
            raise ValueError(f'This checkpoint needs a {suite} run, not {manifest.get("suite")}')
    else:
        candidates = []
        for path in (state_dir / 'runs').glob('*.json'):
            if path.name.endswith('-findings.json'):
                continue
            saved = json.loads(path.read_text(encoding='utf-8'))
            if saved.get('suite') == suite and saved.get('upload_status') == 'accepted':
                candidates.append(saved)
        if not candidates:
            raise ValueError(f'No saved {suite} evidence. Run run-tests --suite {suite} --guided first.')
        manifest = max(candidates, key=lambda item: item['created_at'])
    report = report_findings(client, state_dir=state_dir, run_id=manifest['run_id'])
    case_name = 'fictional-contact' if suite == 'privacy' else 'monitor'
    row = next((item for item in report['cases'] if item['case'] == case_name), None)
    if row is None:
        raise ValueError(f'Saved {suite} run is missing its {case_name} case')
    url = _stream_url(report['destination'])
    target = _trace_url(url, row, model=view != 'metadata')
    ready = 'API evidence available' if row['status'] != 'pending' else 'Still indexing; retry findings --guided, without rerunning tests'
    if row['status'] not in ('completed', 'pending'):
        ready = f"Trace status: {row['status']}; a model answer may be unavailable"
    if target == url:
        ready += '; open the saved trace below from Tracing → Traces'
        if view != 'metadata':
            ready += ', then its model span when available'
    title, instruction = BATCH_VIEWS[view]
    markers = f"Saved {suite} run: {report['run_id']} | case: {case_name} | trace: {row['trace_id']}"
    if view == 'tokens':
        markers += (f"\nTokens: input {_display(row['input_tokens'])}, output {_display(row['output_tokens'])}, "
            f"total {_display(row['total_tokens'])}")
    elif view == 'latency':
        markers += (f"\nLatency: tool {_display(row['tool_ms'], ' ms')}, model {_display(row['model_ms'], ' ms')}, "
            f"total {_display(row['total_ms'], ' ms')}")
    _show(target, title, instruction, markers, ready)


def print_checkpoint(client, *, state_dir, checkpoint, run_id=None):
    if checkpoint.startswith(('baseline-', 'efficient-', 'privacy-')):
        return _batch_checkpoint(client, state_dir, checkpoint, run_id)
    if run_id:
        raise ValueError('--run applies only to baseline, efficient and privacy checkpoints')
    feature = checkpoint.split('-', 1)[0]
    feature = {'control': 'guardrail', 'refund': 'agent-demo', 'sample': 'human-workflows',
        'interactive': 'barrybot', 'instrumentation': 'log-traces'}.get(feature, feature)
    path = state_dir / 'navigation.json'
    records = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    saved = records.get(feature)
    if not saved:
        raise ValueError(f'No saved {feature} navigation evidence. Run {feature} --guided first.')
    destination = saved['destination']
    if client.base_url.rstrip('/') != destination['api_base_url']:
        raise ValueError('Current API destination differs from this saved feature')
    if feature in ('scorers', 'integrations'):
        console = _console_url(destination)
        route = '/evaluators' if feature == 'scorers' else '/settings/integrations'
        view = 'Evaluators' if feature == 'scorers' else 'Settings / Integrations'
        instruction = ('Inspect available evaluator definitions. Listing one does not mean it was run on these traces.'
            if feature == 'scorers' else 'Inspect integration options and setup requirements. Listing an integration does not configure a connection.')
        names = [str(item.get('name') or item.get('id') or '(unnamed)') for item in saved.get('entries', [])[:10]]
        _show(console + route, view, instruction,
            'Saved API entries: ' + (', '.join(names) or 'none returned'), 'Catalog read completed; UI visibility depends on account permissions')
        return
    url = _stream_url(destination)
    if feature == 'dataset':
        url = destination['console_url'].rstrip('/') + '/datasets'
        _show(url, 'Datasets / saved dataset', 'Select Datasets in the native navigation; open this exact dataset and inspect its input and expected output columns.',
            f"Dataset: {saved['name']} | ID: {saved['dataset_id']}", 'Saved dataset upload/reconciliation completed; native indexing may take time')
        return
    if feature == 'experiment':
        variant = checkpoint.split('-', 1)[1]
        row = saved.get('variants', {}).get(variant)
        if not row:
            raise ValueError(f'No saved {variant} experiment. Run experiment --guided first.')
        url = (destination['console_url'].rstrip('/') + '/project/'
            + quote(destination['project_id'], safe='') + '/experiments')
        _show(url, 'Experiments / saved variant', 'Select Experiments in this project; open this exact variant. Metrics and release gates in this lesson are application calculations.',
            f"{variant}: {row['name']} | experiment ID: {row['experiment_id']}", 'Saved evaluation upload completed; native indexing may take time')
        return
    if feature in ('trends', 'dashboard'):
        markers = f"Saved project: {destination['project_id']} | stream: {destination['log_stream_id']}"
        instruction = 'Select Trends for this saved stream; inspect the time range and available measurements. An empty chart does not mean zero.'
        if feature == 'dashboard':
            markers += f" | section: {saved['name']} ({saved['section_id']})"
            instruction += ' Find BarryBot release readiness and its four application metric widgets.'
        _show(url, 'Stream / Trends', instruction, markers, 'Configuration read back; metric availability depends on indexed evidence and selected time range')
        return
    if feature in ('barrybot', 'log-traces'):
        label = checkpoint
        if not saved.get('trace_id'):
            raise ValueError(f'No saved {feature} trace. Run {feature} --guided first.')
        row = {'trace_id': saved['trace_id']}
        view = 'Tracing → Traces / application response'
        instruction = ('Open this exact trace under Tracing → Traces; inspect input, output and response_source. '
            'A local fallback response is not a model response.')
    elif feature == 'guardrail':
        label = checkpoint.split('-', 1)[1]
        decision = 'ALLOW' if label == 'allowed' else 'DENY'
        row = saved.get('cases', {}).get(label)
        if not row or row.get('decision') != decision:
            raise ValueError(f'No saved {decision} decision. Run guardrail --guided with a credential-related risky prompt first.')
        instruction = ('Inspect decision ALLOW and protected_calls 1, followed by the protected model span.' if label == 'allowed'
            else 'Inspect decision DENY and protected_calls 0. There must be no model span for this trace.')
        view = 'Agent Control / policy trace'
    else:
        label = checkpoint.split('-', 1)[1] if feature == 'agent-demo' else 'candidate'
        row = saved.get('cases', {}).get(label)
        if not row:
            raise ValueError(f'No saved {label} evidence. Run {feature} --guided first.')
        view = 'Refund agent / trace' if feature == 'agent-demo' else 'Authored sample review / candidate trace'
        instruction = ('Inspect lookup_order, policy checks and refund/escalation tools; these tools are simulated.' if feature == 'agent-demo'
            else 'Inspect helpfulness 5/5 on this candidate and the saved baseline disposition. These are authored example ratings, not collected learner feedback.')
    trace_id = row['trace_id']
    readiness = 'Exact trace read back'
    try:
        trace = client.get(f"/v2/projects/{destination['project_id']}/traces/{trace_id}")
    except GalileoApiError as exc:
        if exc.status_code not in (404, 503):
            raise
        trace = None
    if trace is None:
        readiness = 'Still indexing or unavailable; retry this checkpoint without rerunning the feature'
    else:
        if trace.get('id') != trace_id:
            raise ValueError('API trace does not match the saved feature')
        if trace.get('log_stream_id') not in (None, destination['log_stream_id']):
            raise ValueError('API trace destination does not match the saved feature')
        if feature == 'guardrail':
            metadata = trace.get('user_metadata') or {}
            if metadata.get('decision') != decision:
                raise ValueError(f'Saved policy trace is not {decision}')
            calls = metadata.get('protected_calls')
            zero_calls = type(calls) in (int, str) and str(calls) == '0'
            if decision == 'DENY' and (not zero_calls
                    or any(span.get('type') == 'llm' for span in trace.get('spans') or [])):
                raise ValueError('DENY evidence contains protected model execution')
        row = dict(row, session_id=trace.get('session_id') or saved.get('session_id'))
        url = _trace_url(url, row, model=False)
    _show(url, view, instruction, f'Saved {label} trace: {trace_id}', readiness)
