"""ROS2 bridge from the WebSocket target stream to an AM3008 cmd_vel topic."""
import json
import math
import os
import threading
import time

from turtle_worker import LatestTarget


def limits():
    linear = float(os.getenv("AM3008_MAX_LINEAR_MPS", "0.2"))
    steering = float(os.getenv("AM3008_MAX_STEERING_RAD", "0.4"))
    if not math.isfinite(linear) or not 0 < linear <= 1.0:
        raise ValueError("AM3008_MAX_LINEAR_MPS must be in (0, 1.0].")
    if not math.isfinite(steering) or not 0 < steering <= 0.4:
        raise ValueError("AM3008_MAX_STEERING_RAD must be in (0, 0.4].")
    return linear, steering


def velocity(value, max_linear, max_steering):
    """Map normalized browser axes to m/s and front-wheel steering radians."""
    return float(value["linear"]) * max_linear, -float(value["angular"]) * max_steering


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, DurabilityPolicy
    from geometry_msgs.msg import Twist

    topic = os.getenv("AM3008_CMD_TOPIC", "/cmd_vel")
    if not topic.startswith("/") or "//" in topic or not topic.strip("/"):
        raise ValueError("AM3008_CMD_TOPIC must be an absolute ROS topic name.")
    max_linear, max_steering = limits()

    rclpy.init()
    target = LatestTarget()
    node = Node("hkcamera_am3008_web_bridge")
    qos = QoSProfile(depth=1, durability=DurabilityPolicy.VOLATILE)
    publisher = node.create_publisher(Twist, topic, qos)
    published = None
    expired_before = True
    connection_before = None
    connection_sent_at = 0.0

    def tick():
        nonlocal published, expired_before, connection_before, connection_sent_at
        now = time.monotonic()
        connected = publisher.get_subscription_count() > 0
        if connected != connection_before or now - connection_sent_at > 0.5:
            emit({"type": "turtle_connection", "connected": connected,
                  "topic": topic, "sampled_at": time.time() * 1000})
            connection_sent_at = now
            connection_before = connected
        if not connected:
            target.put({"linear": 0, "angular": 0, "seq": None, "expires": 0})
        value, expired = target.snapshot(now)
        linear, steering = velocity(value, max_linear, max_steering)
        message = Twist()
        message.linear.x = linear
        message.angular.z = steering
        publisher.publish(message)
        if expired and not expired_before:
            emit({"type": "turtle_stop", "reason": "timeout", "topic": topic})
        elif not expired and value.get("seq") != published:
            emit({"type": "turtle_published", "seq": value.get("seq"),
                  "linear": linear, "angular": steering, "topic": topic})
            published = value.get("seq")
        expired_before = expired

    node.create_timer(0.05, tick)
    threading.Thread(target=target.read, daemon=True).start()
    emit({"type": "turtle_ready", "topic": topic})
    try:
        while rclpy.ok() and not target.eof:
            rclpy.spin_once(node, timeout_sec=0.05)
    finally:
        publisher.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
