"""ROS2 turtlesim only: latest target wins, independent expiry, pose feedback."""
import json
import math
import sys
import threading
import time


class LatestTarget:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = {"linear": 0, "angular": 0, "seq": None, "expires": 0}
        self.eof = False

    def put(self, value):
        if not isinstance(value, dict):
            raise ValueError("object required")
        for name in ("linear", "angular", "expires"):
            if type(value.get(name)) not in (int, float) or not math.isfinite(value[name]):
                raise ValueError("finite numbers required")
        if abs(value["linear"]) > 1 or abs(value["angular"]) > 1:
            raise ValueError("normalized axes required")
        with self.lock:
            self.value = dict(value)

    def snapshot(self, now):
        with self.lock:
            value = dict(self.value)
            expired = self.eof or now >= value["expires"] or value["expires"] > now + 0.35
        if expired:
            value.update(linear=0, angular=0)
        return value, expired

    def read(self):
        try:
            while line := sys.stdin.buffer.readline(4096):
                try:
                    self.put(json.loads(line))
                except (ValueError, TypeError):
                    self.put({"linear": 0, "angular": 0, "expires": 0, "seq": None})
        finally:
            with self.lock:
                self.eof = True


def emit(event):
    print(json.dumps(event), flush=True)


def velocity(value):
    return float(value["linear"]) * 2.0, -float(value["angular"]) * 2.0


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, DurabilityPolicy
    from geometry_msgs.msg import Twist
    from turtlesim.msg import Pose

    rclpy.init()
    target = LatestTarget()
    node = Node("hkcamera_turtlesim_bridge")
    qos = QoSProfile(depth=1, durability=DurabilityPolicy.VOLATILE)
    publisher = node.create_publisher(Twist, "/turtle1/cmd_vel", qos)
    last_pose_time = 0.0
    previous = None
    published = None
    expired_before = True
    connection_before = None
    connection_sent_at = 0.0

    def pose_callback(pose):
        nonlocal last_pose_time, previous
        now = time.monotonic()
        if now - last_pose_time < 0.1:
            return
        moving = previous is not None and (math.hypot(pose.x - previous[0], pose.y - previous[1]) > 0.001 or abs(math.atan2(math.sin(pose.theta - previous[2]), math.cos(pose.theta - previous[2]))) > 0.001)
        previous = (pose.x, pose.y, pose.theta)
        last_pose_time = now
        emit({"type": "turtle_pose", "x": pose.x, "y": pose.y,
              "theta": pose.theta, "linear_velocity": pose.linear_velocity,
              "angular_velocity": pose.angular_velocity, "moving": moving,
              "sampled_at": time.time() * 1000})

    subscription = node.create_subscription(Pose, "/turtle1/pose", pose_callback, qos)

    def tick():
        nonlocal published, expired_before, connection_before, connection_sent_at
        now = time.monotonic()
        connected = publisher.get_subscription_count() > 0 and last_pose_time > 0 and now - last_pose_time < 1.0
        if connected != connection_before or now - connection_sent_at > 0.5:
            emit({"type": "turtle_connection", "connected": connected,
                  "sampled_at": time.time() * 1000})
            connection_sent_at = now
            connection_before = connected
        if not connected:
            target.put({"linear": 0, "angular": 0, "seq": None, "expires": 0})
        value, expired = target.snapshot(now)
        message = Twist()
        # Browser positive turn means right; ROS positive angular.z means left.
        message.linear.x, message.angular.z = velocity(value)
        publisher.publish(message)
        if expired and not expired_before:
            emit({"type": "turtle_stop", "reason": "timeout"})
        elif not expired and value.get("seq") != published:
            emit({"type": "turtle_published", "seq": value.get("seq"),
                  "linear": message.linear.x, "angular": message.angular.z})
            published = value.get("seq")
        expired_before = expired

    timer = node.create_timer(0.05, tick)
    threading.Thread(target=target.read, daemon=True).start()
    emit({"type": "turtle_ready", "topic": "/turtle1/cmd_vel"})
    try:
        while rclpy.ok() and not target.eof:
            rclpy.spin_once(node, timeout_sec=0.05)
    finally:
        publisher.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
