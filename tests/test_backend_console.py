"""Console and shutdown checks; subprocess cameras are simulated only."""
from pathlib import Path
from multiprocessing.connection import Client
import json
import multiprocessing as mp
import tempfile
import time
import unittest

from krishnan.backend_console import BackendMonitor, monitor_request, progress_due


class FakeAravis:
    def __init__(self, *_):
        self.initialized = self.acquiring = False

    def open(self):
        self.initialized = True
        return {"model": "FAKE", "serial": "TEST", "pixel_format": "RGB8",
                "bayer_layout": "test", "bayer_conversion": "test"}

    def start(self):
        self.acquiring = True

    def read_frame(self, **_):
        return 2, 2, bytes(range(12))

    def stop(self):
        self.acquiring = False

    def close(self):
        self.stop()
        self.initialized = False
        print("FAKE camera closed", flush=True)


def fake_aravis(runtime):
    from krishnan.aravis_service import run
    run(runtime, camera_factory=FakeAravis, require_admin=False, log_root=runtime / "logs")


def empty_motor_service(runtime):
    from krishnan.motor_service import run
    run(runtime, log_root=runtime / "logs")


def empty_andor_service(runtime):
    from krishnan.device_backend import run
    run(runtime, reconnect_seconds=1, warmup_seconds=.6, log_root=runtime / "logs")


