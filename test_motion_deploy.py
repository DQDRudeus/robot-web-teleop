"""Lifecycle ownership and configuration checks without ROS or real hardware."""
import importlib.util
import json
import itertools
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runtime = load('motion_runtime_under_test', 'deploy/motion_runtime.py')
stack = load('motion_stack_under_test', 'deploy/stack.py')


class MotionDeploymentTests(unittest.TestCase):
    def test_probe_with_closed_stdin_never_starts_driver(self):
        ros, node = Mock(), Mock()
        with patch.dict(sys.modules, {'rclpy':ros, 'rclpy.node':SimpleNamespace(Node=Mock(return_value=node))}), \
             patch.object(runtime.sys, 'argv', ['motion_runtime.py','am3008','--probe']), \
             patch.object(runtime, 'child_environment', return_value={}), \
             patch.object(runtime, 'stdin_closed', return_value=True), \
             patch.object(runtime, 'endpoints', return_value=False), \
             patch.object(runtime, 'existing_driver_process', return_value=False), \
             patch.object(runtime.time, 'monotonic', side_effect=itertools.count(0,.1)), \
             patch.object(runtime.signal, 'signal'), \
             patch.object(runtime, 'emit') as emit, \
             patch.object(runtime.subprocess, 'Popen') as spawn, \
             patch.object(runtime, 'stop_owned'):
            self.assertEqual(runtime.main(), 0)
        emit.assert_called_once_with('motion_probe_ready', target='am3008')
        spawn.assert_not_called()

    def config(self):
        config = stack.defaults()
        config.update(camera_password='private-camera', operator_token='private-control',
                      display=':actual-session', motion_target='am3008', simulator=False,
                      ros_domain=0, managed_motion=True)
        return config

    def test_backend_carries_local_mode_and_serial_parameters_without_shell_commands(self):
        with patch.object(stack, 'camera_binary', return_value=Path('/daemon')):
            env = stack.backend_environment(self.config(), {})
        self.assertEqual(env['MOTION_LIFECYCLE_MANAGED'], '1')
        self.assertEqual(env['MOTION_SERIAL_PORT'], '/dev/ttyCH341USB0')
        self.assertEqual(env['ROS_DOMAIN_ID'], '0')
        self.assertEqual(env['ROS_LOCALHOST_ONLY'], '1')
        self.assertEqual(env['DISPLAY'], ':actual-session')
        self.assertNotIn('sudo', str(env))

    def test_driver_argv_overrides_wrong_yaml_and_preserves_space_paths(self):
        with patch.dict(os.environ, {'MOTION_SERIAL_PORT': '/dev/ttyCH341USB0'}, clear=True), \
             patch.object(runtime.Path, 'exists', return_value=True), \
             patch.object(runtime.os, 'access', return_value=True), \
             patch.object(runtime.shutil, 'which', return_value='/opt/ros/humble/bin/ros2'):
            argv = runtime.command_for('am3008', {'PATH': '/opt/ros/humble/bin'})
        self.assertEqual(argv[:4], ['/opt/ros/humble/bin/ros2', 'run', 'am3008_driver', 'am3008_node'])
        self.assertIn('serial.port:=/dev/ttyCH341USB0', argv)
        self.assertIn('serial.baudrate:=115200', argv)
        self.assertIn('__node:=am3008_node', argv)

    def test_workspace_source_cannot_change_ros_domain_or_local_only(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps({'PATH': '/path with space',
                                                                  'ROS_DOMAIN_ID': '42'}))
        with patch.dict(os.environ, {'MOTION_AM3008_SETUP': '/workspace with space/install/setup.bash',
                                    'ROS_DOMAIN_ID': '0'}, clear=True), \
             patch.object(runtime.Path, 'is_file', return_value=True), \
             patch.object(runtime.subprocess, 'run', return_value=result) as run:
            env = runtime.child_environment('am3008')
        self.assertEqual(env['ROS_DOMAIN_ID'], '0')
        self.assertEqual(env['ROS_LOCALHOST_ONLY'], '1')
        self.assertEqual(run.call_args.args[0][4], '/workspace with space/install/setup.bash')
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_other_controllers_are_refused_but_web_worker_is_allowed(self):
        node = Mock()
        node.get_subscriptions_info_by_topic.return_value = [
            SimpleNamespace(node_name='am3008_node', topic_type='geometry_msgs/msg/Twist')]
        node.get_publishers_info_by_topic.return_value = [SimpleNamespace(node_name='teleop_twist_keyboard')]
        with self.assertRaisesRegex(ValueError, 'Another controller'):
            runtime.endpoints(node, 'am3008')
        node.get_publishers_info_by_topic.return_value = [SimpleNamespace(node_name='hkcamera_am3008_web_bridge')]
        self.assertTrue(runtime.endpoints(node, 'am3008'))

    def test_stop_only_signals_owned_process_group(self):
        process = Mock(pid=321)
        process.poll.return_value = None
        with patch.object(runtime.os, 'killpg', create=True) as kill:
            runtime.stop_owned(process)
        kill.assert_called_once_with(321, signal.SIGINT)
        with patch.object(runtime.os, 'killpg', create=True) as kill:
            runtime.stop_owned(None)
        kill.assert_not_called()

    def test_initial_discovery_waits_for_name_and_type_but_not_other_controllers(self):
        node, spin = Mock(), Mock()
        with patch.object(runtime, 'endpoints', side_effect=[runtime.GraphNotReady('unknown'), True]), \
             patch.object(runtime.time, 'monotonic', side_effect=itertools.count()):
            self.assertTrue(runtime.initial_endpoints(node, 'am3008', spin, lambda: False))
        spin.assert_called_once()
        with patch.object(runtime, 'endpoints', side_effect=ValueError('Another controller')):
            with self.assertRaisesRegex(ValueError, 'Another controller'):
                runtime.initial_endpoints(node, 'am3008', spin, lambda: False)

    def test_persistent_unknown_and_shutdown_never_become_ready(self):
        with patch.object(runtime, 'endpoints', side_effect=runtime.GraphNotReady('unknown:wrong-type')), \
             patch.object(runtime.time, 'monotonic', side_effect=itertools.count()):
            with self.assertRaisesRegex(runtime.GraphNotReady, 'wrong-type'):
                runtime.initial_endpoints(Mock(), 'am3008', Mock(), lambda: False)
        with patch.object(runtime, 'endpoints') as query:
            self.assertFalse(runtime.initial_endpoints(Mock(), 'am3008', Mock(), lambda: True))
        query.assert_not_called()

    def test_turtlesim_requires_real_desktop_and_backend_can_enable_alone(self):
        with patch.object(runtime.shutil, 'which', return_value='/opt/ros/humble/bin/ros2'):
            with self.assertRaisesRegex(ValueError, 'DISPLAY'):
                runtime.command_for('turtlesim', {'PATH': '/opt/ros/humble/bin'})
        units = stack.unit_files('/home/nvidia/Desktop/hkcamera')
        self.assertIn('[Install]\nWantedBy=multi-user.target', units['hkcamera-stack-backend.service'])
        self.assertNotIn('sudo', units['hkcamera-stack-backend.service'])

    def test_legacy_turtle_service_does_not_spawn_duplicate_for_managed_mode(self):
        config = self.config()
        config['motion_target'] = 'turtlesim'
        config['simulator'] = True
        with patch.object(stack.os, 'chdir'), patch.object(stack.os, 'execvpe') as execute:
            self.assertEqual(stack.run_component('turtle', config), 0)
        execute.assert_not_called()

    def test_competing_controller_after_ready_stops_supervisor_without_killing_adopted_driver(self):
        ros = Mock()
        ros.ok.return_value = True
        node = Mock()
        node_module = SimpleNamespace(Node=Mock(return_value=node))
        with patch.dict(sys.modules, {'rclpy': ros, 'rclpy.node': node_module}), \
             patch.object(runtime.sys, 'argv', ['motion_runtime.py', 'am3008']), \
             patch.object(runtime, 'child_environment', return_value={}), \
             patch.object(runtime, 'stdin_closed', return_value=False), \
             patch.object(runtime, 'endpoints', side_effect=[True, ValueError('Another controller')]), \
             patch.object(runtime.time, 'monotonic', side_effect=itertools.count()), \
             patch.object(runtime.signal, 'signal'), \
             patch.object(runtime, 'emit'), \
             patch.object(runtime, 'stop_owned') as stop:
            with self.assertRaisesRegex(ValueError, 'Another controller'):
                runtime.main()
        stop.assert_called_once_with(None)
        node.destroy_node.assert_called_once()
        ros.shutdown.assert_called_once()

    def test_owned_driver_discovery_can_settle_before_ready(self):
        ros, node, child = Mock(), Mock(), Mock()
        child.poll.return_value = None
        ready = [False]
        def emitted(kind, **values):
            if kind == 'motion_runtime_ready':
                ready[0] = True
        with patch.dict(sys.modules, {'rclpy': ros, 'rclpy.node': SimpleNamespace(Node=Mock(return_value=node))}), \
             patch.object(runtime.sys, 'argv', ['motion_runtime.py', 'am3008']), \
             patch.object(runtime, 'child_environment', return_value={}), \
             patch.object(runtime, 'stdin_closed', side_effect=lambda: ready[0]), \
             patch.object(runtime, 'endpoints', side_effect=[False, runtime.GraphNotReady('name not ready'), True]), \
             patch.object(runtime.time, 'monotonic', side_effect=itertools.count(0, .1)), \
             patch.object(runtime.signal, 'signal'), \
             patch.object(runtime, 'existing_driver_process', return_value=False), \
             patch.object(runtime, 'command_for', return_value=['existing-driver']), \
             patch.object(runtime.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(runtime, 'emit', side_effect=emitted), \
             patch.object(runtime, 'stop_owned') as stop:
            self.assertEqual(runtime.main(), 0)
        self.assertTrue(ready[0])
        spawn.assert_called_once()
        stop.assert_called_once_with(child)


if __name__ == '__main__':
    unittest.main()
