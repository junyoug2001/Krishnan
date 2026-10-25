"""Motor safety and GUI checks using simulated controller calls, never USB."""
from dataclasses import asdict
import ctypes as C
import multiprocessing as mp
from multiprocessing.connection import Client
from pathlib import Path
import tempfile
import time
import unittest

from krishnan.kinesis_motor import KinesisMotor, KinesisSDK, MotorSettings, MotorStopUnverified
from krishnan.motor_service import MotorHub, run


class FakeSDK:
    check = staticmethod(KinesisSDK.check)

    def __init__(self):
        self.calls = []
        self.bits = 0x80000400
        self.position = 0
        self.messages = []

    def ISC_CheckConnection(self, s):
        return True

    def ISC_GetStatusBits(self, s):
        return self.bits

    def ISC_GetPosition(self, s):
        return self.position

    def ISC_GetRealValueFromDeviceUnit(self, s, value, result, unit):
        result._obj.value = value / (49152000 / 360)
        return 0

    def ISC_GetDeviceUnitFromRealValue(self, s, value, result, unit):
        result._obj.value = round(value * (49152000 / 360))
        return 0

    def ISC_ClearMessageQueue(self, s):
        self.messages.clear()

    def ISC_GetNextMessage(self, s, mt, mi, md):
        if not self.messages:
            return False
        mt._obj.value, mi._obj.value, md._obj.value = self.messages.pop(0)
        return True

    def ISC_MoveToPosition(self, s, target):
        self.calls.append(("move", target))
        self.bits |= 0x10
        return 0

    def ISC_MoveJog(self, s, direction):
        self.calls.append(("jog", direction))
        self.bits |= 0x40
        return 0

    def ISC_StopImmediate(self, s):
        self.calls.append(("stop", s))
        self.bits &= ~0x2F0
        return 0

    ISC_StopProfiled = ISC_StopImmediate

    def ISC_SetVelParams(self, s, a, v):
        return 0

    def ISC_StopPolling(self, s):
        pass

    def ISC_Close(self, s):
        self.calls.append(("close", s))


