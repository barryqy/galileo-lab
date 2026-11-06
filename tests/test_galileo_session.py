import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

PROD = 'https://devnet.cisco.com/v1/devenv-community/galileo'
STAGE = 'https://devnet-testing.cisco.com/v1/devenv-community/galileo'
PROJECT = '2e652072-412e-4bef-97a5-1a8f4dc5b68e'
STREAM = '9ac1ae85-1c47-4447-8050-df5fbb5b2ae6'


def environment():
    return {'HOSTNAME': 'devpod-fixture-environment', 'DEVENV_USER': 'fixture user',
        'DEVENV_PASSWORD': 'provided by environment', 'LLM_BASE_URL': PROD + '/llm'}


def descriptor(base=PROD):
    return {'token': 'provided by session service', 'base_url': base + '/proxy',
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        'project_id': PROJECT, 'log_stream_id': STREAM, 'environment_id': 'fixture-environment'}


def response(payload=None, status=200):
    result = Mock(status_code=status, text='response may contain sensitive data', headers={})
    result.json.return_value = descriptor() if payload is None else payload
    return result


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('galileo_session'), 'session bootstrap is missing')
        import galileo_session
        self.module = galileo_session

    def test_bootstrap_binds_actual_environment_and_uses_basic_auth_without_redirects(self):
        with patch('galileo_session.requests.post', return_value=response()) as post:
            session = self.module.bootstrap_session(environment())
        self.assertEqual(session.base_url, PROD + '/proxy')
        self.assertEqual(session.token, 'provided by session service')
        self.assertEqual(post.call_args.args, (PROD + '/sessions/fixture-environment',))
        self.assertEqual(post.call_args.kwargs['auth'], ('fixture user', 'provided by environment'))
        self.assertIs(post.call_args.kwargs['allow_redirects'], False)
        self.assertEqual(post.call_args.kwargs['timeout'], 30)

    def test_explicit_bootstrap_precedes_supported_llm_origin_fallback(self):
        env = environment()
        env['GALILEO_LAB_BOOTSTRAP_URL'] = STAGE + '/sessions'
        with patch('galileo_session.requests.post', return_value=response(descriptor(STAGE))) as post:
            self.assertEqual(self.module.bootstrap_session(env).base_url, STAGE + '/proxy')
        self.assertEqual(post.call_args.args[0], STAGE + '/sessions/fixture-environment')

    def test_foreign_local_and_malformed_bootstrap_inputs_never_make_requests(self):
        for source in ('http://devnet.cisco.com', 'https://foreign.invalid', 'http://localhost:8000',
            'https://127.0.0.1', 'https://devnet.cisco.com@foreign.invalid',
            'https://devnet.cisco.com:444', 'https://devnet.cisco.com?next=foreign'):
            with self.subTest(source=source):
                env = environment()
                env['LLM_BASE_URL'] = source
                with patch('galileo_session.requests.post') as post:
                    with self.assertRaisesRegex(RuntimeError, 'instructor'):
                        self.module.bootstrap_session(env)
                    post.assert_not_called()
        for value in ('https://devnet.cisco.com/wrong', PROD + '/sessions?next=x',
            PROD + '/sessions#fragment', 'https://foreign.invalid/sessions'):
            env = environment()
            env['GALILEO_LAB_BOOTSTRAP_URL'] = value
            with patch('galileo_session.requests.post') as post:
                with self.assertRaises(RuntimeError):
                    self.module.bootstrap_session(env)
                post.assert_not_called()

    def test_missing_environment_credentials_or_actual_id_stop_before_request(self):
        for field, value in (('DEVENV_USER', ''), ('DEVENV_PASSWORD', ''), ('HOSTNAME', 'laptop'),
            ('HOSTNAME', 'devpod-../foreign'), ('HOSTNAME', 'devpod-')):
            env = environment()
            env[field] = value
            with patch('galileo_session.requests.post') as post:
                with self.assertRaises(RuntimeError):
                    self.module.bootstrap_session(env)
                post.assert_not_called()

    def test_descriptor_rejects_changed_origin_path_identity_and_expired_sessions(self):
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        for field, value in (('base_url', STAGE + '/proxy'), ('base_url', PROD + '/other'),
            ('base_url', PROD + '/proxy?x=1'), ('project_id', 'other-project'),
            ('log_stream_id', 'other-stream'), ('environment_id', 'another-environment'),
            ('expires_at', expired), ('expires_at', 'not a date'), ('expires_at', '2099-01-01T00:00:00'),
            ('token', ''), ('token', 'bad\nheader'), ('token', 3)):
            payload = descriptor()
            payload[field] = value
            with self.subTest(field=field, value=value):
                with patch('galileo_session.requests.post', return_value=response(payload)):
                    with self.assertRaises(RuntimeError):
                        self.module.bootstrap_session(environment())
        for payload in ([], None, {'token': 'incomplete'}):
            result = response()
            result.json.return_value = payload
            with patch('galileo_session.requests.post', return_value=result):
                with self.assertRaises(RuntimeError):
                    self.module.bootstrap_session(environment())

    def test_failures_never_expose_response_or_credentials(self):
        for result in (response(status=401), response(status=302),
            requests.ConnectionError('provided by environment; response may contain sensitive data')):
            with patch('galileo_session.requests.post', side_effect=result if isinstance(result, Exception) else None,
                return_value=None if isinstance(result, Exception) else result):
                with self.assertRaises(RuntimeError) as caught:
                    self.module.bootstrap_session(environment())
            self.assertNotIn('provided by environment', str(caught.exception))
            self.assertNotIn('sensitive data', str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_session_environment_fields_are_separate_from_native_key(self):
        with patch('galileo_session.requests.post', return_value=response()):
            fields = self.module.bootstrap_session(environment()).environment_fields()
        self.assertNotIn('GALILEO_API_KEY', fields)
        self.assertEqual(fields['GALILEO_LAB_SESSION_TOKEN'], 'provided by session service')
        env = environment()
        env.update(fields)
        self.assertEqual(self.module.session_from_env(env).base_url, PROD + '/proxy')
        env['GALILEO_LAB_GATEWAY_URL'] = 'https://api.multitenant.galileocloud.io'
        with self.assertRaises(RuntimeError):
            self.module.session_from_env(env)


class PreparedTransportTests(unittest.TestCase):
    def test_real_requests_preparation_keeps_basic_and_session_credentials_on_their_own_routes(self):
        import base64
        import json
        from galileo_client import GalileoClient
        from galileo_session import bootstrap_session
        prepared = []

        def send(request, **kwargs):
            prepared.append((request, kwargs))
            result = requests.Response()
            result.status_code = 200
            result._content = json.dumps(descriptor() if request.url.endswith('/sessions/fixture-environment') else
                {'id': 'fixture current user'}).encode()
            result.request = request
            return result

        env = environment()
        with patch('requests.sessions.Session.send', side_effect=send):
            session = bootstrap_session(env)
            env.update(session.environment_fields())
            env.update({'GALILEO_LAB_PROFILE': 'workshop', 'GALILEO_API_BASE_URL': session.base_url})
            with patch.dict('os.environ', env, clear=True):
                result = GalileoClient().get('/v2/current_user')
        self.assertEqual(result, {'id': 'fixture current user'})
        self.assertEqual(len(prepared), 2)
        bootstrap, gateway = prepared
        basic = base64.b64encode(b'fixture user:provided by environment').decode()
        self.assertEqual(bootstrap[0].headers['Authorization'], 'Basic ' + basic)
        self.assertNotIn('Galileo-API-Key', bootstrap[0].headers)
        self.assertEqual(gateway[0].headers['Galileo-API-Key'], 'provided by session service')
        self.assertNotIn('Authorization', gateway[0].headers)
        self.assertEqual(bootstrap[0].url, PROD + '/sessions/fixture-environment')
        self.assertEqual(gateway[0].url, PROD + '/proxy/v2/current_user')
        self.assertIs(bootstrap[1]['allow_redirects'], False)
        self.assertIs(gateway[1]['allow_redirects'], False)

    def test_expiry_requires_rfc3339_and_repr_hides_token(self):
        from galileo_session import bootstrap_session
        for expiry in ('2099-01-01', '20990101T000000+0000', '2099-01-01 00:00:00+00:00'):
            payload = descriptor()
            payload['expires_at'] = expiry
            with patch('galileo_session.requests.post', return_value=response(payload)):
                with self.assertRaises(RuntimeError):
                    bootstrap_session(environment())
        with patch('galileo_session.requests.post', return_value=response()):
            self.assertNotIn('provided by session service', repr(bootstrap_session(environment())))


if __name__ == '__main__':
    unittest.main()
