"""One management entry for independently supervised NX components."""
import argparse
import getpass
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
HOME = Path('/home/nvidia')
CONFIG = HOME / '.config/hkcamera/stack.json'
TARGET = 'hkcamera-stack.target'
COMPONENTS = ('backend', 'tunnel', 'video', 'turtle')
CONTROL_PATH = '/ws/control'


class ConfigurationError(ValueError):
    pass


def defaults():
    return dict(camera_host='camera.invalid', camera_user='admin',
                ssh_target='tunnel@gateway.invalid', ssh_identity='',
                backend_python=str(ROOT / '.venv/bin/python') if
                (ROOT / '.venv/bin/python').is_file() else '/usr/bin/python3',
                token_file=str(HOME / '.config/hkcamera/operator-token.json'),
                ros_setup='/opt/ros/humble/setup.bash', ros_domain=42,
                base_cmd_topic='/cmd_vel',
                base_max_linear_mps=0.2, base_max_steering_rad=0.4,
                managed_motion=False,
                am3008_setup=str(HOME / 'robot_ws/install/setup.bash'),
                am3008_serial_port='/dev/ttyCH341USB0',
                am3008_baudrate=115200, am3008_timeout_ms=100,
                simulator=True, display=os.environ.get('DISPLAY', ''),
                xauthority=os.environ.get('XAUTHORITY', ''))


def selected_motion_target(config):
    target = config.get('motion_target')
    if target is None:
        target = 'turtlesim' if config.get('simulator', True) else 'none'
    if target not in ('turtlesim', 'am3008', 'none'):
        raise ValueError('motion_target must be turtlesim, am3008, or none.')
    return target


def validate(config):
    for name in ('camera_password', 'operator_token', 'camera_host', 'camera_user',
                 'ssh_target', 'backend_python', 'token_file', 'ros_setup'):
        if not isinstance(config.get(name), str) or not config[name]:
            raise ValueError('Missing configuration field: ' + name)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+', config['ssh_target']):
        raise ValueError('ssh_target must be user@host, without shell options.')
    if type(config.get('simulator')) is not bool or type(config.get('ros_domain')) is not int:
        raise ValueError('Invalid simulator or ROS domain configuration.')
    if not 0 <= config['ros_domain'] <= 232:
        raise ValueError('ROS domain must be between 0 and 232.')
    target = selected_motion_target(config)
    if target == 'turtlesim' and not config.get('display'):
        raise ValueError('Simulator needs the actual NX desktop DISPLAY; do not guess it.')
    topic = config.get('base_cmd_topic', '/cmd_vel')
    if (not isinstance(topic, str) or not re.fullmatch(r'/[A-Za-z0-9_/]+', topic)
            or '//' in topic or topic.endswith('/')):
        raise ValueError('base_cmd_topic must be an absolute ROS topic name.')
    linear = config.get('base_max_linear_mps', 0.2)
    steering = config.get('base_max_steering_rad', 0.4)
    if (isinstance(linear, bool) or not isinstance(linear, (int, float))
            or not math.isfinite(linear) or not 0 < linear <= 1.0):
        raise ValueError('base_max_linear_mps must be in (0, 1.0].')
    if (isinstance(steering, bool) or not isinstance(steering, (int, float))
            or not math.isfinite(steering) or not 0 < steering <= 0.4):
        raise ValueError('base_max_steering_rad must be in (0, 0.4].')
    for name in ('backend_python', 'token_file', 'ros_setup'):
        if not PurePosixPath(config[name]).is_absolute():
            raise ValueError(name + ' must be an absolute path.')
    if type(config.get('managed_motion', False)) is not bool:
        raise ValueError('managed_motion must be a boolean.')
    if config.get('managed_motion', False):
        for name in ('am3008_setup', 'am3008_serial_port'):
            value = config.get(name, defaults()[name])
            if not isinstance(value, str) or not PurePosixPath(value).is_absolute():
                raise ValueError(name + ' must be an absolute path.')
        if not config.get('am3008_serial_port', defaults()['am3008_serial_port']).startswith('/dev/'):
            raise ValueError('am3008_serial_port must identify a /dev device.')
        for name, upper in (('am3008_baudrate', 4000000), ('am3008_timeout_ms', 10000)):
            value = config.get(name, defaults()[name])
            if type(value) is not int or not 0 < value <= upper:
                raise ValueError(name + ' is outside its valid integer range.')
    return config


def load_config(path=CONFIG):
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('Configuration must be a private regular file (chmod 600).')
    return validate(json.loads(path.read_text(encoding='utf-8')))


