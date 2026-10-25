"""Persistent motor process with serial SDK ownership and bounded motion leases."""
from __future__ import annotations

from dataclasses import asdict
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
from pathlib import Path
import json
import os
import queue
import secrets
import sys
import threading
import time

from .device_backend import _lock_file, report
from .kinesis_motor import KinesisMotor, KinesisSDK, MotorSettings, MotorStopUnverified
from .settings import SETTINGS_PATH, load_settings
from .backend_console import BackendMonitor, backend_logging

PREFERENCES_PATH = SETTINGS_PATH.with_name("motor-settings.json")


class MotorHub:
    def __init__(self, emit, *, sdk=None, preferences_path=PREFERENCES_PATH):
        self.emit = emit
        self.preferences_path = Path(preferences_path)
        self.config = load_settings()
        self.serials = self.config["kinesis_serials"]
        saved = json.loads(self.preferences_path.read_text(encoding="utf-8")) if self.preferences_path.exists() else {}
        self.preferences = {s: asdict(MotorSettings.parse(saved.get(s, {"label": f"Motor {i + 1}"})))
                            for i, s in enumerate(self.serials)}
        self.sdk = sdk
        self.motors = {}
        self.states = {}
        self.available = []
        self.closing = False
        self._fault_retry = {}
        for s in self.serials:
            self._disconnected(s)

    def _disconnected(self, serial, error=""):
        self.states[serial] = {"serial": serial, "connected": False, "position": None,
                               "homed": False, "moving": False, "enabled": False,
                               "operation": "Disconnected", "limits": [0, 360],
                               "settings": self.preferences[serial], "error": error,
                               "observed": time.time()}

    def publish(self):
        self.emit("motors", {"protocol": 1, "motors": list(self.states.values()),
                             "available": self.available, "closing": self.closing})

    def dispatch(self, method, *args, **kwargs):
        allowed = {"discover", "connect", "disconnect", "move", "home", "jog", "hold",
                   "release", "stop", "enable", "identify", "apply", "status"}
        if method not in allowed:
            raise ValueError("Unknown motor operation")
        if self.closing and method not in {"stop", "status", "disconnect", "release"}:
            raise RuntimeError("Motor backend is shutting down")
        if method in {"connect", "discover", "apply"} and any(m.operation for m in self.motors.values()):
            raise RuntimeError("Stop all motors before discovery, connecting or changing settings")
        getattr(self, method)(*args, **kwargs)
        self.tick()

    def _sdk(self):
        if self.sdk is None:
            self.sdk = KinesisSDK(self.config["kinesis_sdk_root"])
        return self.sdk

    def discover(self):
        self.available = self._sdk().discover()
        report("Kinesis discovery: " + ", ".join(x["serial"] for x in self.available))

    def connect(self, serial=None):
        self.discover()
        targets = self.serials if serial is None else [serial]
        for s in targets:
            if s not in self.serials:
                raise ValueError("Serial is not configured")
            if s in self.motors:
                continue
            motor = KinesisMotor(self._sdk(), s, self.preferences[s], report)
            try:
                motor.connect()
            except Exception as exc:
                self._disconnected(s, str(exc))
                self.emit("motor_error", f"{s}: {exc}")
                report(f"Motor {s}: connection failed: {exc}")
            else:
                self.motors[s] = motor
                self._fault_retry.pop(s, None)
                self.states[s] = motor.snapshot()

    def _motor(self, serial):
        if serial not in self.motors:
            raise RuntimeError("Connect this motor first")
        return self.motors[serial]

    def disconnect(self, serial=None):
        errors = []
        for s in list(self.motors) if serial is None else [serial]:
            motor = self.motors.get(s)
            if motor is None:
                continue
            try:
                motor.close()
            except MotorStopUnverified as exc:
                errors.append(f"{s}: {exc}")
                motor.last_error = str(exc)
                self.states[s] = motor.snapshot()
                continue
            except Exception as exc:
                errors.append(f"{s}: {exc}")
                report(f"Motor {s}: SDK close reported an error: {exc}")
            finally:
                if not motor.opened:
                    del self.motors[s]
                    self._fault_retry.pop(s, None)
                    self._disconnected(s, errors[-1] if errors and errors[-1].startswith(s) else "")
            report(f"Motor {s}: disconnected")
        if errors:
            raise RuntimeError("; ".join(errors))

    def status(self):
        pass

    def move(self, serial, target):
        self._motor(serial).move_to(target)

    def home(self, serial):
        self._motor(serial).home()

    def jog(self, serial, direction, token=None, drive=None):
        self._motor(serial).jog(direction, token, drive=drive)

    def hold(self, serial, token):
        self._motor(serial).hold(token)

    def release(self, serial, token):
        self._motor(serial).release(token)

    def stop(self, serial=None, immediate=False):
        for s in list(self.motors) if serial is None else [serial]:
            try:
                self._motor(s).stop(immediate=immediate)
            except Exception as exc:
                self.emit("motor_error", f"{s}: stop failed: {exc}")
                report(f"Motor {s}: stop failed: {exc}")

    def enable(self, serial, on):
        if type(on) is not bool:
            raise ValueError("Enable must be true or false")
        self._motor(serial).enable(on)

    def identify(self, serial):
        self._motor(serial).identify()

    def apply(self, serial, values):
        cfg = MotorSettings.parse(values)
        motor = self._motor(serial)
        try:
            motor.apply(asdict(cfg))
        except Exception:
            # A partial settings update is not a valid basis for a later move.
            self.disconnect(serial)
            raise
        self.preferences[serial] = asdict(cfg)
        temporary = self.preferences_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.preferences, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.preferences_path)
        report(f"Motor {serial}: settings applied and saved for next connection")

    def tick(self):
        for s, motor in list(self.motors.items()):
            if time.monotonic() < self._fault_retry.get(s, 0):
                continue
            try:
                self.states[s] = motor.tick()
            except Exception as exc:
                report(f"Motor {s}: fault: {exc}; requesting stop and closing SDK connection")
                try:
                    motor.close()
                except MotorStopUnverified as cleanup:
                    motor.last_error = str(cleanup)
                    self.states[s] = motor.snapshot()
                    self._fault_retry[s] = time.monotonic() + 5
                    self.emit("motor_error", f"{s}: stop not verified; retaining controller handle")
                    continue
                except Exception as cleanup:
                    report(f"Motor {s}: cleanup error: {cleanup}")
                del self.motors[s]
                self._disconnected(s, str(exc))
                self.emit("motor_error", f"{s}: {exc}")
        self.publish()

    def detached(self):
        report("Motor GUI detached; requesting immediate stop on every connected mount")
        self.stop(immediate=True)

    def close(self):
        self.closing = True
        self.stop(immediate=True)
        # Keep polling briefly to receive the stopped status before closing handles.
        deadline = time.monotonic() + 3
        while any(m.operation for m in self.motors.values()) and time.monotonic() < deadline:
            self.tick()
            time.sleep(0.1)
        if any(m.operation or m.snapshot()["moving"] for m in self.motors.values()):
            raise RuntimeError("Motor stop has not been verified; keeping controller handles open")
        self.disconnect()
        self.publish()


