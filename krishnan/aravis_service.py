"""Elevated, reconnectable Aravis camera process for Krishnan's Camera tab."""

from __future__ import annotations

from datetime import datetime
from multiprocessing import AuthenticationError
from multiprocessing.connection import Connection, Listener
from pathlib import Path
import ctypes
import json
import os
import queue
import secrets
import sys
import threading
import time
import zlib

import numpy as np

from .aravis_sdk import AravisSDKCamera
from .settings import load_settings
from .backend_console import BackendMonitor, backend_logging


STARTUP_WAIT_SECONDS = 60
PREVIEW_STRIDE = 3
_LOG_PATH: Path | None = None


def _log(message: str) -> None:
    line = f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}] {message}"
    print(line, flush=True)


def _lock(path: Path):
    import msvcrt
    stream = path.open("a+b")
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        stream.close()
        return None
    return stream


def run(runtime: Path, *, camera_factory=AravisSDKCamera, require_admin=True, log_root=None) -> None:
    with backend_logging(runtime, "aravis", log_root) as log_path:
        _run(runtime, log_path=log_path, camera_factory=camera_factory, require_admin=require_admin)


def _run(runtime: Path, *, log_path, camera_factory, require_admin) -> None:
    global _LOG_PATH
    runtime.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = log_path
    config = load_settings()
    if require_admin and config['aravis_manage_network'] and not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("Start the Aravis camera service with administrator access")
    lock = _lock(runtime / "service.lock")
    if lock is None:
        raise RuntimeError("Another Aravis camera service is already running")
    camera = camera_factory(config)
    listener = None
    monitor = None
    info_path = runtime / "service.json"
    client: Connection | None = None
    cleanup_error = None
    try:
        token = secrets.token_bytes(32)
        monitor = BackendMonitor("Aravis", token, log_path, ("stop", "disconnect", "close"))
        monitor.update(phase="waiting", device_state="Camera closed", cleanup="None")
        listener = Listener(("127.0.0.1", 0), authkey=token)
        info = {"protocol": 1, "service_version": 7, "pid": os.getpid(),
                "port": listener.address[1],
                "authkey": token.hex(), "monitor_port": monitor.port, "log_path": str(log_path)}
        temporary = runtime / f"service.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(info), encoding="utf-8")
        os.replace(temporary, info_path)
        _log(f"Aravis service ready (PID {os.getpid()}); waiting for Krishnan")

        incoming: queue.Queue[Connection] = queue.Queue()
        busy = threading.Event()

        def accept_forever() -> None:
            while True:
                try:
                    connection = listener.accept()
                    if busy.is_set():
                        connection.send(("busy",))
                        connection.close()
                    else:
                        busy.set()
                        incoming.put(connection)
                except AuthenticationError:
                    continue
                except (EOFError, OSError):
                    return

        threading.Thread(target=accept_forever, name="aravis-accept", daemon=True).start()
        deadline = time.monotonic() + STARTUP_WAIT_SECONDS
        latest_full_frame: tuple[int, int, bytes] | None = None
        while time.monotonic() < deadline:
            if list(monitor.drain()):
                _log("Console requested shutdown before camera connection")
                break
            try:
                client = incoming.get(timeout=0.25)
            except queue.Empty:
                continue
            _log("GUI connection received")
            shutdown_requested = False
            try:
                client.send(("hello", 1, os.getpid()))
                last_frame_at = 0.0
                last_request_at = time.monotonic()
                timing_count = 0
                last_statistics_at = 0.0
                grab_total_ms = 0.0
                preview_total_ms = 0.0
                send_total_ms = 0.0
                while True:
                    monitor.update(phase="connected", device_state="Acquiring" if camera.acquiring else
                                   "Camera open" if camera.initialized else "Camera closed",
                                   gui_connected=True, cleanup="None")
                    if hasattr(camera,'statistics') and time.monotonic()-last_statistics_at>=1:
                        monitor.update(camera_statistics=camera.statistics())
                        last_statistics_at=time.monotonic()
                    controls = list(monitor.drain())
                    if controls:
                        _log(f"Console safe {controls[-1]} request received")
                        monitor.update(phase="stopping", cleanup="Closing Aravis and restoring camera Ethernet")
                        camera.close()
                        shutdown_requested = True
                        break
                    try:
                        if not client.poll(1.0):
                            if (camera.acquiring and
                                    time.monotonic() - last_request_at >= 10.0):
                                camera.close()
                                shutdown_requested = True
                                _log("No GUI frame requests for 10 seconds; camera closed")
                                break
                            continue
                        request = client.recv()
                        last_request_at = time.monotonic()
                    except (EOFError, OSError):
                        _log("GUI connection lost")
                        break
                    if request == ("detach",):
                        _log("GUI detached")
                        break
                    try:
                        if request == ("ping",):
                            result = None
                        elif request == ("statistics",):
                            result = camera.statistics() if hasattr(camera,'statistics') else {}
                        elif request == ("start",):
                            monitor.update(phase="starting", cleanup="Preparing camera Ethernet and warming up color")
                            details = camera.open()
                            camera.start()
                            latest_full_frame = None
                            _log(f"Acquisition started: {details['model']} {details['serial']}; "
                                 f"{details['pixel_format']} {details['bayer_layout']} "
                                 f"{details['bayer_conversion']}")
                            result = details
                            monitor.update(device=details)
                        elif (isinstance(request, tuple) and len(request) == 2
                              and request[0] == "frame"):
                            preview_fps = request[1]
                            if type(preview_fps) is not int or not 1 <= preview_fps <= 16:
                                raise ValueError("Preview FPS must be 1–16")
                            wait = 1.0 / preview_fps - (time.perf_counter() - last_frame_at)
                            if wait > 0:
                                time.sleep(wait)
                            last_frame_at = time.perf_counter()
                            read_started = time.perf_counter()
                            width, height, pixels = camera.read_frame(timeout_s=2.0)
                            grabbed_at = time.perf_counter()
                            latest_full_frame = (width, height, pixels)
                            rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
                            preview = rgb[::PREVIEW_STRIDE, ::PREVIEW_STRIDE, :]
                            result = (preview.shape[1], preview.shape[0], preview.tobytes())
                            prepared_at = time.perf_counter()
                        elif request == ("snapshot",):
                            if latest_full_frame is None:
                                raise RuntimeError("No Aravis frame is available for capture")
                            width, height, pixels = latest_full_frame
                            result = (width, height, zlib.compress(pixels, level=1))
                        elif request == ("stop",):
                            camera.close()
                            latest_full_frame = None
                            _log("Acquisition stopped; camera closed")
                            shutdown_requested = True
                            result = None
                        else:
                            raise ValueError("Unknown Aravis camera request")
                        client.send(("ok", result))
                        if isinstance(request, tuple) and len(request) == 2 and request[0] == "frame":
                            sent_at = time.perf_counter()
                            timing_count += 1
                            grab_total_ms += (grabbed_at - read_started) * 1000
                            preview_total_ms += (prepared_at - grabbed_at) * 1000
                            send_total_ms += (sent_at - prepared_at) * 1000
                            if timing_count >= 50:
                                monitor.update(camera_statistics=camera.statistics() if hasattr(camera, 'statistics') else {},
                                               preview_timing_ms={"acquisition": grab_total_ms / timing_count,
                                               "preview": preview_total_ms / timing_count,
                                               "send": send_total_ms / timing_count})
                                timing_count = 0
                                grab_total_ms = preview_total_ms = send_total_ms = 0.0
                        if shutdown_requested:
                            break
                    except Exception as exc:
                        _log(f"Camera error: {type(exc).__name__}: {exc}")
                        monitor.update(error=f"{type(exc).__name__}: {exc}")
                        try:
                            client.send(("error", f"{type(exc).__name__}: {exc}"))
                        except (EOFError, OSError):
                            break
            finally:
                monitor.update(phase="stopping", gui_connected=False,
                               cleanup="Stopping acquisition and releasing camera handle")
                if camera.initialized:
                    try:
                        camera.close()
                        _log("GUI disconnected; acquisition stopped and camera handle closed")
                    except Exception as exc:
                        cleanup_error = str(exc)
                        _log(f"Camera stop error: {exc}")
                        try:
                            camera.close()
                        except Exception as cleanup_exc:
                            _log(f"Camera cleanup error: {cleanup_exc}")
                client.close()
                client = None
                busy.clear()
                deadline = time.monotonic()
                _log("Camera closed; Aravis service exiting without reconnect grace")
        if camera.initialized:
            _log("Reconnect timeout reached; closing camera and exiting")
        else:
            _log("Aravis service exiting")
    finally:
        try:
            camera.close()
            if hasattr(camera,'statistics'):
                _log('Final Aravis statistics: '+json.dumps(camera.statistics()))
        except Exception as exc:
            cleanup_error = str(exc)
            _log(f"Camera cleanup error: {exc}")
            if monitor is not None:
                monitor.update(phase="fault", error=str(exc))
        if monitor is not None:
            monitor.update(phase="fault" if cleanup_error else "closed",
                           device_state="Unconfirmed" if cleanup_error else "Camera closed", gui_connected=False,
                           cleanup="SDK cleanup error" if cleanup_error else "Complete", error=cleanup_error)
            monitor.close()
        if client is not None:
            client.close()
        if listener is not None:
            listener.close()
        try:
            if json.loads(info_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                info_path.unlink()
        except (OSError, ValueError):
            pass
        lock.close()


if __name__ == "__main__":
    try:
        run(Path(sys.argv[1]).expanduser().resolve())
    except KeyboardInterrupt:
        _log("Manual shutdown requested")
    except BaseException as exc:
        _log(f"Service failed: {type(exc).__name__}: {exc}")
        raise
