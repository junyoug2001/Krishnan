"""K10CR2 native Kinesis adapter. A motor service process owns every call."""
from __future__ import annotations

import ctypes as C
from dataclasses import asdict, dataclass, field
import math
import os
from pathlib import Path
import time


MAX_SPEED = 15.0
MAX_ACCELERATION = 10.0


class MotorStopUnverified(RuntimeError):
    """The controller still reports movement; retain its handle for recovery."""


def number(value, low, high, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return value


@dataclass
class MotorSettings:
    label: str = "Rotation mount"
    velocity: float = 5.0
    acceleration: float = 5.0
    home_velocity: float = 5.0
    jog_step: float = 5.0
    jog_velocity: float = 5.0
    jog_acceleration: float = 5.0
    jog_mode: str = "step"
    stop_mode: str = "profiled"
    backlash: float = 1.0
    direction: int = 0
    drive_speeds: list[float] = field(default_factory=lambda: [2.5, 5.0, 7.5, 10.0])

    @classmethod
    def parse(cls, values):
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown motor setting")
        result = cls(**values)
        if not isinstance(result.label, str) or not 1 <= len(result.label.strip()) <= 50:
            raise ValueError("Motor label must contain 1–50 characters")
        for key, low, high in (("velocity", 0.1, MAX_SPEED),
                               ("acceleration", 0.1, MAX_ACCELERATION),
                               ("home_velocity", 0.1, 10.0), ("jog_step", 0.001, 360),
                               ("jog_velocity", 0.1, MAX_SPEED),
                               ("jog_acceleration", 0.1, MAX_ACCELERATION),
                               ("backlash", 0, 5)):
            setattr(result, key, number(getattr(result, key), low, high, key))
        if result.jog_mode not in ("step", "held", "continuous"):
            raise ValueError("Invalid jog mode")
        if result.stop_mode not in ("profiled", "immediate"):
            raise ValueError("Invalid stop mode")
        if type(result.direction) is not int or result.direction not in (0, 1, 2):
            raise ValueError("Invalid move direction")
        if not isinstance(result.drive_speeds, list) or len(result.drive_speeds) != 4:
            raise ValueError("Four drive speeds are required")
        result.drive_speeds = [number(v, 0.1, MAX_SPEED, "Drive speed") for v in result.drive_speeds]
        return result


class DeviceInfo(C.Structure):
    _pack_ = 1
    _fields_ = [("type_id", C.c_uint32), ("description", C.c_char * 65),
                ("serial", C.c_char * 16), ("pid", C.c_uint32),
                ("known", C.c_bool), ("motor", C.c_int32),
                ("piezo", C.c_bool), ("laser", C.c_bool), ("custom", C.c_bool),
                ("rack", C.c_bool), ("channels", C.c_int16)]


class HardwareInfo(C.Structure):
    _pack_ = 1
    _fields_ = [("serial", C.c_uint32), ("model", C.c_char * 8),
                ("type_id", C.c_uint16), ("firmware", C.c_uint32),
                ("notes", C.c_char * 48), ("data", C.c_uint8 * 12),
                ("hardware_version", C.c_uint16), ("modification", C.c_uint16),
                ("channels", C.c_int16)]


class KinesisSDK:
    def __init__(self, folder: str):
        if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("Kinesis requires 64-bit Windows Python")
        self.folder = Path(folder)
        self._directory = os.add_dll_directory(str(self.folder))
        try:
            self.dll = C.CDLL(str(self.folder / "Thorlabs.MotionControl.IntegratedStepperMotors.dll"))
            self._bind()
        except BaseException:
            self._directory.close()
            raise

    def _bind(self):
        def bind(name, result, *args):
            fn = getattr(self.dll, name)
            fn.restype, fn.argtypes = result, list(args)
            setattr(self, name, fn)
        s, short, integer = C.c_char_p, C.c_int16, C.c_int32
        bind("TLI_BuildDeviceList", short)
        bind("TLI_GetDeviceListExt", short, C.c_void_p, C.c_uint32)
        bind("TLI_GetDeviceInfo", short, s, C.POINTER(DeviceInfo))
        for name in ("Open", "RequestStatus", "RequestVelParams", "RequestJogParams",
                     "RequestHomingParams", "RequestBacklash", "EnableChannel", "DisableChannel",
                     "Home", "StopImmediate", "StopProfiled"):
            bind("ISC_" + name, short, s)
        for name in ("Close", "StopPolling", "ClearMessageQueue"):
            bind("ISC_" + name, None, s)
        bind("ISC_Identify", None, s)
        for name in ("CheckConnection", "LoadSettings"):
            bind("ISC_" + name, C.c_bool, s)
        bind("ISC_StartPolling", C.c_bool, s, integer)
        bind("ISC_GetHardwareInfoBlock", short, s, C.POINTER(HardwareInfo))
        bind("ISC_GetStatusBits", C.c_uint32, s)
        for name in ("GetPosition", "GetStageAxisMinPos", "GetStageAxisMaxPos", "GetBacklash"):
            bind("ISC_" + name, integer, s)
        bind("ISC_GetSoftLimitMode", short, s)
        bind("ISC_SetLimitsSoftwareApproachPolicy", None, s, short)
        bind("ISC_SetRotationModes", short, s, short, short)
        bind("ISC_GetRealValueFromDeviceUnit", short, s, integer, C.POINTER(C.c_double), integer)
        bind("ISC_GetDeviceUnitFromRealValue", short, s, C.c_double, C.POINTER(integer), integer)
        for name in ("SetVelParams", "SetJogVelParams"):
            bind("ISC_" + name, short, s, integer, integer)
        for name in ("GetVelParams", "GetJogVelParams"):
            bind("ISC_" + name, short, s, C.POINTER(integer), C.POINTER(integer))
        bind("ISC_SetHomingVelocity", short, s, C.c_uint32)
        bind("ISC_GetHomingVelocity", C.c_uint32, s)
        bind("ISC_SetJogStepSize", short, s, C.c_uint32)
        bind("ISC_GetJogStepSize", C.c_uint32, s)
        bind("ISC_SetJogMode", short, s, short, short)
        bind("ISC_SetBacklash", short, s, integer)
        for name in ("MoveToPosition", "MoveRelative"):
            bind("ISC_" + name, short, s, integer)
        for name in ("MoveJog", "MoveAtVelocity"):
            bind("ISC_" + name, short, s, short)
        bind("ISC_GetNextMessage", C.c_bool, s, C.POINTER(C.c_uint16),
             C.POINTER(C.c_uint16), C.POINTER(C.c_uint32))

    @staticmethod
    def check(operation, code):
        if code:
            detail = "; close Kinesis and check the USB connection" if code in (2, 3, 7) else ""
            raise RuntimeError(f"Kinesis {operation}: error {code}{detail}")

    def discover(self):
        self.check("BuildDeviceList", self.TLI_BuildDeviceList())
        buf = C.create_string_buffer(4096)
        self.check("GetDeviceList", self.TLI_GetDeviceListExt(buf, len(buf)))
        result = []
        for serial in buf.value.decode("ascii").split(","):
            if not serial.strip():
                continue
            info = DeviceInfo()
            if self.TLI_GetDeviceInfo(serial.encode("ascii"), C.byref(info)) == 1 and info.type_id == 55:
                result.append({"serial": serial, "description": info.description.decode("ascii")})
        return result


class KinesisMotor:
    def __init__(self, sdk: KinesisSDK, serial: str, values: dict, log):
        self.sdk, self.serial, self.s, self.log = sdk, serial, serial.encode("ascii"), log
        self.settings = MotorSettings.parse(values)
        self.opened = self.polling = False
        self.operation = None
        self.last_error = ""
        self.lower, self.upper = 0.0, 360.0
        self._cached = {}

    def call(self, name, *args):
        code = getattr(self.sdk, "ISC_" + name)(self.s, *args)
        self.sdk.check(name, code)

    def raw(self, value, unit=0):
        result = C.c_int32()
        self.call("GetDeviceUnitFromRealValue", float(value), C.byref(result), unit)
        return result.value

    def real(self, value, unit=0):
        result = C.c_double()
        self.call("GetRealValueFromDeviceUnit", value, C.byref(result), unit)
        if not math.isfinite(result.value):
            raise RuntimeError("Invalid unit conversion")
        return result.value

    def connect(self):
        self.call("Open")
        self.opened = True
        try:
            if not self.sdk.ISC_StartPolling(self.s, 200):
                raise RuntimeError("Cannot start motor polling")
            self.polling = True
            self.call("RequestStatus")
            time.sleep(0.6)
            if self.sdk.ISC_GetStatusBits(self.s) & 0x2F0:
                raise RuntimeError("Mount is already moving; stop it in its current controller")
            hardware = HardwareInfo()
            self.call("GetHardwareInfoBlock", C.byref(hardware))
            if hardware.model.rstrip(b"\0") != b"K10CR2" or hardware.serial != int(self.serial):
                raise RuntimeError("Expected K10CR2 hardware identity was not returned")
            if not self.sdk.ISC_LoadSettings(self.s):
                raise RuntimeError("Cannot load this serial's Kinesis stage profile")
            time.sleep(0.6)
            if abs(self.raw(360) - 49152000) > 1:
                raise RuntimeError("Stage profile has an incorrect K10CR2 degree scale")
            for value in (0, 60, 360):
                if abs(self.real(self.raw(value)) - value) > 0.0001:
                    raise RuntimeError("Degree conversion round trip failed")
            self.lower = self.real(self.sdk.ISC_GetStageAxisMinPos(self.s))
            self.upper = self.real(self.sdk.ISC_GetStageAxisMaxPos(self.s))
            if abs(self.lower) > 0.001 or abs(self.upper - 360) > 0.001:
                raise RuntimeError("Expected a 0–360 degree K10CR2 profile")
            self.apply(asdict(self.settings))
            self.log(f"Motor {self.serial}: connected; K10CR2, degree scale and limits verified")
        except BaseException:
            self.close(stop=False)
            raise

    def _idle(self, *, homed=False, enabled=False):
        current = self.snapshot()
        if self.operation or current["moving"]:
            raise RuntimeError("Stop the motor before starting another operation")
        if homed and not current["homed"]:
            raise RuntimeError("Home this motor before using absolute moves, jog or drive")
        if enabled and not current["enabled"]:
            raise RuntimeError("Enable this motor first")
        return current

    def apply(self, values):
        self._idle()
        cfg = MotorSettings.parse(values)
        self.call("SetVelParams", self.raw(cfg.acceleration, 2), self.raw(cfg.velocity, 1))
        self.call("SetJogVelParams", self.raw(cfg.jog_acceleration, 2), self.raw(cfg.jog_velocity, 1))
        self.call("SetHomingVelocity", self.raw(cfg.home_velocity, 1))
        self.call("SetJogStepSize", self.raw(cfg.jog_step))
        self.call("SetJogMode", 2 if cfg.jog_mode == "step" else 1,
                  2 if cfg.stop_mode == "profiled" else 1)
        self.call("SetBacklash", self.raw(cfg.backlash))
        self.call("SetRotationModes", 2, cfg.direction)
        self.sdk.ISC_SetLimitsSoftwareApproachPolicy(self.s, 0)
        self.call("RequestVelParams")
        self.call("RequestJogParams")
        self.call("RequestHomingParams")
        self.call("RequestBacklash")
        time.sleep(0.4)
        a, v, ja, jv = C.c_int32(), C.c_int32(), C.c_int32(), C.c_int32()
        self.call("GetVelParams", C.byref(a), C.byref(v))
        self.call("GetJogVelParams", C.byref(ja), C.byref(jv))
        reads = [(self.real(a.value, 2), cfg.acceleration),
                 (self.real(v.value, 1), cfg.velocity),
                 (self.real(ja.value, 2), cfg.jog_acceleration),
                 (self.real(jv.value, 1), cfg.jog_velocity),
                 (self.real(self.sdk.ISC_GetHomingVelocity(self.s), 1), cfg.home_velocity),
                 (self.real(self.sdk.ISC_GetJogStepSize(self.s)), cfg.jog_step),
                 (self.real(self.sdk.ISC_GetBacklash(self.s)), cfg.backlash)]
        if any(abs(actual - expected) > 0.02 for actual, expected in reads):
            raise RuntimeError("Motor settings readback does not match the requested values")
        if self.sdk.ISC_GetSoftLimitMode(self.s) != 0:
            raise RuntimeError("SDK did not enable rejection of moves outside the travel range")
        self.settings = cfg
        self.last_error = ""

    def snapshot(self):
        if not self.opened or not self.sdk.ISC_CheckConnection(self.s):
            raise RuntimeError("Motor USB connection is unavailable")
        bits = self.sdk.ISC_GetStatusBits(self.s)
        self._cached = {"serial": self.serial, "connected": True, "model": "K10CR2/M",
                        "position": self.real(self.sdk.ISC_GetPosition(self.s)),
                        "homed": bool(bits & 0x400), "moving": bool(bits & 0x2F0),
                        "enabled": bool(bits & 0x80000000), "forward_limit": bool(bits & 1),
                        "reverse_limit": bool(bits & 2), "status_bits": f"0x{bits:08X}",
                        "operation": self.operation["kind"] if self.operation else "Idle",
                        "settings": asdict(self.settings), "limits": [self.lower, self.upper],
                        "error": self.last_error, "observed": time.time()}
        return dict(self._cached)

    def _begin(self, kind, *, timeout, target=None, direction=0, token=None, velocity=0, acceleration=1):
        self.sdk.ISC_ClearMessageQueue(self.s)
        now = time.monotonic()
        self.operation = {"kind": kind, "deadline": now + timeout, "target": target,
                          "direction": direction, "token": token, "hold_at": now,
                          "velocity": velocity, "acceleration": acceleration, "completed": False}
        self.last_error = ""
        self.log(f"Motor {self.serial}: {kind}" + (f" to {target:.4f}°" if target is not None else ""))

    def home(self):
        self._idle(enabled=True)
        self._begin("Homing", timeout=360 / self.settings.home_velocity + 25, target=0)
        try:
            self.call("Home")
        except BaseException:
            self.operation = None
            raise

    def move_to(self, target):
        self._idle(homed=True, enabled=True)
        target = number(target, self.lower, self.upper, "Target angle")
        self._begin("Moving", timeout=360 / self.settings.velocity + 20, target=target)
        try:
            self.call("MoveToPosition", self.raw(target))
        except BaseException:
            self.operation = None
            raise

    def jog(self, direction, token=None, *, drive=None):
        current = self._idle(homed=True, enabled=True)
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError("Invalid jog direction")
        cfg = self.settings
        if drive is None and cfg.jog_mode == "step":
            target = number(current["position"] + direction * cfg.jog_step,
                            self.lower, self.upper, "Jog target")
            self._begin("Jog step", timeout=cfg.jog_step / cfg.jog_velocity + 20, target=target)
            command, args = "MoveJog", (1 if direction > 0 else 2,)
        else:
            if drive is not None and (type(drive) is not int or not 0 <= drive < 4):
                raise ValueError("Invalid drive preset")
            velocity = cfg.drive_speeds[drive] if drive is not None else cfg.jog_velocity
            acceleration = cfg.acceleration if drive is not None else cfg.jog_acceleration
            held = drive is not None or cfg.jog_mode == "held"
            if held and (not isinstance(token, str) or not token):
                raise ValueError("Held motion requires a GUI lease")
            margin = velocity * velocity / (2 * acceleration) + velocity * 0.3 + 0.1
            remaining = self.upper - current["position"] if direction > 0 else current["position"] - self.lower
            if remaining <= margin:
                raise ValueError("Too close to the travel boundary for continuous motion")
            self._begin("Drive held" if drive is not None else "Jog held" if held else "Jog continuous",
                        timeout=60, direction=direction, token=token if held else None,
                        velocity=velocity, acceleration=acceleration)
            if drive is not None:
                command, args = "MoveAtVelocity", (1 if direction > 0 else 2,)
            else:
                command, args = "MoveJog", (1 if direction > 0 else 2,)
        try:
            if drive is not None:
                self.call("SetVelParams", self.raw(acceleration, 2), self.raw(velocity, 1))
            self.call(command, *args)
        except BaseException:
            self.operation = None
            raise

    def hold(self, token):
        if self.operation and self.operation["token"] == token:
            self.operation["hold_at"] = time.monotonic()

    def release(self, token):
        if self.operation and self.operation["token"] == token:
            self.stop()

    def stop(self, immediate=False):
        if not self.opened:
            return
        current = self.snapshot()
        if not self.operation and not current["moving"]:
            return
        self.call("StopImmediate" if immediate or self.settings.stop_mode == "immediate" else "StopProfiled")
        self._begin("Stopping", timeout=8)

    def tick(self):
        current = self.snapshot()
        op = self.operation
        if op is None:
            if current["moving"]:
                self.stop(immediate=True)
                self.last_error = "Unrequested motion detected; stop requested"
            return self.snapshot()
        mt, mi, md = C.c_uint16(), C.c_uint16(), C.c_uint32()
        for _ in range(100):
            if not self.sdk.ISC_GetNextMessage(self.s, C.byref(mt), C.byref(mi), C.byref(md)):
                break
            if mt.value == 2 and mi.value == (0 if op["kind"] == "Homing" else 1):
                op["completed"] = True
        now = time.monotonic()
        if op["kind"] == "Stopping" and not current["moving"]:
            self._finish(current)
        elif now >= op["deadline"]:
            if op["kind"] == "Stopping":
                raise RuntimeError("Motor did not confirm stopping within eight seconds")
            self.stop(immediate=True)
            self.last_error = "Motion timeout; stop requested"
        elif op["token"] and now - op["hold_at"] > 0.8:
            self.stop(immediate=True)
            self.last_error = "Held control released or GUI heartbeat lost; stopped"
        elif op["direction"]:
            margin = op["velocity"] ** 2 / (2 * op["acceleration"]) + op["velocity"] * 0.3 + 0.1
            remaining = self.upper - current["position"] if op["direction"] > 0 else current["position"] - self.lower
            if remaining <= margin:
                self.stop()
                self.last_error = "Stopped before the configured travel boundary"
        elif op["completed"] and not current["moving"]:
            if not current["homed"]:
                raise RuntimeError("Motion completed without a valid home reference")
            delta = abs((current["position"] - op["target"] + 180) % 360 - 180)
            if delta > 0.1:
                raise RuntimeError(f"Position mismatch: target {op['target']:.4f}, readback {current['position']:.4f}")
            self._finish(current)
        return self.snapshot()

    def _finish(self, current):
        self.log(f"Motor {self.serial}: stopped at {current['position']:.4f}°")
        self.operation = None
        # Drive temporarily uses a preset; restore the normal move profile.
        self.call("SetVelParams", self.raw(self.settings.acceleration, 2), self.raw(self.settings.velocity, 1))

    def enable(self, on):
        self._idle()
        self.call("EnableChannel" if on else "DisableChannel")
        self.log(f"Motor {self.serial}: {'enabled' if on else 'disabled'}")

    def identify(self):
        self.call("Identify")

    def close(self, *, stop=True):
        if not self.opened:
            return
        code = self.sdk.ISC_StopImmediate(self.s) if stop else 0
        if stop and code == 0 and self.sdk.ISC_CheckConnection(self.s):
            deadline = time.monotonic() + 3
            while self.sdk.ISC_GetStatusBits(self.s) & 0x2F0:
                if time.monotonic() >= deadline:
                    raise MotorStopUnverified("Controller still reports movement; SDK handle retained")
                time.sleep(0.1)
        try:
            if stop:
                self.sdk.check("StopImmediate during close", code)
        finally:
            try:
                if self.polling:
                    self.sdk.ISC_StopPolling(self.s)
            finally:
                self.sdk.ISC_Close(self.s)
                self.opened = self.polling = False
                self.operation = None
