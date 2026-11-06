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
            if fixture_session:
                root.joinpath('sitecustomize.py').write_text(
                    'import requests\nfrom datetime import datetime, timedelta, timezone\n'
                    'class FixtureResponse:\n'
                    '    status_code = 200\n'
                    '    def json(self):\n'
                    '        return {"token": "provided by fixture service", '
                    '"base_url": "https://devnet.cisco.com/v1/devenv-community/galileo/proxy", '
                    '"expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), '
                    '"project_id": "2e652072-412e-4bef-97a5-1a8f4dc5b68e", '
                    '"log_stream_id": "9ac1ae85-1c47-4447-8050-df5fbb5b2ae6", '
                    '"environment_id": "fixture-environment"}\n'
                    'requests.post = lambda *args, **kwargs: FixtureResponse()\n')
            if env_text is not None:
                root.joinpath('.env').write_text(env_text)
            root.joinpath('curl').write_text(
                '#!/bin/sh\n'
                'if [ "$2" = "https://workshop-seed.invalid/fixture" ]; then\n'
                '  echo workshop-fixture-key\n'
                'else\n'
                '  echo unexpected-key-fetch >&2\n'
                '  exit 99\n'
                'fi\n')
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

    def test_workshop_seed_takes_precedence_over_other_profiles(self):
        result = self.run_init('workshop', credential=False, extra_env={
            'GALILEO_WORKSHOP_KEY_SEED_URL': 'https://workshop-seed.invalid/fixture',
            'GALILEO_KEY_SEED_URL': 'https://legacy-seed.invalid/fixture',
            'GALILEO_OBSERVABILITY_KEY_SEED_URL': 'https://splunkse-seed.invalid/fixture',
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('|.galileo/workshop|', result.stdout)
        self.assertNotIn('unexpected-key-fetch', result.stderr)
        self.assertNotIn('workshop-fixture-key', result.stdout)

    def test_clean_workshop_automatically_bootstraps_without_a_native_key(self):
        env = {'HOSTNAME': 'devpod-fixture-environment', 'DEVENV_USER': 'fixture user',
            'DEVENV_PASSWORD': 'provided by fixture environment',
            'LLM_BASE_URL': 'https://devnet.cisco.com/v1/llm'}
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
            'LLM_BASE_URL': 'https://devnet.cisco.com/v1/llm'}
        for shell in ('bash', 'zsh'):
            for ending in ('observability', ''):
                with self.subTest(shell=shell, ending=ending):
                    result = self.run_init('workshop && export GALILEO_API_KEY="provided by native runtime" '
                        '&& source ./0-init-lab.sh ' + ending + ' && test -z "$GALILEO_LAB_SESSION_TOKEN" '
                        '&& test -z "$GALILEO_LAB_GATEWAY_URL"', credential=False,
                        extra_env=env, shell=shell, fixture_session=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('/galileo/proxy', result.stdout)

    def test_native_key_and_workshop_seed_precede_automatic_session_bootstrap(self):
        env = {'GALILEO_LAB_BOOTSTRAP_URL': 'https://foreign.invalid/sessions'}
        for shell in ('bash', 'zsh'):
            for credential in (True, False):
                settings = dict(env)
                if not credential:
                    settings['GALILEO_WORKSHOP_KEY_SEED_URL'] = 'https://workshop-seed.invalid/fixture'
                with self.subTest(shell=shell, credential=credential):
                    result = self.run_init('workshop && test -z "$GALILEO_LAB_SESSION_TOKEN"',
                        credential=credential, extra_env=settings, shell=shell)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('https://api.multitenant.galileocloud.io|', result.stdout)



if __name__ == '__main__':
    unittest.main()
