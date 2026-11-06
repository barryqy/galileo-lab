import unittest
from unittest.mock import Mock, patch

from galileo_client import GalileoClient


class ClientTests(unittest.TestCase):
    def test_dataset_cell_named_user_metadata_keeps_its_original_types(self):
        response = Mock(status_code=204, text="", headers={})
        payload = {"edits": [{"edit_type": "update_row", "row_id": "row",
            "values": {"input": "Known case", "user_metadata": {"revision": 2}}}]}
        with patch("galileo_client.requests.request", return_value=response) as request:
            GalileoClient(api_key="provided by the runtime").patch("/v2/datasets/dataset/content", json=payload)
        self.assertEqual(request.call_args.kwargs["json"], payload)
        self.assertIsInstance(request.call_args.kwargs["json"]["edits"][0]["values"]["user_metadata"]["revision"], int)

    def test_application_input_metadata_is_not_treated_as_trace_metadata(self):
        response = Mock(status_code=200, text="{}", headers={})
        response.json.return_value = {}
        payload = {"traces": [{"input": {"user_metadata": {"attempt": 2}},
            "user_metadata": {"executed": True}}]}
        with patch("galileo_client.requests.request", return_value=response) as request:
            GalileoClient(api_key="provided by the runtime").post("/v2/projects/project/traces", json=payload)
        sent = request.call_args.kwargs["json"]["traces"][0]
        self.assertEqual(sent["input"], payload["traces"][0]["input"])
        self.assertEqual(sent["user_metadata"], {"executed": "true"})

    def test_metadata_uses_api_strings_without_changing_metrics_or_outputs(self):
        response = Mock(status_code=200, text="{}")
        response.headers = {"ETag": "dataset-version"}
        response.json.return_value = {}
        payload = {"traces": [{"user_metadata": {"executed": False, "calls": 0, "model": None},
            "metrics": {"completion": 0.0}, "spans": [{"user_metadata": {"used_fallback": True},
                "output": {"approved": False}}]}]}
        with patch("galileo_client.requests.request", return_value=response) as request:
            GalileoClient(api_key="provided by the runtime").post("/v2/projects/project/traces", json=payload)
        sent = request.call_args.kwargs["json"]["traces"][0]
        self.assertEqual(sent["user_metadata"], {"executed": "false", "calls": "0"})
        self.assertEqual(sent["spans"][0]["user_metadata"], {"used_fallback": "true"})
        self.assertEqual(sent["metrics"], {"completion": 0.0})
        self.assertEqual(sent["spans"][0]["output"], {"approved": False})
        self.assertFalse(payload["traces"][0]["user_metadata"]["executed"])


def session_environment():
    from test_galileo_session import descriptor, environment
    data = descriptor()
    env = environment()
    env.update({'GALILEO_LAB_PROFILE': 'workshop', 'GALILEO_API_BASE_URL': data['base_url'],
        'GALILEO_LAB_SESSION_TOKEN': data['token'], 'GALILEO_LAB_GATEWAY_URL': data['base_url'],
        'GALILEO_LAB_SESSION_EXPIRES_AT': data['expires_at'],
        'GALILEO_LAB_SESSION_PROJECT_ID': data['project_id'],
        'GALILEO_LAB_SESSION_LOG_STREAM_ID': data['log_stream_id'],
        'GALILEO_LAB_SESSION_ENVIRONMENT_ID': data['environment_id']})
    return env