class MotorChecks(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.motor = KinesisMotor(self.sdk, "55000000", {}, lambda *_: None)
        self.motor.opened = True

    def test_invalid_targets_never_reach_motion_api(self):
        for target in (-1, 360.01, float("nan"), float("inf"), True):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.motor.move_to(target)
        self.assertEqual(self.sdk.calls, [])

    def test_unhomed_or_disabled_mount_never_moves(self):
        for bits in (0x80000000, 0x400):
            self.sdk.bits = bits
            with self.assertRaises(RuntimeError):
                self.motor.move_to(60)
        self.assertEqual(self.sdk.calls, [])

    def test_completed_move_requires_matching_position(self):
        self.motor.move_to(60)
        self.assertEqual(self.sdk.calls, [("move", 8192000)])
        self.sdk.bits &= ~0x10
        self.sdk.position = 8192000
        self.sdk.messages.append((2, 1, 8192000))
        state = self.motor.tick()
        self.assertIsNone(self.motor.operation)
        self.assertAlmostEqual(state["position"], 60)

    def test_false_completion_fails(self):
        self.motor.move_to(60)
        self.sdk.bits &= ~0x10
        self.sdk.position = 100
        self.sdk.messages.append((2, 1, 100))
        with self.assertRaisesRegex(RuntimeError, "Position mismatch"):
            self.motor.tick()

    def test_held_jog_stops_if_lease_expires(self):
        self.sdk.position = 8192000
        self.motor.settings.jog_mode = "held"
        self.motor.jog(1, "lease")
        self.motor.operation["hold_at"] -= 2
        self.motor.tick()
        self.assertEqual(self.motor.operation["kind"], "Stopping")
        self.assertEqual(self.sdk.calls[-1][0], "stop")
        self.motor.tick()
        self.assertIsNone(self.motor.operation)

    def test_release_must_match_current_lease(self):
        self.sdk.position = 8192000
        self.motor.settings.jog_mode = "held"
        self.motor.jog(1, "new")
        self.motor.release("old")
        self.assertEqual(self.sdk.calls[-1][0], "jog")
        self.motor.release("new")
        self.assertEqual(self.sdk.calls[-1][0], "stop")

    def test_step_jog_cannot_cross_boundary(self):
        with self.assertRaises(ValueError):
            self.motor.jog(-1)
        self.sdk.position = round(359 * 49152000 / 360)
        with self.assertRaises(ValueError):
            self.motor.jog(1)
        self.assertEqual(self.sdk.calls, [])

    def test_continuous_jog_checks_stopping_distance(self):
        self.motor.settings.jog_mode = "continuous"
        self.sdk.position = round(359 * 49152000 / 360)
        with self.assertRaises(ValueError):
            self.motor.jog(1)
        self.assertEqual(self.sdk.calls, [])

    def test_continuous_jog_stops_before_boundary(self):
        self.motor.settings.jog_mode = "continuous"
        self.sdk.position = 8192000
        self.motor.jog(1)
        self.sdk.position = round(359 * 49152000 / 360)
        self.motor.tick()
        self.assertEqual(self.sdk.calls[-1][0], "stop")

    def test_settings_enforce_speed_acceleration_and_finite_values(self):
        for values in ({"velocity": 16}, {"jog_acceleration": 11}, {"home_velocity": float("nan")},
                       {"drive_speeds": [1, 2, 3, 100]}, {"direction": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MotorSettings.parse(values)

    def test_gui_detachment_stops_both_motors(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = MotorHub(lambda *_: None, sdk=self.sdk,
                           preferences_path=Path(folder) / "preferences.json")
            hub.motors = {s: KinesisMotor(self.sdk, s, {}, lambda *_: None) for s in hub.serials}
            for motor in hub.motors.values():
                motor.opened = True
                motor.operation = {"kind": "Moving"}
            hub.detached()
            self.assertEqual(sum(c[0] == "stop" for c in self.sdk.calls), 2)

    def test_connect_cannot_block_watchdog_during_motion(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = MotorHub(lambda *_: None, sdk=self.sdk,
                           preferences_path=Path(folder) / "preferences.json")
            self.motor.operation = {"kind": "Moving"}
            hub.motors[self.motor.serial] = self.motor
            with self.assertRaisesRegex(RuntimeError, "Stop all motors"):
                hub.dispatch("connect")

    def test_close_error_on_one_motor_still_closes_both_handles(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = MotorHub(lambda *_: None, sdk=self.sdk,
                           preferences_path=Path(folder) / "preferences.json")
            hub.motors = {s: KinesisMotor(self.sdk, s, {}, lambda *_: None) for s in hub.serials}
            for motor in hub.motors.values():
                motor.opened = True
            self.sdk.ISC_StopImmediate = lambda s: 7 if s == hub.serials[0].encode() else 0
            with self.assertRaises(RuntimeError):
                hub.disconnect()
            self.assertEqual(sum(c[0] == "close" for c in self.sdk.calls), 2)
            self.assertFalse(hub.motors)

    def test_controller_handle_is_retained_when_stop_cannot_be_verified(self):
        from unittest.mock import patch
        self.sdk.bits |= 0x10
        self.sdk.ISC_StopImmediate = lambda s: 0  # Reports success but remains moving.
        with patch("krishnan.kinesis_motor.time.monotonic", side_effect=[0, 4]), \
                patch("krishnan.kinesis_motor.time.sleep"):
            with self.assertRaises(MotorStopUnverified):
                self.motor.close()
        self.assertTrue(self.motor.opened)
        self.assertFalse(any(c[0] == "close" for c in self.sdk.calls))


class MotorServiceChecks(unittest.TestCase):
    def test_ipc_handshake_readback_detach_and_shutdown_without_hardware(self):
        context = mp.get_context("spawn")
        shutdown = context.Event()
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            process = context.Process(target=run, args=(runtime,), kwargs={"manual_shutdown": shutdown,
                                                                             "log_root": runtime / "logs"})
            process.start()
            try:
                import json
                deadline = time.monotonic() + 10
                while not (runtime / "backend.json").exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                info = json.loads((runtime / "backend.json").read_text(encoding="utf-8"))
                client = Client(("127.0.0.1", info["port"]), authkey=bytes.fromhex(info["authkey"]))
                self.assertTrue(client.poll(5))
                self.assertEqual(client.recv()[0], "hello")
                event = client.recv()
                self.assertEqual(event[1], "motors")
                self.assertFalse(any(s["connected"] for s in event[2]["motors"]))
                client.send(("call", 1, "status", (), {}))
                while client.poll(5):
                    message = client.recv()
                    if message[0] == "done":
                        self.assertEqual(message, ("done", 1, None))
                        break
                else:
                    self.fail("No completion response")
                client.send(("detach",))
                client.close()
            finally:
                shutdown.set()
                process.join(8)
                if process.is_alive():
                    process.terminate()
                    process.join()
            self.assertEqual(process.exitcode, 0)
            self.assertFalse((runtime / "backend.json").exists())


class MotorGuiChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import wx
        cls.app = wx.App(False)

    def test_out_of_range_go_is_disabled_and_readback_is_degrees(self):
        import wx
        from krishnan.motor_view import MotorPanel, MotorSettingsDialog
        frame = wx.Frame(None, size=(650, 780))
        panel = MotorPanel(frame)
        card = panel.cards["55000000"]
        card.update({"serial": card.serial, "connected": True, "position": 60.0,
                     "homed": True, "enabled": True, "moving": False, "operation": "Idle",
                     "settings": asdict(MotorSettings()), "limits": [0, 360], "error": "",
                     "forward_limit": False, "reverse_limit": False})
        for target in ("-1", "361", "nan", "inf", "text"):
            card.target.SetValue(target)
            self.assertFalse(card.go.IsEnabled(), target)
        card.target.SetValue("60")
        self.assertTrue(card.go.IsEnabled())
        self.assertIn("60.0000", card.position.GetLabel())
        card._last_received -= 3
        card.buttons()
        self.assertFalse(card.go.IsEnabled())
        self.assertTrue(card.stop.IsEnabled())
        dialog = MotorSettingsDialog(frame, card.serial, asdict(MotorSettings()))
        self.assertGreater(dialog.GetSize().width, 400)
        dialog.Destroy()
        frame.Layout()
        self.assertLess(panel.GetBestSize().width, 650)
        panel.shutdown()
        frame.Destroy()


if __name__ == "__main__":
    unittest.main()
