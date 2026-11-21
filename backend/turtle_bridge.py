"""Optional subprocess transport; the web backend does not need rclpy installed."""
import asyncio
import json
import os
import time
from pathlib import Path


class TurtleBridge:
    def __init__(self, hub):
        self.hub = hub
        self.proc = None
        self.task = None
        self.ready = False
        self.linked = False
        self.link_time = 0.0
        self.last_seq = {}
        self.target = "none"
        self.runtime_proc = None
        self.runtime_task = None
        self.runtime_ready = False
        self.runtime_owned = False
        self.runtime_error = ""
        self.generation = 0
        self.closing = False

    @property
    def connected(self):
        runtime_ok = (self.runtime_proc is None or
                      self.runtime_ready and self.runtime_proc.returncode is None)
        return self.ready and self.linked and runtime_ok and time.monotonic() - self.link_time < 1.5

    async def start(self, target=None):
        target = target if target is not None else os.getenv("MOTION_TARGET")
        if target is None:
            target = "turtlesim" if os.getenv("TURTLESIM_BRIDGE") == "1" else "none"
        self.target = target
        self.ready = self.linked = False
        self.link_time = 0.0
        self.last_seq.clear()
        self.runtime_error = ""
        self.closing = False
        self.runtime_ready = self.runtime_owned = False
        self.generation += 1
        if target not in ("turtlesim", "am3008"):
            return
        env = os.environ.copy()
        env["MOTION_TARGET"] = target
        worker = "turtle_worker.py" if target == "turtlesim" else "am3008_worker.py"
        try:
            if env.get("MOTION_LIFECYCLE_MANAGED") == "1":
                self.runtime_proc = await asyncio.create_subprocess_exec(
                    env.get("ROS_PYTHON", "/usr/bin/python3"), "-u",
                    str(Path(__file__).resolve().parents[1] / "deploy" / "motion_runtime.py"), target,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
                self.runtime_task = asyncio.create_task(self.read_runtime(self.runtime_proc, self.generation))
            self.proc = await asyncio.create_subprocess_exec(
                env.get("ROS_PYTHON", "/usr/bin/python3"), "-u",
                str(Path(__file__).with_name(worker)),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=None, env=env)
            self.task = asyncio.create_task(self.read_events(self.proc, self.generation))
        except OSError as error:
            self.runtime_error = str(error)
            print("[TURTLE_BRIDGE] " + str(error), flush=True)

    async def read_events(self, proc=None, generation=None):
        proc = proc or self.proc
        generation = self.generation if generation is None else generation
        try:
            while line := await proc.stdout.readline():
                if generation != self.generation:
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if event.get("type") not in ("turtle_ready", "turtle_connection", "turtle_published", "turtle_pose", "turtle_stop"):
                    continue
                if event["type"] == "turtle_ready":
                    self.ready = True
                if event["type"] == "turtle_connection":
                    sampled = event.get("sampled_at", 0)
                    self.linked = bool(event.get("connected")) and abs(time.time() * 1000 - sampled) < 1500
                    self.link_time = time.monotonic()
                    event = dict(event, connected=self.connected)
                await self.hub.broadcast(event)
        finally:
            if generation == self.generation:
                self.ready = False
                self.linked = False
                await self.hub.broadcast({"type": "turtle_unavailable"})

    async def read_runtime(self, proc, generation):
        try:
            while line := await proc.stdout.readline():
                if generation != self.generation:
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if event.get("type") == "motion_runtime_ready":
                    self.runtime_ready = True
                    self.runtime_owned = bool(event.get("owned"))
                elif event.get("type") == "motion_runtime_error":
                    self.runtime_ready = False
                    self.runtime_error = str(event.get("reason", "ROS launcher failed"))
                else:
                    continue
                await self.hub.broadcast(event)
        finally:
            if generation == self.generation:
                self.runtime_ready = False
                if not self.closing:
                    self.runtime_error = self.runtime_error or "ROS node launcher stopped"
                    self.linked = False
                    # Losing the supervisor also revokes the worker input stream.
                    # A validated target must not keep running after lifecycle failure.
                    await self.stop()
                    if self.proc and self.proc.stdin:
                        self.proc.stdin.close()
                    await self.hub.broadcast({"type": "turtle_connection", "connected": False,
                                              "sampled_at": time.time() * 1000})

    async def probe(self):
        """Read-only ROS graph check; never creates a driver or motion publisher."""
        proc = await asyncio.create_subprocess_exec(
            os.getenv('ROS_PYTHON', '/usr/bin/python3'), '-u',
            str(Path(__file__).resolve().parents[1] / 'deploy/motion_runtime.py'),
            self.target, '--probe', stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=os.environ.copy())
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            events = []
            for line in output.splitlines():
                try:
                    events.append(json.loads(line))
                except (ValueError, UnicodeDecodeError):
                    pass
            if proc.returncode != 0 or not any(e.get('type') == 'motion_probe_ready' for e in events):
                reason = next((e.get('reason') for e in reversed(events) if e.get('reason')), 'ROS recovery preflight failed')
                raise RuntimeError(reason)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def wait_connected(self, timeout=None):
        if timeout is None:
            timeout = 35.0 if self.runtime_proc else 12.0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return
            if self.runtime_error:
                raise RuntimeError(self.runtime_error)
            if self.runtime_proc and self.runtime_proc.returncode is not None:
                raise RuntimeError("ROS node launcher exited; inspect the NX backend log")
            if not self.proc or self.proc.returncode is not None:
                raise RuntimeError("ROS control worker exited; inspect the NX backend log")
            await asyncio.sleep(0.1)
        raise RuntimeError("No ROS command subscriber found for the selected mode")

    async def write(self, message):
        if not self.ready or not self.proc or self.proc.returncode is not None:
            return False
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            await asyncio.wait_for(self.proc.stdin.drain(), timeout=0.1)
            return True
        except (OSError, asyncio.TimeoutError):
            self.ready = False
            return False

    async def motion(self, client, seq, linear, angular):
        if not self.connected:
            return False
        if seq <= self.last_seq.get(client, -1):
            return False
        self.last_seq = {client: seq}
        return await self.write({"linear": linear, "angular": angular,
                                 "seq": seq, "expires": time.monotonic() + 0.3})

    async def stop(self):
        await self.write({"linear": 0, "angular": 0, "seq": None,
                          "expires": time.monotonic() + 0.3})

    async def close(self):
        self.closing = True
        await self.stop()
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await self.proc.wait()
        if self.task:
            await self.task
        if self.runtime_proc and self.runtime_proc.returncode is None:
            if self.runtime_proc.stdin:
                self.runtime_proc.stdin.close()
            try:
                # The supervisor gives its owned ROS process group up to 10 s
                # for SIGINT/TERM/KILL cleanup; do not kill it mid-cleanup.
                await asyncio.wait_for(self.runtime_proc.wait(), timeout=12)
            except asyncio.TimeoutError:
                self.runtime_proc.terminate()
                try:
                    await asyncio.wait_for(self.runtime_proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.runtime_proc.kill()
                    await self.runtime_proc.wait()
        if self.runtime_task:
            await self.runtime_task
        self.proc = self.task = self.runtime_proc = self.runtime_task = None
        self.runtime_ready = self.runtime_owned = False
        self.ready = self.linked = False
        self.last_seq.clear()
