import asyncio
import hashlib
import json
import itertools
import math
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from anyio import CancelScope
from turtle_bridge import TurtleBridge
from video_quality import VideoQuality
from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
BASE_RECEIPTS = itertools.count(1)


def receipt_payload(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(word in key.lower() for word in ("password", "token", "secret", "authorization")) else receipt_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [receipt_payload(item) for item in value]
    return value


async def acknowledge_base(ws, client_id, message):
    target = turtle_bridge.target
    mode = {"turtlesim": "ros2_turtlesim", "am3008": "ros2_cmd_vel"}.get(target, "receive_only")
    event = {"type": "base_received", "node": socket.gethostname(),
             "client_id": client_id, "rx": next(BASE_RECEIPTS),
             "seq": message.get("seq"), "received_at": time.time_ns() // 1_000_000,
             "message": receipt_payload(message), "executed": False,
             "mode": mode}
    print("[BASE_RX] " + json.dumps(event, ensure_ascii=True), flush=True)
    await ws.send_json(event)


class EventHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        async with self.lock:
            clients = list(self.clients)
        async def deliver(ws):
            try:
                await asyncio.wait_for(ws.send_text(payload), timeout=0.1)
            except Exception:
                return ws
            return None
        dead = [ws for ws in await asyncio.gather(*(deliver(ws) for ws in clients)) if ws is not None]
        if dead:
            async with self.lock:
                for ws in dead:
                    self.clients.discard(ws)


class ControlLease:
    def __init__(self) -> None:
        self.operator_id: str | None = None
        self.operator_ws: WebSocket | None = None
        self.last_seq: dict[str, int] = {}
        self.role = "viewer"
        self.speed_scale = 0.25

    def claim(self, client_id: str, ws: WebSocket, role: str = "advanced") -> str | None:
        previous = self.operator_id if self.operator_id != client_id else None
        self.operator_id = client_id
        self.operator_ws = ws
        self.role = role
        self.speed_scale = 0.25
        if previous or client_id not in self.last_seq:
            self.last_seq = {client_id: -1}
        return previous

    def release(self, client_id: str) -> None:
        if self.operator_id == client_id:
            self.operator_id = None
            self.operator_ws = None
            self.role = "viewer"
            self.speed_scale = 0.25
            self.last_seq.pop(client_id, None)

    def can_control(self, client_id: str) -> bool:
        return self.operator_id == client_id

    def accept_seq(self, client_id: str, seq: int) -> bool:
        last = self.last_seq.get(client_id, -1)
        if seq <= last:
            return False
        self.last_seq[client_id] = seq
        return True


class BaseControlAdapter:
    """Placeholder until chassis/boat hardware is identified.

    It validates and records normalized targets, then reports that motion is not
    connected. This deliberately avoids mapping joystick input to motors before
    the controller model and protocol are known.
    """

    def __init__(self, hub: EventHub) -> None:
        self.hub = hub
        self.connected = False
        self.last_target = {"forward": 0.0, "turn": 0.0}

    async def motion(self, forward: float, turn: float) -> bool:
        if not self.connected:
            self.last_target = {"forward": 0.0, "turn": 0.0}
            return False
        self.last_target = {"forward": forward, "turn": turn}
        await self.hub.broadcast({"type": "base_state", "connected": self.connected, "target": self.last_target})
        return self.connected

    async def stop(self, reason: str = "stop") -> None:
        self.last_target = {"forward": 0.0, "turn": 0.0}
        await self.hub.broadcast({"type": "base_state", "connected": self.connected, "target": self.last_target, "reason": reason})


class CameraDaemon:
    def __init__(self, hub: EventHub) -> None:
        self.hub = hub
        self.proc: subprocess.Popen[str] | None = None
        self.lock = asyncio.Lock()
        self.state = "init"
        self.last_status = ""
        self.continuous_ptz = False

    def binary(self) -> Path | None:
        override = os.getenv("CAMERA_DAEMON", "")
        if override:
            path = Path(override).expanduser()
            return path if path.is_file() else None
        for path in [ROOT / "cpp" / "build" / "hik_camera_daemon", ROOT / "cpp" / "hik_camera_daemon"]:
            if path.exists():
                return path
        return None

    async def start(self) -> None:
        binary = self.binary()
        if not binary:
            self.state = "not_built"
            await self.hub.broadcast({"type": "camera", "state": self.state})
            return

        password = os.getenv("CAMERA_PASSWORD", "")
        if not password:
            self.state = "missing_password"
            await self.hub.broadcast({"type": "camera", "state": self.state})
            return

        env = os.environ.copy()
        env.setdefault("CAMERA_HOST", "camera.invalid")
        env.setdefault("CAMERA_PORT", "8000")
        env.setdefault("CAMERA_USER", "admin")
        env.setdefault("CAMERA_CHANNEL", "1")
        env["CAMERA_PASSWORD"] = password

        self.proc = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self.state = "starting"
        asyncio.create_task(self._read_output())
        await self.hub.broadcast({"type": "camera", "state": self.state})

    async def _read_output(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        while True:
            line = await asyncio.to_thread(self.proc.stdout.readline)
            if not line:
                self.state = "exited"
                await self.hub.broadcast({"type": "camera", "state": self.state})
                break

            line = line.strip()
            await self.hub.broadcast({"type": "camera_log", "line": line})
            if line.startswith("[ok] login"):
                self.state = "logged_in"
                await self.hub.broadcast({"type": "camera", "state": self.state})
            elif line.startswith("[ok] preview"):
                await self.hub.broadcast({"type": "video", "state": "sdk_preview"})
            elif line.startswith("[status]"):
                self.last_status = line
                await self.hub.broadcast({"type": "camera_status", "line": line})
            elif line.startswith("[ready]") and "ptz_mode=continuous" in line:
                self.continuous_ptz = True
                await self.hub.broadcast({"type": "ptz_ready", "mode": "continuous"})

    async def send(self, line: str) -> bool:
        async with self.lock:
            if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
                self.state = "offline"
                await self.hub.broadcast({"type": "camera", "state": self.state})
                return False
            try:
                self.proc.stdin.write(line.rstrip() + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.state = "offline"
                await self.hub.broadcast({"type": "camera", "state": self.state})
                return False
            return True

    async def ptz(self, command: str, speed: int, duration_ms: int) -> bool:
        allowed = {"left", "right", "up", "down", "zoom_in", "zoom_out", "stop"}
        if command not in allowed:
            await self.hub.broadcast({"type": "error", "message": f"bad ptz command: {command}"})
            return False
        if not self.continuous_ptz:
            await self.hub.broadcast({"type": "error", "message": "PTZ daemon needs continuous-control update"})
            return False
        return await self.send(f"ptz {command} {max(1, min(7, speed))} {max(50, min(3000, duration_ms))}")

    async def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                await self.send("quit")
                await asyncio.to_thread(self.proc.wait, 2)
            except Exception:
                self.proc.send_signal(signal.SIGTERM)


hub = EventHub()
lease = ControlLease()
control_lock = asyncio.Lock()
camera = CameraDaemon(hub)
base_adapter = BaseControlAdapter(hub)
turtle_bridge = TurtleBridge(hub)
video_quality = VideoQuality()
motion_mode_switching = False
motion_mode_error = ""
motion_recovering = False
motion_recovery_task: asyncio.Task | None = None
control_watchdog_task: asyncio.Task | None = None
control_deadline = 0.0
channel_deadlines = {"ptz": 0.0, "base": 0.0}
last_motion = {"ptz": {"pan": 0.0, "tilt": 0.0, "zoom": 0.0}, "base": {"forward": 0.0, "turn": 0.0}}
OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "")
TEST_OPERATOR_TOKEN = os.getenv("TEST_OPERATOR_TOKEN", "")
TOKEN_FILE = Path(os.getenv("OPERATOR_TOKEN_FILE", str(Path.home() / ".config/hkcamera/operator-token.json")))


def load_token_record() -> dict[str, str] | None:
    if not TOKEN_FILE.exists():
        return None
    record = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if len(bytes.fromhex(record["salt"])) != 16 or len(bytes.fromhex(record["digest"])) != 32:
        raise ValueError("Invalid saved operator credential")
    return record


TOKEN_RECORD = load_token_record()


def token_digest(token: str, salt: bytes) -> str:
    return hashlib.scrypt(token.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32).hex()


def save_operator_token(token: str) -> dict[str, str]:
    salt = secrets.token_bytes(16)
    record = {"salt": salt.hex(), "digest": token_digest(token, salt)}
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=TOKEN_FILE.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, TOKEN_FILE)
        return record
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def status() -> dict[str, Any]:
    return {
        "protocol": {"control_path": "/ws/control", "legacy_control_path": "/ws",
                     "revision": "integrated-20261021"},
        "camera": {
            "state": camera.state,
            "last_status": camera.last_status,
            "host": os.getenv("CAMERA_HOST", "camera.invalid"),
            "port": int(os.getenv("CAMERA_PORT", "8000")),
            "channel": int(os.getenv("CAMERA_CHANNEL", "1")),
            "ptz_mode": "continuous" if camera.continuous_ptz else "pulse",
        },
        "control": {
            "operator": lease.operator_id,
            "operator_role": lease.role,
            "speed_scale": lease.speed_scale,
            "speed_ceiling_mps": float(os.getenv("AM3008_MAX_LINEAR_MPS", "0.2")),
            "motion_target": turtle_bridge.target,
            "mode_switching": motion_mode_switching,
            "motion_recovering": motion_recovering,
            "motion_error": motion_mode_error or turtle_bridge.runtime_error,
            "motion_managed": os.getenv("MOTION_LIFECYCLE_MANAGED") == "1",
            "base_mode": ("ros2_cmd_vel" if turtle_bridge.target == "am3008" else
                          "ros2_turtlesim" if turtle_bridge.target == "turtlesim" else "receive_only"),
            "turtle_ready": turtle_bridge.ready,
            "base_connected": not motion_mode_switching and not motion_recovering and (base_adapter.connected or turtle_bridge.connected),
            "base_name": ("小乌龟" if turtle_bridge.target == "turtlesim" and turtle_bridge.connected else
                          "AM3008底盘" if turtle_bridge.target == "am3008" and turtle_bridge.connected else "底盘"),
            "motion_model": ("turtlesim" if turtle_bridge.target == "turtlesim" else
                             os.getenv("BASE_MOTION_MODEL", "am3008_ackermann" if turtle_bridge.target == "am3008" else "unconfigured")),
        },
        "video": {
            "stage": "sdk_preview_counter",
            "next": "sdk_decode_to_mjpeg_or_webrtc",
            "quality": video_quality.status(),
        },
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global control_watchdog_task, motion_recovery_task
    await camera.start()
    await turtle_bridge.start()
    control_watchdog_task = asyncio.create_task(control_watchdog())
    motion_recovery_task = asyncio.create_task(motion_recovery_loop())
    await video_quality.start()
    try:
        yield
    finally:
        await video_quality.close()
        if motion_recovery_task:
            motion_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await motion_recovery_task
        if control_watchdog_task:
            control_watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await control_watchdog_task
        await stop_all("shutdown")
        await turtle_bridge.close()
        await camera.stop()


app = FastAPI(title="hkcamera", lifespan=lifespan)


class ControlStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("input-controls.js", "driving-settings.js"):
            response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", ControlStaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
async def api_status() -> JSONResponse:
    await camera.send("status")
    return JSONResponse(status())


@app.post("/api/control/release")
async def release_control(
    x_client_id: str = Header(default=""),
    x_operator_token: str = Header(default=""),
) -> JSONResponse:
    if not valid_operator_token(x_operator_token):
        return JSONResponse({"ok": False, "reason": "invalid_token"}, status_code=403)
    await relinquish_control(x_client_id)
    await hub.broadcast({"type": "control_state", "operator": lease.operator_id})
    return JSONResponse({"ok": True, "operator": lease.operator_id})


def valid_operator_token(token: str) -> bool:
    if not token or (TOKEN_RECORD is None and not OPERATOR_TOKEN):
        return False
    if len(token) > 128:
        return False
    if TOKEN_RECORD:
        return secrets.compare_digest(token_digest(token, bytes.fromhex(TOKEN_RECORD["salt"])), TOKEN_RECORD["digest"])
    return secrets.compare_digest(token.encode("utf-8"), OPERATOR_TOKEN.encode("utf-8"))


async def relinquish_control(client_id: str) -> None:
    async with control_lock:
        if lease.can_control(client_id):
            try:
                await stop_all("release")
            finally:
                lease.release(client_id)


def clamp_unit(value: Any) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or not -1 <= value <= 1:
        raise ValueError("expected number")
    return max(-1.0, min(1.0, float(value)))


def is_stale(timestamp_ms: Any, max_age_ms: int = 300) -> bool:
    if type(timestamp_ms) not in (float, int) or not math.isfinite(timestamp_ms):
        return True
    return not -max_age_ms <= time.time() * 1000 - float(timestamp_ms) <= max_age_ms


def sequence(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("invalid sequence")
    return value


async def stop_all(reason: str = "stop") -> None:
    channel_deadlines.update(ptz=0.0, base=0.0)
    last_motion["ptz"] = {"pan": 0.0, "tilt": 0.0, "zoom": 0.0}
    last_motion["base"] = {"forward": 0.0, "turn": 0.0}
    if camera.continuous_ptz:
        await camera.ptz("stop", 1, 50)
    await base_adapter.stop(reason)
    await turtle_bridge.stop()
    await hub.broadcast({"type": "motion_stop", "reason": reason})


async def switch_motion_mode(client_id: str, target: Any) -> dict[str, Any]:
    """Reserve the switch under the control lock; never replay the old lease."""
    global motion_mode_switching, motion_mode_error
    if target not in ("turtlesim", "am3008"):
        return {"type": "motion_mode", "target": turtle_bridge.target,
                "state": "error", "reason": "invalid_target"}
    async with control_lock:
        if motion_mode_switching or motion_recovering:
            return {"type": "motion_mode", "target": turtle_bridge.target,
                    "state": "error", "reason": "mode_switching"}
        if not lease.can_control(client_id):
            return {"type": "motion_mode", "target": turtle_bridge.target,
                    "state": "error", "reason": "operator_required"}
        motion_mode_switching = True
        motion_mode_error = ""
        lease.release(client_id)
    try:
        await stop_all("mode_switch")
        await hub.broadcast({"type": "control_state", "operator": None})
        await hub.broadcast({"type": "motion_mode", "target": target, "state": "switching"})
        await turtle_bridge.close()
        await turtle_bridge.start(target)
        await turtle_bridge.wait_connected()
        event = {"type": "motion_mode", "target": target, "state": "ready"}
    except asyncio.CancelledError:
        await turtle_bridge.close()
        raise
    except Exception as error:
        motion_mode_error = str(error)
        await turtle_bridge.close()
        event = {"type": "motion_mode", "target": turtle_bridge.target,
                 "state": "error", "reason": motion_mode_error}
    finally:
        motion_mode_switching = False
    await hub.broadcast(event)
    await hub.broadcast({"type": "status", "status": status()})
    return event


async def recover_motion_once() -> bool:
    global motion_recovering, motion_mode_error
    async with control_lock:
        if (os.getenv('MOTION_LIFECYCLE_MANAGED') != '1' or motion_mode_switching or
                motion_recovering or turtle_bridge.connected or
                turtle_bridge.target not in ('am3008', 'turtlesim')):
            return False
        target = turtle_bridge.target
        motion_recovering = True
    try:
        await hub.broadcast({'type': 'status', 'status': status()})
        await base_adapter.stop('reconnecting')
        await turtle_bridge.probe()
        await turtle_bridge.close()
        await turtle_bridge.start(target)
        await turtle_bridge.wait_connected()
        async with control_lock:
            # A newly connected driver must not inherit a held key or old lease.
            await stop_all('motion_reconnected')
            if lease.operator_id:
                lease.release(lease.operator_id)
            motion_mode_error = ''
        await hub.broadcast({'type': 'control_state', 'operator': None})
        return True
    except asyncio.CancelledError:
        raise
    except Exception as error:
        motion_mode_error = str(error)
        await turtle_bridge.close()
        return False
    finally:
        motion_recovering = False
        await hub.broadcast({'type': 'status', 'status': status()})


async def motion_recovery_loop():
    global motion_mode_error
    delay = 5
    unhealthy_since = time.monotonic()
    while True:
        await asyncio.sleep(delay)
        if turtle_bridge.connected or motion_mode_switching:
            unhealthy_since = time.monotonic()
            delay = 5
            continue
        # Allow normal initialization to finish before retrying a silent worker.
        if not turtle_bridge.runtime_error and time.monotonic() - unhealthy_since < 40:
            continue
        try:
            ok = await recover_motion_once()
        except Exception as error:
            motion_mode_error = 'Recovery attempt failed: ' + str(error)
            ok = False
        delay = 5 if ok else min(delay * 2, 30)
        unhealthy_since = time.monotonic()


def ptz_command_from_axes(pan: float, tilt: float, zoom: float, speed: int | None = None) -> tuple[str, int] | None:
    axes = [("zoom_in" if zoom > 0 else "zoom_out", abs(zoom)),
            ("right" if pan > 0 else "left", abs(pan)),
            ("down" if tilt > 0 else "up", abs(tilt))]
    command, magnitude = max(axes, key=lambda item: item[1])
    if magnitude < 0.10:
        return None
    return command, speed if speed is not None else max(1, min(7, round(1 + magnitude * 6)))


async def control_watchdog() -> None:
    global control_deadline
    while True:
        await asyncio.sleep(0.05)
        async with control_lock:
            for channel, deadline in list(channel_deadlines.items()):
                if deadline and time.monotonic() > deadline:
                    channel_deadlines[channel] = 0.0
                    if channel == "ptz":
                        await camera.ptz("stop", 1, 50)
                    else:
                        await base_adapter.stop("timeout")
                    await hub.broadcast({"type": "motion_stop", "channel": channel, "reason": "timeout"})


async def receive_control_message(ws):
    try:
        return await ws.receive_json()
    except RuntimeError:
        if (ws.application_state == WebSocketState.DISCONNECTED or
                ws.client_state == WebSocketState.DISCONNECTED):
            raise WebSocketDisconnect(code=1006) from None
        raise


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await control_websocket(ws)


@app.websocket("/ws/control")
async def control_websocket(ws: WebSocket) -> None:
    global TOKEN_RECORD, control_deadline
    client_id = str(uuid4())
    await hub.add(ws)
    await ws.send_json({"type": "hello", "client_id": client_id, "server_time": time.time() * 1000, "status": status()})

    try:
        while True:
            try:
                msg = await receive_control_message(ws)
            except ValueError:
                await ws.send_json({"type": "error", "message": "invalid JSON"})
                continue
            if not isinstance(msg, dict):
                await ws.send_json({"type": "error", "message": "expected object"})
                continue
            kind = msg.get("type")

            if motion_mode_switching and kind in ("claim_control", "motion_mode", "base", "ptz"):
                if kind == "motion_mode":
                    await ws.send_json({"type": "motion_mode", "target": turtle_bridge.target,
                                        "state": "error", "reason": "mode_switching"})
                    continue
                await ws.send_json({"type": "control" if kind == "claim_control" else "error",
                                    "ok": False, "reason": "mode_switching",
                                    "message": "motion mode is switching", "operator": lease.operator_id})
                continue

            if kind == "hello":
                await ws.send_json({"type": "hello", "client_id": client_id, "server_time": time.time() * 1000, "status": status()})
                continue

            if kind == "claim_control":
                token = str(msg.get("operator_token", ""))
                async with control_lock:
                    role = "test" if TEST_OPERATOR_TOKEN and secrets.compare_digest(token.encode("utf-8"), TEST_OPERATOR_TOKEN.encode("utf-8")) else "advanced" if await asyncio.to_thread(valid_operator_token, token) else "viewer"
                    protected = bool(lease.operator_id and lease.operator_id != client_id and lease.role == "advanced")
                    already_owned = lease.operator_id is not None
                    ok = role != "viewer" and not protected
                    reason = "occupied" if protected else "" if ok else "invalid_token"
                    previous = lease.claim(client_id, ws, role) if ok else None
                    if ok:
                        video_quality.reset_feedback()
                    if ok and already_owned:
                        await stop_all("operator_replaced")
                await ws.send_json({"type": "control", "ok": ok, "operator": lease.operator_id, "reason": reason,
                                    "role": lease.role if ok else "viewer", "speed_scale": lease.speed_scale})
                await hub.broadcast({"type": "control_state", "operator": lease.operator_id,
                                     "operator_role": lease.role, "speed_scale": lease.speed_scale})

            elif kind == "speed":
                async with control_lock:
                    ok = lease.can_control(client_id) and lease.role == "advanced"
                    try:
                        scale = clamp_unit(msg.get("scale"))
                        if scale < 0.25:
                            raise ValueError("speed range")
                    except (ValueError, TypeError):
                        ok = False
                    if ok:
                        await turtle_bridge.stop()
                        lease.speed_scale = scale
                await ws.send_json({"type": "speed", "ok": ok, "scale": lease.speed_scale})

            elif kind in ("video_settings", "video_refresh", "video_metrics"):
                advanced = lease.can_control(client_id) and lease.role == "advanced"
                controller = lease.can_control(client_id)
                if kind == "video_metrics":
                    if controller:
                        video_quality.report(msg)
                elif kind == "video_refresh" and controller:
                    video_quality.refresh()
                elif kind == "video_settings" and advanced:
                    video_quality.configure(msg)
                else:
                    await ws.send_json({"type": "error", "message": "video settings require advanced control"})

            elif kind == "motion_mode":
                event = await switch_motion_mode(client_id, msg.get("target"))
                # Success/failure after reserving a switch is broadcast to all clients.
                # Validation denials have no broadcast, and belong only to this socket.
                if event.get("reason") in ("invalid_target", "mode_switching", "operator_required"):
                    await ws.send_json(event)

            elif kind == "change_token":
                current = str(msg.get("current_token", ""))
                new = str(msg.get("new_token", ""))
                async with control_lock:
                    reason = ""
                    if not await asyncio.to_thread(valid_operator_token, current):
                        reason = "invalid_token"
                    elif (TEST_OPERATOR_TOKEN and current == TEST_OPERATOR_TOKEN) or not 8 <= len(new) <= 128 or new.isspace():
                        reason = "invalid_length"
                    else:
                        try:
                            record = await asyncio.to_thread(save_operator_token, new)
                        except OSError:
                            reason = "save_failed"
                        else:
                            TOKEN_RECORD = record
                            if lease.operator_id:
                                await stop_all("credential_changed")
                                lease.release(lease.operator_id)
                await ws.send_json({"type": "token_changed", "ok": not reason, "reason": reason})
                if not reason:
                    await hub.broadcast({"type": "control_state", "operator": lease.operator_id})

            elif kind == "release_control":
                await relinquish_control(client_id)
                await hub.broadcast({"type": "control_state", "operator": lease.operator_id})

            elif kind == "ptz":
                if "command" in msg:
                    if ws.url.path != "/ws":
                        await ws.send_json({"type": "error", "message": "use sequenced PTZ axes on /ws/control"})
                        continue
                    try:
                        speed = int(msg.get("speed", 4))
                        duration_ms = int(msg.get("duration_ms", 300))
                    except (TypeError, ValueError):
                        await ws.send_json({"type": "error", "message": "invalid PTZ parameters"})
                        continue
                    async with control_lock:
                        allowed = lease.can_control(client_id)
                        if allowed:
                            channel_deadlines["ptz"] = time.monotonic() + 0.3
                        ok = allowed and await camera.ptz(str(msg.get("command", "")), speed, min(300, duration_ms))
                    if not allowed:
                        await ws.send_json({"type": "error", "message": "viewer cannot control camera"})
                    elif not ok:
                        await ws.send_json({"type": "error", "message": "PTZ command was not sent"})
                    continue
                try:
                    if is_stale(msg.get("timestamp")):
                        raise ValueError("stale")
                    pan = clamp_unit(msg.get("pan", 0))
                    tilt = clamp_unit(msg.get("tilt", 0))
                    zoom = clamp_unit(msg.get("zoom", 0))
                    speed = msg.get("speed")
                    if speed is not None and (type(speed) is not int or not 1 <= speed <= 7):
                        raise ValueError("invalid speed")
                    seq = sequence(msg.get("seq"))
                except (TypeError, ValueError):
                    await ws.send_json({"type": "error", "message": "invalid PTZ parameters"})
                    continue
                async with control_lock:
                    allowed = lease.can_control(client_id)
                    fresh = allowed and lease.accept_seq(client_id, seq)
                    if fresh:
                        channel_deadlines["ptz"] = time.monotonic() + 0.3
                    command = ptz_command_from_axes(pan, tilt, zoom, speed)
                    control_deadline = time.monotonic() + 0.3 if fresh else control_deadline
                    last_motion["ptz"] = {"pan": pan, "tilt": tilt, "zoom": zoom} if fresh else last_motion["ptz"]
                    ok = fresh and (await camera.ptz(command[0], command[1], 300) if command else await camera.ptz("stop", 1, 50))
                if not allowed:
                    await ws.send_json({"type": "error", "message": "viewer cannot control camera"})
                elif not fresh:
                    await ws.send_json({"type": "error", "message": "stale or repeated PTZ command"})
                elif not ok:
                    await ws.send_json({"type": "error", "message": "PTZ command was not sent"})

            elif kind == "base":
                async with control_lock:
                    if not lease.can_control(client_id):
                        await ws.send_json({"type": "error", "message": "viewer cannot send base targets"})
                    else:
                        await acknowledge_base(ws, client_id, msg)
                        if motion_recovering or not turtle_bridge.connected:
                            await turtle_bridge.stop()
                            await ws.send_json({"type": "turtle_rejected", "seq": msg.get("seq"), "reason": "disconnected"})
                        else:
                            try:
                                if is_stale(msg.get("timestamp")):
                                    raise ValueError("expired")
                                seq = sequence(msg.get("seq"))
                                linear = clamp_unit(msg.get("linear"))
                                angular = clamp_unit(msg.get("angular"))
                                # Enforced here for every input source, including forged browser messages.
                                scale = 0.25 if lease.role == "test" else lease.speed_scale
                                accepted = await turtle_bridge.motion(client_id, seq, linear * scale, angular)
                                if not accepted:
                                    raise ValueError("repeated or unavailable")
                            except (ValueError, TypeError):
                                await turtle_bridge.stop()
                                await ws.send_json({"type": "turtle_rejected", "seq": msg.get("seq")})

            elif kind == "stop":
                async with control_lock:
                    allowed = lease.can_control(client_id)
                    if allowed:
                        control_deadline = 0.0
                        await stop_all(str(msg.get("reason", "stop")))
                if not allowed:
                    await ws.send_json({"type": "error", "message": "viewer cannot stop controls"})

            elif kind == "status":
                await camera.send("status")
                await ws.send_json({"type": "status", "status": status()})

    except WebSocketDisconnect:
        pass
    finally:
        # ASGI servers cancel disconnected sockets. Finish motor zeroing and
        # lease cleanup before that cancellation can interrupt an awaited send.
        # Remove the dead socket first so stop broadcasts only reach live peers.
        with CancelScope(shield=True):
            await hub.remove(ws)
            await relinquish_control(client_id)
            await hub.broadcast({"type": "control_state", "operator": lease.operator_id})


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        # 8080 已由 ZLMediaKit 的 WebRTC/HTTP 服务占用，控制后端使用 8090。
        port=int(os.getenv("WEB_PORT", "8090")),
        reload=False,
        app_dir=str(Path(__file__).parent),
    )
