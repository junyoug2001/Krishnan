"""GUI proxy for the independent, reconnectable instrument backend."""

from __future__ import annotations

from concurrent.futures import Future
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

from .bootstrap import prepare_paths


def _runtime_dir(service: str = "instrument") -> Path:
    checkout = str(prepare_paths()).casefold().encode("utf-8")
    identity = hashlib.sha256(checkout).hexdigest()[:12]
    root = Path(tempfile.gettempdir()) / "krishnan-backend" / identity
    return root / "motors" if service == "motors" else root


def _connect_existing(runtime: Path) -> Connection | None:
    try:
        info = json.loads((runtime / "backend.json").read_text(encoding="utf-8"))
        if info.get("protocol") != 1:
            return None
        connection = Client(("127.0.0.1", int(info["port"])),
                            authkey=bytes.fromhex(info["authkey"]))
        if connection.poll(5):
            greeting = connection.recv()
            if greeting[0] == "hello":
                return connection
            if greeting[0] == "busy":
                connection.close()
                raise RuntimeError("Another Krishnan GUI is attached to the backend")
        connection.close()
    except (OSError, EOFError, AuthenticationError, ValueError, KeyError, TypeError):
        pass
    return None


def _start_backend(runtime: Path, service: str = "instrument") -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    log = (runtime / "backend.log").open("ab", buffering=0)
    try:
        kwargs = {"cwd": str(prepare_paths()), "stdin": subprocess.DEVNULL,
                  "stdout": log, "stderr": subprocess.STDOUT, "close_fds": True}
        kwargs["env"] = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.DETACHED_PROCESS)
        else:
            kwargs["start_new_session"] = True
        module = "krishnan.motor_service" if service == "motors" else "krishnan.device_backend"
        command = [sys.executable, "-m", module, str(runtime)]
        if os.name == "nt":
            # Windowless hardware owner; Console status is served by this process.
            window_python = str(Path(sys.executable).with_name("pythonw.exe"))
            if Path(window_python).exists():
                command = [window_python, "-m", module, str(runtime)]
            try:
                subprocess.Popen(
                    command,
                    **{**kwargs, "creationflags": kwargs["creationflags"]
                       | subprocess.CREATE_BREAKAWAY_FROM_JOB})
            except OSError:
                subprocess.Popen(command, **kwargs)
        else:
            subprocess.Popen(command, **kwargs)
    finally:
        log.close()


class ProcessWorker:
    """Asynchronous GUI proxy; closing it leaves the backend running."""

    def __init__(self, emit: Callable[[str, object], None], *, service: str = "instrument") -> None:
        if service not in ("instrument", "motors"):
            raise ValueError("Unknown backend service")
        self.emit = emit
        self.state = "DISCONNECTED"
        self.simulated = False
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._next_id = 0
        self._closing = False
        runtime = _runtime_dir(service)
        runtime.mkdir(parents=True, exist_ok=True)
        connection = _connect_existing(runtime)
        if connection is None:
            _start_backend(runtime, service)
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                time.sleep(0.2)
                connection = _connect_existing(runtime)
                if connection is not None:
                    break
        if connection is None:
            raise RuntimeError(f"Device backend did not start; inspect {runtime / 'backend.log'}")
        self._connection = connection
        self._reader = threading.Thread(target=self._read, name="krishnan-ipc-reader", daemon=True)
        self._reader.start()

    def _send(self, message: tuple) -> None:
        with self._send_lock:
            if self._closing:
                raise RuntimeError("GUI is detaching from the backend")
            self._connection.send(message)

    def submit(self, method: str, *args, **kwargs) -> Future:
        future: Future = Future()
        with self._send_lock:
            self._next_id += 1
            request_id = self._next_id
            with self._pending_lock:
                self._pending[request_id] = future
            try:
                self._connection.send(("call", request_id, method, args, kwargs))
            except BaseException as exc:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                future.set_exception(exc)
                self.emit("error", str(exc))
        return future

    def stop(self) -> None:
        try:
            self._send(("stop",))
        except (BrokenPipeError, EOFError, OSError, RuntimeError) as exc:
            self.emit("error", f"Stop could not reach device backend: {exc}")

    def software_trigger(self) -> None:
        self._send(("trigger",))

    def _read(self) -> None:
        try:
            while True:
                message = self._connection.recv()
                kind = message[0]
                if kind == "event":
                    _, name, payload = message
                    if name == "state":
                        self.state = payload
                    elif name == "inventory":
                        self.simulated = bool(payload["simulation"])
                    self.emit(name, payload)
                elif kind == "done":
                    _, request_id, error = message
                    with self._pending_lock:
                        future = self._pending.pop(request_id, None)
                    if future is not None:
                        if error is None:
                            future.set_result(None)
                        else:
                            future.set_exception(RuntimeError(error))
        except (EOFError, OSError):
            pass
        finally:
            error = RuntimeError("Device backend connection was lost")
            if not self._closing:
                self.state = "FAULT"
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for future in pending:
                if not future.done():
                    future.set_exception(error)
            if not self._closing:
                self.emit("error", "Backend connection lost; reopen Krishnan to reconnect")
                self.emit("state", "FAULT")

    def close(self) -> None:
        """Detach only. The backend owns the two timed cleanup phases."""
        self._closing = True
        with self._send_lock:
            try:
                self._connection.send(("detach",))
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._connection.close()
        self._reader.join(timeout=1.0)
