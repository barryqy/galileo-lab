import base64
import contextlib
import hashlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests
import galileo_session as sessions

BASE = 'https://o11y.barrysecure.com/v1/galileo'
REQUEST_ID = 'r' * 43


def environment():
    return {'HOSTNAME': 'devpod-fixture-environment', 'DEVENV_USER': 'Cisco fixture user',
        'DEVENV_PASSWORD': 'provided by Cisco environment', 'LLM_API_KEY': 'provided by model service',
        'LLM_BASE_URL': 'https://devnet.cisco.com/v1/llm'}


def descriptor():
    return {'token': 'provided by access service', 'renewal_token': 'provided by renewal service',
        'base_url': BASE + '/proxy',
        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=59)).isoformat(),
        'project_id': sessions.PROJECT_ID, 'log_stream_id': sessions.STREAM_ID,
        'environment_id': 'fixture-environment'}


def started():
    return {'connection_id': REQUEST_ID,
        'verification_url': 'https://o11y.barrysecure.com/connect?request=' + REQUEST_ID,
        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=9)).isoformat(), 'poll_interval': 2}


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


def session_environment():
    env = environment()
    value = descriptor()
    env.update({'GALILEO_LAB_PROFILE': 'workshop', 'GALILEO_API_BASE_URL': value['base_url'],
        'GALILEO_LAB_GATEWAY_URL': value['base_url'], 'GALILEO_LAB_SESSION_TOKEN': value['token'],
        'GALILEO_LAB_SESSION_RENEWAL_TOKEN': value['renewal_token'],
        'GALILEO_LAB_SESSION_EXPIRES_AT': value['expires_at'],
        'GALILEO_LAB_SESSION_PROJECT_ID': value['project_id'],
        'GALILEO_LAB_SESSION_LOG_STREAM_ID': value['log_stream_id'],
        'GALILEO_LAB_SESSION_ENVIRONMENT_ID': value['environment_id']})
    return env


class BarrysecureSessionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(sessions, 'initialize_session', None)), 'device Connect setup is missing')

    def test_default_connect_uses_verifier_pairing_without_cisco_or_netrc_auth(self):
        prepared = []
        outputs = [response(started(), 201), response({'status': 'pending'}, 202), response(descriptor())]

        def send(adapter, request, **kwargs):
            prepared.append((request, kwargs))
            result = outputs.pop(0)
            result.request = request
            return result

        stderr = io.StringIO()
        with patch.dict('os.environ', environment(), clear=True):
            with patch('requests.adapters.HTTPAdapter.send', send):
                with patch('requests.sessions.get_netrc_auth', return_value=('netrc user', 'netrc password')) as netrc:
                    with patch('requests.sessions.get_environ_proxies', return_value={'https': 'http://fixture-proxy.invalid'}):
                        with patch('galileo_session.time.sleep'), contextlib.redirect_stderr(stderr):
                            result = sessions.initialize_session(environment())
        self.assertEqual(result.base_url, BASE + '/proxy')
        self.assertEqual(result.renewal_token, 'provided by renewal service')
        self.assertEqual(prepared[0][0].url, BASE + '/connections')
        self.assertEqual(prepared[1][0].url, BASE + '/connections/' + REQUEST_ID + '/poll')
        first = json.loads(prepared[0][0].body)
        poll = json.loads(prepared[1][0].body)
        self.assertEqual(json.loads(prepared[2][0].body), poll)
        self.assertEqual(first['environment_id'], 'fixture-environment')
        self.assertEqual(len(poll['verifier']), 43)
        self.assertEqual(first['challenge'], base64.urlsafe_b64encode(hashlib.sha256(poll['verifier'].encode()).digest()).decode().rstrip('='))
        netrc.assert_not_called()
        for request, kwargs in prepared:
            self.assertNotIn('Authorization', request.headers)
            self.assertNotIn('Galileo-API-Key', request.headers)
            self.assertEqual(kwargs['proxies']['https'], 'http://fixture-proxy.invalid')
            self.assertNotIn('provided by Cisco', str(request.body))
            self.assertNotIn('provided by model', str(request.body))
        self.assertIn(started()['verification_url'], stderr.getvalue())
        self.assertNotIn('provided by', stderr.getvalue())
        self.assertNotIn(poll['verifier'], stderr.getvalue())

    def test_connect_link_is_validated_before_any_output_or_poll(self):
        links = ['https://foreign.invalid/connect?request=' + REQUEST_ID,
            'http://o11y.barrysecure.com/connect?request=' + REQUEST_ID,
            'https://o11y.barrysecure.com/v1/galileo/connect?request=' + REQUEST_ID,
            started()['verification_url'] + '&request=' + REQUEST_ID,
            started()['verification_url'] + '&other=1', started()['verification_url'] + '#fragment',
            'https://o11y.barrysecure.com/connect?request=another-request']
        for url in links:
            value = started()
            value['verification_url'] = url
            stderr = io.StringIO()
            with self.subTest(url=url), patch('galileo_session.requests.post', return_value=response(value, 201)) as post:
                with contextlib.redirect_stderr(stderr), self.assertRaises(RuntimeError):
                    sessions.initialize_session(environment())
            self.assertEqual(post.call_count, 1)
            self.assertIs(post.call_args.kwargs['allow_redirects'], False)
            self.assertEqual(stderr.getvalue(), '')

    def test_reused_active_session_renews_once_without_starting_connect(self):
        with patch('galileo_session.requests.post', return_value=response(descriptor())) as post:
            value = sessions.initialize_session(session_environment())
        self.assertEqual(value.token, 'provided by access service')
        self.assertNotIn('provided by', repr(value))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args, (BASE + '/sessions/fixture-environment/renew',))
        self.assertEqual(value.environment_fields()['GALILEO_LAB_SESSION_RENEWAL_TOKEN'], 'provided by renewal service')

    def test_expired_session_renews_with_opaque_bearer_only(self):
        env = session_environment()
        env['GALILEO_LAB_SESSION_EXPIRES_AT'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        prepared = []

        def send(adapter, request, **kwargs):
            prepared.append(request)
            result = response(descriptor())
            result.request = request
            return result

        with patch.dict('os.environ', env, clear=True), patch('requests.adapters.HTTPAdapter.send', send):
            with patch('requests.sessions.get_netrc_auth', return_value=('netrc user', 'netrc password')) as netrc:
                result = sessions.initialize_session(env)
        self.assertEqual(result.base_url, BASE + '/proxy')
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].url, BASE + '/sessions/fixture-environment/renew')
        self.assertEqual(prepared[0].headers['Authorization'], 'Bearer provided by renewal service')
        self.assertNotIn('Galileo-API-Key', prepared[0].headers)
        self.assertNotIn('provided by Cisco', str(prepared[0].body))
        netrc.assert_not_called()

    def test_barrysecure_session_rejects_wrong_scope_renewal_and_unbounded_expiry(self):
        for field, value in (('base_url', 'https://foreign.invalid/proxy'), ('project_id', 'another project'),
            ('log_stream_id', 'another stream'), ('environment_id', 'another environment'),
            ('renewal_token', ''), ('renewal_token', 'bad\nheader'),
            ('expires_at', (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()),
            ('expires_at', (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())):
            payload = descriptor()
            payload[field] = value
            with self.subTest(field=field), patch('galileo_session.requests.post', side_effect=[response(started(), 201), response(payload)]):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
                    sessions.initialize_session(environment())

    def test_poll_retries_lost_response_with_the_same_verifier_and_stays_bounded(self):
        results = [response(started(), 201), requests.Timeout('private transport details'), response(descriptor())]
        with patch('galileo_session.requests.post', side_effect=results) as post:
            with patch('galileo_session.time.sleep'), contextlib.redirect_stderr(io.StringIO()):
                sessions.initialize_session(environment())
        self.assertEqual(post.call_args_list[1].kwargs['json'], post.call_args_list[2].kwargs['json'])
        self.assertEqual(post.call_args_list[1].args, post.call_args_list[2].args)
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch('galileo_session.requests.post', side_effect=lambda *a, **kw: response(started(), 201) if a[0].endswith('/connections') else response({'status': 'pending'}, 202)) as post:
            with patch('galileo_session.time.monotonic', side_effect=lambda: clock[0]), patch('galileo_session.time.sleep', side_effect=sleep):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
                    sessions.initialize_session(environment())
        self.assertLessEqual(clock[0], 600)
        self.assertLessEqual(post.call_count, 301)

    def test_malformed_expired_revoked_and_redirect_responses_are_secret_safe(self):
        for result in (response({'error': 'private value'}, 401), response({}, 410), response({}, 302),
            response([], 201), requests.ConnectionError('provided by Cisco environment')):
            with patch('galileo_session.requests.post', side_effect=result if isinstance(result, Exception) else None,
                return_value=None if isinstance(result, Exception) else result):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError) as caught:
                    sessions.initialize_session(environment())
            self.assertNotIn('private value', str(caught.exception))
            self.assertNotIn('provided by Cisco', str(caught.exception))
        for status in (401, 409, 410, 302):
            with patch('galileo_session.requests.post', side_effect=[response(started(), 201), response({}, status)]):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
                    sessions.initialize_session(environment())

    def test_explicit_devnet_bootstrap_keeps_its_basic_mode_and_legacy_descriptor(self):
        from test_galileo_session import PROD, descriptor as native_descriptor
        env = session_environment()
        env['GALILEO_LAB_BOOTSTRAP_URL'] = PROD + '/sessions'
        with patch('galileo_session.requests.post', return_value=response(native_descriptor())) as post:
            value = sessions.initialize_session(env)
        self.assertEqual(value.base_url, PROD + '/proxy')
        self.assertEqual(value.renewal_token, '')
        self.assertEqual(post.call_args.args, (PROD + '/sessions/fixture-environment',))
        self.assertEqual(post.call_args.kwargs['auth'], ('Cisco fixture user', 'provided by Cisco environment'))
        self.assertNotIn('headers', post.call_args.kwargs)
        legacy = environment()
        legacy.update(value.environment_fields())
        self.assertEqual(sessions.session_from_env(legacy).base_url, PROD + '/proxy')

    def test_invalid_connection_metadata_is_not_printed(self):
        for field, value in (('connection_id', '../foreign'), ('poll_interval', 1000),
            ('expires_at', (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()),
            ('expires_at', (datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat())):
            payload = started()
            payload[field] = value
            stderr = io.StringIO()
            with patch('galileo_session.requests.post', return_value=response(payload, 201)) as post:
                with contextlib.redirect_stderr(stderr), self.assertRaises(RuntimeError):
                    sessions.initialize_session(environment())
            self.assertEqual(stderr.getvalue(), '')
            self.assertEqual(post.call_count, 1)


    def test_explicit_setup_recovers_only_after_definitive_renewal_rejection(self):
        env = session_environment()
        env['GALILEO_LAB_SESSION_EXPIRES_AT'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        for status in (401, 403, 404, 410):
            outputs = [response({'error': 'invalid renewal'}, status), response(started(), 201), response(descriptor())]
            with self.subTest(status=status), patch('galileo_session.requests.post', side_effect=outputs) as post:
                with contextlib.redirect_stderr(io.StringIO()):
                    value = sessions.initialize_session(env)
            self.assertEqual(value.base_url, BASE + '/proxy')
            self.assertEqual([call.args[0] for call in post.call_args_list], [BASE + '/sessions/fixture-environment/renew',
                BASE + '/connections', BASE + '/connections/' + REQUEST_ID + '/poll'])
        for failure in (requests.Timeout('private timeout details'), response({}, 503), response({}, 302)):
            with patch('galileo_session.requests.post', side_effect=failure if isinstance(failure, Exception) else None,
                return_value=None if isinstance(failure, Exception) else failure) as post:
                with self.assertRaises(RuntimeError) as caught:
                    sessions.initialize_session(env)
            self.assertEqual(post.call_count, 1)
            self.assertNotIn('private timeout details', str(caught.exception))

    def test_poll_response_arriving_after_deadline_is_rejected(self):
        clock = [0.0]
        def post(path, **kwargs):
            if path == '/connections':
                return response(started(), 201)
            clock[0] = 601.0
            return response(descriptor())
        with patch.object(sessions, '_barry_post', side_effect=post), patch.object(sessions.time, 'monotonic', side_effect=lambda: clock[0]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
                sessions.connect_session(environment())

    def test_stalled_poll_is_interrupted_by_the_posix_cli_deadline(self):
        import signal
        import time
        if not hasattr(signal, 'setitimer'):
            self.skipTest('POSIX CLI timer is unavailable')
        calls = []
        def post(path, **kwargs):
            calls.append(kwargs['timeout'])
            if path == '/connections':
                value = started()
                value['expires_at'] = (datetime.now(timezone.utc)+timedelta(seconds=0.15)).isoformat()
                return response(value, 201)
            time.sleep(1)
            return response(descriptor())
        previous = signal.getsignal(signal.SIGALRM)
        begin = time.monotonic()
        with patch.object(sessions, '_barry_post', side_effect=post):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
                sessions.connect_session(environment())
        self.assertLess(time.monotonic()-begin, 0.7)
        self.assertLessEqual(calls[-1], 0.15)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))


    def test_trickling_response_body_cannot_extend_the_posix_connect_window(self):
        import signal
        import time
        if not hasattr(signal, 'setitimer'):
            self.skipTest('POSIX CLI timer is unavailable')
        chunks_received = []

        class SlowBody:
            def stream(self, chunk_size, decode_content=True):
                for chunk in (b'{', b'"token"', b':', b'"late"', b'}'):
                    chunks_received.append(chunk)
                    yield chunk
                    time.sleep(0.1)

            def close(self):
                pass

        def send(adapter, request, **kwargs):
            if request.url.endswith('/connections'):
                value = started()
                value['expires_at'] = (datetime.now(timezone.utc)+timedelta(seconds=0.15)).isoformat()
                result = response(value, 201)
            else:
                result = requests.Response()
                result.status_code = 200
                result.raw = SlowBody()
            result.request = request
            return result

        begin = time.monotonic()
        with patch('requests.adapters.HTTPAdapter.send', send):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
                sessions.connect_session(environment())
        self.assertTrue(chunks_received, 'response delivered data before it stalled')
        self.assertLess(time.monotonic()-begin, 0.4)
        self.assertLess(len(chunks_received), 5)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))


    def test_unexpired_revoked_session_starts_fresh_connect_during_explicit_setup(self):
        results = [response({'error': 'revoked'}, 401), response(started(), 201), response(descriptor())]
        with patch('galileo_session.requests.post', side_effect=results) as post:
            with contextlib.redirect_stderr(io.StringIO()):
                sessions.initialize_session(session_environment())
        self.assertEqual([call.args[0] for call in post.call_args_list], [BASE + '/sessions/fixture-environment/renew',
            BASE + '/connections', BASE + '/connections/' + REQUEST_ID + '/poll'])



class BarrysecureClientTests(unittest.TestCase):
    def test_client_renews_once_without_connect_and_preserves_multipart_payload(self):
        from galileo_client import GalileoClient
        prepared = []
        outputs = [response({}, 401), response(descriptor()), response({'id': 'fixture-dataset'}, 201)]
        payload = b'input,ground_truth\nhello,world\n'

        def send(adapter, request, **kwargs):
            prepared.append(request)
            result = outputs.pop(0)
            result.request = request
            return result

        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('requests.adapters.HTTPAdapter.send', send):
                with patch('requests.sessions.get_netrc_auth', return_value=('Cisco fixture', 'native password')) as netrc:
                    result = GalileoClient().post('/v2/datasets', files={'file': ('fixture.csv', io.BytesIO(payload), 'text/csv')})
        self.assertEqual(result, {'id': 'fixture-dataset'})
        self.assertEqual([r.url for r in prepared], [BASE + '/proxy/v2/datasets',
            BASE + '/sessions/fixture-environment/renew', BASE + '/proxy/v2/datasets'])
        self.assertIn(payload, prepared[0].body)
        self.assertIn(payload, prepared[2].body)
        self.assertNotIn('Authorization', prepared[0].headers)
        self.assertNotIn('Authorization', prepared[2].headers)
        self.assertEqual(prepared[1].headers['Authorization'], 'Bearer provided by renewal service')
        netrc.assert_not_called()

    def test_revoked_renewal_fails_without_interactive_connect_or_second_write(self):
        from galileo_client import GalileoClient
        calls = []

        def send(adapter, request, **kwargs):
            calls.append(request.url)
            result = response({'error': 'private detail'}, 401)
            result.request = request
            return result

        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('requests.adapters.HTTPAdapter.send', send), patch('galileo_session.connect_session') as connect:
                with self.assertRaises(RuntimeError) as caught:
                    GalileoClient().post('/v2/datasets', files={'file': io.BytesIO(b'fixture csv')})
        self.assertEqual(calls, [BASE + '/proxy/v2/datasets', BASE + '/sessions/fixture-environment/renew'])
        self.assertNotIn('private detail', str(caught.exception))
        connect.assert_not_called()

    def test_native_key_cannot_use_barrysecure_even_without_a_session_descriptor(self):
        from galileo_client import GalileoClient
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(RuntimeError):
                GalileoClient(api_key='provided by native runtime', base_url=BASE + '/proxy')



if __name__ == '__main__':
    unittest.main()
