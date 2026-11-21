"""NX-only camera UI repair using the existing local ZLM; no backend restart."""
import configparser
import json
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import quote, urlencode
from urllib.request import build_opener, ProxyHandler, Request


def fixed_html(text):
    if '/api/video/local.mp4' in text:
        raise ValueError('Unexpected experimental page; restore its backup first')
    text, count = re.subn(r'const zlmBase\s*=[\s\S]*?;',
        'const zlmBase = location.protocol === "http:" && ["8090", "8093"].includes(location.port)\n'
        '        ? `http://${location.hostname}:8080`\n        : location.origin;', text)
    if count != 1:
        raise ValueError('Expected exactly one ZLM URL definition; nothing changed')
    text = text.replace(' && motionTarget !== "turtlesim"', '')
    text = text.replace(' || motionTarget === "turtlesim") return false;', ') return false;')
    return re.sub(r'input-controls\.js\?v=[^"\s]+',
                  'input-controls.js?v=20261111-ptz-local-video-1', text)


def fixed_inputs(text):
    if 'function ptzAllowed()' not in text:
        raise ValueError('Unrecognized keyboard/gamepad script; nothing changed')
    return text.replace(" && motionTarget!=='turtlesim'", '')


def call(secret, name, params):
    request = Request('http://127.0.0.1:8080/index/api/' + name,
                      data=urlencode(dict(params, secret=secret)).encode())
    with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
        result = json.load(response)
    if result.get('code') != 0:
        raise ValueError('Local ZLM rejected ' + name + '; no secrets printed')
    return result


def restore_source(home):
    configs = [home / 'Desktop/ZLMediaKit/release/linux' / build / 'config.ini'
               for build in ('Debug', 'Release')]
    secret = None
    result = None
    for path in configs:
        if not path.is_file():
            continue
        cfg = configparser.ConfigParser(interpolation=None, strict=False)
        cfg.read(path)
        try:
            candidate = cfg['api']['secret']
            result = call(candidate, 'getMediaList', {'app': 'live', 'stream': 'hkcamera'})
            secret = candidate
            break
        except (KeyError, OSError, ValueError):
            continue
    if secret is None:
        raise ValueError('Local ZLM8080 is not reachable with its existing config. Keep cloud services unchanged; inspect the existing MediaServer/config.')
    if not result.get('data'):
        cfg = json.loads((home / '.config/hkcamera/stack.json').read_text())
        url = ('rtsp://' + quote(cfg.get('camera_user', 'admin'), safe='') + ':' +
               quote(cfg['camera_password'], safe='') + '@' + cfg.get('camera_host', 'camera.invalid') +
               ':554/Streaming/Channels/101')
        call(secret, 'addStreamProxy', {'vhost': '__defaultVhost__', 'app': 'live',
             'stream': 'hkcamera', 'url': url, 'rtp_type': 0, 'retry_count': -1,
             'enable_rtsp': 1, 'enable_rtmp': 1})
    for _ in range(20):
        result = call(secret, 'getMediaList', {'app': 'live', 'stream': 'hkcamera'})
        schemas = sorted({s.get('schema', '') for s in result.get('data') or []})
        if 'rtsp' in schemas or 'rtmp' in schemas:
            print('LOCAL_ZLM_READY:', ', '.join(schemas))
            return
        time.sleep(1)
    raise ValueError('Local source did not become ready; inspect existing ZLM logs')


def main():
    home = Path.home()
    if sys.platform != 'linux' or home.name != 'nvidia':
        raise ValueError('Run in the NX terminal as nvidia, not on cloud/Windows')
    root = home / 'Desktop/hkcamera'
    html, inputs = root / 'web/index.html', root / 'web/input-controls.js'
    if any(p.resolve() != p.absolute() for p in (html, inputs)):
        raise ValueError('Unexpected symlink; nothing changed')
    old_html, old_inputs = html.read_text(encoding='utf-8'), inputs.read_text(encoding='utf-8')
    new_html, new_inputs = fixed_html(old_html), fixed_inputs(old_inputs)
    if (new_html, new_inputs) != (old_html, old_inputs):
        backup = root / '.updates' / ('camera-ui-' + str(time.time_ns()))
        backup.mkdir(parents=True)
        shutil.copy2(html, backup / 'index.html')
        shutil.copy2(inputs, backup / 'input-controls.js')
        try:
            html.write_text(new_html, encoding='utf-8')
            inputs.write_text(new_inputs, encoding='utf-8')
        except BaseException:
            shutil.copy2(backup / 'index.html', html)
            shutil.copy2(backup / 'input-controls.js', inputs)
            raise
        print('BACKUP:', backup)
    print('PTZ_UI_FIXED; backend, cloud and movement configuration unchanged')
    restore_source(home)
    print('Hard-refresh LAN8093 and public pages. Camera must be logged_in/continuous and control claimed.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('STOP:', str(error), file=sys.stderr)
        raise SystemExit(1)