def save_config(config, path=CONFIG):
    validate(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(config, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def configure():
    if os.getuid() == 0 or not sys.stdin.isatty():
        raise ValueError('Configure as nvidia in an interactive NX desktop terminal, not sudo/heredoc.')
    config = load_config() if CONFIG.exists() else defaults()
    for name in ('camera_host', 'camera_user', 'ssh_target', 'ssh_identity', 'backend_python', 'token_file'):
        value = input(f'{name} [{config.get(name, "")}]: ').strip()
        if value:
            config[name] = value
    target_default = selected_motion_target(config)
    target = input(f'motion_target [turtlesim/am3008/none] [{target_default}]: ').strip()
    if target:
        config['motion_target'] = target
    else:
        config['motion_target'] = target_default
    config['simulator'] = config['motion_target'] == 'turtlesim'
    for name in ('ros_setup', 'base_cmd_topic', 'base_max_linear_mps', 'base_max_steering_rad'):
        value = input(f'{name} [{config.get(name, defaults().get(name, ""))}]: ').strip()
        if value:
            config[name] = value
    domain = input(f"ros_domain [{config.get('ros_domain', 42)}]: ").strip()
    if domain:
        try:
            config['ros_domain'] = int(domain)
        except ValueError as error:
            raise ValueError('ros_domain must be an integer.') from error
    for name in ('base_max_linear_mps', 'base_max_steering_rad'):
        try:
            config[name] = float(config.get(name, defaults()[name]))
        except (TypeError, ValueError) as error:
            raise ValueError(name + ' must be a number.') from error
    for name, label in (('camera_password', 'Camera login password'),
                        ('operator_token', 'EXISTING web control password')):
        value = getpass.getpass(label + ' (hidden; Enter keeps saved value): ')
        config[name] = value or config.get(name, '')
    config['display'] = os.environ.get('DISPLAY', config.get('display', ''))
    config['xauthority'] = os.environ.get('XAUTHORITY', config.get('xauthority', ''))
    save_config(config)
    print('Saved private stack configuration. No service or existing password record changed.')


def redact(text, config):
    text = re.sub(r'(?i)rtsps?://\S+', '[RTSP URL hidden]', text)
    for name in ('camera_password', 'operator_token'):
        secret = config.get(name, '')
        if secret:
            text = text.replace(urllib.parse.quote(secret, safe=''), '[hidden]').replace(secret, '[hidden]')
    return text


def camera_binary():
    for path in (ROOT / 'cpp/hik_camera_daemon', ROOT / 'cpp/build/hik_camera_daemon'):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise ValueError('Camera SDK daemon missing; refusing to silently disable PTZ.')


def port_free(port):
    with socket.socket() as listener:
        # Allow Linux TIME_WAIT reuse without sharing a live listener.
        if os.name == 'nt':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('0.0.0.0', port))
        except OSError:
            raise ValueError(f'Port {port} occupied; no existing process was stopped.') from None


def ros_environment(config):
    result = subprocess.run(
        ['bash', '-c', 'source "$1" && exec /usr/bin/python3 -c "$2"',
         'hkcamera-ros', config['ros_setup'],
         'import json,os; print(json.dumps(dict(os.environ)))'],
        capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError('ROS setup failed; no service started by this check.')
    env = json.loads(result.stdout)
    env.update(ROS_DOMAIN_ID=str(config['ros_domain']), ROS_LOCALHOST_ONLY='1')
    return env


def backend_environment(config, base):
    target = selected_motion_target(config)
    env = dict(base)
    env.update(WEB_HOST='0.0.0.0', WEB_PORT='8093',
               CAMERA_DAEMON=str(camera_binary()), CAMERA_HOST=config['camera_host'],
               CAMERA_USER=config['camera_user'], CAMERA_PASSWORD=config['camera_password'],
               CAMERA_PORT='8000', CAMERA_CHANNEL='1',
               OPERATOR_TOKEN=config['operator_token'], OPERATOR_TOKEN_FILE=config['token_file'],
               MOTION_TARGET=target,
               TURTLESIM_BRIDGE='1' if target == 'turtlesim' else '0',
               AM3008_CMD_TOPIC=config.get('base_cmd_topic', '/cmd_vel'),
               AM3008_MAX_LINEAR_MPS=str(config.get('base_max_linear_mps', 0.2)),
               AM3008_MAX_STEERING_RAD=str(config.get('base_max_steering_rad', 0.4)),
               ROS_DOMAIN_ID=str(config['ros_domain']), ROS_LOCALHOST_ONLY='1',
               ROS_PYTHON='/usr/bin/python3', PYTHONDONTWRITEBYTECODE='1')
    env.update(MOTION_LIFECYCLE_MANAGED='1' if config.get('managed_motion', False) else '0',
               MOTION_ROS_SETUP=config['ros_setup'],
               MOTION_AM3008_SETUP=config.get('am3008_setup', defaults()['am3008_setup']),
               MOTION_SERIAL_PORT=config.get('am3008_serial_port', defaults()['am3008_serial_port']),
               MOTION_SERIAL_BAUD=str(config.get('am3008_baudrate', 115200)),
               MOTION_SERIAL_TIMEOUT_MS=str(config.get('am3008_timeout_ms', 100)),
               DISPLAY=config.get('display', ''))
    if config.get('xauthority'):
        env['XAUTHORITY'] = config['xauthority']
    return env


def tunnel_command(config):
    command = ['ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=3',
               '-R', '127.0.0.1:18090:127.0.0.1:8093',
               '-L', '127.0.0.1:19350:127.0.0.1:1935']
    if config.get('ssh_identity'):
        command.extend(['-o', 'IdentitiesOnly=yes', '-i', config['ssh_identity']])
    return command + [config['ssh_target']]


def unit_files(root):
    root = str(root)
    if not re.fullmatch(r'/[A-Za-z0-9/_.-]+', root):
        raise ValueError('Installation requires a simple absolute NX project path.')
    names = [f'hkcamera-stack-{part}.service' for part in COMPONENTS]
    files = {TARGET: '[Unit]\nDescription=Integrated hkcamera stack\nWants=' +
             ' '.join(names) + '\n\n[Install]\nWantedBy=multi-user.target\n'}
    for part, name in zip(COMPONENTS, names):
        after = 'network-online.target'
        if part == 'backend':
            after += ' hkcamera-stack-turtle.service'
        if part == 'video':
            after += ' hkcamera-stack-tunnel.service'
        files[name] = (
            '[Unit]\nDescription=hkcamera ' + part + '\nPartOf=' + TARGET +
            '\nAfter=' + after + '\nWants=network-online.target\nStartLimitIntervalSec=0\n'
            '\n[Service]\nType=simple\nUser=nvidia\nWorkingDirectory=' + root +
            '\nExecStart=/usr/bin/python3 ' + root + '/deploy/stack.py _run ' + part +
            '\nRestart=on-failure\nRestartPreventExitStatus=78\nRestartSec=5\nTimeoutStopSec=' +
            ('30' if part == 'backend' else '15') + '\nKillMode=' +
            ('mixed' if part == 'backend' else 'control-group') +
            '\nUMask=0077\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n')
        if part == 'backend':
            files[name] += '\n[Install]\nWantedBy=multi-user.target\n'
    return files


def preflight(config, check_running=True):
    camera_binary()
    frontend = (ROOT / 'web/index.html').read_text(encoding='utf-8-sig')
    if 'new WebSocket(`${wsProtocol}//${location.host}/ws/control`)' not in frontend:
        raise ValueError('Frontend route mismatch; refusing to start an inconsistent stack.')
    for binary in ('ssh', 'ffmpeg', 'ffprobe'):
        if not shutil.which(binary):
            raise ValueError('Missing executable: ' + binary)
    target = selected_motion_target(config)
    env = ros_environment(config) if target != 'none' else dict(os.environ)
    check = ['import fastapi,uvicorn']
    result = subprocess.run([config['backend_python'], '-c', check[0]],
                            env=env, capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError('Backend Python lacks FastAPI/uvicorn; nothing was installed automatically.')
    if target == 'turtlesim':
        result = subprocess.run(['/usr/bin/python3', '-c', 'import rclpy; from turtlesim.msg import Pose'],
                                env=env, capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError('ROS2 turtlesim dependencies are unavailable.')
    elif target == 'am3008':
        result = subprocess.run(['/usr/bin/python3', '-c', 'import rclpy; from geometry_msgs.msg import Twist'],
                                env=env, capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError('ROS2 geometry_msgs/Twist dependencies are unavailable.')
    if not check_running:
        return
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            name = (proc / 'exe').resolve().name
            if name in ('hik_camera_daemon', 'turtlesim_node'):
                raise ValueError(f'Existing {name} PID {proc.name}; inspect before one-time cutover. No process stopped.')
        except OSError:
            continue
    port_free(8093)
    port_free(19350)


def file_stamp(path):
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def watch_video_policy(process, path, stamp, stopped):
    """Reconnect only this service's own FFmpeg after verified encoder changes."""
    while not stopped.wait(1):
        if process.poll() is not None:
            return
        if file_stamp(path) != stamp:
            print('Verified source settings changed; reconnecting this video producer.', flush=True)
            try:
                process.terminate()
            except OSError:
                pass
            return


def run_video(config):
    for _ in range(20):
        try:
            with socket.create_connection(('127.0.0.1', 19350), timeout=1):
                break
        except OSError:
            time.sleep(1)
    else:
        raise ValueError('Video tunnel not listening; waiting for service-manager retry.')
    url = ('rtsp://' + urllib.parse.quote(config['camera_user'], safe='') + ':' +
           urllib.parse.quote(config['camera_password'], safe='') + '@' +
           config['camera_host'] + ':554/Streaming/Channels/101')
    probe = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-i', url,
                            '-select_streams', 'v:0', '-show_entries', 'stream=codec_name',
                            '-of', 'default=noprint_wrappers=1:nokey=1'],
                           capture_output=True, text=True, errors='replace', timeout=20)
    if probe.returncode or not probe.stdout.strip():
        print(redact(probe.stderr, config), flush=True)
        if re.search(r'\b(401|403)\b|unauthori[sz]ed|forbidden', probe.stderr, re.I):
            raise ConfigurationError('Camera rejected authentication; automatic retries stopped to avoid account lockout.')
        raise ValueError('RTSP probe failed. Check camera credentials/network; video is not ready.')
    if probe.stdout.strip() != 'h264':
        raise ConfigurationError('Source is not H264. Configure encoding deliberately; no automatic transcode.')
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning', '-nostats',
               '-progress', 'pipe:1', '-stats_period', '5', '-rtsp_transport', 'tcp',
               '-i', url, '-map', '0:v:0', '-an', '-c:v', 'copy', '-f', 'flv',
               'rtmp://127.0.0.1:19350/live/hkcamera']
    policy_path = Path.home() / '.config/hkcamera/video-quality.json'
    stamp = file_stamp(policy_path)
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors='replace') as process:
        stopped = threading.Event()
        watcher = threading.Thread(target=watch_video_policy, args=(process, policy_path, stamp, stopped), daemon=True)
        watcher.start()
        try:
            for line in process.stdout:
                if line.startswith('frame=') or not re.match(r'^[a-z0-9_]+=', line):
                    print(redact(line, config).strip(), flush=True)
            return process.wait() or 1
        finally:
            stopped.set()
            watcher.join(timeout=2)


def run_component(part, config):
    os.chdir(ROOT)
    target = selected_motion_target(config)
    if part == 'video':
        return run_video(config)
    if part == 'tunnel':
        port_free(19350)
        command, env = tunnel_command(config), dict(os.environ)
    else:
        if part == 'turtle' and (target != 'turtlesim' or config.get('managed_motion', False)):
            return 0
        env = ros_environment(config) if target != 'none' else dict(os.environ)
        if part == 'backend':
            port_free(8093)
            env = backend_environment(config, env)
            command = [config['backend_python'], '-u', str(ROOT / 'backend/app.py')]
        else:
            env.update(DISPLAY=config['display'])
            if config.get('xauthority'):
                if not Path(config['xauthority']).is_file():
                    raise ValueError('Desktop XAUTHORITY is not ready; no display-security bypass attempted.')
                env['XAUTHORITY'] = config['xauthority']
            command = [str(Path(config['ros_setup']).parent / 'bin/ros2'), 'run', 'turtlesim', 'turtlesim_node']
    os.execvpe(command[0], command, env)


def systemctl(*args):
    command = ([] if os.getuid() == 0 else ['sudo']) + ['systemctl', *args]
    return subprocess.run(command, check=True).returncode


def integrate_motion():
    """Opt into the mode supervisor from a real NX desktop session."""
    if os.getuid() == 0:
        raise ValueError('Run integrate as nvidia, without sudo, so the NX desktop session is captured.')
    config = load_config()
    display = os.environ.get('DISPLAY')
    if not display:
        raise ValueError('Open the NX desktop terminal before integrate; DISPLAY must be real, not guessed.')
    setup = Path(config.get('am3008_setup', defaults()['am3008_setup']))
    if not setup.is_file():
        raise ValueError('AM3008 workspace setup missing: ' + str(setup))
    config.update(managed_motion=True, display=display,
                  xauthority=os.environ.get('XAUTHORITY', config.get('xauthority', '')),
                  am3008_setup=str(setup),
                  am3008_serial_port=config.get('am3008_serial_port', '/dev/ttyCH341USB0'),
                  am3008_baudrate=config.get('am3008_baudrate', 115200),
                  am3008_timeout_ms=config.get('am3008_timeout_ms', 100))
    validate(config)
    backup = CONFIG.with_name('stack.before-integrated-motion-' + str(time.time_ns()) + '.json')
    original = load_config()
    save_config(original, backup)
    save_config(config)
    print('Motion integration configured. Private backup:', backup)
    print('No service restarted. Existing external drivers will be reused, never stopped by the web backend.')


def show_status():
    from release_identity import verify_tree
    try:
        print('Installed files:', json.dumps(verify_tree(ROOT)))
    except (OSError, ValueError) as error:
        print('Installed version not verified:', type(error).__name__)
    print('Installed files alone do not identify the currently running process.')
    subprocess.run(['systemctl', '--no-pager', '--full', 'status', TARGET,
                    *[f'hkcamera-stack-{part}.service' for part in COMPONENTS]])
    try:
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open('http://127.0.0.1:8093/api/status', timeout=3) as response:
            data = json.load(response)
        print('Backend control route:', data.get('protocol', {}).get('control_path', 'UNVERIFIED OLD BACKEND'))
        print('Camera:', data['camera']['state'], 'PTZ:', data['camera']['ptz_mode'])
        print('Base connected:', data['control']['base_connected'])
    except Exception as error:
        print('Backend health unavailable:', type(error).__name__)
    print('Local status is NOT proof of public WSS, cloud media registration or decoded browser video.')


def main():
    parser = argparse.ArgumentParser(description='NX integrated service entry; never stops unrelated processes.')
    parser.add_argument('action', choices=('configure', 'check', 'install', 'start', 'stop',
                                          'enable', 'status', 'logs', 'restart', 'integrate', '_run'))
    parser.add_argument('component', nargs='?', choices=COMPONENTS)
    args = parser.parse_args()
    if sys.platform != 'linux' or getpass.getuser() not in ('nvidia', 'root'):
        parser.error('Run on NX as nvidia (sudo only for install/service management).')
    if args.action == 'configure':
        configure()
        return 0
    if args.action == 'integrate':
        integrate_motion()
        return 0
    if args.action == 'status':
        show_status()
        return 0
    if args.action == 'logs':
        return subprocess.run(['sudo', 'journalctl', '--no-pager', '-f',
                               *[value for part in COMPONENTS for value in ('-u', f'hkcamera-stack-{part}.service')]]).returncode
    if args.action in ('stop', 'enable'):
        return systemctl(args.action, TARGET)
    if args.action == 'start':
        active = subprocess.run(['systemctl', 'is-active', '--quiet', TARGET])
        if active.returncode == 0:
            print('Stack already active. Use status to check readiness or restart <component> to retry one component.')
            return 0
    config = load_config()
    if args.action == '_run':
        if not args.component:
            parser.error('_run needs a component')
        return run_component(args.component, config)
    if args.action in ('check', 'start', 'install'):
        preflight(config, check_running=args.action != 'install')
    if args.action == 'check':
        print('Preflight passed; no service started and no existing process stopped.')
    elif args.action == 'install':
        if os.getuid() != 0:
            parser.error('Use sudo for install. It does not start or enable services.')
        units = unit_files(ROOT)
        for name, content in units.items():
            path = Path('/etc/systemd/system') / name
            if path.exists() and 'Integrated hkcamera stack' not in path.read_text() and 'Description=hkcamera ' not in path.read_text():
                raise ValueError('Refusing to overwrite an unrecognized unit: ' + name)
        for name, content in units.items():
            path = Path('/etc/systemd/system') / name
            if path.exists() and path.read_text(encoding='utf-8') != content:
                shutil.copy2(path, path.with_name(path.name + '.backup-' + str(time.time_ns())))
            path.write_text(content, encoding='utf-8')
        systemctl('daemon-reload')
        print('Units installed, NOT started or enabled. No original 8090/ZLM unit modified.')
    elif args.action == 'start':
        return systemctl('start', TARGET)
    elif args.action == 'restart':
        if not args.component:
            parser.error('restart needs one component; other components stay running')
        return systemctl('restart', f'hkcamera-stack-{args.component}.service')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ConfigurationError as error:
        print('Configuration required:', str(error), file=sys.stderr)
        raise SystemExit(78)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Subprocess exception strings can contain a credential-bearing RTSP URL.
        print('Stack action failed:', type(error).__name__,
              str(error) if isinstance(error, ValueError) else '(check component journal)', file=sys.stderr)
        raise SystemExit(1)
