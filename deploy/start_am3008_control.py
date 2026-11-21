"""Switch an existing manual NX 8093 backend to AM3008 without reading stdin."""
import argparse
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request


PORT = 8093
PROBE = r'''
import json,time,rclpy
from rclpy.node import Node
rclpy.init(args=[])
n=Node('hkcamera_am3008_preflight')
try:
    end=time.monotonic()+2.5
    while time.monotonic()<end:
        rclpy.spin_once(n,timeout_sec=0.1)
    topic=__import__('os').environ['AM3008_CMD_TOPIC']
    subscribers=n.get_subscriptions_info_by_topic(topic)
    publishers=n.get_publishers_info_by_topic(topic)
    def names(items):
        return [dict(name=x.node_name,namespace=x.node_namespace,type=x.topic_type) for x in items]
    print(json.dumps(dict(subscribers=names(subscribers),publishers=names(publishers))))
finally:
    n.destroy_node()
    rclpy.shutdown()
'''


@dataclass
class Backend:
    pid: int
    birth: str
    cwd: Path
    argv: list
    env: dict


def birth(pid):
    # comm may contain spaces; the remaining suffix begins at field 3.
    return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]


def listener_pids():
    result = subprocess.run(['ss', '-ltnp', 'sport = :8093'], capture_output=True,
                            text=True, check=True, timeout=5)
    pids = set(re.findall(r'pid=(\d+)', result.stdout))
    if 'LISTEN' in result.stdout and not pids:
        raise ValueError('8093 listener ownership is not visible; no process was stopped.')
    return {int(pid) for pid in pids}


def listener():
    pids = listener_pids()
    if len(pids) != 1:
        raise ValueError('Expected one visible 8093 listener; no process was stopped.')
    return pids.pop()


def check_manager(cgroups):
    for line in cgroups.splitlines():
        path = line.split(':', 2)[-1]
        leaf = path.rsplit('/', 1)[-1]
        terminal = leaf.startswith(('app-org.gnome.Terminal', 'app-gnome-org.gnome.Terminal', 'app-org.kde.konsole'))
        if '/system.slice/' in path or (leaf.endswith('.service') and not terminal):
            raise ValueError('8093 is service-managed; this manual launcher will not stop it.')


def inspect_backend(root, pid):
    proc = Path('/proc') / str(pid)
    if proc.stat().st_uid != os.getuid():
        raise ValueError('8093 belongs to a different user; no process was stopped.')
    check_manager((proc / 'cgroup').read_text())
    argv = [x.decode() for x in (proc / 'cmdline').read_bytes().split(b'\0') if x]
    env = dict(x.decode().split('=', 1) for x in (proc / 'environ').read_bytes().split(b'\0') if b'=' in x)
    cwd = (proc / 'cwd').resolve()
    app = (root / 'backend/app.py').resolve()
    script_matches = any((cwd / arg).resolve() == app for arg in argv if arg.endswith('app.py'))
    module_matches = 'app:app' in argv and cwd == app.parent
    if not argv or not (script_matches or module_matches):
        raise ValueError('8093 is not the selected project backend; no process was stopped.')
    executable = shutil.which(argv[0], path=env.get('PATH'))
    if not executable:
        raise ValueError('Cannot resolve the original backend interpreter.')
    argv[0] = executable
    return Backend(pid, birth(pid), cwd, argv, env)


def credential_environment(backend):
    env = dict(backend.env)
    token = env.get('OPERATOR_TOKEN_FILE', str(Path.home() / '.config/hkcamera/operator-token.json'))
    path = Path(token)
    if not path.is_absolute():
        path = backend.cwd / path
    saved = False
    if path.is_file():
        record = json.loads(path.read_text())
        saved = len(bytes.fromhex(record['salt'])) == 16 and len(bytes.fromhex(record['digest'])) == 32
    if not saved and not env.get('OPERATOR_TOKEN'):
        raise ValueError('Existing web credential cannot be recovered; no default password was installed.')
    env['OPERATOR_TOKEN_FILE'] = str(path)
    return env


def driver_environments(base):
    domains = {}
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            command = (proc / 'cmdline').read_bytes().lower()
            if b'am3008' not in command or b'hkcamera' in command:
                continue
            values = dict(x.decode().split('=', 1) for x in (proc / 'environ').read_bytes().split(b'\0') if b'=' in x)
            domain = int(values.get('ROS_DOMAIN_ID', '0'))
            if 0 <= domain <= 232:
                domains[domain] = {k: values[k] for k in ('ROS_LOCALHOST_ONLY', 'RMW_IMPLEMENTATION') if k in values}
        except (OSError, ValueError, UnicodeError):
            continue
    for value in (base.get('ROS_DOMAIN_ID', '0'), '0', '42'):
        domain = int(value)
        if not 0 <= domain <= 232:
            raise ValueError('Invalid ROS domain in the existing environment.')
        domains.setdefault(domain, {})
    return domains


