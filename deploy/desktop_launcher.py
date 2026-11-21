"""NX desktop entry for the existing supervised control services.

The launcher never starts a second Python web server and never embeds a web
password. Service installation/configuration remains an explicit setup step.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
URL = 'http://127.0.0.1:8093'
BACKEND = 'hkcamera-stack-backend.service'
MEDIA = ('hkcamera-stack-tunnel.service', 'hkcamera-stack-video.service')
DESKTOP_NAME = 'hkcamera-control.desktop'


class LauncherError(ValueError):
    pass


def run(argv, **options):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, **options)


def desktop_argument(value):
    """Quote one Exec argument (desktop string escapes, then Exec escapes)."""
    value = str(value)
    if any(ord(char) < 32 for char in value):
        raise LauncherError('Desktop command paths cannot contain control characters.')
    replacements = {'\\': r'\\\\', '"': r'\\"', '`': r'\\`', '$': r'\\$'}
    return '"' + ''.join(replacements.get(char, char) for char in value).replace('%', '%%') + '"'


def desktop_entry(launcher, icon):
    icon = str(icon)
    if '\n' in icon or '\r' in icon:
        raise LauncherError('Icon path contains a newline.')
    argv = ['/usr/bin/python3', str(launcher), '--open', '--with-media']
    return ('[Desktop Entry]\nVersion=1.0\nType=Application\n'
            'Name=小车遥控工作台\nName[en]=Robot Control\n'
            'Comment=启动 NX 控制服务并打开本地控制界面\n'
            'Exec=' + ' '.join(desktop_argument(arg) for arg in argv) + '\n'
            'Icon=' + icon.replace('\\', '\\\\') + '\n'
            'Terminal=false\nStartupNotify=false\nCategories=Education;Science;\n')


def desktop_directory(home):
    binary = shutil.which('xdg-user-dir')
    if binary:
        result = run([binary, 'DESKTOP'], capture_output=True, text=True, timeout=5)
        candidate = Path(result.stdout.strip())
        if result.returncode == 0 and candidate.is_absolute() and candidate != home:
            return candidate
    return home / 'Desktop'


def write_atomic(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def install(home=None):
    home = Path.home() if home is None else Path(home)
    launcher = ROOT / 'deploy/desktop_launcher.py'
    asset = ROOT / 'deploy/assets/hkcamera-control.svg'
    if not launcher.is_file() or not asset.is_file():
        raise LauncherError('Launcher files are incomplete; copy the full update before installing the icon.')
    data = Path(os.environ.get('XDG_DATA_HOME') or str(home / '.local/share'))
    if not data.is_absolute():
        raise LauncherError('XDG_DATA_HOME must be an absolute path.')
    icon = data / 'icons/hicolor/scalable/apps/hkcamera-control.svg'
    write_atomic(icon, asset.read_text(encoding='utf-8'), 0o644)
    content = desktop_entry(launcher, icon)
    application = data / 'applications' / DESKTOP_NAME
    desktop = desktop_directory(home) / DESKTOP_NAME
    write_atomic(application, content, 0o644)
    write_atomic(desktop, content, 0o755)
    gio = shutil.which('gio')
    if gio:
        # GNOME may refuse metadata on some filesystems. Executable desktop
        # entry remains installed; its standard Allow Launching action works.
        run([gio, 'set', str(desktop), 'metadata::trusted', 'true'],
            capture_output=True, text=True, timeout=5)
    print('桌面入口已安装：' + str(desktop))
    print('应用程序入口：' + str(application))
    print('如桌面提示不受信任，右键此图标选择“允许启动”。安装图标没有启动或重启任何服务。')
    return desktop


def requested_services(with_media):
    return (BACKEND,) + (MEDIA if with_media else ())


def service_state(unit):
    if unit not in (BACKEND, *MEDIA):
        raise LauncherError('Unknown service target.')
    loaded = run(['/usr/bin/systemctl', 'show', '--property=LoadState', '--value', unit],
                 capture_output=True, text=True, timeout=5)
    if loaded.returncode or loaded.stdout.strip() != 'loaded':
        raise LauncherError('服务尚未安装：' + unit + '\n先按 INTEGRATED_MODES.md 完成 NX 首次整合。')
    active = run(['/usr/bin/systemctl', 'is-active', '--quiet', unit],
                 capture_output=True, text=True, timeout=5)
    return active.returncode == 0


def ensure_services(with_media=False):
    missing = [unit for unit in requested_services(with_media) if not service_state(unit)]
    if not missing:
        return []
    pkexec = shutil.which('pkexec')
    if not pkexec:
        raise LauncherError('服务尚未运行且未找到桌面授权程序 pkexec。\n请在 NX 终端运行：\nsudo systemctl start ' + ' '.join(missing))
    result = run([pkexec, '/usr/bin/systemctl', 'start', *missing],
                 capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise LauncherError('服务启动未完成（可能取消了系统授权）。\n请查看 NX 服务状态；启动器不会关闭现有服务。')
    return missing


def backend_ready():
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with client.open(URL + '/api/status', timeout=2) as response:
            status = json.load(response)
        return (isinstance(status, dict) and isinstance(status.get('protocol'), dict) and
                status['protocol'].get('control_path') == '/ws/control' and
                isinstance(status.get('control'), dict))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def wait_for_backend(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend_ready():
            return
        time.sleep(0.5)
    raise LauncherError('本地网页后端尚未就绪。\n在 NX 终端查看：\nsudo journalctl -u ' + BACKEND + ' -n 40 --no-pager')


def open_workbench(with_media=False):
    ensure_services(with_media)
    wait_for_backend()
    opener = shutil.which('xdg-open')
    if not opener:
        raise LauncherError('浏览器启动工具 xdg-open 不存在，请手动打开：' + URL)
    result = run([opener, URL], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise LauncherError('服务已运行，浏览器未能自动打开。请手动访问：' + URL)


def show_error(message):
    print('小车工作台：' + message, file=sys.stderr)
    zenity = shutil.which('zenity')
    if zenity and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        try:
            run([zenity, '--error', '--no-markup', '--title=小车遥控工作台',
                 '--width=520', '--text=' + message], timeout=180)
        except (OSError, subprocess.SubprocessError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='NX desktop launcher for supervised robot control.')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--install', action='store_true', help='Install a user desktop/application icon, without sudo.')
    actions.add_argument('--open', action='store_true', help='Start inactive known services and open the local webpage.')
    actions.add_argument('--diagnose', action='store_true', help='Print readiness only; never start or stop services.')
    parser.add_argument('--with-media', action='store_true', help='Also start the fixed control tunnel and video services if inactive.')
    args = parser.parse_args(argv)
    try:
        if sys.platform != 'linux':
            raise LauncherError('请在 NX 的 Linux 桌面中运行此启动器。')
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            raise LauncherError('请以 NX 桌面用户运行，不要使用 sudo 启动此软件。')
        if args.install:
            install()
        elif args.diagnose:
            for unit in requested_services(args.with_media):
                print(unit + ': ' + ('active' if service_state(unit) else 'inactive'))
            print('Local control webpage: ' + ('ready' if backend_ready() else 'not ready'))
        else:
            if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
                raise LauncherError('请从 NX 图形桌面打开，当前进程没有桌面会话。')
            open_workbench(args.with_media)
        return 0
    except (LauncherError, OSError, subprocess.SubprocessError) as error:
        show_error(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
