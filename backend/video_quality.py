"""Shared source-quality policy using native HCNetSDK, never HTTP fallback."""
import asyncio
import json
import math
import os
from pathlib import Path
import tempfile
import time
import xml.etree.ElementTree as ET

if __package__:
    from .sdk_encoder import SDKEncoder as CameraEncoder, SDKError
else:
    from sdk_encoder import SDKEncoder as CameraEncoder, SDKError

PRESETS = {
    "low": {"width": 640, "height": 360, "fps": 8, "kbps": 384},
    "balanced": {"width": 1280, "height": 720, "fps": 15, "kbps": 1024},
    "high": {"width": 1920, "height": 1080, "fps": 25, "kbps": 2048},
}


class VideoQuality:
    def __init__(self, encoder=None, path=None):
        self.encoder = encoder
        self.path = Path(path) if path else Path.home() / '.config/hkcamera/video-quality.json'
        self.policy = {'auto': False, 'profile': 'balanced'}
        self.current = None
        self.resolutions = []
        self.error = ''
        self.busy = False
        self.task = None
        self.pending = None
        self.refresh_requested = False
        self.level = 1
        self.bad = self.good = 0
        self.changed_at = 0.0
        self.sample_at = 0.0
        self.auth_blocked = False
        self.retry_at = 0.0

    def status(self):
        return dict(auto=self.policy['auto'], profile=self.policy['profile'],
                    current=self.current, resolutions=self.resolutions, error=self.error,
                    busy=self.busy, source='hcnetsdk', shared=True,
                    feedback_fresh=time.monotonic() - self.sample_at < 10)

    async def start(self):
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            self.validate(saved)
            self.policy = saved
            self.pending = dict(saved)
        except (OSError, ValueError, TypeError):
            pass
        self.refresh_requested = True
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    @staticmethod
    def validate(message):
        if not isinstance(message, dict):
            raise ValueError('Invalid video policy')
        if type(message.get('auto')) is not bool or message.get('profile') not in (*PRESETS, 'custom'):
            raise ValueError('Invalid video policy')
        if message['profile'] == 'custom':
            for field, minimum, maximum in [('width', 160, 8192), ('height', 90, 8192),
                                             ('fps', 1, 30), ('kbps', 64, 8192)]:
                if type(message.get(field)) is not int or not minimum <= message[field] <= maximum:
                    raise ValueError('Invalid video ' + field)

    def configure(self, message):
        try:
            self.validate(message)
        except (ValueError, TypeError) as error:
            self.error = str(error)
            return
        self.pending = {k: message[k] for k in ('auto', 'profile', 'width', 'height', 'fps', 'kbps') if k in message}
        self.auth_blocked = False
        self.retry_at = 0.0

    def refresh(self):
        self.auth_blocked = False
        self.refresh_requested = True
        self.retry_at = 0.0

    def reset_feedback(self):
        self.bad = self.good = 0
        self.sample_at = 0.0

    def report(self, message):
        now = time.monotonic()
        if now - self.sample_at < 1 or message.get('active') is not True:
            return
        loss, rtt = message.get('loss'), message.get('rtt')
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in (loss, rtt)):
            return
        if not 0 <= loss <= 1 or not 0 <= rtt <= 30 or type(message.get('stall')) is not bool:
            return
        if now - self.sample_at > 10:
            self.bad = self.good = 0
        self.sample_at = now
        if not self.policy['auto'] or self.current is None or self.busy or self.error:
            return
        bad = loss > 0.06 or rtt > 0.5 or message['stall']
        self.bad = self.bad + 1 if bad else 0
        self.good = self.good + 1 if not bad and loss < 0.01 and rtt < 0.2 else 0
        if self.bad >= 3 and self.level > 0 and now - self.changed_at >= 20:
            self.pending = dict(self.policy, adaptive_level=self.level - 1)
            self.bad = self.good = 0
        elif self.good >= 15 and self.level < self.ceiling() and now - self.changed_at >= 60:
            self.pending = dict(self.policy, adaptive_level=self.level + 1)
            self.bad = self.good = 0

    def ceiling(self):
        return ['low', 'balanced', 'high'].index(self.policy['profile']) if self.policy['profile'] != 'custom' else 2

    def target(self, policy):
        ceiling = {k: policy[k] for k in ('width', 'height', 'fps', 'kbps')} if policy['profile'] == 'custom' else dict(PRESETS[policy['profile']])
        if 'adaptive_level' not in policy:
            target = ceiling
        else:
            preset = list(PRESETS.values())[policy['adaptive_level']]
            target = {k: min(ceiling[k], preset[k]) for k in ceiling}
        candidates = [r for r in self.resolutions if r['width'] <= target['width'] and r['height'] <= target['height']]
        if policy['profile'] != 'custom' or 'adaptive_level' in policy:
            if not candidates:
                # Keep the camera's supported smallest resolution; bitrate/fps
                # still reduce uplink. Never pretend an unsupported size applied.
                candidates = self.resolutions[:1]
            if not candidates:
                raise ValueError('No supported resolution reported')
            target.update(max(candidates, key=lambda r: r['width'] * r['height']))
        elif {'width': target['width'], 'height': target['height']} not in self.resolutions:
            raise ValueError('Unsupported resolution pair')
        return target

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                json.dump(self.policy, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    async def once(self):
        if not self.pending and not self.refresh_requested:
            return
        if self.auth_blocked:
            return
        if time.monotonic() < self.retry_at:
            return
        self.busy = True
        policy, self.pending = self.pending, None
        self.refresh_requested = False
        try:
            if self.encoder is None:
                self.encoder = CameraEncoder()
            self.current, self.resolutions = await asyncio.to_thread(self.encoder.inspect)
            if policy:
                target = self.target(policy)
                if policy['profile'] != 'custom' or 'adaptive_level' in policy:
                    adapt = getattr(self.encoder, 'adapt', None)
                    if adapt:
                        target = adapt(target)
                self.current = await asyncio.to_thread(self.encoder.apply, target)
                self.policy = {k: v for k, v in policy.items() if k != 'adaptive_level'}
                self.level = policy.get('adaptive_level', self.ceiling())
                self.changed_at = time.monotonic()
                self.bad = self.good = 0
                await asyncio.to_thread(self.save)
            self.error = ''
        except SDKError as error:
            self.auth_blocked = error.stage == 'login' and error.code in (1, 153)
            self.error = f'HCNetSDK {error.stage} 失败，错误码 {error.code}'
            if not error.rollback_verified:
                self.error += '；旧配置恢复未确认，请重新读取'
            if error.code in (7,8,9,10):
                self.pending = self.pending or policy
                self.refresh_requested = True
                self.retry_at = time.monotonic() + 30
        except (OSError, ValueError, ET.ParseError) as error:
            self.error = '摄像头编码参数不可用或设备不支持所选配置，请检查连接并重新读取参数'
            if 'HCNetSDK' in str(error):
                self.error = str(error)
            if 'rollback unverified' in str(error):
                self.error = '编码修改失败，旧配置恢复尚未确认，请重新读取摄像头参数'
            elif isinstance(error, OSError):
                # Camera may power on after NX. Keep retrying saved policy with
                # a delay, without restarting controls or flooding device login.
                self.pending = self.pending or policy
                self.refresh_requested = True
                self.retry_at = time.monotonic() + 30
        finally:
            self.busy = False

    async def run(self):
        while True:
            try:
                await self.once()
            except Exception as error:
                # Keep a malformed vendor response from permanently killing
                # the video-settings worker. Never log credentials or XML.
                print('[video_quality] retry after ' + type(error).__name__, flush=True)
                self.error = '画质设置暂时失败，正在等待重试'
                self.refresh_requested = True
                self.retry_at = time.monotonic() + 30
            await asyncio.sleep(1)
