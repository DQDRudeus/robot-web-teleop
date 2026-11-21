import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('desktop_launcher_tested', ROOT / 'deploy/desktop_launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class DesktopLauncherTests(unittest.TestCase):
    def test_desktop_exec_escapes_reserved_characters_and_percent_fields(self):
        argument = launcher.desktop_argument('/home/nvidia/My Project/a"$`\\%F.py')
        self.assertTrue(argument.startswith('"/home/nvidia/My Project/'))
        self.assertIn(r'\\"', argument)
        self.assertIn(r'\\$', argument)
        self.assertIn(r'\\`', argument)
        self.assertIn(r'\\\\', argument)
        self.assertIn('%%F.py', argument)
        with self.assertRaises(launcher.LauncherError):
            launcher.desktop_argument('/tmp/file\nInjected=1')

    def test_install_respects_xdg_desktop_spaces_and_never_starts_services(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            home = Path(directory)
            desktop = home / '我的 NX 桌面'
            def find(binary):
                return '/usr/bin/xdg-user-dir' if binary == 'xdg-user-dir' else None
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(home / '.local/share')}, clear=True), \
                 patch.object(launcher.shutil, 'which', side_effect=find), \
                 patch.object(launcher, 'run', return_value=SimpleNamespace(returncode=0, stdout=str(desktop) + '\n')) as run:
                path = launcher.install(home)
            self.assertEqual(path, desktop / launcher.DESKTOP_NAME)
            entry = path.read_text(encoding='utf-8')
            self.assertIn('Terminal=false', entry)
            self.assertIn('"--open" "--with-media"', entry)
            self.assertIn('"/usr/bin/python3"', entry)
            self.assertTrue((home / '.local/share/applications' / launcher.DESKTOP_NAME).is_file())
            self.assertFalse(any('systemctl' in str(call) for call in run.call_args_list))

    def test_running_services_do_not_prompt_or_restart(self):
        with patch.object(launcher, 'service_state', return_value=True), \
             patch.object(launcher, 'run') as run:
            self.assertEqual(launcher.ensure_services(True), [])
        run.assert_not_called()

    def test_only_inactive_fixed_units_start_in_one_authorization(self):
        states = {launcher.BACKEND: True, launcher.MEDIA[0]: False, launcher.MEDIA[1]: False}
        with patch.object(launcher, 'service_state', side_effect=lambda unit: states[unit]), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/pkexec'), \
             patch.object(launcher, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(launcher.ensure_services(True), list(launcher.MEDIA))
        run.assert_called_once_with(['/usr/bin/pkexec', '/usr/bin/systemctl', 'start', *launcher.MEDIA],
                                    capture_output=True, text=True, timeout=120)
        self.assertNotIn('restart', str(run.call_args))
        self.assertNotIn('8090', str(run.call_args))

    def test_uninstalled_service_and_denied_authorization_do_not_open_browser(self):
        with patch.object(launcher, 'run', return_value=SimpleNamespace(returncode=0, stdout='not-found\n')):
            with self.assertRaisesRegex(launcher.LauncherError, '尚未安装'):
                launcher.service_state(launcher.BACKEND)
        with patch.object(launcher, 'service_state', return_value=False), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/pkexec'), \
             patch.object(launcher, 'run', return_value=SimpleNamespace(returncode=126)), \
             patch.object(launcher, 'wait_for_backend') as ready:
            with self.assertRaisesRegex(launcher.LauncherError, '授权'):
                launcher.open_workbench()
        ready.assert_not_called()

    def test_health_must_be_expected_backend_and_wait_precedes_browser(self):
        response = io.StringIO(json.dumps({'protocol': {'control_path': '/ws/control'}, 'control': {}}))
        with patch.object(launcher.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertTrue(launcher.backend_ready())
        response = io.StringIO(json.dumps({'protocol': [], 'control': {}}))
        with patch.object(launcher.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertFalse(launcher.backend_ready())
        order = []
        with patch.object(launcher, 'ensure_services', side_effect=lambda _: order.append('services')), \
             patch.object(launcher, 'wait_for_backend', side_effect=lambda: order.append('ready')), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/xdg-open'), \
             patch.object(launcher, 'run', side_effect=lambda *a, **k: order.append('browser') or SimpleNamespace(returncode=0)):
            launcher.open_workbench(True)
        self.assertEqual(order, ['services', 'ready', 'browser'])

    def test_saved_launcher_help_works_with_exhausted_stdin(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'deploy/desktop_launcher.py'), '--help'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--install', result.stdout)
        self.assertIn('--open', result.stdout)

    def test_platform_guard_does_not_launch_or_prompt_for_input(self):
        with patch.object(launcher.sys, 'platform', 'win32'), \
             patch.object(launcher, 'show_error') as error, \
             patch.object(launcher, 'open_workbench') as open_page:
            self.assertEqual(launcher.main(['--open']), 1)
        open_page.assert_not_called()
        error.assert_called_once()


if __name__ == '__main__':
    unittest.main()
