"""Own one explicitly selected ROS driver; stdin EOF releases only our child.

This helper is launched by the backend as nvidia. No sudo, shell command from
the browser, or global process shutdown is accepted here.
"""
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time


TARGETS = {
    'turtlesim': ('turtlesim', 'turtlesim_node', '/turtle1/cmd_vel'),
    'am3008': ('am3008_driver', 'am3008_node', '/cmd_vel'),
}


def emit(kind, **values):
    print(json.dumps(dict(type=kind, **values)), flush=True)


def child_environment(target):
    setup = os.environ.get('MOTION_AM3008_SETUP' if target == 'am3008' else
                           'MOTION_ROS_SETUP', '/opt/ros/humble/setup.bash')
    if not Path(setup).is_file():
        raise ValueError('ROS workspace setup file is missing: ' + setup)
    result = subprocess.run(
        ['bash', '-c', 'source "$1" >&2 && exec /usr/bin/python3 -c "$2"',
         'hkcamera-motion', setup,
         'import os,json; print(json.dumps(dict(os.environ)))'],
        capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError('ROS workspace could not be loaded; rebuild/fix its install environment.')
    env = json.loads(result.stdout)
    env.update(ROS_DOMAIN_ID=os.environ.get('ROS_DOMAIN_ID', '0'),
               ROS_LOCALHOST_ONLY='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def command_for(target, env):
    package, executable, _ = TARGETS[target]
    ros2 = shutil.which('ros2', path=env.get('PATH'))
    if not ros2:
        raise ValueError('ROS2 executable is missing from the configured workspace.')
    command = [ros2, 'run', package, executable]
    if target == 'am3008':
        port = os.environ.get('MOTION_SERIAL_PORT', '/dev/ttyCH341USB0')
        if not port.startswith('/dev/') or not Path(port).exists():
            raise ValueError('Configured chassis serial device is missing: ' + port)
        if not os.access(port, os.R_OK | os.W_OK):
            raise ValueError('NX user cannot read/write the chassis serial device; check dialout access.')
        command += ['--ros-args', '-r', '__node:=am3008_node',
                    '-p', 'serial.port:=' + port,
                    '-p', 'serial.baudrate:=' + os.environ.get('MOTION_SERIAL_BAUD', '115200'),
                    '-p', 'serial.timeout_ms:=' + os.environ.get('MOTION_SERIAL_TIMEOUT_MS', '100')]
        topic = os.environ.get('AM3008_CMD_TOPIC', '/cmd_vel')
        if topic != '/cmd_vel':
            command += ['-r', '/cmd_vel:=' + topic]
    else:
        if not env.get('DISPLAY'):
            raise ValueError('NX desktop DISPLAY is not configured. Run stack.py integrate in the NX desktop terminal.')
        authority = env.get('XAUTHORITY')
        if authority and not Path(authority).is_file():
            raise ValueError('NX desktop XAUTHORITY is not ready; sign in to the NX desktop first.')
    return command


def existing_driver_process(executable):
    """Detect a driver outside our graph without assuming it may be killed."""
    for entry in Path('/proc').glob('[0-9]*'):
        if entry.name == str(os.getpid()):
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
            if any(Path(item.decode(errors='replace')).name == executable for item in argv if item):
                return True
        except OSError:
            pass
    return False


class GraphNotReady(ValueError):
    pass


def endpoints(node, target):
    topic = os.environ.get('AM3008_CMD_TOPIC', '/cmd_vel') if target == 'am3008' else '/turtle1/cmd_vel'
    subscribers = node.get_subscriptions_info_by_topic(topic)
    matching = [entry for entry in subscribers
                if entry.topic_type == 'geometry_msgs/msg/Twist' and
                (('am3008' in entry.node_name.lower()) if target == 'am3008'
                 else ('turtlesim' in entry.node_name.lower()))]
    publishers = node.get_publishers_info_by_topic(topic)
    others = [entry.node_name for entry in publishers
              if not entry.node_name.startswith('hkcamera_')]
    if others:
        raise ValueError('Another controller publishes to ' + topic + ': ' + ', '.join(others) + '. Exit that controller first.')
    if subscribers and not matching:
        details = ', '.join(f'{entry.node_name}:{entry.topic_type}' for entry in subscribers)
        raise GraphNotReady('The control topic has an unrecognized subscriber: ' + details +
                            '; no second driver was started.')
    if len(matching) > 1:
        raise ValueError('Multiple matching chassis/simulator subscribers exist; resolve duplicate drivers first.')
    return bool(matching)


def initial_endpoints(node, target, spin, stopping):
    # Discovery can expose an endpoint before its name/type settles. Never
    # spawn a driver while that endpoint remains unidentified.
    until = time.monotonic() + 8
    while True:
        if stopping():
            return False
        try:
            return endpoints(node, target)
        except GraphNotReady:
            if time.monotonic() >= until:
                raise
            spin(node, timeout_sec=0.1)


def stdin_closed():
    readable, _, _ = select.select([sys.stdin], [], [], 0)
    return bool(readable) and not sys.stdin.buffer.read1(4096)


def stop_owned(process):
    if process is None or process.poll() is not None:
        return
    # start_new_session gives this child its own group. Never target any other PID.
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        process.wait(timeout=2)
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def main():
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in TARGETS or (len(sys.argv) == 3 and sys.argv[2] != '--probe'):
        raise ValueError('Expected a fixed target: turtlesim or am3008.')
    target = sys.argv[1]
    probe = len(sys.argv) == 3
    env = child_environment(target)
    import rclpy
    from rclpy.node import Node
    rclpy.init()
    node = Node('hkcamera_motion_supervisor')
    process = None
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        until = time.monotonic() + 2
        while time.monotonic() < until and not stopping:
            rclpy.spin_once(node, timeout_sec=0.1)
        if stopping or (not probe and stdin_closed()):
            return 0
        visible = initial_endpoints(node, target, rclpy.spin_once, lambda: stopping or (not probe and stdin_closed()))
        if stopping or (not probe and stdin_closed()):
            return 0
        if probe:
            if not visible and existing_driver_process(TARGETS[target][1]):
                raise ValueError('A driver process already exists outside this ROS graph; no recovery attempted.')
            emit('motion_probe_ready', target=target)
            return 0
        if not visible:
            if existing_driver_process(TARGETS[target][1]):
                raise ValueError('A driver process already exists outside this ROS graph. Match its domain/environment or stop it yourself; no duplicate was started.')
            command = command_for(target, env)
            process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                       stdout=sys.stderr, stderr=sys.stderr,
                                       start_new_session=True)
            until = time.monotonic() + 10
            while not stopping and time.monotonic() < until:
                if stdin_closed():
                    return 0
                if process.poll() is not None:
                    raise ValueError('ROS driver exited during startup; inspect the backend journal for its serial/display error.')
                rclpy.spin_once(node, timeout_sec=0.1)
                try:
                    if endpoints(node, target):
                        visible = True
                        break
                except GraphNotReady:
                    # Only during startup. Once ready, graph loss must still stop.
                    continue
            if not visible:
                raise ValueError('ROS driver started but its control subscription did not appear.')
        emit('motion_runtime_ready', target=target, owned=process is not None)
        checked_at = time.monotonic()
        while not stopping and not stdin_closed():
            if process is not None and process.poll() is not None:
                raise ValueError('Managed ROS driver exited; select the mode again after fixing its device/display error.')
            rclpy.spin_once(node, timeout_sec=0.2)
            if time.monotonic() - checked_at >= 1:
                if not endpoints(node, target):
                    raise ValueError('The selected ROS driver disconnected; control has stopped.')
                checked_at = time.monotonic()
        return 0
    finally:
        stop_owned(process)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        emit('motion_runtime_error', reason=str(error))
        raise SystemExit(1)
