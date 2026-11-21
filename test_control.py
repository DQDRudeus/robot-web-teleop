import asyncio
import os
from unittest.mock import patch
import importlib.util
import io
import unittest
import tempfile
import time
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from contextlib import redirect_stdout

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).parent / "backend"))
spec = importlib.util.spec_from_file_location("hkcamera_test_app", Path(__file__).parent / "backend" / "app.py")
backend = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {"TEST_OPERATOR_TOKEN": "test"}):
    spec.loader.exec_module(backend)


class FakeCamera:
    state = "logged_in"
    last_status = ""
    continuous_ptz = True

    def __init__(self):
        self.commands = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, command):
        return True

    async def ptz(self, command, speed, duration):
        self.commands.append((command, speed, duration))
        return True


def receive(ws, kind, predicate=lambda event: True):
    for _ in range(20):
        event = ws.receive_json()
        if event["type"] == kind and predicate(event):
            return event
    raise AssertionError(f"No {kind} event received")


class ControlTests(unittest.TestCase):
    def setUp(self):
        backend.hub = backend.EventHub()
        backend.lease = backend.ControlLease()
        backend.control_lock = asyncio.Lock()
        backend.camera = FakeCamera()
        backend.base_adapter = backend.BaseControlAdapter(backend.hub)
        backend.turtle_bridge = backend.TurtleBridge(backend.hub)
        backend.control_deadline = 0.0
        backend.channel_deadlines.update(ptz=0.0, base=0.0)
        backend.OPERATOR_TOKEN = "custom-test-token"
        self.credentials = tempfile.TemporaryDirectory()
        backend.TOKEN_FILE = Path(self.credentials.name) / "operator-token.json"
        backend.TOKEN_RECORD = None
        backend.video_quality = backend.VideoQuality(path=Path(self.credentials.name) / 'video.json')
        self.client = TestClient(backend.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.credentials.cleanup()

    def claim(self, ws):
        ws.send_json({"type": "claim_control", "operator_token": "custom-test-token"})
        return receive(ws, "control")

    def test_custom_token_and_capability(self):
        with self.client.websocket_connect("/ws/control") as ws:
            hello = receive(ws, "hello")
            self.assertEqual(hello["status"]["camera"]["ptz_mode"], "continuous")
            self.assertTrue(self.claim(ws)["ok"])
            ws.send_json({"type": "ptz", "seq": 1, "pan": -0.35, "tilt": 0, "zoom": 0, "timestamp": time.time() * 1000})
            ws.send_json({"type": "status"})
            receive(ws, "status")
            self.assertEqual(backend.camera.commands[-1], ("left", 3, 300))

    def test_ptz_is_independent_of_disconnected_test_base_on_both_routes(self):
        backend.turtle_bridge.target = 'turtlesim'
        backend.turtle_bridge.ready = False
        for path in ('/ws/control', '/ws'):
            with self.subTest(path=path), self.client.websocket_connect(path) as ws:
                receive(ws, 'hello')
                self.assertTrue(self.claim(ws)['ok'])
                ws.send_json({'type': 'ptz', 'seq': 1, 'pan': .6,
                              'timestamp': time.time() * 1000})
                ws.send_json({'type': 'status'})
                receive(ws, 'status')
                self.assertEqual(backend.camera.commands[-1][0], 'right')

    def test_input_script_is_versioned_and_not_cached(self):
        response = self.client.get('/static/input-controls.js?v=20261111-ptz-local-video-1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('input-controls.js?v=20261111-ptz-local-video-1', self.client.get('/').text)

    def test_both_ws_paths_advertise_the_same_contract_and_control_ptz(self):
        for path in ('/ws/control', '/ws'):
            with self.subTest(path=path), self.client.websocket_connect(path) as ws:
                hello = receive(ws, 'hello')
                self.assertEqual(hello['status']['protocol']['control_path'], '/ws/control')
                self.assertTrue(self.claim(ws)['ok'])
                ws.send_json({'type': 'ptz', 'seq': 1, 'pan': -0.35, 'tilt': 0,
                              'zoom': 0, 'timestamp': time.time() * 1000})
                ws.send_json({'type': 'status'})
                receive(ws, 'status')
                self.assertEqual(backend.camera.commands[-1], ('left', 3, 300))

    def test_incorrect_token_denied(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            ws.send_json({"type": "claim_control", "operator_token": "camera-password-is-not-a-token"})
            self.assertEqual(receive(ws, "control")["reason"], "invalid_token")
            self.assertIsNone(backend.lease.operator_id)

    def test_second_operator_and_viewer_commands_denied(self):
        with self.client.websocket_connect("/ws/control") as owner, self.client.websocket_connect("/ws/control") as viewer:
            receive(owner, "hello")
            viewer_id = receive(viewer, "hello")["client_id"]
            self.assertTrue(self.claim(owner)["ok"])
            self.assertFalse(self.claim(viewer)["ok"])
            self.assertNotEqual(backend.lease.operator_id, viewer_id)
            viewer.send_json({"type": "ptz", "seq": 1, "pan": 0.4, "tilt": 0, "zoom": 0, "timestamp": time.time() * 1000})
            self.assertIn("viewer", receive(viewer, "error")["message"])

    def test_release_stops_owner(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            self.claim(ws)
            ws.send_json({"type": "ptz", "seq": 1, "pan": -.4, "timestamp": time.time() * 1000})
            ws.send_json({"type": "release_control"})
            receive(ws, "control_state", lambda event: event["operator"] is None)
            self.assertEqual(backend.camera.commands[-1][0], "stop")
            self.assertIsNone(backend.lease.operator_id)

    def test_owner_disconnect_stops_but_viewer_release_does_not(self):
        with self.client.websocket_connect("/ws/control") as viewer:
            receive(viewer, "hello")
            with self.client.websocket_connect("/ws/control") as owner:
                owner_id = receive(owner, "hello")["client_id"]
                self.claim(owner)
                receive(viewer, "control_state", lambda event: event["operator"] == owner_id)
                viewer.send_json({"type": "release_control"})
                receive(viewer, "control_state", lambda event: event["operator"] == owner_id)
                self.assertEqual(backend.camera.commands, [])
            receive(viewer, "control_state", lambda event: event["operator"] is None)
            self.assertEqual(backend.camera.commands, [("stop", 1, 50)])

    def test_http_release_requires_token(self):
        with self.client.websocket_connect("/ws/control") as ws:
            client_id = receive(ws, "hello")["client_id"]
            self.claim(ws)
            headers = {"x-client-id": client_id}
            self.assertEqual(self.client.post("/api/control/release", headers=headers).status_code, 403)
            self.assertEqual(backend.lease.operator_id, client_id)
            headers["x-operator-token"] = "custom-test-token"
            forbidden = {**headers, 'x-operator-token':'test'}
            self.assertEqual(self.client.post('/api/control/release', headers=forbidden).status_code,403)
            self.assertEqual(backend.lease.operator_id,client_id)
            self.assertEqual(self.client.post("/api/control/release", headers=headers).status_code, 200)
            self.assertEqual(backend.camera.commands[-1][0], "stop")

    def test_invalid_ptz_parameters_keep_connection(self):
        with self.client.websocket_connect("/ws") as ws:
            receive(ws, "hello")
            self.claim(ws)
            ws.send_json({"type": "ptz", "command": "left", "speed": "invalid"})
            self.assertEqual(receive(ws, "error")["message"], "invalid PTZ parameters")
            ws.send_json({"type": "status"})
            receive(ws, "status")
            self.assertEqual(backend.camera.commands, [])

    def test_unicode_operator_token(self):
        backend.OPERATOR_TOKEN = "\u81ea\u5b9a\u4e49\u53e3\u4ee4"
        self.assertTrue(backend.valid_operator_token(backend.OPERATOR_TOKEN))
        self.assertFalse(backend.valid_operator_token("hkcamera"))

    def test_change_password_persists_hash_and_stops_operator(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            self.claim(ws)
            ws.send_json({"type": "change_token", "current_token": "custom-test-token", "new_token": "new-custom-password"})
            self.assertTrue(receive(ws, "token_changed")["ok"])
            receive(ws, "control_state", lambda event: event["operator"] is None)
            self.assertEqual(backend.camera.commands[-1][0], "stop")
            self.assertFalse(backend.valid_operator_token("custom-test-token"))
            self.assertTrue(backend.valid_operator_token("new-custom-password"))
            self.assertNotIn("new-custom-password", backend.TOKEN_FILE.read_text())
            backend.TOKEN_RECORD = backend.load_token_record()
            self.assertTrue(backend.valid_operator_token("new-custom-password"))
            ws.send_json({"type": "claim_control", "operator_token": "new-custom-password"})
            self.assertTrue(receive(ws, "control")["ok"])

    def test_change_password_rejects_bad_current_and_short_new(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            for current, new, reason in [("wrong", "new-password", "invalid_token"), ("custom-test-token", "short", "invalid_length")]:
                ws.send_json({"type": "change_token", "current_token": current, "new_token": new})
                self.assertEqual(receive(ws, "token_changed")["reason"], reason)
            self.assertFalse(backend.TOKEN_FILE.exists())
            self.assertTrue(backend.valid_operator_token("custom-test-token"))

    def test_change_password_save_failure_keeps_old_password(self):
        from unittest.mock import patch
        with self.client.websocket_connect("/ws") as ws, patch.object(backend, "save_operator_token", side_effect=OSError):
            receive(ws, "hello")
            ws.send_json({"type": "change_token", "current_token": "custom-test-token", "new_token": "new-password"})
            self.assertEqual(receive(ws, "token_changed")["reason"], "save_failed")
            self.assertTrue(backend.valid_operator_token("custom-test-token"))

    def test_old_daemon_cannot_receive_hold_heartbeats(self):
        daemon = backend.CameraDaemon(backend.hub)
        daemon.send = AsyncMock(return_value=True)
        self.assertFalse(asyncio.run(daemon.ptz("left", 3, 600)))
        daemon.send.assert_not_awaited()
        daemon.continuous_ptz = True
        self.assertTrue(asyncio.run(daemon.ptz("left", 100, 10000)))
        daemon.send.assert_awaited_once_with("ptz left 7 3000")

    def test_watchdog_stops_without_browser_stop(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            self.claim(ws)
            ws.send_json({"type": "ptz", "seq": 1, "pan": .4, "timestamp": time.time() * 1000})
            event = receive(ws, "motion_stop", lambda row: row.get("reason") == "timeout")
            self.assertEqual(event["channel"], "ptz")
            self.assertEqual(backend.camera.commands[-1][0], "stop")

    def test_invalid_axes_timestamps_sequences_never_dispatch(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            self.claim(ws)
            for extra in ({"pan": 2}, {"seq": True}, {"seq": 1.5}, {"timestamp": 0}, {"timestamp": time.time() * 1000 + 10000}):
                msg = {"type": "ptz", "seq": 1, "pan": .2, "timestamp": time.time() * 1000, **extra}
                ws.send_json(msg)
                receive(ws, "error")
            self.assertEqual(backend.camera.commands, [])
            msg = {"type": "ptz", "seq": 1, "pan": .2, "timestamp": time.time() * 1000}
            ws.send_json(msg)
            ws.send_json(msg)
            self.assertIn("repeated", receive(ws, "error")["message"])
            self.assertEqual(len(backend.camera.commands), 1)

    def test_unconfigured_base_never_outputs_nonzero(self):
        output = io.StringIO()
        with redirect_stdout(output), self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            self.claim(ws)
            ws.send_json({"type": "base", "seq": 1, "linear": .5, "angular": -.2, "timestamp": time.time() * 1000})
            event = receive(ws, "base_received")
            self.assertEqual(event["message"]["linear"], .5)
            self.assertFalse(event["executed"])
            self.assertIn('[BASE_RX]', output.getvalue())
            self.assertEqual(backend.base_adapter.last_target, {"forward": 0, "turn": 0})

    def test_base_receipt_does_not_require_motion_format_ros_or_camera(self):
        backend.camera.state = 'offline'
        backend.camera.continuous_ptz = False
        backend.base_adapter.motion = AsyncMock(side_effect=AssertionError('hardware adapter must not be called'))
        with redirect_stdout(io.StringIO()), self.client.websocket_connect('/ws/control') as ws:
            receive(ws, 'hello')
            self.claim(ws)
            ws.send_json({'type':'base', 'test':'arbitrary', 'timestamp':0})
            event = receive(ws, 'base_received')
            self.assertEqual(event['message']['test'], 'arbitrary')
            self.assertFalse(event['executed'])
            backend.base_adapter.motion.assert_not_awaited()

    def test_viewer_base_messages_are_not_acknowledged_as_received_targets(self):
        with redirect_stdout(io.StringIO()) as output, self.client.websocket_connect('/ws/control') as ws:
            receive(ws, 'hello')
            ws.send_json({'type':'base','linear':1})
            self.assertIn('viewer', receive(ws, 'error')['message'])
            self.assertNotIn('[BASE_RX]', output.getvalue())

    def test_selected_speed_applies_to_pan_and_zoom(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            self.claim(ws)
            for seq, axes, speed, command in [(1, {"pan": .2}, 6, "right"), (2, {"zoom": -.3}, 2, "zoom_out")]:
                ws.send_json({"type": "ptz", "seq": seq, **axes, "speed": speed, "timestamp": time.time() * 1000})
                ws.send_json({"type": "status"})
                receive(ws, "status")
                self.assertEqual(backend.camera.commands[-1], (command, speed, 300))
            ws.send_json({"type":"ptz", "seq":3, "zoom":.4, "speed":8, "timestamp":time.time()*1000})
            receive(ws,"error")
            self.assertEqual(len(backend.camera.commands),2)

    def test_turtle_forwarded_after_receipt(self):
        backend.turtle_bridge.ready = True
        backend.turtle_bridge.linked = True
        backend.turtle_bridge.link_time = time.monotonic()
        backend.turtle_bridge.motion = AsyncMock(return_value=True)
        backend.turtle_bridge.stop = AsyncMock()
        with self.client.websocket_connect("/ws") as ws:
            client = receive(ws, "hello")["client_id"]
            self.claim(ws)
            ws.send_json({"type": "base", "seq": 15, "linear": .5, "angular": -.3, "timestamp": time.time() * 1000})
            self.assertFalse(receive(ws, "base_received")["executed"])
            ws.send_json({"type": "status"})
            receive(ws, "status")
            backend.turtle_bridge.motion.assert_awaited_once_with(client, 15, .125, -.3)
        backend.turtle_bridge.stop.assert_awaited()

    def test_ready_bridge_without_device_is_not_connected(self):
        backend.turtle_bridge.ready = True
        backend.turtle_bridge.motion = AsyncMock(return_value=True)
        backend.turtle_bridge.stop = AsyncMock()
        with self.client.websocket_connect("/ws") as ws:
            hello = receive(ws, "hello")
            self.assertFalse(hello["status"]["control"]["base_connected"])
            self.claim(ws)
            ws.send_json({"type": "base", "seq": 1, "linear": 1, "angular": 0, "timestamp": time.time() * 1000})
            receive(ws, "base_received")
            self.assertEqual(receive(ws, "turtle_rejected")["reason"], "disconnected")
            backend.turtle_bridge.motion.assert_not_awaited()

    def test_invalid_turtle_target_is_received_but_never_forwarded(self):
        backend.turtle_bridge.ready = True
        backend.turtle_bridge.linked = True
        backend.turtle_bridge.link_time = time.monotonic()
        backend.turtle_bridge.motion = AsyncMock(return_value=True)
        backend.turtle_bridge.stop = AsyncMock()
        with self.client.websocket_connect("/ws") as ws:
            receive(ws, "hello")
            self.claim(ws)
            for fields in ({"linear": 4}, {"angular": float("inf")}, {"timestamp": 0}, {"seq": True}):
                message = {"type": "base", "seq": 1, "linear": .5, "angular": 0, "timestamp": time.time() * 1000}
                message.update(fields)
                ws.send_json(message)
                receive(ws, "base_received")
                receive(ws, "turtle_rejected")
            backend.turtle_bridge.motion.assert_not_awaited()
            self.assertEqual(backend.turtle_bridge.stop.await_count, 4)

    def test_viewer_never_reaches_turtle_bridge(self):
        backend.turtle_bridge.ready = True
        backend.turtle_bridge.motion = AsyncMock()
        backend.turtle_bridge.stop = AsyncMock()
        with self.client.websocket_connect("/ws") as ws:
            receive(ws, "hello")
            ws.send_json({"type": "base", "seq": 1, "linear": 1, "angular": 0, "timestamp": time.time() * 1000})
            receive(ws, "error")
            backend.turtle_bridge.motion.assert_not_awaited()

    def test_test_role_can_be_replaced_but_advanced_cannot(self):
        with self.client.websocket_connect('/ws/control') as low, self.client.websocket_connect('/ws/control') as advanced, self.client.websocket_connect('/ws/control') as another:
            receive(low, 'hello')
            advanced_id = receive(advanced, 'hello')['client_id']
            receive(another, 'hello')
            low.send_json({'type':'claim_control', 'operator_token':'test'})
            self.assertEqual(receive(low, 'control')['role'], 'test')
            self.assertTrue(self.claim(advanced)['ok'])
            low.send_json({'type':'claim_control', 'operator_token':'test'})
            self.assertEqual(receive(low, 'control')['reason'], 'occupied')
            self.assertFalse(self.claim(another)['ok'])
            self.assertEqual(backend.lease.operator_id, advanced_id)

    def test_test_speed_is_enforced_server_side_and_cannot_change_video(self):
        with self.client.websocket_connect('/ws/control') as ws:
            client = receive(ws, 'hello')['client_id']
            ws.send_json({'type':'claim_control','operator_token':'test'})
            receive(ws, 'control')
            ws.send_json({'type':'speed','scale':1})
            self.assertFalse(receive(ws, 'speed')['ok'])
            backend.turtle_bridge.ready = backend.turtle_bridge.linked = True
            backend.turtle_bridge.link_time = time.monotonic()
            backend.turtle_bridge.motion = AsyncMock(return_value=True)
            ws.send_json({'type':'base','seq':1,'linear':1,'angular':.3,'speed':1,'role':'advanced','timestamp':time.time()*1000})
            ws.send_json({'type':'status'})
            receive(ws,'status')
            backend.turtle_bridge.motion.assert_awaited_once_with(client,1,.25,.3)
            ws.send_json({'type':'video_settings','profile':'high','auto':True})
            self.assertIn('advanced',receive(ws,'error')['message'])
            self.assertIsNone(backend.video_quality.pending)

    def test_advanced_speed_range_and_stop_before_speed_change(self):
        with self.client.websocket_connect('/ws/control') as ws:
            receive(ws,'hello')
            self.claim(ws)
            backend.turtle_bridge.stop = AsyncMock()
            for scale in (True, float('nan'), 2, .1, '1'):
                ws.send_json({'type':'speed','scale':scale})
                self.assertFalse(receive(ws,'speed')['ok'])
            ws.send_json({'type':'speed','scale':.8})
            self.assertTrue(receive(ws,'speed')['ok'])
            self.assertEqual(backend.lease.speed_scale,.8)
            backend.turtle_bridge.stop.assert_awaited_once()

    def test_channel_timeout_cannot_be_extended_by_other_channel(self):
        with self.client.websocket_connect("/ws/control") as ws:
            receive(ws, "hello")
            self.claim(ws)
            ws.send_json({"type": "ptz", "seq": 1, "pan": .5, "timestamp": time.time() * 1000})
            for seq in range(2, 7):
                time.sleep(.08)
                ws.send_json({"type": "base", "seq": seq, "linear": .2, "timestamp": time.time() * 1000})
                receive(ws, "base_received")
            self.assertIn("stop", [row[0] for row in backend.camera.commands])


if __name__ == "__main__":
    unittest.main()
