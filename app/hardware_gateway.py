"""Local HTTP hardware gateway server and reference motion drivers (GRBL, Klipper, Loopback)."""

from __future__ import annotations

import logging
import math
import socket
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

LOGGER = logging.getLogger("autolab.gateway")

_MAX_IDEMPOTENCY_ENTRIES = 512


class GatewayDriverError(RuntimeError):
    """Raised when a low-level hardware driver fails to initialize or execute a command."""


class AxisLimits(BaseModel):
    """Deterministic software travel bounds in millimetres."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x_min: Annotated[float, Field(ge=-2000, le=2000)] = 0.0
    x_max: Annotated[float, Field(ge=-2000, le=2000)] = 250.0
    y_min: Annotated[float, Field(ge=-2000, le=2000)] = 0.0
    y_max: Annotated[float, Field(ge=-2000, le=2000)] = 250.0
    z_min: Annotated[float, Field(ge=-500, le=500)] = 0.0
    z_max: Annotated[float, Field(ge=-500, le=500)] = 150.0


class GatewayConfig(BaseModel):
    """Configuration binding the local hardware gateway to a project revision and limits."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    project_id: Annotated[str, Field(min_length=1, max_length=120)]
    revision: Annotated[int, Field(strict=True, ge=0)]
    driver_kind: Literal["loopback", "grbl", "klipper"] = "loopback"
    commissioning_reviewed: bool = False
    auto_home_on_prepare: bool = True
    watchdog_timeout_s: Annotated[float, Field(gt=0.05, le=300.0)] = 30.0
    limits_mm: AxisLimits = Field(default_factory=AxisLimits)
    max_feedrate_mm_min: Annotated[float, Field(gt=0.0, le=30000.0)] = 3000.0
    serial_port: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    baudrate: Annotated[int, Field(ge=9600, le=921600)] = 115200
    klipper_url: Annotated[str | None, Field(min_length=1, max_length=300)] = None


class MotionDriver(Protocol):
    """Minimal hardware driver boundary separated from HTTP orchestration and safety gates."""

    driver_name: str

    def home(self) -> dict[str, Any]: ...
    def move_to(self, x: float, y: float, z: float, feedrate: float) -> dict[str, Any]: ...
    def rotate(self, axis: str, angle_deg: float, feedrate: float) -> dict[str, Any]: ...
    def stop(self) -> dict[str, Any]: ...
    def emergency_stop(self) -> dict[str, Any]: ...
    def reset_estop(self) -> dict[str, Any]: ...


