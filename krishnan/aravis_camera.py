"""GUI-side client for the elevated Aravis camera service."""

from __future__ import annotations

from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection
from pathlib import Path
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import zlib

import numpy as np

from .aux_camera import CameraFrame, CameraSettings
from .bootstrap import prepare_paths
from .settings import load_settings


def _runtime_dir() -> Path:
    checkout = str(prepare_paths()).casefold().encode("utf-8")
    identity = hashlib.sha256(checkout).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "krishnan-aravis" / identity


def _connect(runtime: Path) -> Connection | None:
    try:
        info = json.loads((runtime / "service.json").read_text(encoding="utf-8"))
        if info.get("protocol") != 1:
            return None
        connection = Client(("127.0.0.1", int(info["port"])),
                            authkey=bytes.fromhex(info["authkey"]))
        if connection.poll(5):
            greeting = connection.recv()
            if greeting[0] == "hello":
                if info.get("service_version") != 7:
                    connection.close()
                    raise RuntimeError("An older Aravis backend is running; safely stop it once to load this update")
                return connection
            if greeting[0] == "busy":
                connection.close()
                raise RuntimeError("Another Krishnan camera tab is using Aravis")
        connection.close()
    except (OSError, EOFError, AuthenticationError, ValueError, KeyError, TypeError):
        pass
    return None


def _start_service(runtime: Path) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        raise RuntimeError("The configured Aravis GigE runtime requires Windows")
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        python = Path(sys.executable)
    if not python.is_file():
        raise FileNotFoundError(python)
    if not load_settings()['aravis_manage_network']:
        subprocess.Popen([str(python),'-m','krishnan.aravis_service',str(runtime)],
                         cwd=str(prepare_paths()),creationflags=subprocess.CREATE_NO_WINDOW)
        return
    parameters = subprocess.list2cmdline(["-m", "krishnan.aravis_service", str(runtime)])
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                              ctypes.c_wchar_p, ctypes.c_wchar_p,
                              ctypes.c_wchar_p, ctypes.c_int]
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None, "runas", str(python), parameters, str(prepare_paths()), 0)
    if not result or result <= 32:
        raise RuntimeError(f"Could not start Aravis service with administrator access ({result})")


class AravisAuxiliaryCamera:
    """Camera adapter whose SDK calls stay outside the GUI process."""

    def __init__(self) -> None:
        self.connection: Connection | None = None
        self.running = False
        self.sequence = 0
        self.details: dict | None = None
        self.preview_fps = 14
        self.last_full_frame: CameraFrame | None = None
        self.last_statistics: dict = {}
        self._write_lock = threading.Lock()

    def open(self) -> None:
        if self.connection is not None:
            try:
                self._request("ping")
                return
            except (OSError, EOFError, RuntimeError):
                self.connection.close()
                self.connection = None
                self.running = False
        runtime = _runtime_dir()
        self.connection = _connect(runtime)
        if self.connection is None:
            _start_service(runtime)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                time.sleep(0.25)
                self.connection = _connect(runtime)
                if self.connection is not None:
                    break
        if self.connection is None:
            raise RuntimeError(f"Aravis service did not start; inspect {runtime / 'service.log'}")

    def _request(self, *command):
        connection = self.connection
        if connection is None:
            raise RuntimeError("Aravis service is not connected")
        with self._write_lock:
            connection.send(command)
        if not connection.poll(90 if command == ("start",) else 60 if command == ("stop",) else 5):
            raise TimeoutError("Aravis backend did not respond; check Console IPC state")
        response = connection.recv()
        if response[0] == "error":
            raise RuntimeError(response[1])
        if response[0] != "ok":
            raise RuntimeError(f"Unexpected Aravis response: {response[0]}")
        return response[1]

    def configure(self, settings: CameraSettings) -> None:
        # Keep current camera exposure, gain, ROI, and pixel format untouched.
        if self.connection is None:
            raise RuntimeError("Connect to Aravis before preview")

    def statistics(self) -> dict:
        return self._request('statistics')

    def start(self) -> None:
        self.details = self._request("start")
        self.running = True
        self.last_full_frame = None

    def read_frame(self, timeout_s: float = 1.0) -> CameraFrame:
        if not self.running:
            raise RuntimeError("Aravis acquisition is stopped")
        width, height, pixels = self._request("frame", self.preview_fps)
        if not (0 < width <= 10000 and 0 < height <= 10000
                and len(pixels) == width * height * 3):
            raise RuntimeError("Aravis sent an invalid frame")
        self.sequence += 1
        rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
        return CameraFrame(rgb, self.sequence, time.time())

    def capture_frame(self) -> CameraFrame:
        """Retrieve the latest unscaled color frame for a TIFF capture."""
        width, height, packed = self._request("snapshot")
        pixels = zlib.decompress(packed)
        if not (0 < width <= 10000 and 0 < height <= 10000
                and len(pixels) == width * height * 3):
            raise RuntimeError("Aravis sent an invalid full-resolution frame")
        rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
        self.last_full_frame = CameraFrame(rgb, self.sequence, time.time())
        return self.last_full_frame

    def stop(self) -> None:
        if self.running:
            try:
                try:
                    self.capture_frame()
                except Exception:
                    pass
                self.last_statistics = self.statistics()
                self._request("stop")
            finally:
                self.running = False

    def close(self) -> None:
        if self.connection is None:
            return
        try:
            self.stop()
        finally:
            self.detach()

    def detach(self) -> None:
        """Notify the service to immediately stop, close the camera and exit."""
        if self.connection is None:
            return
        try:
            with self._write_lock:
                self.connection.send(("detach",))
        except (OSError, EOFError):
            pass
        finally:
            self.connection.close()
            self.connection = None
            self.running = False
