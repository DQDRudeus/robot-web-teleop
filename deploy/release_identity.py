"""Content identity for the maintained incremental runtime, never credentials."""
import argparse
import hashlib
import json
from pathlib import Path

FILES = (
    'backend/app.py', 'backend/turtle_bridge.py', 'backend/turtle_worker.py',
    'backend/am3008_worker.py',
    'backend/video_quality.py',
    'backend/sdk_encoder.py', 'cpp/hik_encoder.cpp', 'cpp/Makefile',
    'web/index.html', 'web/input-controls.js', 'web/lucide.min.js', 'web/lucide-LICENSE.txt',
    'web/driving-settings.js',
    'deploy/stack.py', 'deploy/release_identity.py', 'deploy/start_am3008_control.py',
    'deploy/motion_runtime.py',
    'deploy/desktop_launcher.py', 'deploy/assets/hkcamera-control.svg',
    'deploy/repair_camera_ui.py',
    'public-control/gateway_routes.py',
    'requirements-control.txt',
)


def hashes(root, names=FILES):
    root = Path(root).resolve()
    result = {}
    for name in names:
        path = root / name
        if path.resolve() != path.absolute() or not path.is_file():
            raise ValueError('Missing file or symlink in release: ' + name)
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def code_id(files):
    data = json.dumps(files, sort_keys=True, separators=(',', ':')).encode('ascii')
    return hashlib.sha256(data).hexdigest()


def verify_tree(root):
    root = Path(root)
    path = root / 'manifest.json'
    if not path.is_file():
        return dict(status='unversioned', code_id=None, changed=[])
    manifest = json.loads(path.read_text(encoding='utf-8'))
    expected = manifest.get('files', {})
    if (manifest.get('kind') != 'integrated-incremental'
            or set(expected) != set(FILES)
            or manifest.get('code_id') != code_id(expected)):
        raise ValueError('Unrecognized or inconsistent release manifest; do not infer deployment.')
    changed = []
    for name, digest in expected.items():
        try:
            actual = hashes(root, (name,))[name]
        except ValueError:
            actual = None
        if actual != digest:
            changed.append(name)
    return dict(status='matching' if not changed else 'modified',
                code_id=manifest['code_id'], changed=changed)


def main():
    parser = argparse.ArgumentParser(description='Verify installed runtime bytes, not running processes.')
    parser.add_argument('root', nargs='?', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = verify_tree(args.root)
    print(json.dumps(result, indent=2))
    print('File verification is NOT proof that the running process uses this release.')
    return 0 if result['status'] == 'matching' else 1


if __name__ == '__main__':
    raise SystemExit(main())