@dataclass
class LoopbackStageDriver:
    """Deterministic in-memory G-code loopback stage driver for offline HIL and dry runs."""

    driver_name: str = "loopback_gcode_stage"
    homed: bool = False
    estop_active: bool = False
    position_mm: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation_deg: dict[str, float] = field(default_factory=dict)
    emitted_gcode: list[str] = field(default_factory=list)
    inject_fault: str | None = None

    def home(self) -> dict[str, Any]:
        if self.inject_fault:
            raise GatewayDriverError(f"Loopback driver fault: {self.inject_fault}")
        if self.estop_active:
            raise GatewayDriverError("Cannot home while emergency stop is engaged.")
        self.emitted_gcode.extend(["G28", "G21", "G90"])
        self.position_mm = [0.0, 0.0, 0.0]
        self.rotation_deg.clear()
        self.homed = True
        return {"homed": True, "position_mm": list(self.position_mm), "gcode": "G28; G21; G90"}

    def move_to(self, x: float, y: float, z: float, feedrate: float) -> dict[str, Any]:
        if self.inject_fault:
            raise GatewayDriverError(f"Loopback driver fault: {self.inject_fault}")
        if self.estop_active:
            raise GatewayDriverError("Motion blocked: emergency stop engaged.")
        if not self.homed:
            raise GatewayDriverError("Stage must be homed before linear motion.")
        cmd = f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{feedrate:.1f}"
        self.emitted_gcode.append(cmd)
        self.position_mm = [round(x, 4), round(y, 4), round(z, 4)]
        return {"gcode": cmd, "position_mm": list(self.position_mm)}

    def rotate(self, axis: str, angle_deg: float, feedrate: float) -> dict[str, Any]:
        if self.estop_active:
            raise GatewayDriverError("Motion blocked: emergency stop engaged.")
        if not self.homed:
            raise GatewayDriverError("Stage must be homed before indexed rotation.")
        cmd = f"G1 A{angle_deg:.3f} F{feedrate:.1f} ; axis={axis}"
        self.emitted_gcode.append(cmd)
        self.rotation_deg[axis] = round(angle_deg, 4)
        return {"gcode": cmd, "axis": axis, "angle_deg": self.rotation_deg[axis]}

    def stop(self) -> dict[str, Any]:
        self.emitted_gcode.append("M400 ; stop_wait")
        self.homed = False
        return {"stopped": True, "gcode": "M400"}

    def emergency_stop(self) -> dict[str, Any]:
        self.emitted_gcode.extend(["!", "\\x18 ; GRBL_SOFT_RESET"])
        self.estop_active = True
        self.homed = False
        return {"estop": True, "gcode": "!\\x18"}

    def reset_estop(self) -> dict[str, Any]:
        self.emitted_gcode.append("$X ; unlock")
        self.estop_active = False
        self.homed = False
        return {"estop_cleared": True, "gcode": "$X"}


class GrblSerialDriver:
    """Real GRBL / FluidNC / RP2040 G-code serial driver (pyserial optional or stream)."""

    driver_name = "grbl_serial"

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = 115200,
        timeout_s: float = 2.0,
        stream: Any = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._stream = stream
        self._lock = threading.Lock()
        self.homed = False
        self.estop_active = False
        self.position_mm = [0.0, 0.0, 0.0]

    def _ensure_stream(self):
        if self._stream is not None:
            return self._stream
        if not self.port:
            raise GatewayDriverError("No serial_port configured for GrblSerialDriver.")
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GatewayDriverError(
                "pyserial is not installed in this environment. Install pyserial to open "
                f"{self.port!r}, or use driver_kind='loopback' / inject a serial stream."
            ) from exc
        self._stream = serial.Serial(self.port, self.baudrate, timeout=self.timeout_s)
        return self._stream

    def _send_line(self, line: str) -> str:
        stream = self._ensure_stream()
        payload = (line.strip() + "\n").encode("ascii")
        stream.write(payload)
        if hasattr(stream, "flush"):
            stream.flush()
        reply = stream.readline()
        text = reply.decode("ascii", errors="replace").strip() if isinstance(reply, bytes) else str(
            reply
        ).strip()
        if text.lower().startswith("error:") or text.lower().startswith("alarm:"):
            raise GatewayDriverError(f"GRBL rejected {line!r}: {text}")
        return text or "ok"

    def home(self) -> dict[str, Any]:
        if self.estop_active:
            raise GatewayDriverError("Cannot home while emergency stop is engaged.")
        with self._lock:
            self._send_line("$H")
            self._send_line("G21")
            self._send_line("G90")
            self.position_mm = [0.0, 0.0, 0.0]
            self.homed = True
        return {"homed": True, "position_mm": [0.0, 0.0, 0.0], "gcode": "$H"}

    def move_to(self, x: float, y: float, z: float, feedrate: float) -> dict[str, Any]:
        if self.estop_active or not self.homed:
            raise GatewayDriverError("GRBL stage must be unlocked and homed before motion.")
        cmd = f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{feedrate:.1f}"
        with self._lock:
            ack = self._send_line(cmd)
            self._send_line("G4 P0")
            self.position_mm = [round(x, 4), round(y, 4), round(z, 4)]
        return {"gcode": cmd, "ack": ack, "position_mm": list(self.position_mm)}

    def rotate(self, axis: str, angle_deg: float, feedrate: float) -> dict[str, Any]:
        if self.estop_active or not self.homed:
            raise GatewayDriverError("GRBL stage must be unlocked and homed before motion.")
        letter = "A" if axis.lower() in ("x", "y", "z", "a") else "A"
        cmd = f"G1 {letter}{angle_deg:.3f} F{feedrate:.1f}"
        with self._lock:
            ack = self._send_line(cmd)
        return {"gcode": cmd, "ack": ack, "angle_deg": angle_deg}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            stream = self._ensure_stream()
            stream.write(b"!")
            if hasattr(stream, "flush"):
                stream.flush()
            self.homed = False
        return {"stopped": True, "realtime_cmd": "!"}

    def emergency_stop(self) -> dict[str, Any]:
        with self._lock:
            stream = self._ensure_stream()
            stream.write(b"!\x18")
            if hasattr(stream, "flush"):
                stream.flush()
            self.estop_active = True
            self.homed = False
        return {"estop": True, "realtime_bytes": "!\\x18"}

    def reset_estop(self) -> dict[str, Any]:
        with self._lock:
            self._send_line("$X")
            self.estop_active = False
            self.homed = False
        return {"estop_cleared": True, "gcode": "$X"}


