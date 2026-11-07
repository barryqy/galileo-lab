import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import galileo_lab


class PlayerTests(unittest.TestCase):
    def test_player_reuses_the_saved_identity_in_color_without_credentials_or_network(self):
        parser = galileo_lab.build_parser()
        self.assertIn('player', parser._subparsers._group_actions[0].choices)
        args = parser.parse_args(['player'])
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            identity = state / 'agent.json'
            saved = '{"agent_name": "AmberWillowBadgerGrove-c1d85c"}\n'
            identity.write_text(saved)
            output = io.StringIO()
            with patch.object(galileo_lab, 'STATE_DIR', state), patch.dict(os.environ, {}, clear=True):
                with patch.object(galileo_lab, 'load_local_env', side_effect=AssertionError('No credential loading')):
                    with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
                        with patch.object(galileo_lab.DevNetLLM, 'from_env', side_effect=AssertionError('No model access')):
                            with contextlib.redirect_stdout(output):
                                args.func(args)
                                args.func(args)
            self.assertEqual(output.getvalue(),
                'Your agent: \033[1;32mAmberWillowBadgerGrove-c1d85c\033[0m\n' * 2)
            self.assertEqual(identity.read_text(), saved)

    def test_player_subprocess_creates_one_identity_shared_with_agent_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('sitecustomize.py').write_text(
                'import socket\n'
                'def no_network(*args, **kwargs):\n'
                '    raise AssertionError("No network for player")\n'
                'socket.socket.connect = no_network\n')
            env = {'PATH': os.environ['PATH'], 'PYTHONPATH': directory,
                'GALILEO_STATE_DIR': str(root / 'state')}
            command = [sys.executable, '-B', str(Path('galileo_lab.py').resolve())]
            first = subprocess.run(command + ['player'], env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            identity = root / 'state' / 'agent.json'
            saved = identity.read_bytes()
            name = json.loads(saved)['agent_name']
            self.assertRegex(name, r'^(?:[A-Z][a-z]+){4}-[a-f0-9]{6}$')
            self.assertEqual(first.stdout, f'Your agent: \033[1;32m{name}\033[0m\n')
            for cmd, expected in (
                ('player', f'Your agent: \033[1;32m{name}\033[0m\n'),
                ('agent-name', f'Your agent: {name}\n'),
            ):
                result = subprocess.run(command + [cmd], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(identity.read_bytes(), saved)

    def test_shell_player_keeps_cwd_and_profile_when_repeated_or_switched(self):
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lab = root / 'lab with spaces'
                lab.mkdir()
                for source in Path('.').glob('*.py'):
                    shutil.copy2(source, lab / source.name)
                shutil.copy2('0-init-lab.sh', lab)
                lab.joinpath('.venv').symlink_to(Path('.venv').resolve(), target_is_directory=True)
                home = root / 'home'
                old_player = home / '.local' / 'bin' / 'player'
                old_player.parent.mkdir(parents=True)
                old_player.write_text('#!/bin/sh\nprintf "existing player\\n"\n')
                old_player.chmod(0o700)
                poison = root / 'poison'
                poison.mkdir()
                poison.joinpath('python3').write_text('#!/bin/sh\necho wrong-python >&2\nexit 90\n')
                poison.joinpath('python3').chmod(0o700)
                lab.joinpath('sitecustomize.py').write_text(
                    'import socket\n'
                    'def no_network(*args, **kwargs):\n'
                    '    raise AssertionError("No network for player")\n'
                    'socket.socket.connect = no_network\n')
                other = root / 'other'
                other.mkdir()
                names = {
                    '.galileo': 'CalmCedarOtterTrail-112233',
                    '.galileo/workshop': 'AmberWillowBadgerGrove-c1d85c',
                    '.galileo/observability': 'SilverMapleLynxRiver-aabbcc',
                }
                saved = {}
                for state, name in names.items():
                    identity = lab / state / 'agent.json'
                    identity.parent.mkdir(parents=True, exist_ok=True)
                    identity.write_text(json.dumps({'agent_name': name}) + '\n')
                    saved[identity] = identity.read_bytes()
                command = ''
                for profile, state in (
                    ('workshop', '.galileo/workshop'),
                    ('workshop', '.galileo/workshop'),
                    ('observability', '.galileo/observability'),
                    ('workshop', '.galileo/workshop'),
                    ('', '.galileo'),
                ):
                    command += (
                        'export GALILEO_API_KEY="fixture initialization"\n'
                        f'source "$LAB/0-init-lab.sh" {profile} >/dev/null || exit 1\n'
                        'unset GALILEO_API_KEY\n'
                        'export PATH="$POISON:$PATH"\n'
                        'cd "$OTHER" || exit 2\n'
                        'player || exit 3\n'
                        'player || exit 4\n'
                        'test "$PWD" = "$OTHER" || exit 5\n'
                        f'test "${{GALILEO_LAB_PROFILE:-}}" = "{profile}" || exit 6\n'
                        f'test "${{GALILEO_STATE_DIR:-.galileo}}" = "{state}" || exit 7\n'
                    )
                env = {'PATH': str(old_player.parent) + ':' + os.environ['PATH'],
                    'HOME': str(home), 'LAB': str(lab), 'OTHER': str(other),
                    'POISON': str(poison), 'PYTHONPATH': str(lab), 'PYTHONDONTWRITEBYTECODE': '1'}
                result = subprocess.run([shell, '-c', command], cwd=other, env=env,
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                expected = ''
                for state in ('.galileo/workshop', '.galileo/workshop', '.galileo/observability',
                        '.galileo/workshop', '.galileo'):
                    expected += f'Your agent: \033[1;32m{names[state]}\033[0m\n' * 2
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, '')
                for identity, contents in saved.items():
                    self.assertEqual(identity.read_bytes(), contents)
                self.assertFalse(other.joinpath('.galileo').exists())
                self.assertEqual(old_player.read_text(), '#!/bin/sh\nprintf "existing player\\n"\n')


if __name__ == '__main__':
    unittest.main()
