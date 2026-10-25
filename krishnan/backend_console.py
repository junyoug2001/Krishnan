"""In-process backend status endpoints, safe control queues, and persistent logs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
import json
import os
import queue
import sys
import threading
import time

from .settings import SETTINGS_PATH


def log_directory(name):
    return SETTINGS_PATH.parent / "logs" / name.lower()


@contextmanager
def backend_logging(runtime: Path, name: str, log_root=None):
    runtime.mkdir(parents=True, exist_ok=True)
    logs = Path(log_root) / name if log_root is not None else log_directory(name)
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"{name}-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.log"
    previous = sys.stdout, sys.stderr
    with path.open("a", encoding="utf-8", buffering=1) as stream:
        sys.stdout = sys.stderr = stream
        try:
            yield path
        except BaseException:
            import traceback
            traceback.print_exc()
            raise
        finally:
            sys.stdout, sys.stderr = previous


class BackendMonitor:
    """Observer threads never touch an SDK; the owning loop handles controls."""
    def __init__(self, name, token, log_path, controls):
        self.name = name
        self.commands = queue.Queue()
        self.allowed = set(controls)
        self._lock = threading.Lock()
        self._state = {"backend": name, "pid": os.getpid(), "phase": "starting",
                       "device_state": "Unknown", "gui_connected": False,
                       "log_path": str(log_path), "controls": list(controls), "updated": time.time()}
        self.listener = Listener(("127.0.0.1", 0), authkey=token)
        self.port = self.listener.address[1]
        self.closed = False
        threading.Thread(target=self._listen, daemon=True, name=f"{name}-console").start()

    def update(self, **values):
        with self._lock:
            self._state.update(values, updated=time.time())

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def _listen(self):
        while not self.closed:
            try:
                connection = self.listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        try:
            if not connection.poll(2):
                return
            request = connection.recv()
            if request == ("status",):
                response = ("ok", self.snapshot())
            elif (isinstance(request, tuple) and len(request) == 2 and request[0] == "control"
                  and request[1] in self.allowed):
                self.commands.put(request[1])
                response = ("ok", {"accepted": request[1]})
            else:
                response = ("error", "Unknown console request")
            connection.send(response)
        except (OSError, EOFError):
            pass
        finally:
            connection.close()

    def drain(self):
        while True:
            try:
                yield self.commands.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self.closed = True
        self.listener.close()


def monitor_request(runtime: Path, action=None, *, info_name="backend.json"):
    """Return IPC state, never reconstruct current device state from a log."""
    info_path = runtime / info_name
    if not info_path.exists():
        return {"available": False, "reason": "No backend endpoint"}
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if "monitor_port" not in info:
            return {"available": False, "legacy": True, "pid": info.get("pid"),
                    "reason": "Older backend: safely close it once to load Console IPC"}
        with Client(("127.0.0.1", int(info["monitor_port"])),
                    authkey=bytes.fromhex(info["authkey"])) as connection:
            connection.send(("control", action) if action else ("status",))
            if not connection.poll(2):
                raise TimeoutError("Backend status timed out")
            kind, payload = connection.recv()
            if kind != "ok":
                raise RuntimeError(payload)
            return {"available": True, **payload}
    except (OSError, EOFError, AuthenticationError, ValueError, KeyError, TimeoutError) as exc:
        return {"available": False, "reason": f"IPC unavailable: {exc}"}


def progress_due(remaining, elapsed_since_log, warned):
    """Only log ten-minute progress and the final one-minute warning."""
    final_warning = 0 < remaining <= 60 and not warned
    return elapsed_since_log >= 600 or final_warning, warned or final_warning