class KlipperMoonrakerDriver:
    """Real Klipper / Moonraker local HTTP API driver (supports custom httpx transport)."""

    driver_name = "klipper_moonraker"

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise GatewayDriverError("klipper_url must be a valid HTTP(S) origin.")
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self.timeout_s = timeout_s
        self.homed = False
        self.estop_active = False
        self.position_mm = [0.0, 0.0, 0.0]

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
                res = client.post(f"{self.base_url}{path}", json=payload or {})
                res.raise_for_status()
                return res.json() if res.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayDriverError(f"Moonraker request {path} failed: {exc}") from exc

    def home(self) -> dict[str, Any]:
        if self.estop_active:
            raise GatewayDriverError("Cannot home while Klipper emergency stop is engaged.")
        self._post("/printer/gcode/script", {"script": "G28\nG90"})
        self.position_mm = [0.0, 0.0, 0.0]
        self.homed = True
        return {"homed": True, "position_mm": [0.0, 0.0, 0.0], "gcode": "G28; G90"}

    def move_to(self, x: float, y: float, z: float, feedrate: float) -> dict[str, Any]:
        if self.estop_active or not self.homed:
            raise GatewayDriverError("Klipper printer must be homed before motion.")
        cmd = f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{feedrate:.1f}"
        self._post("/printer/gcode/script", {"script": f"{cmd}\nM400"})
        self.position_mm = [round(x, 4), round(y, 4), round(z, 4)]
        return {"gcode": cmd, "position_mm": list(self.position_mm)}

    def rotate(self, axis: str, angle_deg: float, feedrate: float) -> dict[str, Any]:
        if self.estop_active or not self.homed:
            raise GatewayDriverError("Klipper printer must be homed before rotary move.")
        cmd = (
            f"MANUAL_STEPPER STEPPER=stepper_{axis} "
            f"MOVE={angle_deg:.3f} SPEED={feedrate/60.0:.2f}"
        )
        self._post("/printer/gcode/script", {"script": cmd})
        return {"gcode": cmd, "angle_deg": angle_deg}

    def stop(self) -> dict[str, Any]:
        self._post("/printer/gcode/script", {"script": "M400"})
        self.homed = False
        return {"stopped": True, "gcode": "M400"}

    def emergency_stop(self) -> dict[str, Any]:
        self._post("/printer/emergency_stop", {})
        self.estop_active = True
        self.homed = False
        return {"estop": True}

    def reset_estop(self) -> dict[str, Any]:
        self._post("/printer/firmware_restart", {})
        self.estop_active = False
        self.homed = False
        return {"estop_cleared": True}


