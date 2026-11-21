"""Launcher tests replace ROS, /proc, processes and signals; no device access."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock


spec = importlib.util.spec_from_file_location('am3008_start', Path(__file__).parent / 'deploy/start_am3008_control.py')
start = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = start
spec.loader.exec_module(start)


def graph(name='am3008_driver', publishers=()):
    return json.dumps({'subscribers': [{'name': name, 'namespace': '/', 'type': 'geometry_msgs/msg/Twist'}],
                       'publishers': [{'name': p} for p in publishers]})


class LauncherTests(unittest.TestCase):
    def test_driver_domain_localhost_and_rmw_are_preserved(self):
        result = SimpleNamespace(returncode=0, stdout=graph())
        with patch.object(start, 'driver_environments', return_value={7: {'ROS_LOCALHOST_ONLY': '1', 'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp'}}), \
             patch.object(start.subprocess, 'run', return_value=result) as run:
            env, subscribers = start.choose_driver({'OPERATOR_TOKEN': 'existing-not-printed'}, '/cmd_vel')
        self.assertEqual(env['ROS_DOMAIN_ID'], '7')
        self.assertEqual(env['ROS_LOCALHOST_ONLY'], '1')
        self.assertEqual(env['RMW_IMPLEMENTATION'], 'rmw_cyclonedds_cpp')
        self.assertEqual(env['OPERATOR_TOKEN'], 'existing-not-printed')
        self.assertEqual(subscribers[0]['name'], 'am3008_driver')
        self.assertNotIn('create_publisher', run.call_args.args[0][-1])

    def test_echo_is_not_accepted_as_hardware_subscriber(self):
        with patch.object(start, 'driver_environments', return_value={0: {}}), \
             patch.object(start.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=graph('_ros2cli_123'))):
            with self.assertRaisesRegex(ValueError, 'found 0'):
                start.choose_driver({}, '/cmd_vel')

    def test_other_active_publisher_prevents_switch(self):
        with patch.object(start, 'driver_environments', return_value={0: {}}), \
             patch.object(start.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=graph(publishers=['teleop_twist_keyboard']))):
            with self.assertRaisesRegex(ValueError, 'Another cmd_vel publisher'):
                start.choose_driver({}, '/cmd_vel')

    def test_two_domains_are_not_guessed(self):
        with patch.object(start, 'driver_environments', return_value={0: {}, 42: {}}), \
             patch.object(start.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=graph())):
            with self.assertRaisesRegex(ValueError, 'found 2'):
                start.choose_driver({}, '/cmd_vel')

    def test_pid_reuse_never_signals_a_process(self):
        backend = start.Backend(77, 'before', Path('.'), ['python3'], {})
        with patch.object(start, 'birth', return_value='after'), patch.object(start.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, 'owner changed'):
                start.switch(Path('.'), backend, {})
        kill.assert_not_called()

    def test_existing_password_is_reused_without_prompt(self):
        backend = start.Backend(77, '1', Path('/home/nvidia'), [], {'OPERATOR_TOKEN': 'existing-secret', 'OPERATOR_TOKEN_FILE': '/not-present'})
        with patch.object(start.Path, 'is_file', return_value=False):
            self.assertEqual(start.credential_environment(backend)['OPERATOR_TOKEN'], 'existing-secret')
        backend.env = {'OPERATOR_TOKEN_FILE': '/not-present'}
        with patch.object(start.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(ValueError, 'no default password'):
                start.credential_environment(backend)

    def test_other_systemd_service_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'service-managed'):
            start.check_manager('0::/system.slice/custom-control.service')
        start.check_manager('0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-123.scope')

    def test_failed_spawn_restores_original_mode_when_port_is_free(self):
        backend = start.Backend(77, '1', Path('.'), ['python3'], {'OPERATOR_TOKEN': 'old'})
        with patch.object(start, 'birth', return_value='1'), patch.object(start, 'listener', return_value=77), \
             patch.object(start, 'listener_pids', return_value=set()), \
             patch.object(start.Path, 'exists', return_value=False), patch.object(start.os, 'kill') as kill, \
             patch.object(start, 'spawn', side_effect=[OSError('cannot spawn'), (Mock(), Path('recovery.log'))]) as spawn:
            with self.assertRaisesRegex(ValueError, 'original mode restart requested'):
                start.switch(Path('.'), backend, {'MOTION_TARGET': 'am3008'})
        self.assertEqual(spawn.call_args.args[1], backend.env)
        kill.assert_called_once_with(77, start.signal.SIGINT)

    def test_success_checks_the_new_listener_before_reporting_ready(self):
        backend = start.Backend(77, '1', Path('.'), ['python3'], {'OPERATOR_TOKEN': 'old'})
        process = Mock(pid=88)
        process.poll.return_value = None
        with patch.object(start, 'birth', return_value='1'), patch.object(start, 'listener', side_effect=[77, 88]), \
             patch.object(start, 'listener_pids', return_value=set()), \
             patch.object(start.Path, 'exists', return_value=False), patch.object(start.os, 'kill') as kill, \
             patch.object(start, 'status', return_value={'base_mode': 'ros2_cmd_vel', 'base_connected': True}), \
             patch.object(start, 'spawn', return_value=(process, Path('web.log'))) as spawn:
            self.assertEqual(start.switch(Path('.'), backend, {'MOTION_TARGET': 'am3008'}), Path('web.log'))
        kill.assert_called_once_with(77, start.signal.SIGINT)
        self.assertEqual(spawn.call_count, 1)

    def test_recovery_does_not_start_when_another_process_takes_port(self):
        backend = start.Backend(77, '1', Path('.'), ['python3'], {})
        with patch.object(start, 'birth', return_value='1'), patch.object(start, 'listener', return_value=77), \
             patch.object(start, 'listener_pids', side_effect=[set(), {88}]), \
             patch.object(start.Path, 'exists', return_value=False), patch.object(start.os, 'kill'), \
             patch.object(start, 'spawn', side_effect=OSError('cannot spawn')) as spawn:
            with self.assertRaisesRegex(ValueError, 'no duplicate recovery'):
                start.switch(Path('.'), backend, {})
        self.assertEqual(spawn.call_count, 1)

    def test_wrong_machine_with_exhausted_stdin_exits_without_prompt(self):
        if sys.platform == 'linux':
            self.skipTest('Windows guard is checked by this test on the development host.')
        result = subprocess.run([sys.executable, '-B', str(Path(start.__file__))],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'Run this saved script on NX', result.stderr)


if __name__ == '__main__':
    unittest.main()