class SessionClientTests(unittest.TestCase):
    def test_session_auth_uses_its_own_token_and_fixed_gateway_prefix(self):
        from test_galileo_session import PROD, response
        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('galileo_client.requests.request', return_value=response({})) as request:
                client = GalileoClient()
                client.get('/v2/current_user', headers={'Galileo-API-Key': 'unrelated native key'})
        self.assertEqual(client.api_key, '')
        self.assertEqual(request.call_args.args[1], PROD + '/proxy/v2/current_user')
        self.assertEqual(request.call_args.kwargs['headers']['Galileo-API-Key'], 'provided by session service')
        self.assertIs(request.call_args.kwargs['allow_redirects'], False)

    def test_native_override_keeps_native_reports_accessible_after_session_init(self):
        from test_galileo_session import PROD, response
        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('galileo_client.requests.request', return_value=response({})) as request:
                client = GalileoClient(api_key='provided by native runtime')
                client.get('/v2/current_user')
                custom = GalileoClient(api_key='provided by native runtime', base_url='https://native.invalid')
            self.assertEqual(custom.base_url, 'https://native.invalid')
            with self.assertRaises(RuntimeError):
                GalileoClient(api_key='provided by native runtime', base_url=PROD + '/proxy')
        self.assertEqual(client.base_url, 'https://api.multitenant.galileocloud.io')
        self.assertEqual(request.call_args.kwargs['headers']['Galileo-API-Key'], 'provided by native runtime')

    def test_native_environment_key_wins_over_stale_session_fields(self):
        env = session_environment()
        env['GALILEO_API_KEY'] = 'provided by native runtime'
        with patch.dict('os.environ', env, clear=True):
            self.assertEqual(GalileoClient().base_url, 'https://api.multitenant.galileocloud.io')

    def test_session_token_never_flows_to_native_or_a_different_profile(self):
        env = session_environment()
        for changes in ({'GALILEO_LAB_PROFILE': 'observability'},
            {'GALILEO_API_BASE_URL': 'https://api.multitenant.galileocloud.io'},
            {'GALILEO_LAB_GATEWAY_URL': 'https://foreign.invalid'}):
            with self.subTest(changes=changes):
                with patch.dict('os.environ', dict(env, **changes), clear=True):
                    with self.assertRaises(RuntimeError):
                        GalileoClient()
        with patch.dict('os.environ', env, clear=True):
            with self.assertRaises(RuntimeError):
                GalileoClient(base_url='https://api.multitenant.galileocloud.io')

    def test_auth_failure_renews_once_and_replays_at_the_same_base(self):
        from galileo_session import WorkshopSession
        from test_galileo_session import descriptor, response
        data = descriptor()
        data['token'] = 'renewed by session service'
        renewal = WorkshopSession(**data)
        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('galileo_client.bootstrap_session', return_value=renewal) as bootstrap:
                with patch('galileo_client.requests.request', side_effect=[response(status=401), response({})]) as request:
                    client = GalileoClient()
                    self.assertEqual(client.post('/v2/projects/project/traces', json={'traces': []}), {})
        self.assertEqual(bootstrap.call_count, 1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[1], request.call_args_list[1].args[1])
        self.assertEqual(request.call_args_list[0].kwargs['headers']['Galileo-API-Key'], 'provided by session service')
        self.assertEqual(request.call_args_list[1].kwargs['headers']['Galileo-API-Key'], 'renewed by session service')

    def test_auth_failure_is_bounded_and_changed_renewal_base_is_rejected(self):
        from galileo_session import WorkshopSession
        from test_galileo_session import STAGE, descriptor, response
        from galileo_client import GalileoApiError
        for base in (None, STAGE):
            data = descriptor() if base is None else descriptor(base)
            with patch.dict('os.environ', session_environment(), clear=True):
                with patch('galileo_client.bootstrap_session', return_value=WorkshopSession(**data)) as bootstrap:
                    with patch('galileo_client.requests.request', return_value=response(status=401)) as request:
                        with self.assertRaises(RuntimeError) as caught:
                            GalileoClient().get('/v2/current_user')
            self.assertNotIn('sensitive data', str(caught.exception))
            self.assertEqual(bootstrap.call_count, 1)
            self.assertEqual(request.call_count, 2 if base is None else 1)

    def test_ambiguous_mutation_failure_is_never_retried_and_transport_error_is_safe(self):
        import requests
        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('galileo_client.bootstrap_session') as bootstrap:
                with patch('galileo_client.requests.request', side_effect=requests.Timeout('token or password in exception')) as request:
                    with self.assertRaisesRegex(RuntimeError, 'Workshop') as caught:
                        GalileoClient().post('/v2/projects/project/traces', json={'traces': []})
        self.assertNotIn('password', str(caught.exception))
        self.assertEqual(request.call_count, 1)
        bootstrap.assert_not_called()

    def test_gateway_path_cannot_escape_the_validated_base(self):
        from test_galileo_session import response
        for path in ('/../v2/current_user', '/v2/../../current_user', '/v2/%2e%2e/../current_user',
            '//foreign.invalid/v2/current_user', 'https://foreign.invalid/v2/current_user', '/v2/current_user#fragment'):
            with self.subTest(path=path):
                with patch.dict('os.environ', session_environment(), clear=True):
                    with patch('galileo_client.requests.request', return_value=response({})) as request:
                        with self.assertRaises(RuntimeError):
                            GalileoClient().get(path)
                        request.assert_not_called()

    def test_renewal_does_not_contact_a_changed_bootstrap_destination(self):
        from test_galileo_session import STAGE, response
        with patch.dict('os.environ', session_environment(), clear=True):
            client = GalileoClient()
            with patch.dict('os.environ', {'GALILEO_LAB_BOOTSTRAP_URL': STAGE + '/sessions'}):
                with patch('galileo_client.bootstrap_session') as bootstrap:
                    with patch('galileo_client.requests.request', return_value=response(status=401)):
                        with self.assertRaises(RuntimeError):
                            client.get('/v2/current_user')
                bootstrap.assert_not_called()

    def test_renewal_rewinds_seekable_multipart_files_to_the_original_offset(self):
        import io
        import requests
        from galileo_session import WorkshopSession
        from test_galileo_session import descriptor
        payload = b'input,ground_truth\nhello,world\n'
        for form in ('bare', 'pair', 'triple', 'quadruple', 'repeated fields'):
            with self.subTest(form=form):
                source = io.BytesIO(b'skip this prefix' + payload)
                source.seek(len(b'skip this prefix'))
                if form == 'bare':
                    files = {'file': source}
                elif form == 'pair':
                    files = {'file': ('fixture.csv', source)}
                elif form == 'triple':
                    files = {'file': ('fixture.csv', source, 'text/csv')}
                elif form == 'quadruple':
                    files = {'file': ('fixture.csv', source, 'text/csv', {'X-Fixture': 'csv'})}
                else:
                    files = [('file', ('fixture.csv', source, 'text/csv')),
                        ('file', ('other.csv', io.BytesIO(b'other fixture row'))),
                        ('inline', ('inline.txt', b'inline fixture bytes'))]
                bodies = []

                def send(adapter, request, **kwargs):
                    bodies.append(request.body)
                    result = requests.Response()
                    result.status_code = 401 if len(bodies) == 1 else 201
                    result._content = b'{"id":"fixture-dataset"}'
                    result.request = request
                    return result

                with patch.dict('os.environ', session_environment(), clear=True):
                    with patch('requests.adapters.HTTPAdapter.send', send):
                        with patch('galileo_client.bootstrap_session', return_value=WorkshopSession(**descriptor())) as bootstrap:
                            result = GalileoClient().post('/v2/datasets', data={'project_id': 'fixture-project'}, files=files)
                self.assertEqual(result, {'id': 'fixture-dataset'})
                self.assertEqual(len(bodies), 2)
                self.assertEqual(bootstrap.call_count, 1)
                for body in bodies:
                    self.assertIn(payload, body)
                    self.assertNotIn(b'skip this prefix', body)
                    self.assertIn(b'fixture-project', body)
                    if form == 'repeated fields':
                        self.assertIn(b'other fixture row', body)
                        self.assertIn(b'inline fixture bytes', body)

    def test_nonseekable_multipart_source_is_not_replayed_after_auth_failure(self):
        import io
        import requests

        class ForwardOnly(io.BytesIO):
            def seekable(self):
                return False

        attempts = []

        def send(adapter, request, **kwargs):
            attempts.append(request.body)
            result = requests.Response()
            result.status_code = 401
            result._content = b'{}'
            result.request = request
            return result

        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('requests.adapters.HTTPAdapter.send', send):
                with patch('galileo_client.bootstrap_session') as bootstrap:
                    with self.assertRaisesRegex(RuntimeError, 'reopen.*source'):
                        GalileoClient().post('/v2/datasets', files={'file': ForwardOnly(b'fixture csv')})
        self.assertEqual(len(attempts), 1)
        bootstrap.assert_not_called()

    def test_failed_rewind_stops_before_a_second_write_without_exposing_file_errors(self):
        import io
        import requests

        class FailedRewind(io.BytesIO):
            def seek(self, offset, whence=0):
                raise OSError('sensitive file error')

        attempts = []

        def send(adapter, request, **kwargs):
            attempts.append(request.body)
            result = requests.Response()
            result.status_code = 401
            result._content = b'{}'
            result.request = request
            return result

        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('requests.adapters.HTTPAdapter.send', send):
                with patch('galileo_client.bootstrap_session') as bootstrap:
                    with self.assertRaisesRegex(RuntimeError, 'reopen.*source') as caught:
                        GalileoClient().post('/v2/datasets', files={'file': FailedRewind(b'fixture csv')})
        self.assertNotIn('sensitive file error', str(caught.exception))
        self.assertEqual(len(attempts), 1)
        bootstrap.assert_not_called()


    def test_forward_only_upload_still_succeeds_without_authentication_failure(self):
        import io
        import requests

        class ForwardOnly(io.BytesIO):
            def tell(self):
                raise OSError('position unavailable')

        bodies = []

        def send(adapter, request, **kwargs):
            bodies.append(request.body)
            result = requests.Response()
            result.status_code = 201
            result._content = b'{"id":"fixture-dataset"}'
            result.request = request
            return result

        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('requests.adapters.HTTPAdapter.send', send):
                result = GalileoClient().post('/v2/datasets', files={'file': ForwardOnly(b'fixture csv')})
        self.assertEqual(result, {'id': 'fixture-dataset'})
        self.assertEqual(len(bodies), 1)
        self.assertIn(b'fixture csv', bodies[0])

    def test_multipart_transport_timeout_never_renews_or_replays_the_write(self):
        import io
        import requests
        bodies = []

        def send(adapter, request, **kwargs):
            bodies.append(request.body)
            raise requests.Timeout('sensitive transport details')

        with patch.dict('os.environ', session_environment(), clear=True):
            with patch('requests.adapters.HTTPAdapter.send', send):
                with patch('galileo_client.bootstrap_session') as bootstrap:
                    with self.assertRaisesRegex(RuntimeError, 'Check findings') as caught:
                        GalileoClient().post('/v2/datasets', files={'file': io.BytesIO(b'fixture csv')})
        self.assertEqual(len(bodies), 1)
        self.assertNotIn('sensitive transport details', str(caught.exception))
        bootstrap.assert_not_called()



if __name__ == "__main__":
    unittest.main()