def choose_driver(base, topic):
    choices = []
    for domain, settings in driver_environments(base).items():
        env = dict(base)
        env.update(ROS_DOMAIN_ID=str(domain), ROS_LOCALHOST_ONLY='0', AM3008_CMD_TOPIC=topic)
        env.update(settings)
        result = subprocess.run(['/usr/bin/python3', '-c', PROBE], env=env,
                                capture_output=True, text=True, timeout=8)
        if result.returncode:
            continue
        graph = json.loads(result.stdout.strip().splitlines()[-1])
        subscribers = [x for x in graph['subscribers'] if
                       x['type'] == 'geometry_msgs/msg/Twist' and
                       'am3008' in (x['namespace'] + '/' + x['name']).lower()]
        if not subscribers:
            continue
        other = [x['name'] for x in graph['publishers'] if not x['name'].startswith('hkcamera_')]
        if other:
            raise ValueError('Another cmd_vel publisher is active: ' + ', '.join(other) +
                             '. Exit keyboard/navigation control before retrying.')
        choices.append((env, subscribers))
    if len(choices) != 1:
        raise ValueError('Expected one AM3008 Twist subscriber/domain; found ' + str(len(choices)) +
                         '. Start the chassis driver first; no process was stopped.')
    return choices[0]


def preflight(root, backend, topic):
    for name in ('backend/app.py', 'backend/turtle_bridge.py', 'backend/turtle_worker.py', 'backend/am3008_worker.py'):
        path = root / name
        compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec')
    if 'MOTION_TARGET' not in (root / 'backend/turtle_bridge.py').read_text():
        raise ValueError('The new bridge has not replaced the old file.')
    prepare_logs(root)
    env = credential_environment(backend)
    setup = env.get('ROS_SETUP', '/opt/ros/humble/setup.bash')
    if not Path(setup).is_file():
        raise ValueError('ROS setup.bash is missing.')
    result = subprocess.run(['bash', '-c', 'source "$1" && exec /usr/bin/python3 -c "$2"',
                             'hkcamera-am3008', setup, 'import json,os;print(json.dumps(dict(os.environ)))'],
                            env=env, capture_output=True, text=True, timeout=10, check=True)
    env = json.loads(result.stdout)
    check = subprocess.run([backend.argv[0], '-c', 'import fastapi,uvicorn'], env=env,
                           capture_output=True, timeout=8)
    if check.returncode:
        raise ValueError('Original backend interpreter lacks FastAPI/uvicorn.')
    selected, subscribers = choose_driver(env, topic)
    selected.update(MOTION_TARGET='am3008', TURTLESIM_BRIDGE='0',
                    ROS_PYTHON='/usr/bin/python3', AM3008_CMD_TOPIC=topic,
                    AM3008_MAX_LINEAR_MPS='0.2', AM3008_MAX_STEERING_RAD='0.4',
                    WEB_PORT='8093', PYTHONDONTWRITEBYTECODE='1')
    return selected, subscribers


def status():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8093/api/status', timeout=1) as response:
        return json.load(response)['control']


def prepare_logs(root):
    directory = root / '.state'
    if directory.is_symlink():
        raise ValueError('Log directory must not be a symlink.')
    directory.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryFile(dir=directory):
        pass
    return directory


def spawn(backend, env, root, label):
    directory = prepare_logs(root)
    log = directory / ('am3008-' + label + '-' + str(time.time_ns()) + '.log')
    fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output:
        process = subprocess.Popen(backend.argv, cwd=backend.cwd, env=env,
                                   stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    return process, log


def switch(root, backend, env):
    if birth(backend.pid) != backend.birth or listener() != backend.pid:
        raise ValueError('8093 owner changed during preflight; no process was stopped.')
    os.kill(backend.pid, signal.SIGINT)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (Path('/proc') / str(backend.pid)).exists():
        time.sleep(0.1)
    if (Path('/proc') / str(backend.pid)).exists():
        raise ValueError('Old backend did not exit; no forced kill was attempted.')
    if listener_pids():
        raise ValueError('8093 was rebound after shutdown; no second backend was started.')
    process, log = None, None
    try:
        process, log = spawn(backend, env, root, 'web')
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and process.poll() is None:
            try:
                control = status()
                if (control.get('base_mode') == 'ros2_cmd_vel' and control.get('base_connected')
                        and listener() == process.pid):
                    return log
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.25)
    except OSError:
        pass  # Popen/log failures must also enter the original-mode recovery below.
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            raise ValueError('New backend has not exited; inspect ' + str(log))
    # Restore the original environment/mode, not previously overwritten source bytes.
    if listener_pids():
        raise ValueError('8093 is occupied; no duplicate recovery backend was started. Log: ' + str(log))
    spawn(backend, backend.env, root, 'original-mode')
    raise ValueError('AM3008 startup failed; original mode restart requested. Log: ' + str(log))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', default='/cmd_vel')
    args = parser.parse_args()
    if sys.platform != 'linux' or getpass.getuser() != 'nvidia':
        raise ValueError('Run this saved script on NX as nvidia, without sudo.')
    if not re.fullmatch(r'/[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*', args.topic):
        raise ValueError('Invalid absolute ROS topic.')
    root = Path(__file__).resolve().parents[1]
    backend = inspect_backend(root, listener())
    env, subscribers = preflight(root, backend, args.topic)
    print('Matched ROS domain', env['ROS_DOMAIN_ID'], 'subscribers:',
          ', '.join(x['namespace'].rstrip('/') + '/' + x['name'] for x in subscribers), flush=True)
    log = switch(root, backend, env)
    print('READY: web ROS cmd_vel link is connected. Refresh the existing page and claim control.')
    print('Default linear limit: 0.2; angular.z numeric limit: 0.4. Log:', log)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('STOP:', str(error) if isinstance(error, ValueError) else type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
