import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InitTests(unittest.TestCase):
    def run_init(self, profile, credential=True, extra_env=None, *, shell='bash', env_text=None, fixture_session=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('0-init-lab.sh').write_text(Path('0-init-lab.sh').read_text())
            root.joinpath('galileo_session.py').write_text(Path('galileo_session.py').read_text())
            root.joinpath('.venv').symlink_to(Path('.venv').resolve(), target_is_directory=True)
            root.joinpath('sitecustomize.py').write_text(
                'import requests\nimport sys\n'
                'def unexpected_request(*args, **kwargs):\n'
                '    print("unexpected-session-request", file=sys.stderr)\n'
                '    raise requests.RequestException("session request disabled in this fixture")\n'
                'requests.post = unexpected_request\n')
            if fixture_session:
                gateway = ('https://o11y.barrysecure.com/v1/galileo' if str(fixture_session).startswith('barrysecure')
                    else 'https://devnet.cisco.com/v1/devenv-community/galileo')
                root.joinpath('sitecustomize.py').write_text(
                    'import requests\nfrom datetime import datetime, timedelta, timezone\n'
                    'from pathlib import Path\n'
                    'class FixtureResponse:\n'
                    '    def __init__(self, url, headers=None):\n'
                    '        self.renewal = (headers or {}).get("Authorization", "Bearer provided by fixture renewal").removeprefix("Bearer ")\n'
                    '        self.url = url\n'
                    '        self.status_code = 201 if url.endswith("/connections") else 200\n'
                    + ('        if url.endswith("/renew") and Path("session-requests.txt").read_text().count("/renew\\n") == 1: self.status_code = 401\n' if fixture_session == 'barrysecure rejected' else '') +
                    '    def json(self):\n'
                    '        if self.url.endswith("/connections"):\n'
                    '            return {"connection_id": "r"*43, '
                    '"verification_url": "https://o11y.barrysecure.com/connect?request=" + "r"*43, '
                    '"expires_at": (datetime.now(timezone.utc)+timedelta(minutes=9)).isoformat(), "poll_interval": 2}\n'
                    '        return {"token": "provided by fixture service", '
                    '"renewal_token": self.renewal, '
                    '"base_url": ' + repr(gateway + '/proxy') + ', '
                    '"expires_at": (datetime.now(timezone.utc)+timedelta(minutes=59)).isoformat(), '
                    '"project_id": "2e652072-412e-4bef-97a5-1a8f4dc5b68e", '
                    '"log_stream_id": "9ac1ae85-1c47-4447-8050-df5fbb5b2ae6", '
                    '"environment_id": "fixture-environment"}\n'
                    'def post(url, *args, **kwargs):\n'
                    '    with Path("session-requests.txt").open("a") as log: log.write(url + "\\n")\n'
                    '    return FixtureResponse(url, kwargs.get("headers"))\n'
                    'requests.post = post\n')
            if env_text is not None:
                root.joinpath('.env').write_text(env_text)
            root.joinpath('curl').write_text(
                '#!/bin/sh\n'
                'echo unexpected-key-fetch >&2\n'
                'exit 99\n')
            root.joinpath('curl').chmod(0o700)
            env = {'PATH': directory + ':' + os.environ['PATH'], 'PYTHONPATH': directory}
            if credential:
                env['GALILEO_API_KEY'] = 'provided by runtime'
            if extra_env:
                env.update(extra_env)
            command = 'source ./0-init-lab.sh ' + profile
            command += (
                ' && printf "%s|%s|%s|%s|%s|%s|%s|%s"'
                ' "$GALILEO_API_BASE_URL" "$GALILEO_PROJECT" "$GALILEO_STATE_DIR"'
                ' "$GALILEO_CONSOLE_URL" "$GALILEO_PROJECT_ID"'
                ' "$GALILEO_LOG_STREAM" "$GALILEO_LOG_STREAM_ID" "$GALILEO_KEY_SEED_URL"')
            return subprocess.run([shell, '-c', command], cwd=root, env=env,
                text=True, capture_output=True, check=False)

    def test_named_profiles_return_to_the_original_default_destination(self):
        for shell in ('bash', 'zsh'):
            for profile in ('workshop', 'observability'):
                with self.subTest(shell=shell, profile=profile):
                    result = self.run_init(profile + ' && source ./0-init-lab.sh', shell=shell)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(
                        'https://api.galileo.ai|DevNet Galileo Lab|.galileo|'
                        'https://app.galileo.ai/barry-2||devnet-runtime||', result.stdout)

    def test_custom_legacy_settings_survive_a_named_profile_roundtrip(self):
        custom = {
            'GALILEO_API_BASE_URL': 'https://custom-api.invalid',
            'GALILEO_CONSOLE_URL': 'https://custom-console.invalid/tenant',
            'GALILEO_PROJECT': 'Custom project $(touch should-not-exist)',
            'GALILEO_LOG_STREAM': 'custom-stream',
            'GALILEO_PROJECT_ID': 'custom-project-id',
            'GALILEO_LOG_STREAM_ID': 'custom-stream-id',
            'GALILEO_STATE_DIR': '.galileo/custom',
            'GALILEO_KEY_SEED_URL': 'https://legacy-seed.invalid/fixture',
            'GALILEO_WORKSHOP_KEY_SEED_URL': 'https://workshop-seed.invalid/fixture',
        }
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                result = self.run_init('workshop && source ./0-init-lab.sh && test ! -e should-not-exist',
                    extra_env=custom, shell=shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    'https://custom-api.invalid|Custom project $(touch should-not-exist)|.galileo/custom|'
                    'https://custom-console.invalid/tenant|custom-project-id|custom-stream|custom-stream-id|'
                    'https://legacy-seed.invalid/fixture', result.stdout)

    def test_first_legacy_call_keeps_custom_environment_settings(self):
        result = self.run_init('', extra_env={
            'GALILEO_API_BASE_URL': 'https://custom-api.invalid',
            'GALILEO_PROJECT': 'Custom project',
            'GALILEO_STATE_DIR': '.galileo/custom',
            'GALILEO_CONSOLE_URL': 'https://custom-console.invalid/legacy',
            'GALILEO_PROJECT_ID': 'custom-project',
            'GALILEO_LOG_STREAM': 'custom-stream',
            'GALILEO_LOG_STREAM_ID': 'custom-stream-id',
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('https://custom-api.invalid|Custom project|.galileo/custom|'
            'https://custom-console.invalid/legacy|custom-project|custom-stream|custom-stream-id|', result.stdout)

    def test_named_profile_changes_and_repeats_keep_the_requested_destination(self):
        for sequence, tenant, project_id, stream_id in (
            ('workshop && source ./0-init-lab.sh observability', 'splunkse',
                '59f3d09d-c104-4f40-a3b8-77af51ea55d1', '3d95e2c0-38cd-4c1a-bdd8-7a259de8a33b'),
            ('workshop && source ./0-init-lab.sh workshop', 'workshop',
                '2e652072-412e-4bef-97a5-1a8f4dc5b68e', '9ac1ae85-1c47-4447-8050-df5fbb5b2ae6'),
            ('observability && source ./0-init-lab.sh observability', 'splunkse',
                '59f3d09d-c104-4f40-a3b8-77af51ea55d1', '3d95e2c0-38cd-4c1a-bdd8-7a259de8a33b')):
            with self.subTest(sequence=sequence):
                result = self.run_init(sequence)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'https://console.multitenant.galileocloud.io/{tenant}|'
                    f'{project_id}|ai-observability-lab|{stream_id}', result.stdout)

    def test_named_seed_urls_do_not_leak_into_other_profiles_or_the_default(self):
        env = {'GALILEO_KEY_SEED_URL': 'https://legacy-seed.invalid/fixture',
            'GALILEO_WORKSHOP_KEY_SEED_URL': 'https://workshop-seed.invalid/fixture'}
        for ending in ('observability', ''):
            with self.subTest(ending=ending):
                result = self.run_init('workshop && source ./0-init-lab.sh ' + ending, extra_env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.endswith('|https://legacy-seed.invalid/fixture'), result.stdout)
                self.assertNotIn('unexpected-key-fetch', result.stderr)

    def test_preloaded_api_key_survives_profile_changes_and_roundtrip(self):
        result = self.run_init('workshop && source ./0-init-lab.sh observability '
            '&& source ./0-init-lab.sh && test "$GALILEO_API_KEY" = "provided by runtime"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('unexpected-key-fetch', result.stderr)

    def test_preloaded_api_key_ignores_obsolete_seed_settings_in_every_profile(self):
        env = {
            'GALILEO_KEY_SEED_URL': 'https://legacy-seed.invalid/fixture',
            'GALILEO_OBSERVABILITY_KEY_SEED_URL': 'https://splunkse-seed.invalid/fixture',
            'GALILEO_WORKSHOP_KEY_SEED_URL': 'https://workshop-seed.invalid/fixture',
            'GALILEO_LAB_BOOTSTRAP_URL': 'https://foreign.invalid/sessions',
        }
        for shell in ('bash', 'zsh'):
            for profile in ('', 'observability', 'workshop'):
                with self.subTest(shell=shell, profile=profile):
                    result = self.run_init(profile + ' && test "$GALILEO_API_KEY" = "provided by runtime" '
                        '&& test -z "$GALILEO_LAB_SESSION_TOKEN"', extra_env=env, shell=shell)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('provided by runtime', result.stdout + result.stderr)
                    self.assertNotIn('unexpected-key-fetch', result.stderr)
                    self.assertNotIn('unexpected-session-request', result.stderr)

    def test_env_file_native_key_keeps_its_existing_precedence_with_obsolete_seeds(self):
        for shell in ('bash', 'zsh'):
            for profile in ('', 'observability', 'workshop'):
                with self.subTest(shell=shell, profile=profile):
                    result = self.run_init(profile + ' && test "$GALILEO_API_KEY" = "provided by env file"',
                        shell=shell, env_text='GALILEO_API_KEY=provided by env file\n'
                        'GALILEO_WORKSHOP_KEY_SEED_URL=https://workshop-seed.invalid/fixture\n')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('provided by env file', result.stdout + result.stderr)
                    self.assertNotIn('unexpected-key-fetch', result.stderr)
                    self.assertNotIn('unexpected-session-request', result.stderr)

    def test_legacy_and_observability_without_a_key_fail_without_network_access(self):
        for shell in ('bash', 'zsh'):
            for profile in ('', 'observability'):
                with self.subTest(shell=shell, profile=profile):
                    result = self.run_init(profile, credential=False, shell=shell)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('GALILEO_API_KEY', result.stdout)
                    self.assertIn('instructor', result.stdout.lower())
                    self.assertNotIn('session is ready', result.stdout)
                    self.assertNotIn('unexpected-key-fetch', result.stderr)
                    self.assertNotIn('unexpected-session-request', result.stderr)

    def test_seed_only_credentials_fail_without_fetching_or_session_fallback(self):
        cases = (
            ('', {'GALILEO_KEY_SEED_URL': 'https://legacy-seed.invalid/fixture'}),
            ('observability', {'GALILEO_OBSERVABILITY_KEY_SEED_URL': 'https://splunkse-seed.invalid/fixture'}),
            ('workshop', {'GALILEO_WORKSHOP_KEY_SEED_URL': 'https://workshop-seed.invalid/fixture'}),
        )
        for shell in ('bash', 'zsh'):
            for profile, seed_env in cases:
                with self.subTest(shell=shell, profile=profile):
                    env = dict(seed_env, HOSTNAME='devpod-fixture-environment')
                    result = self.run_init(profile, credential=False, extra_env=env, shell=shell,
                        fixture_session='barrysecure')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('GALILEO_API_KEY', result.stdout)
                    self.assertIn('removed', result.stdout.lower())
                    self.assertIn('instructor', result.stdout.lower())
                    self.assertNotIn('session is ready', result.stdout)
                    self.assertNotIn('unexpected-key-fetch', result.stderr)
                    self.assertNotIn('Connect', result.stderr)
                    for url in seed_env.values():
                        self.assertNotIn(url, result.stdout + result.stderr)

    def test_env_file_can_override_the_restored_legacy_destination(self):
        result = self.run_init('workshop && printf "GALILEO_PROJECT=Updated env project\\n" > .env '
            '&& source ./0-init-lab.sh', env_text=
            'GALILEO_API_BASE_URL=https://env-api.invalid\n'
            'GALILEO_CONSOLE_URL=https://env-console.invalid/legacy\n'
            'GALILEO_PROJECT=Env project\n'
            'GALILEO_LOG_STREAM=env-stream\n'
            'GALILEO_STATE_DIR=.galileo/env\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('https://env-api.invalid|Updated env project|.galileo/env|'
            'https://env-console.invalid/legacy||env-stream||', result.stdout)

    def test_new_profile_never_fetches_the_old_tenant_seed(self):
        result = self.run_init('observability', credential=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('unexpected-key-fetch', result.stderr)
        self.assertIn('instructor', result.stdout.lower())

    def test_new_profile_uses_splunkse_and_separate_state(self):
        result = self.run_init('observability')
        self.assertEqual(result.returncode, 0)
        self.assertIn('https://api.multitenant.galileocloud.io|AI Observability Lab|.galileo/observability', result.stdout)
        self.assertNotIn('provided by runtime', result.stdout)

    def test_legacy_profile_keeps_its_destination(self):
        result = self.run_init('')
        self.assertEqual(result.returncode, 0)
        self.assertIn('https://api.galileo.ai|DevNet Galileo Lab|', result.stdout)

    def test_workshop_profile_selects_workshop_resources(self):
        result = self.run_init('workshop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'https://api.multitenant.galileocloud.io|AI Observability Lab|.galileo/workshop|'
            'https://console.multitenant.galileocloud.io/workshop|'
            '2e652072-412e-4bef-97a5-1a8f4dc5b68e|ai-observability-lab|'
            '9ac1ae85-1c47-4447-8050-df5fbb5b2ae6', result.stdout)
        self.assertNotIn('provided by runtime', result.stdout)

    def test_workshop_switch_uses_separate_state(self):
        result = self.run_init('observability && source ./0-init-lab.sh workshop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('|.galileo/workshop|https://console.multitenant.galileocloud.io/workshop|', result.stdout)
        self.assertNotIn('.galileo/observability', result.stdout)

    def test_workshop_requires_its_own_credential_source(self):
        result = self.run_init('workshop', credential=False, extra_env={
            'GALILEO_KEY_SEED_URL': 'https://legacy-seed.invalid/fixture',
            'GALILEO_OBSERVABILITY_KEY_SEED_URL': 'https://splunkse-seed.invalid/fixture',
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('instructor', result.stdout.lower())
        self.assertNotIn('unexpected-key-fetch', result.stderr)

    def test_workshop_seed_only_configuration_fails_even_with_other_profile_seeds(self):
        result = self.run_init('workshop', credential=False, extra_env={
            'GALILEO_WORKSHOP_KEY_SEED_URL': 'https://workshop-seed.invalid/fixture',
            'GALILEO_KEY_SEED_URL': 'https://legacy-seed.invalid/fixture',
            'GALILEO_OBSERVABILITY_KEY_SEED_URL': 'https://splunkse-seed.invalid/fixture',
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('removed', result.stdout.lower())
        self.assertNotIn('unexpected-key-fetch', result.stderr)
        self.assertNotIn('unexpected-session-request', result.stderr)
        self.assertNotIn('https://', result.stdout + result.stderr)

    def test_clean_workshop_automatically_bootstraps_without_a_native_key(self):
        env = {'HOSTNAME': 'devpod-fixture-environment', 'DEVENV_USER': 'fixture user',
            'DEVENV_PASSWORD': 'provided by fixture environment',
            'LLM_BASE_URL': 'https://devnet.cisco.com/v1/llm',
            'GALILEO_LAB_BOOTSTRAP_URL': 'https://devnet.cisco.com/v1/devenv-community/galileo/sessions'}
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                result = self.run_init('workshop && test -z "$GALILEO_API_KEY" '
                    '&& test -n "$GALILEO_LAB_SESSION_TOKEN" '
                    '&& test "$GALILEO_LAB_PROFILE" = workshop', credential=False,
                    extra_env=env, shell=shell, fixture_session=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('https://devnet.cisco.com/v1/devenv-community/galileo/proxy|', result.stdout)
                self.assertNotIn('provided by fixture', result.stdout + result.stderr)
                self.assertNotIn('unexpected-key-fetch', result.stderr)

    def test_session_fields_are_cleared_when_switching_to_a_native_profile(self):
        env = {'HOSTNAME': 'devpod-fixture-environment', 'DEVENV_USER': 'fixture user',
            'DEVENV_PASSWORD': 'provided by fixture environment',
            'LLM_BASE_URL': 'https://devnet.cisco.com/v1/llm',
            'GALILEO_LAB_BOOTSTRAP_URL': 'https://devnet.cisco.com/v1/devenv-community/galileo/sessions'}
        for shell in ('bash', 'zsh'):
            for ending in ('observability', ''):
                with self.subTest(shell=shell, ending=ending):
                    result = self.run_init('workshop && export GALILEO_API_KEY="provided by native runtime" '
                        '&& source ./0-init-lab.sh ' + ending + ' && test -z "$GALILEO_LAB_SESSION_TOKEN" '
                        '&& test -z "$GALILEO_LAB_GATEWAY_URL"', credential=False,
                        extra_env=env, shell=shell, fixture_session=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('/galileo/proxy', result.stdout)

    def test_native_key_precedes_automatic_session_bootstrap(self):
        env = {'GALILEO_LAB_BOOTSTRAP_URL': 'https://foreign.invalid/sessions'}
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                result = self.run_init('workshop && test -z "$GALILEO_LAB_SESSION_TOKEN"',
                    extra_env=env, shell=shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('https://api.multitenant.galileocloud.io|', result.stdout)


    def test_barrysecure_connects_once_and_reuses_session_when_sourced_again(self):
        from test_barrysecure_session import environment
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                result = self.run_init('workshop && source ./0-init-lab.sh workshop '
                    '&& test -n "$GALILEO_LAB_SESSION_RENEWAL_TOKEN" '
                    '&& test -z "$GALILEO_API_KEY" && test "$(wc -l < session-requests.txt)" -eq 3',
                    credential=False, extra_env=environment(), shell=shell, fixture_session='barrysecure')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('https://o11y.barrysecure.com/v1/galileo/proxy|', result.stdout)
                self.assertEqual(result.stderr.count('Connect this lab once'), 0)
                self.assertNotIn('provided by', result.stdout + result.stderr)

    def test_preloaded_barrysecure_session_renews_without_connect_on_setup(self):
        from test_barrysecure_session import session_environment
        for shell in ('bash', 'zsh'):
            result = self.run_init('workshop && test -n "$GALILEO_LAB_SESSION_RENEWAL_TOKEN"',
                credential=False, extra_env=session_environment(), shell=shell, fixture_session='barrysecure')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Connect', result.stderr)
            self.assertIn('https://o11y.barrysecure.com/v1/galileo/proxy|', result.stdout)

    def test_expired_barrysecure_session_renews_during_setup_without_connect(self):
        from datetime import datetime, timedelta, timezone
        from test_barrysecure_session import session_environment
        env = session_environment()
        env['GALILEO_LAB_SESSION_EXPIRES_AT'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        env['GALILEO_LAB_SESSION_RENEWAL_TOKEN'] = 'provided by fixture renewal'
        for shell in ('bash', 'zsh'):
            result = self.run_init('workshop && test "$(wc -l < session-requests.txt)" -eq 1',
                credential=False, extra_env=env, shell=shell, fixture_session='barrysecure')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Connect', result.stderr)

    def test_barrysecure_session_does_not_carry_renewal_into_native_profiles(self):
        from test_barrysecure_session import session_environment
        for shell in ('bash', 'zsh'):
            for ending in ('observability', ''):
                result = self.run_init('workshop && export GALILEO_API_KEY="provided by native runtime" '
                    '&& source ./0-init-lab.sh ' + ending + ' && test -z "$GALILEO_LAB_SESSION_RENEWAL_TOKEN" '
                    '&& test -z "$GALILEO_LAB_GATEWAY_URL"', credential=False,
                    extra_env=session_environment(), shell=shell, fixture_session='barrysecure')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('barrysecure.com', result.stdout)


    def test_fresh_shell_restores_custom_legacy_settings_after_connect(self):
        from test_barrysecure_session import environment
        env = environment()
        env.update({'GALILEO_API_BASE_URL': 'https://custom-native.invalid', 'GALILEO_PROJECT': 'custom legacy project',
            'GALILEO_LOG_STREAM': 'legacy-stream', 'GALILEO_STATE_DIR': '.galileo/custom',
            'GALILEO_PROJECT_ID': 'legacy-project', 'GALILEO_LOG_STREAM_ID': 'legacy-stream-id'})
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                child = ('source ./0-init-lab.sh && test "$GALILEO_API_BASE_URL" = https://custom-native.invalid '
                    '&& test "$GALILEO_PROJECT" = "custom legacy project" '
                    '&& test "$GALILEO_STATE_DIR" = .galileo/custom '
                    '&& test "$GALILEO_PROJECT_ID" = legacy-project && test -z "$GALILEO_LAB_SESSION_RENEWAL_TOKEN"')
                import shlex
                result = self.run_init('workshop && export GALILEO_API_KEY="provided by native runtime" '
                    '&& ' + shell + ' -c ' + shlex.quote(child), credential=False,
                    extra_env=env, shell=shell, fixture_session='barrysecure')
                self.assertEqual(result.returncode, 0, result.stderr)


    def test_expired_rejected_session_can_connect_again_during_explicit_setup(self):
        from datetime import datetime, timedelta, timezone
        from test_barrysecure_session import session_environment
        env = session_environment()
        env['GALILEO_LAB_SESSION_EXPIRES_AT'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                result = self.run_init('workshop && source ./0-init-lab.sh workshop '
                    '&& test "$GALILEO_LAB_SESSION_TOKEN" = "provided by fixture service" '
                    '&& test "$(wc -l < session-requests.txt)" -eq 4', credential=False,
                    extra_env=env, shell=shell, fixture_session='barrysecure rejected')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.count('Connect this lab once'), 0)
                self.assertNotIn('provided by', result.stdout + result.stderr)


    def test_unexpired_revoked_session_can_reconnect_in_bash_and_zsh(self):
        from test_barrysecure_session import session_environment
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                result = self.run_init('workshop && test "$GALILEO_LAB_SESSION_TOKEN" = "provided by fixture service" '
                    '&& test "$(wc -l < session-requests.txt)" -eq 3', credential=False,
                    extra_env=session_environment(), shell=shell, fixture_session='barrysecure rejected')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.count('Connect this lab once'), 0)
                self.assertNotIn('provided by', result.stdout + result.stderr)



if __name__ == '__main__':
    unittest.main()