def wait_info(runtime, name="backend.json"):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            return json.loads((runtime / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(.03)
    raise TimeoutError("Test backend failed to publish endpoint")


def attach(runtime, name="backend.json"):
    info = wait_info(runtime, name)
    connection = Client(("127.0.0.1", info["port"]), authkey=bytes.fromhex(info["authkey"]))
    if not connection.poll(5) or connection.recv()[0] != "hello":
        raise RuntimeError("No owner handshake")
    return connection


class ConsoleChecks(unittest.TestCase):
    def test_preconfigured_aravis_network_does_not_request_elevation(self):
        from unittest.mock import patch
        from krishnan.aravis_camera import _start_service
        with tempfile.TemporaryDirectory() as folder:
            with patch('krishnan.aravis_camera.load_settings',return_value={'aravis_manage_network':False}),patch('krishnan.aravis_camera.subprocess.Popen') as popen:
                _start_service(Path(folder))
            self.assertIn('krishnan.aravis_service',popen.call_args.args[0])
            self.assertTrue(popen.call_args.kwargs['creationflags'])
    def test_status_is_ipc_and_controls_are_queued_for_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            token = b"test-key"
            monitor = BackendMonitor("Test", token, runtime / "history.log", ["stop"])
            monitor.update(phase="warming", device_state="Idle", cleanup="Temperature gate")
            (runtime / "backend.json").write_text(json.dumps({"monitor_port": monitor.port,
                                                             "authkey": token.hex()}))
            try:
                state = monitor_request(runtime)
                self.assertTrue(state["available"])
                self.assertEqual(state["phase"], "warming")
                self.assertEqual(monitor_request(runtime, "stop")["accepted"], "stop")
                self.assertEqual(list(monitor.drain()), ["stop"])
                (runtime / "history.log").write_text("Detector disconnected\n")
                self.assertEqual(monitor_request(runtime)["device_state"], "Idle")
            finally:
                monitor.close()

    def test_logging_cadence_includes_final_warning_without_shortening_deadline(self):
        warned = False
        last = 0
        moments = []
        for elapsed in range(1, 1800):
            due, warned = progress_due(1800 - elapsed, elapsed - last, warned)
            if due:
                moments.append(elapsed)
                last = elapsed
        self.assertEqual(moments, [600, 1200, 1740])

    def test_legacy_endpoint_is_not_misreported_as_live(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            (runtime / "backend.json").write_text('{"pid":42}')
            result = monitor_request(runtime)
            self.assertFalse(result["available"])
            self.assertTrue(result["legacy"])

    def test_backend_launchers_are_windowless_and_aravis_still_requests_elevation(self):
        from unittest.mock import patch, Mock
        from krishnan.process_worker import _start_backend
        from krishnan.aravis_camera import _start_service
        with tempfile.TemporaryDirectory() as folder:
            with patch("krishnan.process_worker.subprocess.Popen") as popen:
                _start_backend(Path(folder), "motors")
                command = popen.call_args.args[0]
                self.assertTrue(command[0].lower().endswith("pythonw.exe"))
                self.assertIn("krishnan.motor_service", command)
                self.assertNotIn("krishnan.backend_window", command)
            execute = Mock(return_value=33)
            with patch("krishnan.aravis_camera.ctypes.windll.shell32.ShellExecuteW", execute):
                _start_service(Path(folder))
            args = execute.call_args.args
            self.assertEqual(args[1], "runas")
            self.assertTrue(args[2].lower().endswith("pythonw.exe"))
            self.assertEqual(args[-1], 0)


class ConsoleGuiChecks(unittest.TestCase):
    def test_console_preserves_last_ipc_confirmation_without_inferring_state_from_logs(self):
        import wx
        from krishnan.console_view import ConsolePanel
        self.app = wx.GetApp() or wx.App(False)
        frame = wx.Frame(None, size=(650, 800))
        panel = ConsolePanel(frame, {n: lambda: None for n in ("Andor", "Aravis", "Thorlabs")})
        panel.display("Andor", {"available": True, "pid": 42, "updated": time.time(),
                                "device_state": "IDLE", "phase": "warming", "cleanup": "Temperature gate",
                                "deadline": time.time() + 60, "controls": ["stop", "disconnect", "close"]})
        label, buttons = panel.controls["Andor"]
        self.assertIn("warming", label.GetLabel())
        self.assertTrue(buttons["close"].IsEnabled())
        panel.logs.ChangeValue("[Andor] SDK shutdown complete")
        panel.display("Andor", {"available": False, "reason": "IPC unavailable"})
        self.assertIn("unconfirmed", label.GetLabel())
        self.assertIn("warming", label.GetLabel())
        self.assertFalse(buttons["close"].IsEnabled())
        self.assertTrue(buttons["reconnect"].IsEnabled())
        panel.shutdown()
        frame.Destroy()


class ShutdownChecks(unittest.TestCase):
    def setUp(self):
        self.context = mp.get_context("spawn")

    def start(self, target, runtime):
        process = self.context.Process(target=target, args=(runtime,))
        process.start()
        self.addCleanup(self.cleanup, process)
        return process

    def cleanup(self, process):
        if process.is_alive():
            process.terminate()  # Only an offline test subprocess; never a hardware backend.
            process.join()

    def test_aravis_owner_loss_closes_handle_and_exits_without_grace(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            process = self.start(fake_aravis, runtime)
            owner = attach(runtime, "service.json")
            owner.send(("start",))
            self.assertEqual(owner.recv()[0], "ok")
            # Observer works while the frame owner has exclusive camera access.
            state = monitor_request(runtime, info_name="service.json")
            self.assertTrue(state["available"])
            owner.send(("frame", 16))
            response = owner.recv()
            self.assertEqual(response[1][:2], (1, 1))
            self.assertEqual(response[1][2], bytes([0, 1, 2]))
            owner.close()  # Abrupt GUI loss: no graceful detach message.
            process.join(4)
            self.assertEqual(process.exitcode, 0)
            self.assertFalse((runtime / "service.json").exists())
            history = "".join(p.read_text(encoding="utf-8") for p in (runtime / "logs" / "aravis").glob("*.log"))
            self.assertIn("FAKE camera closed", history)
            self.assertIn("without reconnect grace", history)

    def test_aravis_console_safe_close_uses_protocol(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            process = self.start(fake_aravis, runtime)
            owner = attach(runtime, "service.json")
            owner.send(("start",))
            self.assertEqual(owner.recv()[0], "ok")
            result = monitor_request(runtime, "close", info_name="service.json")
            self.assertEqual(result["accepted"], "close")
            process.join(4)
            owner.close()
            self.assertEqual(process.exitcode, 0)

    def test_thorlabs_detach_exits_immediately_after_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            process = self.start(empty_motor_service, runtime)
            owner = attach(runtime)
            owner.recv()  # replay
            self.assertTrue(monitor_request(runtime)["available"])
            owner.close()
            process.join(4)
            self.assertEqual(process.exitcode, 0)
            self.assertFalse((runtime / "backend.json").exists())

    def test_andor_survives_detach_replays_on_reattach_and_obeys_warmup(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            process = self.start(empty_andor_service, runtime)
            owner = attach(runtime)
            owner.close()
            owner = attach(runtime)
            self.assertTrue(owner.poll(2))
            self.assertEqual(owner.recv()[1], "state")
            self.assertTrue(process.is_alive())
            owner.close()
            start = time.monotonic()
            process.join(6)
            self.assertEqual(process.exitcode, 0)
            self.assertGreater(time.monotonic() - start, 1.5)
            history = "".join(p.read_text(encoding="utf-8") for p in (runtime / "logs" / "andor").glob("*.log"))
            self.assertIn("Reconnect grace expired", history)
            self.assertIn("warm-up period started", history)

    def test_andor_cooling_interlocks_and_temperature_gate_are_preserved(self):
        from types import SimpleNamespace
        from dataclasses import dataclass
        from krishnan.worker import InstrumentWorker
        @dataclass
        class Reading:
            celsius: int
        class FakeCCD:
            on = True
            value = -50
            closed = False
            def cooler_is_on(self): return self.on
            def cooler_off(self): self.on = False
            def temperature(self): return Reading(self.value)
            def close(self): self.closed = True
        worker = InstrumentWorker(lambda *_: None)
        camera = FakeCCD()
        worker.camera = camera
        worker.state = "IDLE"
        try:
            with self.assertRaisesRegex(RuntimeError, "cooler off"):
                worker.disconnect()
            worker.begin_orphan_cleanup()
            self.assertFalse(camera.on)
            self.assertFalse(worker.finish_orphan_cleanup())
            self.assertFalse(camera.closed)
            camera.value = -19
            self.assertTrue(worker.finish_orphan_cleanup())
            self.assertTrue(camera.closed)
        finally:
            worker.camera = None
            worker.close()


if __name__ == "__main__":
    unittest.main()
