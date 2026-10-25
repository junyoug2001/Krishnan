"""Compact Kinesis controls; all hardware calls run through the motor process."""
from __future__ import annotations

from dataclasses import asdict
import math
import threading
import time
import uuid

import wx
from wx.lib.scrolledpanel import ScrolledPanel

from .kinesis_motor import MAX_ACCELERATION, MAX_SPEED, MotorSettings
from .process_worker import ProcessWorker
from .settings import load_settings


class MotorSettingsDialog(wx.Dialog):
    def __init__(self, parent, serial, values):
        super().__init__(parent, title=f"Motor settings — {serial}",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.values = MotorSettings.parse(values)
        self.fields = {}
        root = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=4, vgap=9, hgap=10)
        grid.AddGrowableCol(1)
        grid.AddGrowableCol(3)
        grid.Add(wx.StaticText(self, label="Label"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.label = wx.TextCtrl(self, value=self.values.label)
        grid.Add(self.label, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Move direction"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.direction = wx.Choice(self, choices=["Shortest path", "Positive", "Negative"])
        self.direction.SetSelection(self.values.direction)
        grid.Add(self.direction, 1, wx.EXPAND)
        fields = (("velocity", "Move speed (°/s)", .1, MAX_SPEED),
                  ("acceleration", "Move accel. (°/s²)", .1, MAX_ACCELERATION),
                  ("jog_velocity", "Jog speed (°/s)", .1, MAX_SPEED),
                  ("jog_acceleration", "Jog accel. (°/s²)", .1, MAX_ACCELERATION),
                  ("jog_step", "Jog step (°)", .001, 360),
                  ("home_velocity", "Home speed (°/s)", .1, 10),
                  ("backlash", "Backlash (°)", 0, 5))
        for key, caption, low, high in fields:
            grid.Add(wx.StaticText(self, label=caption), 0, wx.ALIGN_CENTER_VERTICAL)
            control = wx.SpinCtrlDouble(self, min=low, max=high,
                                       initial=getattr(self.values, key), inc=.1, size=(105, -1))
            control.SetDigits(3)
            self.fields[key] = control
            grid.Add(control, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Stop"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.stop_mode = wx.Choice(self, choices=["Profiled", "Immediate"])
        self.stop_mode.SetSelection(0 if self.values.stop_mode == "profiled" else 1)
        grid.Add(self.stop_mode, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Jog mode"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mode = wx.Choice(self, choices=["Single step", "Continuous (held)", "Continuous (until Stop)"])
        self.mode.SetSelection(["step", "held", "continuous"].index(self.values.jog_mode))
        grid.Add(self.mode, 1, wx.EXPAND)
        # Complete the last grid row so resizing preserves the two-column form.
        grid.AddSpacer(1)
        grid.AddSpacer(1)
        root.Add(grid, 0, wx.ALL | wx.EXPAND, 14)
        drive = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Drive presets (°/s); hold an arrow to drive")
        self.drives = []
        for i, value in enumerate(self.values.drive_speeds):
            drive.Add(wx.StaticText(drive.GetStaticBox(), label=str(i + 1)), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            control = wx.SpinCtrlDouble(drive.GetStaticBox(), min=.1, max=MAX_SPEED, initial=value, inc=.5, size=(80, -1))
            control.SetDigits(1)
            self.drives.append(control)
            drive.Add(control, 0, wx.RIGHT, 10)
        root.Add(drive, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 14)
        note = wx.StaticText(self, label="Apply saves these preferences for the next connection.\n"
                             "Continuous jogging stops after 60 s; held controls stop on release.\n"
                             "Home and enable states are read from the controller each time.")
        root.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 12)
        self.SetSizerAndFit(root)
        self.SetMinSize(self.GetSize())
        self.FindWindow(wx.ID_OK).SetLabel("Apply and save")
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)

    def _accept(self, _event):
        values = asdict(self.values)
        values.update({key: control.GetValue() for key, control in self.fields.items()})
        values.update(label=self.label.GetValue().strip(), direction=self.direction.GetSelection(),
                      jog_mode=["step", "held", "continuous"][self.mode.GetSelection()],
                      stop_mode=["profiled", "immediate"][self.stop_mode.GetSelection()],
                      drive_speeds=[control.GetValue() for control in self.drives])
        try:
            self.result = asdict(MotorSettings.parse(values))
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid motor settings", wx.OK | wx.ICON_ERROR, self)
            return
        self.EndModal(wx.ID_OK)


class MotorCard(wx.Panel):
    def __init__(self, parent, owner, serial):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.owner, self.serial = owner, serial
        self.data = {"connected": False, "settings": asdict(MotorSettings()), "limits": [0, 360]}
        self._held = None
        self._pending = False
        self._was_connected = False
        self._last_received = 0
        self._ui_error = ""
        self.SetBackgroundColour("#FFFFFF")
        root = wx.BoxSizer(wx.VERTICAL)
        self.title = wx.StaticText(self, label=f"Rotation mount · S/N {serial}")
        self.title.SetFont(self.title.GetFont().Bold())
        root.Add(self.title, 0, wx.ALL, 8)
        readout = wx.BoxSizer(wx.HORIZONTAL)
        self.position = wx.StaticText(self, label="— °")
        self.position.SetFont(wx.Font(25, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        readout.Add(self.position, 1, wx.ALIGN_CENTER_VERTICAL)
        self.indicators = wx.StaticText(self, label="Disconnected")
        readout.Add(self.indicators, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        root.Add(readout, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)
        self.summary = wx.StaticText(self, label="Range 0–360° · connect to read controller state")
        root.Add(self.summary, 0, wx.ALL, 8)

        move_row = wx.BoxSizer(wx.HORIZONTAL)
        move_row.Add(wx.StaticText(self, label="Target °"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.target = wx.TextCtrl(self, value="60", size=(105, -1), style=wx.TE_PROCESS_ENTER)
        move_row.Add(self.target, 1, wx.RIGHT, 5)
        self.go = self._button("Move", lambda _e: self.action("move", self._target()), move_row)
        self.home = self._button("Home", lambda _e: self.action("home"), move_row)
        self.home.SetToolTip("Find the hardware home reference. Ensure clearance through a full rotation.")
        self.stop = self._button("STOP", lambda _e: self.owner.command("stop", self.serial, immediate=True), move_row)
        self.stop.SetBackgroundColour("#C83F34")
        self.stop.SetForegroundColour(wx.WHITE)
        root.Add(move_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        jog_row = wx.BoxSizer(wx.HORIZONTAL)
        jog_row.Add(wx.StaticText(self, label="Jog"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.jog_minus = self._button("−", None, jog_row)
        self.jog_plus = self._button("+", None, jog_row)
        jog_row.AddSpacer(10)
        jog_row.Add(wx.StaticText(self, label="Drive"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.drive = wx.Choice(self, choices=["1: 2.5°/s", "2: 5°/s", "3: 7.5°/s", "4: 10°/s"])
        self.drive.SetSelection(1)
        jog_row.Add(self.drive, 1, wx.RIGHT, 5)
        self.drive_minus = self._button("◀", None, jog_row)
        self.drive_plus = self._button("▶", None, jog_row)
        root.Add(jog_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        for button, direction, drive in ((self.jog_minus, -1, False), (self.jog_plus, 1, False),
                                         (self.drive_minus, -1, True), (self.drive_plus, 1, True)):
            self._bind_motion(button, direction, drive)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        self.settings_button = self._button("Settings…", self.configure, controls)
        self.enable_button = self._button("Enable", lambda _e: self.action("enable", not self.data.get("enabled")), controls)
        self.identify_button = self._button("Identify", lambda _e: self.action("identify"), controls)
        self.connect_button = self._button("Connect", self.toggle_connection, controls)
        self.connect_button.SetMinSize((88, -1))
        root.Add(controls, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        self.status = wx.StaticText(self, label="Disconnected", style=wx.ST_ELLIPSIZE_END)
        self.status.SetMinSize((1, -1))
        root.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        self.SetSizer(root)
        self.target.Bind(wx.EVT_TEXT, lambda _e: self.buttons())
        self.target.Bind(wx.EVT_TEXT_ENTER, lambda _e: self.action("move", self._target()) if self.go.IsEnabled() else None)
        self.buttons()

    def _button(self, label, callback, row):
        button = wx.Button(self, label=label, size=(64 if len(label) < 9 else 82, -1))
        if callback:
            button.Bind(wx.EVT_BUTTON, callback)
        row.Add(button, 0, wx.RIGHT, 5)
        return button

    def _target(self):
        try:
            value = float(self.target.GetValue())
            lo, hi = self.data.get("limits", [0, 360])
            return value if math.isfinite(value) and lo <= value <= hi else None
        except ValueError:
            return None

    def buttons(self):
        connected = self.data.get("connected", False)
        stale = connected and time.monotonic() - self._last_received > 2
        busy = self._pending or self.data.get("moving", False) or self.data.get("operation", "Idle") != "Idle"
        idle = connected and not busy and not stale
        ready = idle and self.data.get("enabled", False) and self.data.get("homed", False)
        self.go.Enable(ready and self._target() is not None)
        self.go.SetToolTip("Enter an angle within the displayed travel range" if self._target() is None else "Move to this absolute angle")
        self.home.Enable(idle and self.data.get("enabled", False))
        self.stop.Enable(connected)
        self.target.Enable(connected)
        self.settings_button.Enable(idle)
        self.enable_button.Enable(idle)
        self.identify_button.Enable(connected and not self._pending)
        self.connect_button.Enable(not self._pending)
        self.connect_button.SetLabel("Disconnect" if connected else "Connect")
        self.enable_button.SetLabel("Disable" if self.data.get("enabled") else "Enable")
        self.drive.Enable(ready)
        step = self.data["settings"]["jog_step"]
        pos = self.data.get("position")
        lo, hi = self.data.get("limits", [0, 360])
        for control, sign, is_drive in ((self.jog_minus, -1, False), (self.jog_plus, 1, False),
                                         (self.drive_minus, -1, True), (self.drive_plus, 1, True)):
            allowed = ready
            if not is_drive and pos is not None and self.data["settings"]["jog_mode"] == "step":
                allowed = allowed and lo <= pos + sign * step <= hi
            if self._held and self._held[1] is control:
                allowed = connected and not stale
            control.Enable(allowed)
        if stale:
            self.status.SetLabel("Readback stale — motion controls disabled; STOP remains available")

    def update(self, data):
        self.data = data
        self._last_received = time.monotonic()
        cfg = data["settings"]
        self.title.SetLabel(f"{cfg['label']} · K10CR2 · S/N {self.serial}")
        connected = data["connected"]
        if connected:
            self.position.SetLabel(f"{data['position']:.4f} °")
            self.position.SetForegroundColour("#143D2C" if data["homed"] else "#8C6417")
            if not self._was_connected:
                self.target.ChangeValue(f"{data['position']:.4f}")
            self.indicators.SetLabel(("Homed" if data["homed"] else "NOT HOMED") + "\n" +
                                    ("Enabled" if data["enabled"] else "Disabled") + "\n" +
                                    f"Fwd {'●' if data['forward_limit'] else '—'}  Rev {'●' if data['reverse_limit'] else '—'}")
        else:
            self.position.SetLabel("— °")
            self.indicators.SetLabel("Disconnected")
        mode = {"step": "step", "held": "held", "continuous": "until Stop"}[cfg["jog_mode"]]
        self.summary.SetLabel(f"0–360° · Move {cfg['velocity']:g}°/s · Accel {cfg['acceleration']:g}°/s² · Jog {cfg['jog_step']:g}° ({mode})")
        previous = max(0, self.drive.GetSelection())
        choices = [f"{i + 1}: {value:g}°/s" for i, value in enumerate(cfg["drive_speeds"])]
        if [self.drive.GetString(i) for i in range(self.drive.GetCount())] != choices:
            self.drive.SetItems(choices)
            self.drive.SetSelection(min(3, previous))
        self.status.SetLabel(data.get("error") or self._ui_error or data.get("operation", "Disconnected"))
        self.status.SetToolTip(self.status.GetLabel())
        self.status.SetForegroundColour("#A12E25" if data.get("error") or self._ui_error else "#60717D")
        self._was_connected = connected
        self.buttons()
        self.Layout()

    def action(self, method, *args, **kwargs):
        if self._pending:
            return
        if method == "move" and (not args or args[0] is None):
            return
        self._pending = True
        self._ui_error = ""
        self.buttons()
        self.owner.command(method, self.serial, *args, callback=self._done, **kwargs)

    def _done(self, error=None):
        self._pending = False
        if error:
            self._ui_error = error
            self.status.SetLabel(error)
            self.status.SetToolTip(error)
        self.buttons()

    def toggle_connection(self, _event):
        self.action("disconnect" if self.data.get("connected") else "connect")

    def configure(self, _event):
        with MotorSettingsDialog(self, self.serial, self.data["settings"]) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.action("apply", dialog.result)

    def _bind_motion(self, button, direction, drive):
        def start(event):
            if not button.IsEnabled() or self._held:
                return
            held = drive or self.data["settings"]["jog_mode"] == "held"
            token = uuid.uuid4().hex if held else None
            if held:
                self._held = (token, button)
                if isinstance(event, wx.MouseEvent) and not button.HasCapture():
                    button.CaptureMouse()
            kwargs = {"token": token}
            if drive:
                kwargs["drive"] = self.drive.GetSelection()
            self.action("jog", direction, **kwargs)
        button.Bind(wx.EVT_LEFT_DOWN, start)
        button.Bind(wx.EVT_LEFT_UP, self.release)
        button.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self.release)
        button.Bind(wx.EVT_KILL_FOCUS, self.release)
        def key_down(event):
            if event.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN):
                start(event)
            else:
                event.Skip()
        def key_up(event):
            if event.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN):
                self.release(event)
            else:
                event.Skip()
        button.Bind(wx.EVT_KEY_DOWN, key_down)
        button.Bind(wx.EVT_KEY_UP, key_up)

    def release(self, _event=None):
        if not self._held:
            return
        token, control = self._held
        self._held = None
        if control.HasCapture():
            control.ReleaseMouse()
        self.owner.command("release", self.serial, token, quiet=True)

    def heartbeat(self):
        if self._held:
            self.owner.command("hold", self.serial, self._held[0], quiet=True)
        self.buttons()


class MotorPanel(ScrolledPanel):
    def __init__(self, parent, *, client_factory=ProcessWorker):
        super().__init__(parent)
        self.client_factory = client_factory
        self.client = None
        self.starting = self.closed = False
        self.pending = []
        self._last_error = ""
        root = wx.BoxSizer(wx.VERTICAL)
        controls = wx.BoxSizer(wx.HORIZONTAL)
        connect = wx.Button(self, label="Connect motors")
        discover = wx.Button(self, label="Discover")
        stop = wx.Button(self, label="STOP ALL")
        stop.SetBackgroundColour("#C83F34")
        stop.SetForegroundColour(wx.WHITE)
        controls.Add(connect, 0, wx.RIGHT, 6)
        controls.Add(discover, 0, wx.RIGHT, 6)
        controls.Add(stop, 0)
        root.Add(controls, 0, wx.ALL, 8)
        self.banner = wx.StaticText(self, label="Close Kinesis before connecting. Home is always an explicit action.",
                                    style=wx.ST_ELLIPSIZE_END)
        self.banner.SetMinSize((1, -1))
        root.Add(self.banner, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        self.cards = {}
        for serial in load_settings()["kinesis_serials"]:
            card = MotorCard(self, self, serial)
            self.cards[serial] = card
            root.Add(card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(root)
        self.SetupScrolling(scroll_x=False)
        connect.Bind(wx.EVT_BUTTON, lambda _e: self.command("connect"))
        discover.Bind(wx.EVT_BUTTON, lambda _e: self.command("discover"))
        stop.Bind(wx.EVT_BUTTON, lambda _e: self.command("stop", immediate=True))
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(200)

    def activate(self):
        if self.client is None and not self.starting and not self.closed:
            self._start()

    def _start(self):
        self.starting = True
        self.banner.SetLabel("Connecting to motor backend…")
        def start():
            try:
                client = self.client_factory(lambda k, p: wx.CallAfter(self.event, k, p), service="motors")
            except Exception as exc:
                wx.CallAfter(self._started, None, str(exc))
            else:
                wx.CallAfter(self._started, client, None)
        threading.Thread(target=start, name="motor-gui-attach", daemon=True).start()

    def _started(self, client, error):
        if self.closed:
            if client:
                client.close()
            return
        self.starting = False
        self.client = client
        pending, self.pending = self.pending, []
        if error:
            self.banner.SetLabel(error)
            for _, _, callback, _ in pending:
                if callback:
                    callback(error)
            return
        self.banner.SetLabel("Motor backend connected · positions in degrees · no automatic homing")
        for method, args, callback, kwargs in pending:
            self.command(method, *args, callback=callback, **kwargs)

    def command(self, method, *args, callback=None, quiet=False, **kwargs):
        if self.closed:
            return
        if self.client is None:
            if quiet:
                return
            self.pending.append((method, args, callback, kwargs))
            self.activate()
            return
        future = self.client.submit(method, *args, **kwargs)
        if not quiet:
            self._last_error = ""
        def finished(done):
            try:
                done.result()
                error = None
            except Exception as exc:
                error = str(exc)
            if error and not quiet:
                wx.CallAfter(self._failure, error)
            if callback:
                wx.CallAfter(callback, error)
        future.add_done_callback(finished)

    def _failure(self, error):
        if not self.closed:
            self._last_error = error
            self.banner.SetLabel(error)
            self.banner.SetToolTip(error)

    def event(self, kind, payload):
        if self.closed:
            return
        if kind == "motors":
            for data in payload["motors"]:
                if data["serial"] in self.cards:
                    self.cards[data["serial"]].update(data)
            available = payload.get("available", [])
            if available and not self._last_error:
                self.banner.SetLabel("Kinesis available: " + ", ".join(d["serial"] for d in available))
            if payload.get("closing"):
                self.banner.SetLabel("Motor backend is stopping and closing its SDK connections")
        elif kind in ("motor_error", "error"):
            self._last_error = str(payload)
            self.banner.SetLabel(str(payload))
        elif kind == "state" and payload == "FAULT":
            self.client = None
            for card in self.cards.values():
                card.release()
                card.data.update(connected=False, moving=False, enabled=False)
                card._pending = False
                card.update(card.data)

    def _tick(self, _event):
        for card in self.cards.values():
            card.heartbeat()

    def shutdown(self):
        for card in self.cards.values():
            card.release()
        self.closed = True
        self.timer.Stop()
        if self.client:
            self.client.close()

