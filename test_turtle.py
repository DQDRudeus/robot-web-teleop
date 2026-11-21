import asyncio
import copy
import io
import json
import sys
import time
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent / "backend"))
from turtle_worker import LatestTarget, velocity
from turtle_bridge import TurtleBridge
import turtle_worker
import am3008_worker


class TargetTests(unittest.TestCase):
    def test_expiry_without_backend(self):
        target = LatestTarget()
        target.put({"linear": 1, "angular": .5, "seq": 1, "expires": 10.3})
        self.assertEqual(target.snapshot(10)[0]["linear"], 1)
        self.assertEqual(velocity(target.snapshot(10.31)[0]), (0, 0))

    def test_eof_always_stops(self):
        target = LatestTarget()
        target.put({"linear": 1, "angular": 0, "expires": 10.3})
        target.eof = True
        self.assertEqual(velocity(target.snapshot(10)[0]), (0, 0))

    def test_newest_wins_and_future_expiry_rejected(self):
        target = LatestTarget()
        for seq in range(100):
            target.put({"linear": seq / 100, "angular": 0, "seq": seq, "expires": 10.3})
        self.assertEqual(target.snapshot(10)[0]["seq"], 99)
        self.assertEqual(velocity(target.snapshot(8)[0]), (0, 0))

    def test_axes_are_bounded_and_finite(self):
        for value in (True, None, float("nan"), float("inf"), 1.1):
            with self.assertRaises(ValueError):
                LatestTarget().put({"linear": value, "angular": 0, "expires": 10.3})

    def test_browser_right_becomes_ros_negative(self):
        self.assertEqual(velocity({"linear": .5, "angular": 1}), (1, -2))
        self.assertEqual(velocity({"linear": -1, "angular": -1}), (-2, 2))

    def test_pure_left_and_right_rotate_without_forward_motion(self):
        self.assertEqual(velocity({"linear": 0, "angular": -1}), (0, 2))
        self.assertEqual(velocity({"linear": 0, "angular": 1}), (0, -2))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_requires_peer_and_fresh_status(self):
        bridge = TurtleBridge(None)
        bridge.ready = True
        bridge.write = AsyncMock(return_value=True)
        self.assertFalse(bridge.connected)
        self.assertFalse(await bridge.motion("a", 1, 1, 0))
        bridge.write.assert_not_awaited()
        bridge.linked = True
        bridge.link_time = time.monotonic()
        self.assertTrue(bridge.connected)
        bridge.link_time -= 2
        self.assertFalse(bridge.connected)
        self.assertFalse(await bridge.motion("a", 2, 1, 0))

    async def test_duplicate_rejected_and_new_client_allowed(self):
        bridge = TurtleBridge(None)
        bridge.write = AsyncMock(return_value=True)
        bridge.ready = bridge.linked = True
        bridge.link_time = time.monotonic()
        self.assertTrue(await bridge.motion("a", 2, .5, 0))
        self.assertFalse(await bridge.motion("a", 2, 1, 0))
        self.assertFalse(await bridge.motion("a", 1, 1, 0))
        self.assertTrue(await bridge.motion("b", 1, 1, 0))
        self.assertEqual(bridge.write.await_count, 2)

    async def test_stop_writes_zero_and_deadline(self):
        bridge = TurtleBridge(None)
        bridge.write = AsyncMock(return_value=True)
        await bridge.stop()
        message = bridge.write.call_args.args[0]
        self.assertEqual(velocity(message), (0, 0))
        self.assertIn("expires", message)


