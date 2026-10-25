"""Independent local device process with reconnect and timed SDK cleanup."""

from __future__ import annotations

from concurrent.futures import Future
from multiprocessing import AuthenticationError
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from datetime import datetime, timedelta
import json
import os
import queue
import secrets
import sys
import threading
import time

from .bootstrap import prepare_paths
from .settings import load_settings
from .backend_console import BackendMonitor, backend_logging, progress_due

prepare_paths()

RECONNECT_SECONDS = 30 * 60
WARMUP_SECONDS = 30 * 60
_REPLAY = ("inventory", "optical", "wavelength_range", "temperature",
           "session", "steps", "shutter", "state", "status", "error")


def report(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{stamp}] {message}", flush=True)


def _lock_file(path: Path):
    """Hold a per-checkout process lock until this backend exits."""
    stream = path.open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
    except OSError:
        stream.close()
        return None
    return stream


def run(runtime: Path, *, reconnect_seconds: float = RECONNECT_SECONDS,
        warmup_seconds: float = WARMUP_SECONDS,
        manual_shutdown: threading.Event | None = None, log_root=None) -> None:
    with backend_logging(runtime, "andor", log_root) as log_path:
        _run(runtime, reconnect_seconds=reconnect_seconds, warmup_seconds=warmup_seconds,
             manual_shutdown=manual_shutdown, log_path=log_path)


