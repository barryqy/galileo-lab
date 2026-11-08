#!/usr/bin/env python3
"""A small support workflow with real OpenTelemetry instrumentation."""

QUESTION = 'Which signals should I check when an AI assistant becomes slow?'
TOP_K = 3
MAX_TOKENS = 180

import argparse
import json
import os
from pathlib import Path
import re
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from barrybot import DevNetLLM
from galileo_lab import STATE_DIR, client_or_exit, ensure_project_and_stream, load_local_env
from observability_tests import get_agent_name, _pii_labels
from workshop_telemetry import WorkshopExporter, TrackSpans, record_completion

DOCS = Path(__file__).parent / 'support_docs'


def answer_question(tracer, llm, question, top_k=TOP_K, max_tokens=MAX_TOKENS, docs_dir=DOCS):
    if not question.strip() or top_k < 1 or max_tokens < 1:
        raise ValueError('Use a non-empty question and positive TOP_K and MAX_TOKENS')
    with tracer.start_as_current_span('answer-question') as request:
        request.set_attributes({'workshop.question': question, 'workshop.top_k': top_k,
            'gen_ai.request.max_tokens': max_tokens, 'gen_ai.request.model': llm.model,
            'workshop.model_endpoint': llm.base_url, 'pii_input': ','.join(_pii_labels(question)) or 'none',
            'pii_detector': 'local-email-phone-pattern-v1'})
        with tracer.start_as_current_span('support-agent', attributes={'workshop.kind': 'agent',
                'workshop.question': question}) as agent:
            # Uncomment this line to observe the real retrieval work as its own SDK span.
            # @tracer.start_as_current_span("retrieve-documents")
            def retrieve_documents(query):
                words = set(re.findall(r'[a-z]{3,}', query.lower()))
                ranked = []
                for path in sorted(Path(docs_dir).glob('*.md')):
                    text = path.read_text(encoding='utf-8')
                    score = len(words & set(re.findall(r'[a-z]{3,}', text.lower())))
                    if text.strip():
                        ranked.append((score, path.name, text))
                ranked.sort(key=lambda item: (-item[0], item[1]))
                documents = ranked[:top_k]
                if not documents:
                    raise ValueError('No local support documents were found; restore support_docs/*.md')
                active = trace.get_current_span()
                if active is not agent:
                    active.set_attributes({'workshop.kind': 'retrieval', 'workshop.question': query,
                        'workshop.output': '\n'.join(name for _, name, _ in documents)})
                return documents

            tools = [{'type': 'function', 'function': {'name': 'search_support_notes',
                'description': 'Search the local support notes for information needed to answer the question.',
                'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                    'required': ['query'], 'additionalProperties': False}}}]
            messages = [{'role': 'system', 'content': 'You are a support agent. Search the support notes '
                'before answering. Use only supported facts, answer concisely, and do not repeat contact details.'},
                {'role': 'user', 'content': question}]
            selected_documents = []
            for _ in range(3):
                with tracer.start_as_current_span('support-model-step', attributes={'workshop.kind': 'model',
                        'workshop.messages': json.dumps(messages), 'gen_ai.request.model': llm.model}) as model:
                    message = llm.chat_step(messages, tools=tools, max_tokens=max_tokens)
                    calls = message.get('tool_calls') or []
                    answer = message.get('content')
                    if not calls and (not isinstance(answer, str) or not answer.strip()):
                        raise ValueError('The model returned no answer')
                    model.set_attribute('workshop.model_message', json.dumps(message))
                    record_completion(model, llm, json.dumps(calls) if calls else answer)
                    model.set_attribute('workshop.response_kind', 'tool-selection' if calls else 'final-answer')
                    if not calls:
                        model.update_name('support-answer')
                messages.append(message)
                if not calls:
                    break
                if not isinstance(calls, list) or len(calls) > 3:
                    raise ValueError('The model returned an invalid number of tool calls')
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                        raise ValueError('The model returned an invalid tool call')
                    function = call['function']
                    if function.get('name') != 'search_support_notes' or not isinstance(call.get('id'), str):
                        raise ValueError('The model selected an unsupported tool')
                    arguments = json.loads(function.get('arguments', '{}'))
                    query = arguments.get('query') if isinstance(arguments, dict) else None
                    if not isinstance(query, str) or not query.strip():
                        raise ValueError('The search tool needs a non-empty query')
                    documents = retrieve_documents(query)
                    names = [name for _, name, _ in documents]
                    selected_documents.extend(names)
                    request.set_attributes({'workshop.documents': json.dumps(selected_documents),
                        'workshop.document_count': len(selected_documents)})
                    print('Search: ' + query + '\nDocuments: ' + ', '.join(names))
                    context = '\n\n'.join(f'[{name}]\n{text}' for _, name, text in documents)
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': context})
            else:
                raise ValueError('The model did not finish an answer within three rounds; inspect this request')
            agent.set_attribute('workshop.output', answer)
            agent.set_status(trace.StatusCode.OK)
        request.set_attribute('workshop.output', answer)
        request.set_attribute('pii_output', ','.join(_pii_labels(answer)) or 'none')
        request.set_status(trace.StatusCode.OK)
    return answer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question', default=QUESTION)
    parser.add_argument('--details', action='store_true', help='Show saved run and native record identifiers')
    parser.add_argument('--top-k', '--context-limit', type=int, default=TOP_K, dest='top_k')
    parser.add_argument('--max-tokens', type=int, default=MAX_TOKENS)
    args = parser.parse_args(argv)
    load_local_env()
    provider = None
    try:
        client = client_or_exit()
        destination = {'api_base_url': client.base_url, **ensure_project_and_stream(client),
            'console_url': os.environ.get('GALILEO_CONSOLE_URL', 'https://app.galileo.ai/barry-2')}
        agent_name = get_agent_name(STATE_DIR)
        exporter = WorkshopExporter(client, destination, STATE_DIR)
        provider = TracerProvider(resource=Resource.create({'service.name': agent_name}))
        provider.add_span_processor(TrackSpans(exporter))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer('devnet.workshop')
        answer = answer_question(tracer, DevNetLLM.from_env(), args.question, args.top_k, args.max_tokens)
        print('\nAnswer: ' + answer)
        if not provider.force_flush() or not exporter.receipts:
            raise RuntimeError('SDK spans are incomplete; inspect the unfinished request before rerunning')
        receipt = exporter.receipts[-1]
        print(f"\nOTel trace ID: {receipt['otel_trace_id']}")
        print(f"Request: {receipt['duration_ns'] / 1_000_000:.1f} ms | Provider tokens: {receipt['usage']['total_tokens'] if receipt['usage']['total_tokens'] is not None else 'unavailable'}")
        if args.details:
            print(f"Run: {receipt['run_id']}\nNative trace UUID: {receipt['native_trace_id']}")
            print(f"Upload: {receipt['upload_status']} | Record lookup: {('available' if receipt['readback_status'] == 'verified' else receipt['readback_status'])}")
        print('Explore: python3 workshop_results.py json')
        print('Open response: python3 workshop_results.py open --view response')
        if receipt['upload_status'] != 'accepted' or receipt['readback_status'] == 'failed':
            raise RuntimeError(receipt.get('delivery_error', 'Trace delivery failed'))
        return 0
    except Exception as exc:
        print(f'Workshop error: {exc}', file=sys.stderr)
        return 1
    finally:
        if provider is not None:
            provider.force_flush()
            provider.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