def run(runtime: Path, *, manual_shutdown=None, log_root=None):
    with backend_logging(runtime, "thorlabs", log_root) as log_path:
        _run(runtime, manual_shutdown=manual_shutdown, log_path=log_path)


def _run(runtime: Path, *, manual_shutdown, log_path):
    runtime.mkdir(parents=True, exist_ok=True)
    lock = _lock_file(runtime / "backend.lock")
    if lock is None:
        return
    token = secrets.token_bytes(32)
    monitor = BackendMonitor("Thorlabs", token, log_path, ("stop", "disconnect", "close"))
    listener = Listener(("127.0.0.1", 0), authkey=token)
    info_path = runtime / "backend.json"
    temporary = runtime / f"backend.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({"protocol": 1, "pid": os.getpid(),
                                     "port": listener.address[1], "authkey": token.hex(),
                                     "monitor_port": monitor.port, "log_path": str(log_path)}), encoding="utf-8")
    os.replace(temporary, info_path)
    incoming = queue.Queue()
    def accept():
        while True:
            try:
                incoming.put(listener.accept())
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                return
    threading.Thread(target=accept, daemon=True, name="motor-accept").start()
    client = None
    detached_at = time.monotonic()
    replay = None
    hub = None
    shutdown_requested = False
    disconnect_requested = False
    cleaned = False
    next_cleanup = 0.0

    def detach():
        nonlocal client, detached_at, shutdown_requested
        if client is not None:
            client.close()
            client = None
            detached_at = time.monotonic()
            hub.detached()
            shutdown_requested = True

    def send(message):
        if client is not None:
            try:
                client.send(message)
            except (OSError, EOFError):
                detach()

    def emit(kind, payload):
        nonlocal replay
        if kind == "motors":
            replay = payload
            monitor.update(devices=payload["motors"], device_state=("Moving" if any(d.get("moving") for d in payload["motors"])
                           else "Connected" if any(d.get("connected") for d in payload["motors"]) else "Disconnected"))
        elif kind == "motor_error":
            monitor.update(error=str(payload))
        send(("event", kind, payload))

    try:
        hub = MotorHub(emit)
        hub.publish()
        last_tick = 0
        report(f"Motor backend PID {os.getpid()} ready; no motors opened")
        while True:
            try:
                newcomer = incoming.get_nowait()
            except queue.Empty:
                newcomer = None
            if newcomer is not None:
                if client is not None:
                    try:
                        newcomer.send(("busy",))
                    except (OSError, EOFError):
                        pass
                    finally:
                        newcomer.close()
                else:
                    client = newcomer
                    detached_at = None
                    send(("hello", 1, os.getpid()))
                    send(("event", "motors", replay))
                    report("Motor GUI connection established")
            if client is not None:
                try:
                    if client.poll(0.03):
                        message = client.recv()
                        if message[0] == "detach":
                            detach()
                        elif message[0] == "stop":
                            hub.stop(immediate=True)
                        elif message[0] == "call":
                            _, request_id, method, args, kwargs = message
                            try:
                                hub.dispatch(method, *args, **kwargs)
                            except Exception as exc:
                                error = f"{type(exc).__name__}: {exc}"
                                report(f"Motor request {method} failed: {error}")
                                hub.publish()
                                send(("done", request_id, error))
                            else:
                                send(("done", request_id, None))
                except (OSError, EOFError):
                    detach()
            else:
                time.sleep(0.03)
            now = time.monotonic()
            for action in monitor.drain():
                report(f"Console safe {action} request received")
                hub.stop(immediate=True)
                if action == "close":
                    shutdown_requested = True
                elif action == "disconnect":
                    disconnect_requested = True
            if now - last_tick >= 0.2:
                hub.tick()
                last_tick = now
            if manual_shutdown is not None and manual_shutdown.is_set():
                report("Manual motor backend shutdown requested")
                shutdown_requested = True
                manual_shutdown.clear()
            if shutdown_requested or disconnect_requested:
                monitor.update(phase="stopping", cleanup="Verifying stopped before releasing controller handles")
                if now >= next_cleanup:
                    try:
                        hub.close()
                    except Exception as exc:
                        monitor.update(phase="fault", error=str(exc), cleanup="Stop not confirmed; retaining control")
                        report(f"Motor cleanup withheld: {exc}")
                        next_cleanup = time.monotonic() + 5
                    else:
                        if shutdown_requested:
                            cleaned = True
                            break
                        hub.closing = False
                        disconnect_requested = False
            elif detached_at is not None and now - detached_at >= 60:
                # Only the initial startup wait; an established GUI detachment closes immediately.
                shutdown_requested = True
            else:
                monitor.update(phase="connected" if client else "waiting", gui_connected=client is not None,
                               cleanup="None", deadline=None)
    finally:
        try:
            if hub is not None and not cleaned:
                hub.close()
        finally:
            monitor.update(phase="closed", device_state="Disconnected", cleanup="Complete")
            monitor.close()
            if client is not None:
                client.close()
            listener.close()
            try:
                if json.loads(info_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    info_path.unlink()
            except (OSError, ValueError):
                pass
            lock.close()
        report("Motor SDK cleanup complete; backend stopped")


if __name__ == "__main__":
    run(Path(sys.argv[1]).expanduser().resolve())