def _run(runtime: Path, *, reconnect_seconds: float, warmup_seconds: float,
         manual_shutdown, log_path: Path) -> None:
    from .worker import InstrumentWorker
    temperature_thresholds = tuple(load_settings()["temperature_log_thresholds_c"])

    runtime.mkdir(parents=True, exist_ok=True)
    process_lock = _lock_file(runtime / "backend.lock")
    if process_lock is None:
        return
    token = secrets.token_bytes(32)
    monitor = BackendMonitor("Andor", token, log_path, ("stop", "disconnect", "close"))
    manual_shutdown = manual_shutdown or threading.Event()
    listener = Listener(("127.0.0.1", 0), authkey=token)
    info_path = runtime / "backend.json"
    info = {"protocol": 1, "pid": os.getpid(), "port": listener.address[1],
            "authkey": token.hex(), "monitor_port": monitor.port, "log_path": str(log_path)}
    temporary = runtime / f"backend.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(info), encoding="utf-8")
    os.replace(temporary, info_path)
    report(f"Backend PID {os.getpid()} ready; waiting for GUI connection")

    incoming: queue.Queue[Connection] = queue.Queue()
    def accept_forever() -> None:
        while True:
            try:
                incoming.put(listener.accept())
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                return
    threading.Thread(target=accept_forever, name="krishnan-accept", daemon=True).start()

    client: Connection | None = None
    client_lock = threading.RLock()
    replay: dict[str, object] = {"state": "DISCONNECTED"}
    detached_at: float | None = time.monotonic()
    warmup_at: float | None = None
    phase = "waiting"
    pending_cleanup: Future | None = None
    last_warm_check = 0.0
    last_progress = time.monotonic()
    clean_exit = False
    manual = False
    last_temperature: float | None = None
    last_cooler: bool | None = None
    grace_warned = warm_warned = False
    last_warm_report = 0.0

    def send(message: tuple) -> None:
        nonlocal client, detached_at, last_progress
        with client_lock:
            if client is None:
                return
            try:
                client.send(message)
            except (BrokenPipeError, EOFError, OSError):
                try:
                    client.close()
                finally:
                    client = None
                    if phase == "waiting":
                        detached_at = time.monotonic()
                        last_progress = 0.0
                        deadline = datetime.now().astimezone() + timedelta(seconds=reconnect_seconds)
                        report(f"GUI connection lost; safety cleanup at {deadline:%H:%M:%S %Z} unless it reconnects")

    def emit(name: str, payload: object) -> None:
        nonlocal last_temperature, last_cooler
        if name == "state" and payload == "DISCONNECTED":
            replay.pop("shutter", None)
        if name in _REPLAY:
            replay[name] = payload
        if name == "state":
            report(f"Device state: {payload}")
        elif name == "error":
            report(f"Device error: {payload}")
        elif name == "inventory" and isinstance(payload, dict):
            last_temperature = float(payload["temperature"]["celsius"])
            last_cooler = bool(payload["cooler_on"])
            report(f"CCD cooler {'on' if last_cooler else 'off'} on connection (readback)")
        elif name == "temperature" and isinstance(payload, dict):
            reading = payload.get("reading", {})
            current = float(reading["celsius"])
            cooler = bool(payload["cooler_on"])
            if last_cooler is None or cooler != last_cooler:
                report(f"CCD cooler {'on' if cooler else 'off'} (readback)")
            if last_temperature is not None:
                for threshold in temperature_thresholds:
                    if (last_temperature < threshold <= current or
                            last_temperature > threshold >= current):
                        report(f"Detector crossed {threshold} °C ({current:.0f} °C now)")
            last_temperature = current
            last_cooler = cooler
        send(("event", name, payload))

    def completion(request_id: int, future: Future) -> None:
        try:
            future.result()
        except BaseException as exc:
            send(("done", request_id, f"{type(exc).__name__}: {exc}"))
        else:
            send(("done", request_id, None))

    worker = InstrumentWorker(emit)
    try:
        while True:
            try:
                newcomer = incoming.get_nowait()
            except queue.Empty:
                newcomer = None
            if newcomer is not None:
                report("GUI connection request received")
                with client_lock:
                    if client is not None:
                        try:
                            newcomer.send(("busy",))
                        except (BrokenPipeError, EOFError, OSError):
                            pass
                        newcomer.close()
                        report("GUI connection refused: another GUI is attached")
                    else:
                        client = newcomer
                        try:
                            client.send(("hello", 1, os.getpid()))
                            for name in _REPLAY:
                                if name in replay:
                                    client.send(("event", name, replay[name]))
                            if worker.session is not None:
                                client.send(("event", "attached_session",
                                             str(worker.session.folder)))
                            if phase != "waiting":
                                client.send(("event", "status",
                                             f"Backend unattended cleanup: {phase}"))
                        except (BrokenPipeError, EOFError, OSError):
                            client.close()
                            client = None
                        else:
                            if phase == "waiting":
                                detached_at = None
                                grace_warned = False
                            report("GUI connected to backend")

            current = client
            if current is not None:
                try:
                    if current.poll(0.2):
                        message = current.recv()
                        kind = message[0]
                        if kind == "detach":
                            with client_lock:
                                if client is current:
                                    client.close()
                                    client = None
                                    if phase == "waiting":
                                        detached_at = time.monotonic()
                                        last_progress = 0.0
                            deadline = datetime.now().astimezone() + timedelta(seconds=reconnect_seconds)
                            report(f"GUI disconnected; safety cleanup at {deadline:%H:%M:%S %Z} unless it reconnects")
                        elif kind == "call":
                            _, request_id, method, args, kwargs = message
                            if phase != "waiting" and method not in (
                                    "read_temperature", "load_completed"):
                                send(("done", request_id,
                                      "Backend is completing unattended shutdown"))
                            else:
                                future = worker.submit(method, *args, **kwargs)
                                future.add_done_callback(
                                    lambda done, rid=request_id: completion(rid, done))
                        elif kind == "stop":
                            worker.stop()
                        elif kind == "trigger":
                            worker.software_trigger()
                        else:
                            emit("error", f"Unknown backend command: {kind}")
                except (EOFError, OSError):
                    with client_lock:
                        if client is current:
                            client.close()
                            client = None
                            if phase == "waiting":
                                detached_at = time.monotonic()
                                last_progress = 0.0
                    deadline = datetime.now().astimezone() + timedelta(seconds=reconnect_seconds)
                    report(f"GUI connection lost; safety cleanup at {deadline:%H:%M:%S %Z} unless it reconnects")
            else:
                time.sleep(0.2)

            now = time.monotonic()
            for action in monitor.drain():
                report(f"Console safe {action} request received")
                if action == "stop":
                    worker.stop()
                elif action == "disconnect":
                    if phase != "waiting":
                        emit("error", "Cannot disconnect during Andor cleanup")
                    else:
                        worker.stop()
                        future = worker.submit("disconnect")
                        def disconnected(done):
                            try:
                                done.result()
                            except Exception as exc:
                                emit("error", f"Safe disconnect withheld: {exc}")
                        future.add_done_callback(disconnected)
                elif action == "close":
                    manual_shutdown.set()
            if phase == "waiting" and detached_at is not None:
                remaining = max(0.0, reconnect_seconds - (now - detached_at))
                due, grace_warned = progress_due(remaining, now - last_progress, grace_warned)
                if due:
                    report(f"GUI absent; reconnect grace has {remaining / 60:.1f} minutes remaining")
                    last_progress = now
            elif phase == "warming" and warmup_at is not None and not manual:
                remaining = max(0.0, warmup_seconds - (now - warmup_at))
                due, warm_warned = progress_due(remaining, now - last_progress, warm_warned)
                if due:
                    report(f"Safety warm-up ongoing; minimum period has {remaining / 60:.1f} minutes remaining")
                    last_progress = now
            if manual_shutdown is not None and manual_shutdown.is_set() and phase == "waiting":
                manual = True
                report("Manual safe shutdown requested; stopping acquisition")
                worker.stop()
                pending_cleanup = worker.submit("begin_orphan_cleanup")
                phase = "stopping"
                emit("status", "Manual safe shutdown: stopping and checking cooler")
            if phase == "waiting" and detached_at is not None:
                if now - detached_at >= reconnect_seconds:
                    report("Reconnect grace expired; stopping acquisition")
                    worker.stop()
                    pending_cleanup = worker.submit("begin_orphan_cleanup")
                    phase = "stopping"
                    emit("status", "GUI absent for 30 minutes; stopping and switching cooler off")
            elif phase == "stopping" and pending_cleanup is not None:
                if pending_cleanup.done():
                    try:
                        pending_cleanup.result()
                    except BaseException as exc:
                        phase = "fault"
                        emit("error", f"Unattended cleanup failed: {exc}")
                        report(f"Safe cleanup failed: {exc}")
                    else:
                        phase = "warming"
                        warmup_at = time.monotonic()
                        last_progress = time.monotonic()
                        warm_warned = False
                        delay = 0 if manual else warmup_seconds
                        emit("status", "Cooler off; monitoring detector warm-up")
                        report(f"Cooler off; {'checking detector temperature now' if manual else f'warm-up period started ({delay / 60:g} minutes)'}")
                    pending_cleanup = None
            elif phase == "warming" and warmup_at is not None:
                if pending_cleanup is None and now - warmup_at >= (0 if manual else warmup_seconds):
                    if now - last_warm_check >= 60 or last_warm_check == 0:
                        last_warm_check = now
                        if now - last_warm_report >= 600 or last_warm_report == 0:
                            report("Checking detector temperature before SDK shutdown")
                            last_warm_report = now
                        pending_cleanup = worker.submit("finish_orphan_cleanup")
                elif pending_cleanup is not None and pending_cleanup.done():
                    try:
                        warmed = pending_cleanup.result()
                    except BaseException as exc:
                        phase = "fault"
                        emit("error", f"SDK shutdown withheld: {exc}")
                        report(f"SDK shutdown withheld: {exc}")
                    else:
                        if warmed:
                            worker.close()
                            report("SDK shutdown complete; backend exiting")
                            clean_exit = True
                            break
                        emit("status", "Waiting for detector to warm above -20 °C")
                        if now - last_warm_report >= 600:
                            report("Detector below -20 °C; backend remains active and will check again")
                            last_warm_report = now
                    pending_cleanup = None
            deadline = None
            if phase == "waiting" and detached_at is not None:
                deadline = time.time() + max(0, reconnect_seconds - (now - detached_at))
            elif phase == "warming" and warmup_at is not None:
                deadline = time.time() + max(0, (0 if manual else warmup_seconds) - (now - warmup_at))
            monitor.update(phase=("connected" if client else "reconnect_grace") if phase == "waiting" else phase,
                           device_state=worker.state, gui_connected=client is not None,
                           deadline=deadline, temperature=replay.get("temperature"),
                           cooler_on=last_cooler, error=replay.get("error"),
                           cleanup="Temperature-gated shutdown" if phase == "warming" else phase)
    finally:
        monitor.update(phase="closed" if clean_exit else "fault", device_state=worker.state)
        monitor.close()
        listener.close()
        if client is not None:
            client.close()
        try:
            if json.loads(info_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                info_path.unlink()
        except (OSError, ValueError):
            pass
        process_lock.close()
        if not clean_exit:
            report("Backend exited without confirmed SDK cleanup")


if __name__ == "__main__":
    run(Path(sys.argv[1]).expanduser().resolve())