class GatewayEnvelope(BaseModel):
    """Common strict header shared by /prepare, /stop, and /estop."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1]
    request_id: Annotated[str, Field(min_length=1, max_length=120)]
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    project_id: Annotated[str, Field(min_length=1, max_length=120)]
    revision: Annotated[int, Field(strict=True, ge=0)]


class ExecuteEnvelope(GatewayEnvelope):
    """Strict step execution payload emitted by HttpGatewayAdapter.perform."""

    step_name: Annotated[str, Field(min_length=1, max_length=160)]
    action: Annotated[str, Field(min_length=1, max_length=80)]
    parameters: dict[str, Any] = Field(default_factory=dict)


class ResetEstopRequest(BaseModel):
    """Explicit operator acknowledgment required to recover from E-stop or watchdog trip."""

    model_config = ConfigDict(extra="forbid")
    operator_confirmed: Literal[True]
    clear_reason: Annotated[str, Field(min_length=3, max_length=240)]


def build_driver(config: GatewayConfig) -> MotionDriver:
    if config.driver_kind == "grbl":
        return GrblSerialDriver(port=config.serial_port, baudrate=config.baudrate)
    if config.driver_kind == "klipper":
        if not config.klipper_url:
            raise GatewayDriverError("klipper_url is required when driver_kind='klipper'.")
        return KlipperMoonrakerDriver(base_url=config.klipper_url)
    return LoopbackStageDriver()


class GatewayController:
    """Stateful safety controller enforcing limits, homing, idempotency, and watchdog."""

    def __init__(self, config: GatewayConfig, driver: MotionDriver | None = None) -> None:
        self.config = config
        self.driver: MotionDriver = driver or build_driver(config)
        self._lock = threading.RLock()
        self._idempotency: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._watchdog_timer: threading.Timer | None = None
        self.state: Literal["idle", "ready", "stopped", "estop"] = "idle"
        self.homed: bool = False
        self.estop_engaged: bool = False
        self.watchdog_tripped: bool = False
        self.last_heartbeat_at: float | None = None
        self.position_mm: list[float] = [0.0, 0.0, 0.0]
        self.history: list[dict[str, Any]] = []

    def _cancel_watchdog_locked(self) -> None:
        if self._watchdog_timer is not None:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None

    def _arm_watchdog_locked(self) -> None:
        self._cancel_watchdog_locked()
        timer = threading.Timer(self.config.watchdog_timeout_s, self._on_watchdog_timeout)
        timer.daemon = True
        timer.start()
        self._watchdog_timer = timer

    def _on_watchdog_timeout(self) -> None:
        with self._lock:
            if self.state != "ready":
                return
            LOGGER.warning("Gateway watchdog tripped on %s", self.config.device_id)
            try:
                self.driver.emergency_stop()
            except Exception:
                pass
            self.watchdog_tripped = True
            self.estop_engaged = True
            self.homed = False
            self.state = "estop"
            self.history.append({"event": "watchdog_estop", "ts": time.time()})

    def _check_identity_locked(self, body: GatewayEnvelope) -> None:
        if body.device_id != self.config.device_id:
            raise HTTPException(409, f"Unknown device_id {body.device_id!r}.")
        if body.project_id != self.config.project_id:
            raise HTTPException(409, f"Project mismatch: expected {self.config.project_id!r}.")
        if body.revision != self.config.revision:
            raise HTTPException(409, f"Revision mismatch: expected {self.config.revision}.")

    def _cache_get_locked(self, endpoint: str, request_id: str) -> dict[str, Any] | None:
        return deepcopy(self._idempotency.get((endpoint, request_id)))

    def _cache_put_locked(
        self, endpoint: str, request_id: str, response: dict[str, Any]
    ) -> dict[str, Any]:
        self._idempotency[(endpoint, request_id)] = deepcopy(response)
        self._idempotency.move_to_end((endpoint, request_id))
        while len(self._idempotency) > _MAX_IDEMPOTENCY_ENTRIES:
            self._idempotency.popitem(last=False)
        return response

    def _validate_target_pose_locked(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        lim = self.config.limits_mm
        feed = float(params.get("feedrate_mm_min", 1200.0))
        if not math.isfinite(feed) or feed <= 0 or feed > self.config.max_feedrate_mm_min:
            raise HTTPException(
                422,
                f"Feedrate {feed:g} mm/min outside limit (0, {self.config.max_feedrate_mm_min:g}].",
            )
        act = action.lower()
        if act in ("move", "move_to", "linear_move", "pipette_move", "aspirate", "dispense"):
            if "target_mm" in params and isinstance(params["target_mm"], (list, tuple)):
                if len(params["target_mm"]) != 3:
                    raise HTTPException(422, "target_mm must have 3 coordinates.")
                tx, ty, tz = [float(v) for v in params["target_mm"]]
            elif any(k in params for k in ("x_mm", "y_mm", "z_mm")):
                tx = float(params.get("x_mm", self.position_mm[0]))
                ty = float(params.get("y_mm", self.position_mm[1]))
                tz = float(params.get("z_mm", self.position_mm[2]))
            elif "translation_mm" in params and isinstance(params["translation_mm"], (list, tuple)):
                dx, dy, dz = [float(v) for v in params["translation_mm"]]
                tx = self.position_mm[0] + dx
                ty = self.position_mm[1] + dy
                tz = self.position_mm[2] + dz
            else:
                tx, ty, tz = (
                    self.position_mm[0] + float(params.get("dx_mm", 0.0)),
                    self.position_mm[1] + float(params.get("dy_mm", 0.0)),
                    self.position_mm[2] + float(params.get("dz_mm", 0.0)),
                )
            if not all(math.isfinite(v) for v in (tx, ty, tz)):
                raise HTTPException(422, "Target coordinates must be finite.")
            if not (lim.x_min <= tx <= lim.x_max):
                raise HTTPException(
                    422, f"Software X limit exceeded: {tx:g} mm not in [{lim.x_min}, {lim.x_max}]."
                )
            if not (lim.y_min <= ty <= lim.y_max):
                raise HTTPException(
                    422, f"Software Y limit exceeded: {ty:g} mm not in [{lim.y_min}, {lim.y_max}]."
                )
            if not (lim.z_min <= tz <= lim.z_max):
                raise HTTPException(
                    422, f"Software Z limit exceeded: {tz:g} mm not in [{lim.z_min}, {lim.z_max}]."
                )
            return {"kind": "move", "x": tx, "y": ty, "z": tz, "feedrate": feed}
        if act in ("rotate", "index"):
            axis = str(params.get("axis", "z")).lower()
            angle = float(params.get("angle_deg", 0.0))
            if axis not in ("x", "y", "z", "a") or not math.isfinite(angle) or abs(angle) > 360:
                raise HTTPException(422, f"Invalid rotation axis/angle: {axis!r}, {angle!r}.")
            return {"kind": "rotate", "axis": axis, "angle_deg": angle, "feedrate": feed}
        if act == "home":
            return {"kind": "home"}
        if act in ("wait", "pause", "operator_prompt", "inspect", "hold"):
            secs = float(params.get("seconds", 0.0))
            if not math.isfinite(secs) or secs < 0 or secs > 60:
                raise HTTPException(422, "Wait duration must be in [0, 60] seconds.")
            return {"kind": "noop", "seconds": secs}
        raise HTTPException(422, f"Unsupported gateway action {action!r}.")

    def prepare(self, body: GatewayEnvelope) -> dict[str, Any]:
        with self._lock:
            self._check_identity_locked(body)
            cached = self._cache_get_locked("prepare", body.request_id)
            if cached is not None:
                return cached
            if not self.config.commissioning_reviewed:
                raise HTTPException(
                    403, "Gateway configuration has commissioning_reviewed=false; review first."
                )
            if self.estop_engaged or self.watchdog_tripped:
                raise HTTPException(
                    409,
                    "Emergency stop or watchdog fault is active; call POST /reset-estop first.",
                )
            driver_ack = self.driver.home() if self.config.auto_home_on_prepare else {"homed": True}
            self.homed = bool(driver_ack.get("homed", True))
            self.position_mm = list(driver_ack.get("position_mm", [0.0, 0.0, 0.0]))
            self.state = "ready"
            self.last_heartbeat_at = time.time()
            self._arm_watchdog_locked()
            res = {
                "schema_version": 1,
                "accepted": True,
                "request_id": body.request_id,
                "device_id": self.config.device_id,
                "project_id": self.config.project_id,
                "revision": self.config.revision,
                "status": "ready",
                "homed": self.homed,
                "position_mm": list(self.position_mm),
                "driver": self.driver.driver_name,
            }
            self.history.append({"event": "prepare", "request_id": body.request_id})
            return self._cache_put_locked("prepare", body.request_id, res)

    def execute(self, body: ExecuteEnvelope) -> dict[str, Any]:
        with self._lock:
            self._check_identity_locked(body)
            cached = self._cache_get_locked("execute", body.request_id)
            if cached is not None:
                return cached
            if self.estop_engaged or self.watchdog_tripped:
                raise HTTPException(409, "Execution blocked: emergency stop or watchdog tripped.")
            if self.state != "ready" or not self.homed:
                raise HTTPException(409, "Stage is not prepared and homed. Call /prepare first.")

            plan = self._validate_target_pose_locked(body.action, body.parameters)
            try:
                if plan["kind"] == "move":
                    drv = self.driver.move_to(plan["x"], plan["y"], plan["z"], plan["feedrate"])
                    fallback_pos = [plan["x"], plan["y"], plan["z"]]
                    self.position_mm = list(drv.get("position_mm", fallback_pos))
                elif plan["kind"] == "rotate":
                    drv = self.driver.rotate(plan["axis"], plan["angle_deg"], plan["feedrate"])
                elif plan["kind"] == "home":
                    drv = self.driver.home()
                    self.position_mm = list(drv.get("position_mm", [0.0, 0.0, 0.0]))
                else:
                    drv = {"noop": True, "seconds": plan["seconds"]}
            except GatewayDriverError as exc:
                self._cancel_watchdog_locked()
                self.state = "stopped"
                raise HTTPException(500, str(exc)) from None

            self.last_heartbeat_at = time.time()
            self._arm_watchdog_locked()
            res = {
                "schema_version": 1,
                "accepted": True,
                "request_id": body.request_id,
                "device_id": self.config.device_id,
                "project_id": self.config.project_id,
                "revision": self.config.revision,
                "status": "completed",
                "step_name": body.step_name,
                "action": body.action,
                "position_mm": list(self.position_mm),
                "driver_result": drv,
            }
            self.history.append(
                {
                    "event": "execute",
                    "request_id": body.request_id,
                    "step_name": body.step_name,
                    "position_mm": list(self.position_mm),
                }
            )
            return self._cache_put_locked("execute", body.request_id, res)

    def stop(self, body: GatewayEnvelope) -> dict[str, Any]:
        with self._lock:
            self._check_identity_locked(body)
            cached = self._cache_get_locked("stop", body.request_id)
            if cached is not None:
                return cached
            self._cancel_watchdog_locked()
            drv = self.driver.stop()
            self.homed = False
            self.state = "stopped"
            res = {
                "schema_version": 1,
                "accepted": True,
                "request_id": body.request_id,
                "device_id": self.config.device_id,
                "project_id": self.config.project_id,
                "revision": self.config.revision,
                "status": "stopped",
                "driver_result": drv,
            }
            self.history.append({"event": "stop", "request_id": body.request_id})
            return self._cache_put_locked("stop", body.request_id, res)

    def estop(self, body: GatewayEnvelope) -> dict[str, Any]:
        with self._lock:
            self._check_identity_locked(body)
            self._cancel_watchdog_locked()
            drv = self.driver.emergency_stop()
            self.estop_engaged = True
            self.homed = False
            self.state = "estop"
            res = {
                "schema_version": 1,
                "accepted": True,
                "request_id": body.request_id,
                "device_id": self.config.device_id,
                "project_id": self.config.project_id,
                "revision": self.config.revision,
                "status": "estop",
                "driver_result": drv,
            }
            self.history.append({"event": "estop", "request_id": body.request_id})
            return res

    def reset_estop(self, body: ResetEstopRequest) -> dict[str, Any]:
        with self._lock:
            drv = self.driver.reset_estop()
            self.estop_engaged = False
            self.watchdog_tripped = False
            self.homed = False
            self.state = "idle"
            self.history.append({"event": "reset_estop", "reason": body.clear_reason})
            return {
                "schema_version": 1,
                "accepted": True,
                "state": self.state,
                "estop_engaged": False,
                "watchdog_tripped": False,
                "driver_result": drv,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": 1,
                "device_id": self.config.device_id,
                "project_id": self.config.project_id,
                "revision": self.config.revision,
                "driver": self.driver.driver_name,
                "state": self.state,
                "homed": self.homed,
                "estop_engaged": self.estop_engaged,
                "watchdog_tripped": self.watchdog_tripped,
                "watchdog_timeout_s": self.config.watchdog_timeout_s,
                "commissioning_reviewed": self.config.commissioning_reviewed,
                "limits_mm": self.config.limits_mm.model_dump(),
                "position_mm": list(self.position_mm),
                "history_len": len(self.history),
            }


def create_hardware_gateway_app(
    config: GatewayConfig,
    driver: MotionDriver | None = None,
) -> FastAPI:
    """Create the local HTTP hardware gateway matching concept_controller.HttpGatewayAdapter."""
    ctrl = GatewayController(config, driver=driver)
    gw = FastAPI(title="AutoLab Hardware Gateway", version="1.0.0")
    gw.state.controller = ctrl

    @gw.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "device_id": config.device_id, "driver": ctrl.driver.driver_name}

    @gw.get("/status")
    def status() -> dict[str, Any]:
        return ctrl.status()

    @gw.post("/prepare")
    def prepare(body: GatewayEnvelope) -> dict[str, Any]:
        return ctrl.prepare(body)

    @gw.post("/execute")
    def execute(body: ExecuteEnvelope) -> dict[str, Any]:
        return ctrl.execute(body)

    @gw.post("/stop")
    def stop(body: GatewayEnvelope) -> dict[str, Any]:
        return ctrl.stop(body)

    @gw.post("/estop")
    def estop(body: GatewayEnvelope) -> dict[str, Any]:
        return ctrl.estop(body)

    @gw.post("/reset-estop")
    def reset_estop(body: ResetEstopRequest) -> dict[str, Any]:
        return ctrl.reset_estop(body)

    @gw.post("/heartbeat")
    def heartbeat() -> dict[str, Any]:
        with ctrl._lock:
            if ctrl.state == "ready":
                ctrl.last_heartbeat_at = time.time()
                ctrl._arm_watchdog_locked()
            return {"status": ctrl.state, "watchdog_tripped": ctrl.watchdog_tripped}

    return gw


def build_gateway_commissioning_checklist(
    project: dict,
    *,
    driver_kind: Literal["loopback", "grbl", "klipper"] = "grbl",
    watchdog_timeout_s: float = 30.0,
    port: int = 8787,
) -> dict[str, Any]:
    """Build project-bound hardware gateway config template + E-stop commissioning checklist."""
    project_id = str(project.get("id", "autolab-project"))
    revision = int(project.get("revision", 1) or 1)
    summary = project.get("summary") or {}
    x_max = min(float(summary.get("workspace_width_mm") or 250.0), 400.0)
    y_max = min(float(summary.get("workspace_depth_mm") or 250.0), 400.0)
    z_max = min(float(summary.get("workspace_height_mm") or 150.0), 250.0)

    config_template = {
        "schema_version": 1,
        "base_url": f"http://127.0.0.1:{port}",
        "device_id": f"{project_id}-stage-1",
        "project_id": project_id,
        "revision": revision,
        "driver_kind": driver_kind,
        "commissioning_reviewed": False,
        "watchdog_timeout_s": float(watchdog_timeout_s),
        "timeout_seconds": 5,
        "limits_mm": {
            "x_min": 0.0,
            "x_max": round(max(50.0, x_max), 2),
            "y_min": 0.0,
            "y_max": round(max(50.0, y_max), 2),
            "z_min": 0.0,
            "z_max": round(max(30.0, z_max), 2),
        },
        "actions": {},
    }
    concept = project.get("concept_design") or {}
    steps = (concept.get("steps") or []) if isinstance(concept, dict) else []
    for step in steps:
        if isinstance(step, dict) and step.get("name"):
            t = step.get("translation_mm") or [0.0, 0.0, 0.0]
            target = [max(0.0, float(v)) for v in t]
            config_template["actions"][step["name"]] = {
                "action": "move_to",
                "parameters": {"target_mm": target, "feedrate_mm_min": 1200.0},
            }

    checklist_items = [
        {
            "id": "estop-physical-circuit",
            "title": "Independent physical E-stop mushroom switch verified",
            "requirement": (
                "Hardwired NC E-stop cuts motor PSU coil directly without relying on USB/HTTP."
            ),
            "passed": False,
        },
        {
            "id": "endstops-and-homing",
            "title": "X/Y/Z limit switches tested manually before $H / G28 homing",
            "requirement": (
                "Trigger each switch by hand while checking controller status before homing."
            ),
            "passed": False,
        },
        {
            "id": "software-travel-bounds",
            "title": "Software limits_mm verified with ruler against physical hard stops",
            "requirement": (
                "Verify 422 rejection on an intentional out-of-range loopback move before arming."
            ),
            "passed": False,
        },
        {
            "id": "watchdog-auto-stop",
            "title": "Watchdog timeout and /estop recovery verified",
            "requirement": (
                "Confirm motion locks out after watchdog expiry and needs /reset-estop."
            ),
            "passed": False,
        },
        {
            "id": "dry-run-10-cycles",
            "title": "Empty-deck dry run (10 cycles, zero crashes/drops)",
            "requirement": (
                "Complete 10 full cycles with empty labware before liquid or biological samples."
            ),
            "passed": False,
        },
    ]
    markdown_lines = [
        f"# Hardware Gateway & E-Stop Commissioning Checklist ({project_id} r{revision})",
        "",
        f"Driver target: `{driver_kind}` · Watchdog timeout: `{watchdog_timeout_s:g} s`",
        "",
        "> Software stop (`POST /stop` and `POST /estop`) NEVER replaces a physical NC "
        "emergency-stop power circuit. Keep `commissioning_reviewed: false` until verified.",
        "",
        "## Mandatory Pre-Arm Checklist",
        "",
    ]
    for item in checklist_items:
        markdown_lines.append(f"- [ ] **{item['title']}** — {item['requirement']}")
    markdown_lines.extend(["", "## Bound Gateway Configuration Template", "", "```json"])
    import json

    markdown_lines.append(json.dumps(config_template, indent=2))
    markdown_lines.append("```")

    return {
        "project_id": project_id,
        "revision": revision,
        "driver_kind": driver_kind,
        "commissioning_reviewed": False,
        "config_template": config_template,
        "checklist": checklist_items,
        "markdown": "\n".join(markdown_lines),
    }


def is_loopback_origin(url: str) -> bool:
    """Allow local gateway probing only against loopback hosts to prevent SSRF."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        resolved = socket.gethostbyname(host)
        return resolved.startswith("127.")
    except OSError:
        return False
