"""Mode switching safety without sockets, ROS installations or real devices."""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent / "backend"))
spec = importlib.util.spec_from_file_location("motion_modes_test_app", Path(__file__).parent / "backend" / "app.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
from turtle_bridge import TurtleBridge


class ModeSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_recovery_recreates_only_bridge_and_revokes_old_motion(self):
        with patch.dict(backend.os.environ, {'MOTION_LIFECYCLE_MANAGED':'1'}), \
             patch.object(backend, 'motion_recovering', False):
            self.bridge.connected = False
            self.assertTrue(await backend.recover_motion_once())
            self.assertIsNone(backend.lease.operator_id)
            self.assertFalse(backend.motion_recovering)
            self.assertEqual(self.calls, ['close', 'start:am3008', 'wait', 'stop:motion_reconnected'])

    async def test_recovery_failure_is_retryable_and_does_not_restart_camera(self):
        with patch.dict(backend.os.environ, {'MOTION_LIFECYCLE_MANAGED':'1'}), \
             patch.object(backend, 'motion_recovering', False):
            self.bridge.connected = False
            self.bridge.wait_connected.side_effect = RuntimeError('serial not ready')
            self.assertFalse(await backend.recover_motion_once())
            self.assertFalse(backend.motion_recovering)
            self.assertIn('serial not ready', backend.motion_mode_error)
            self.bridge.wait_connected.side_effect = None
            self.bridge.wait_connected.return_value = None
            self.assertTrue(await backend.recover_motion_once())
            self.assertEqual(backend.motion_mode_error, '')

    async def test_healthy_or_manual_switch_is_not_restarted_by_recovery(self):
        with patch.dict(backend.os.environ, {'MOTION_LIFECYCLE_MANAGED':'1'}), \
             patch.object(backend, 'motion_recovering', False):
            self.assertFalse(await backend.recover_motion_once())
            self.bridge.connected = False
            with patch.object(backend, 'motion_mode_switching', True):
                self.assertFalse(await backend.recover_motion_once())
            self.assertEqual(self.calls, [])

    async def test_closed_websocket_runtime_error_is_normal_disconnect(self):
        from starlette.websockets import WebSocketState, WebSocketDisconnect
        for side in ('application_state', 'client_state'):
            socket = SimpleNamespace(receive_json=AsyncMock(side_effect=RuntimeError('not connected')),
                                     application_state=WebSocketState.CONNECTED, client_state=WebSocketState.CONNECTED)
            setattr(socket, side, WebSocketState.DISCONNECTED)
            with self.assertRaises(WebSocketDisconnect):
                await backend.receive_control_message(socket)
        socket.application_state = socket.client_state = WebSocketState.CONNECTED
        with self.assertRaises(RuntimeError):
            await backend.receive_control_message(socket)

    async def asyncSetUp(self):
        self.events = []
        self.calls = []

        async def broadcast(event):
            self.events.append(event)

        async def close():
            self.calls.append("close")
            self.bridge.ready = self.bridge.connected = False

        async def start(target):
            self.calls.append("start:" + target)
            self.bridge.target = target
            self.bridge.ready = True

        async def wait():
            self.calls.append("wait")
            self.bridge.connected = True

        self.bridge = SimpleNamespace(target="am3008", ready=True, connected=True,
                                      runtime_error="", close=AsyncMock(side_effect=close),
                                      start=AsyncMock(side_effect=start), wait_connected=AsyncMock(side_effect=wait), probe=AsyncMock())
        self.patches = [patch.object(backend, "hub", SimpleNamespace(broadcast=broadcast)),
                        patch.object(backend, "lease", backend.ControlLease()),
                        patch.object(backend, "control_lock", asyncio.Lock()),
                        patch.object(backend, "turtle_bridge", self.bridge),
                        patch.object(backend, "motion_mode_switching", False),
                        patch.object(backend, "motion_mode_error", ""),
                        patch.object(backend, "stop_all", AsyncMock(side_effect=lambda reason: self.calls.append("stop:" + reason)))]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        backend.lease.claim("owner", object())

    async def test_competing_controller_preflight_does_not_spawn_worker(self):
        with patch.dict(backend.os.environ, {'MOTION_LIFECYCLE_MANAGED':'1'}), patch.object(backend, 'motion_recovering', False):
            self.bridge.connected = False
            self.bridge.probe.side_effect = RuntimeError('Another controller')
            self.assertFalse(await backend.recover_motion_once())
            self.bridge.start.assert_not_awaited()

    async def test_recovery_loop_retries_with_backoff(self):
        delays = []
        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError()
        self.bridge.connected = False
        self.bridge.runtime_error = 'serial late'
        with patch.object(backend.asyncio, 'sleep', side_effect=sleep), patch.object(backend, 'recover_motion_once', AsyncMock(return_value=False)) as retry:
            with self.assertRaises(asyncio.CancelledError):
                await backend.motion_recovery_loop()
        self.assertEqual(delays, [5, 10, 20])
        self.assertEqual(retry.await_count, 2)

    async def test_viewer_and_invalid_target_cannot_change_or_stop_mode(self):
        result = await backend.switch_motion_mode("viewer", "turtlesim")
        self.assertEqual(result["reason"], "operator_required")
        result = await backend.switch_motion_mode("owner", "shell-command")
        self.assertEqual(result["reason"], "invalid_target")
        self.assertEqual(self.calls, [])
        self.assertEqual(backend.lease.operator_id, "owner")

    async def test_switch_zeros_old_target_and_requires_new_lease(self):
        result = await backend.switch_motion_mode("owner", "turtlesim")
        self.assertEqual(result, dict(type="motion_mode", target="turtlesim", state="ready"))
        self.assertEqual(self.calls, ["stop:mode_switch", "close", "start:turtlesim", "wait"])
        self.assertIsNone(backend.lease.operator_id)
        self.assertFalse(backend.motion_mode_switching)
        self.assertEqual(self.events[0], dict(type="control_state", operator=None))
        self.assertEqual(self.events[-1]["status"]["control"]["motion_target"], "turtlesim")

    async def test_failed_new_node_remains_stopped_no_auto_rollback(self):
        self.bridge.wait_connected.side_effect = RuntimeError("serial device missing")
        result = await backend.switch_motion_mode("owner", "turtlesim")
        self.assertEqual(result["state"], "error")
        self.assertIn("serial device missing", result["reason"])
        self.assertEqual(self.bridge.close.await_count, 2)
        self.assertIsNone(backend.lease.operator_id)
        self.assertFalse(self.bridge.connected)
        self.assertFalse(backend.motion_mode_switching)
        self.assertEqual(self.bridge.start.await_count, 1)

    async def test_concurrent_switch_rejected_and_cancellation_cleans_worker(self):
        waiting = asyncio.Event()

        async def block():
            waiting.set()
            await asyncio.Future()

        self.bridge.wait_connected.side_effect = block
        first = asyncio.create_task(backend.switch_motion_mode("owner", "turtlesim"))
        await waiting.wait()
        result = await backend.switch_motion_mode("owner", "am3008")
        self.assertEqual(result["reason"], "mode_switching")
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(self.bridge.close.await_count, 2)
        self.assertFalse(backend.motion_mode_switching)
        self.assertIsNone(backend.lease.operator_id)


class BridgeModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwarded_subscription_waits_for_runtime_validation(self):
        import json
        import time
        hub = SimpleNamespace(broadcast=AsyncMock())
        bridge = TurtleBridge(hub)
        bridge.runtime_proc = SimpleNamespace(returncode=None)
        stream = asyncio.StreamReader()
        for event in ({"type": "turtle_ready"}, {"type": "turtle_connection", "connected": True,
                                                "sampled_at": time.time() * 1000}):
            stream.feed_data((json.dumps(event) + "\n").encode())
        stream.feed_eof()
        await bridge.read_events(SimpleNamespace(stdout=stream))
        relayed = [call.args[0] for call in hub.broadcast.await_args_list]
        self.assertFalse(next(event for event in relayed if event["type"] == "turtle_connection")["connected"])

    async def test_supervisor_failure_zeros_and_closes_worker_input(self):
        import json
        hub = SimpleNamespace(broadcast=AsyncMock())
        bridge = TurtleBridge(hub)
        bridge.ready = bridge.linked = bridge.runtime_ready = True
        stdin = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock())
        bridge.proc = SimpleNamespace(returncode=None, stdin=stdin)
        stream = asyncio.StreamReader()
        stream.feed_data(b'{"type":"motion_runtime_error","reason":"conflicting controller"}\n')
        stream.feed_eof()
        await bridge.read_runtime(SimpleNamespace(stdout=stream), generation=0)
        zero = json.loads(stdin.write.call_args.args[0])
        self.assertEqual((zero["linear"], zero["angular"]), (0, 0))
        stdin.close.assert_called_once()
        self.assertFalse(bridge.connected)
        self.assertEqual(bridge.runtime_error, "conflicting controller")
        self.assertTrue(any(call.args[0].get("connected") is False for call in hub.broadcast.await_args_list))

    async def test_switch_uses_explicit_child_environment_without_mutating_parent(self):
        hub = SimpleNamespace(broadcast=AsyncMock())
        bridge = TurtleBridge(hub)
        stream = asyncio.StreamReader()
        stream.feed_eof()
        child = SimpleNamespace(stdout=stream)
        with patch.dict("os.environ", {"MOTION_TARGET": "am3008", "MOTION_LIFECYCLE_MANAGED": "0", "ROS_DOMAIN_ID": "0"}), \
             patch("turtle_bridge.asyncio.create_subprocess_exec", AsyncMock(return_value=child)) as spawn:
            await bridge.start("turtlesim")
            import os
            self.assertEqual(os.environ["MOTION_TARGET"], "am3008")
            self.assertEqual(spawn.call_args.kwargs["env"]["MOTION_TARGET"], "turtlesim")
            self.assertEqual(spawn.call_args.kwargs["env"]["ROS_DOMAIN_ID"], "0")
            self.assertTrue(spawn.call_args.args[2].endswith("turtle_worker.py"))
            await bridge.task

    async def test_old_worker_cannot_clear_new_generation_readiness(self):
        bridge = TurtleBridge(SimpleNamespace(broadcast=AsyncMock()))
        bridge.generation = 2
        bridge.ready = bridge.linked = True
        stream = asyncio.StreamReader()
        stream.feed_eof()
        await bridge.read_events(SimpleNamespace(stdout=stream), generation=1)
        self.assertTrue(bridge.ready)
        self.assertTrue(bridge.linked)

    async def test_managed_subscriber_is_not_ready_before_supervisor_validation(self):
        import time
        bridge = TurtleBridge(None)
        bridge.ready = bridge.linked = True
        bridge.link_time = time.monotonic()
        bridge.runtime_proc = SimpleNamespace(returncode=None)
        self.assertFalse(bridge.connected)
        bridge.runtime_ready = True
        self.assertTrue(bridge.connected)
        bridge.runtime_proc.returncode = 1
        self.assertFalse(bridge.connected)


if __name__ == "__main__":
    unittest.main()
