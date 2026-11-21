"""Idempotent legacy WS alias, preserving the live canonical block and auth."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

LOCATION = re.compile(r'(?m)^[ \t]*location\s*=\s*(/ws(?:/control)?)\s*\{')
SERVER = re.compile(r'(?m)^[ \t]*server\s*\{')
DEFAULT = Path('/etc/nginx/sites-available/hkcamera')


def block_end(text, opening):
    depth, quote, comment, escaped = 0, '', False, False
    for index in range(opening, len(text)):
        char = text[index]
        if comment:
            if char == '\n': comment = False
            continue
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if quote:
            if char == quote: quote = ''
            continue
        if char in ('"', "'"): quote = char
        elif char == '#': comment = True
        elif char == '{': depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0: return index + 1
    raise ValueError('Unbalanced Nginx block; no changes made.')


def patch_routes(text):
    blocks = {}
    for match in LOCATION.finditer(text):
        path = match[1]
        if path in blocks:
            raise ValueError('Multiple exact WS locations; inspect server scopes manually.')
        end = block_end(text, text.index('{', match.start(), match.end()))
        blocks[path] = (match, end, text[match.start():end])
    if '/ws/control' not in blocks:
        raise ValueError('Live canonical /ws/control block missing; refusing to invent a gateway.')
    match, end, canonical = blocks['/ws/control']
    servers = [(server.start(), block_end(text, server.end() - 1))
               for server in SERVER.finditer(text)]
    def containing_server(location):
        matches = [bounds for bounds in servers if bounds[0] < location.start() < bounds[1]]
        if not matches:
            raise ValueError('WS location is not inside an identifiable server block.')
        return min(matches, key=lambda bounds: bounds[1] - bounds[0])
    canonical_server = containing_server(match)
    if any(containing_server(item[0]) != canonical_server for item in blocks.values()):
        raise ValueError('WS locations belong to different servers; inspect scopes manually.')
    if not re.search(r'proxy_pass\s+http://127\.0\.0\.1:18090\s*;', canonical):
        raise ValueError('Unexpected canonical upstream; no change made.')
    for required in ('proxy_http_version 1.1', '$http_upgrade', 'proxy_set_header Connection', '$http_origin'):
        if required not in canonical:
            raise ValueError('Canonical WS protection/upgrade is incomplete: ' + required)
    alias = canonical[:match.start(1) - match.start()] + '/ws' + canonical[match.end(1) - match.start():]
    if '/ws' in blocks:
        if blocks['/ws'][2] != alias:
            raise ValueError('Existing /ws differs from the canonical policy; inspect instead of overwriting.')
        return text
    return text[:end] + '\n\n' + alias + text[end:]


def atomic_write(path, content, mode):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, mode)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists(): temporary.unlink()


def apply(path=DEFAULT):
    if os.getuid() != 0 or path != DEFAULT:
        raise ValueError('Apply only with sudo on the cloud, using the fixed hkcamera site path.')
    target = path.resolve(strict=True)
    original = target.read_text(encoding='utf-8')
    updated = patch_routes(original)
    if updated == original:
        print('Both routes already share the canonical policy; unchanged.')
        return
    mode = target.stat().st_mode & 0o777
    backup = target.with_name(target.name + '.before-ws-alias-' + str(time.time_ns()))
    shutil.copy2(target, backup)
    try:
        atomic_write(target, updated, mode)
        subprocess.run(['/usr/sbin/nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True)
    except Exception:
        atomic_write(target, original, mode)
        subprocess.run(['/usr/sbin/nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True)
        raise
    print('Added /ws alias with identical origin/auth/upgrade policy. Backup:', backup)
    print('MediaServer, SSH settings, passwords and video locations were not changed.')


def main():
    parser = argparse.ArgumentParser(description='Check first; --apply requires cloud sudo.')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        apply()
    else:
        original = DEFAULT.read_text(encoding='utf-8')
        changed = patch_routes(original) != original
        print('CHECK ONLY:', 'legacy /ws alias needed' if changed else 'both paths consistent')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Gateway action failed:', type(error).__name__, str(error) if isinstance(error, ValueError) else '')
        raise SystemExit(1)