class RosApiContractTests(unittest.TestCase):
    def run_worker(self, subscribers):
        messages = []
        wiring = {}
        target = LatestTarget()
        target.put({"linear": .5, "angular": .5, "seq": 9, "expires": time.monotonic() + .3})

        class Twist:
            def __init__(self):
                self.linear = SimpleNamespace(x=0.)
                self.angular = SimpleNamespace(z=0.)

        class Node:
            def __init__(self, name):
                pass

            def create_publisher(self, kind, topic, qos):
                wiring["publish"] = (kind, topic, qos.depth)
                return SimpleNamespace(publish=lambda msg: messages.append(copy.deepcopy(msg)), get_subscription_count=lambda: subscribers)

            def create_subscription(self, kind, topic, callback, qos):
                wiring["subscribe"] = topic
                wiring["pose"] = callback

            def create_timer(self, period, callback):
                wiring["period"] = period
                wiring["tick"] = callback

            def destroy_node(self):
                wiring["destroyed"] = True

        def spin(node, timeout_sec):
            wiring["pose"](SimpleNamespace(x=5., y=5., theta=0., linear_velocity=1., angular_velocity=-1.))
            wiring["tick"]()
            target.eof = True

        modules = {
            "rclpy": SimpleNamespace(init=lambda: None, ok=lambda: True, spin_once=spin, shutdown=lambda: None),
            "rclpy.node": SimpleNamespace(Node=Node),
            "rclpy.qos": SimpleNamespace(QoSProfile=lambda **kw: SimpleNamespace(**kw), DurabilityPolicy=SimpleNamespace(VOLATILE=0)),
            "geometry_msgs": SimpleNamespace(), "geometry_msgs.msg": SimpleNamespace(Twist=Twist),
            "turtlesim": SimpleNamespace(), "turtlesim.msg": SimpleNamespace(Pose=object),
        }
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(turtle_worker, "LatestTarget", return_value=target), patch.object(target, "read"), redirect_stdout(output):
            turtle_worker.main()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return wiring, messages, events, target, Twist

    def test_worker_publishes_twist_subscribes_pose_and_zeros_at_eof(self):
        wiring, messages, events, target, Twist = self.run_worker(1)
        self.assertEqual(wiring["publish"], (Twist, "/turtle1/cmd_vel", 1))
        self.assertEqual(wiring["subscribe"], "/turtle1/pose")
        self.assertEqual(wiring["period"], .05)
        self.assertEqual((messages[0].linear.x, messages[0].angular.z), (1., -1.))
        self.assertEqual((messages[-1].linear.x, messages[-1].angular.z), (0., 0.))
        self.assertEqual([event["type"] for event in events], ["turtle_ready", "turtle_pose", "turtle_connection", "turtle_published"])
        self.assertTrue(events[2]["connected"])
        self.assertEqual(events[3]["seq"], 9)

    def test_no_command_subscriber_discards_target_even_with_pose(self):
        wiring, messages, events, target, Twist = self.run_worker(0)
        self.assertFalse(next(event for event in events if event["type"] == "turtle_connection")["connected"])
        self.assertTrue(all(msg.linear.x == 0 and msg.angular.z == 0 for msg in messages))
        self.assertFalse(any(event["type"] == "turtle_published" for event in events))
        self.assertEqual(target.snapshot(time.monotonic())[0]["linear"], 0)


class Am3008RosApiContractTests(unittest.TestCase):
    def run_worker(self, subscribers):
        messages = []
        wiring = {}
        target = LatestTarget()
        target.put({"linear": .5, "angular": .5, "seq": 9,
                    "expires": time.monotonic() + .3})

        class Twist:
            def __init__(self):
                self.linear = SimpleNamespace(x=0.)
                self.angular = SimpleNamespace(z=0.)

        class Node:
            def __init__(self, name):
                wiring["node"] = name

            def create_publisher(self, kind, topic, qos):
                wiring["publish"] = (kind, topic, qos.depth)
                return SimpleNamespace(
                    publish=lambda msg: messages.append(copy.deepcopy(msg)),
                    get_subscription_count=lambda: subscribers)

            def create_timer(self, period, callback):
                wiring["period"] = period
                wiring["tick"] = callback

            def destroy_node(self):
                wiring["destroyed"] = True

        def spin(node, timeout_sec):
            wiring["tick"]()
            target.eof = True

        modules = {
            "rclpy": SimpleNamespace(init=lambda: None, ok=lambda: True,
                                      spin_once=spin, shutdown=lambda: None),
            "rclpy.node": SimpleNamespace(Node=Node),
            "rclpy.qos": SimpleNamespace(
                QoSProfile=lambda **kw: SimpleNamespace(**kw),
                DurabilityPolicy=SimpleNamespace(VOLATILE=0)),
            "geometry_msgs": SimpleNamespace(),
            "geometry_msgs.msg": SimpleNamespace(Twist=Twist),
        }
        output = io.StringIO()
        with patch.dict(sys.modules, modules), \
             patch.object(am3008_worker, "LatestTarget", return_value=target), \
             patch.object(target, "read"), \
             patch.dict("os.environ", {"AM3008_CMD_TOPIC": "/cmd_vel",
                                       "AM3008_MAX_LINEAR_MPS": "0.2",
                                       "AM3008_MAX_STEERING_RAD": "0.4"}), \
             redirect_stdout(output):
            am3008_worker.main()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return wiring, messages, events, Twist

    def test_web_axes_publish_keyboard_compatible_twist_to_cmd_vel(self):
        wiring, messages, events, Twist = self.run_worker(1)
        self.assertEqual(wiring["publish"], (Twist, "/cmd_vel", 1))
        self.assertEqual(wiring["period"], .05)
        self.assertEqual((messages[0].linear.x, messages[0].angular.z), (.1, -.2))
        self.assertEqual((messages[-1].linear.x, messages[-1].angular.z), (0., 0.))
        self.assertEqual(events[0]["type"], "turtle_ready")
        self.assertTrue(next(event for event in events if event["type"] == "turtle_connection")["connected"])
        published = next(event for event in events if event["type"] == "turtle_published")
        self.assertEqual(published["topic"], "/cmd_vel")
        self.assertEqual(published["seq"], 9)

    def test_no_cmd_vel_subscriber_never_publishes_nonzero_target(self):
        _, messages, events, _ = self.run_worker(0)
        self.assertTrue(all(msg.linear.x == 0 and msg.angular.z == 0 for msg in messages))
        self.assertFalse(next(event for event in events if event["type"] == "turtle_connection")["connected"])
        self.assertFalse(any(event["type"] == "turtle_published" for event in events))


if __name__ == "__main__":
    unittest.main()
