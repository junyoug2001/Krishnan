"""Solis-shaped workspace with Venkata's light wx/Matplotlib visual language."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import threading
import uuid

import numpy as np
import wx
from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg
from matplotlib.figure import Figure

from .bootstrap import prepare_paths
from .camera_view import CameraPanel
from .core import Session, Step, repeated_steps
from .map_view import MapPanel
from .processing import cosmic_candidates
from .spectral_color import wavelength_rgb
from .worker import InstrumentWorker

prepare_paths()
from andor_spectroscopy.models import ReadoutConfig, ReadoutMode
from data_structure import raman_shift_cm1


BG = "#F4F5F6"
PANEL = "#FFFFFF"
INK = "#24313A"
MUTED = "#60717D"
ORANGE = "#D85B29"
BLUE = "#2589A4"


def _label(parent, text: str, *, bold: bool = False):
    control = wx.StaticText(parent, label=text)
    if bold:
        font = control.GetFont()
        font.MakeBold()
        control.SetFont(font)
    return control


def _row(parent, *controls):
    sizer = wx.BoxSizer(wx.HORIZONTAL)
    for control in controls:
        sizer.Add(control, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 7)
    return sizer


class WavelengthStrip(wx.Panel):
    """Compact Solis-style wavelength overview; drag proposes a center only."""

    def __init__(self, parent, on_propose):
        super().__init__(parent, size=(-1, 96))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((-1, 96))
        self.limits = (0.0, 1200.0)
        self.span = None
        self.center = None
        self.on_propose = on_propose
        self.Bind(wx.EVT_PAINT, self.paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.click)
        self.Bind(wx.EVT_MOTION, self.motion)
        self.Bind(wx.EVT_LEFT_UP, self.release)
        self.dragging = False

    def paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        width, _height = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(PANEL)))
        dc.Clear()
        left, right = 24, max(25, width - 24)
        lo, hi = self.limits
        if hi <= lo:
            return
        x_at = lambda nm: left + int((nm - lo) / (hi - lo) * (right - left))
        dc.SetTextForeground(wx.Colour(INK))
        band_y, band_height = 49, 22
        if self.center is not None:
            marker_x = x_at(self.center)
            center_label = f"{self.center:.2f} nm"
            center_width = dc.GetTextExtent(center_label).width
            dc.DrawText(center_label,
                        min(max(left, marker_x - center_width // 2), right - center_width), 3)
            if self.span is not None:
                boundary_left, boundary_right = sorted(self.span)
                low_label = f"{boundary_left:.2f}"
                high_label = f"{boundary_right:.2f}"
                low_width = dc.GetTextExtent(low_label).width
                high_width = dc.GetTextExtent(high_label).width
                dc.DrawText(low_label, min(max(left, marker_x - low_width - 8),
                                           right - low_width), 20)
                dc.DrawText(high_label, min(max(left, marker_x + 8),
                                            right - high_width), 20)
            else:
                missing = "CCD range unavailable"
                missing_width = dc.GetTextExtent(missing).width
                dc.DrawText(missing, min(max(left, marker_x - missing_width // 2),
                                         right - missing_width), 20)
            dc.SetPen(wx.Pen(wx.Colour(INK), 1))
            dc.SetBrush(wx.Brush(wx.Colour(PANEL)))
            dc.DrawPolygon([wx.Point(marker_x - 10, 36),
                            wx.Point(marker_x + 10, 36), wx.Point(marker_x, 46)])
        else:
            dc.DrawText("Center and CCD range unavailable", left, 5)
        for px in range(left, right):
            nm = lo + (px - left) / max(1, right - left) * (hi - lo)
            dc.SetPen(wx.Pen(wx.Colour(*wavelength_rgb(nm))))
            dc.DrawLine(px, band_y, px, band_y + band_height)
        dc.SetPen(wx.Pen(wx.Colour("#839099"), 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(left, band_y, right - left, band_height + 1)
        dc.SetTextForeground(wx.Colour(MUTED))
        dc.DrawText(f"{lo:.0f} nm", left, 75)
        end_label = f"{hi:.0f} nm"
        dc.DrawText(end_label, right - dc.GetTextExtent(end_label).width, 75)
        if self.span is not None:
            a, b = sorted(self.span)
            x0, x1 = x_at(a), x_at(b)
            dc.SetPen(wx.Pen(wx.Colour(BLUE), 2))
            dc.DrawLine(x0, band_y - 5, x0, band_y + band_height + 5)
            dc.DrawLine(x1, band_y - 5, x1, band_y + band_height + 5)
        if self.center is not None:
            x = x_at(self.center)
            dc.SetPen(wx.Pen(wx.Colour(ORANGE), 2))
            dc.DrawLine(x, band_y - 3, x, band_y + band_height + 6)

    def _propose(self, x: int):
        width = self.GetClientSize().width
        fraction = min(1, max(0, (x - 24) / max(1, width - 48)))
        lo, hi = self.limits
        self.on_propose(lo + fraction * (hi - lo))

    def click(self, event):
        self.dragging = True
        self.CaptureMouse()
        self._propose(event.GetX())

    def motion(self, event):
        if self.dragging:
            self._propose(event.GetX())

    def release(self, event):
        if self.dragging:
            self.dragging = False
            if self.HasCapture():
                self.ReleaseMouse()


class AdvancedDialog(wx.Dialog):
    def __init__(self, parent, inventory: dict | None, readout: ReadoutConfig,
                 advanced: dict):
        super().__init__(parent, title="Advanced acquisition", size=(680, 600),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.inventory = inventory or {}
        self.caps = self.inventory.get("readout", {})
        self.readout = readout
        self.advanced = dict(advanced)
        outer = wx.BoxSizer(wx.VERTICAL)
        notebook = wx.Notebook(self)
        readout_page, readout_grid = self._page(notebook, "Readout")
        roi_page, roi_grid = self._page(notebook, "ROI & tracks")
        electronics_page, electronics_grid = self._page(notebook, "Electronics")
        caps = self.caps
        detector = self.inventory.get("camera", {}).get("detector", (None, None))
        if detector[0] is not None:
            summary = f"Detector: {detector[0]} × {detector[1]} pixels"
        else:
            summary = "Connect a camera to see supported settings and detector size."
        outer.Add(_label(self, summary), 0, wx.ALL, 12)
        mask = int(caps.get("readout_mask", 0x3f))
        supported = [
            ("FVB", ReadoutMode.FVB, 8),
            ("Single track", ReadoutMode.SINGLE_TRACK, 4),
            ("Multi track", ReadoutMode.MULTI_TRACK, 16),
            ("Random track", ReadoutMode.RANDOM_TRACK, 32),
            ("Image", ReadoutMode.IMAGE, 1),
        ]
        self.mode_values = [(label, mode) for label, mode, bit in supported if mask & bit]
        self.mode = wx.Choice(readout_page, choices=[label for label, _ in self.mode_values])
        available_modes = [mode for _, mode in self.mode_values]
        self.mode.SetSelection(available_modes.index(readout.mode)
                               if readout.mode in available_modes else 0)
        self._field(readout_page, readout_grid, "Readout mode", self.mode)
        trigger_labels = {0: "Internal", 1: "External", 6: "External start",
                          7: "External exposure", 10: "Software"}
        trigger_values = self.inventory.get("triggers", [0])
        self.trigger_values = trigger_values
        self.trigger = wx.Choice(readout_page, choices=[trigger_labels.get(v, f"SDK mode {v}")
                                                        for v in trigger_values])
        current_trigger = advanced.get("trigger", 0)
        self.trigger.SetSelection(trigger_values.index(current_trigger)
                                  if current_trigger in trigger_values else 0)
        self._field(readout_page, readout_grid, "Trigger", self.trigger)
        self.fields = {}
        defaults = {
            "hbin": readout.hbin, "vbin": readout.vbin,
            "hstart": readout.hstart, "hend": readout.hend or "",
            "vstart": readout.vstart, "vend": readout.vend or "",
            "track_center": readout.track_center if readout.track_center is not None else "",
            "track_height": readout.track_height if readout.track_height is not None else "",
            "tracks": readout.tracks if readout.tracks is not None else "",
            "track_offset": readout.track_offset if readout.track_offset is not None else "",
            "random_tracks": ";".join(f"{a}:{b}" for a, b in readout.random_tracks),
        }
        for key, value in defaults.items():
            page, grid = (readout_page, readout_grid) if key in ("hbin", "vbin") else (roi_page, roi_grid)
            control = wx.TextCtrl(page, value=str(value))
            self.fields[key] = control
            self._field(page, grid, key.replace("_", " ").title(), control)
        self.selectors = {}
        speed_sets = caps.get("horizontal_speeds_mhz", {})
        speeds = next(iter(speed_sets.values()), ())
        for key, label, values, suffix in (
            ("ad_channel", "AD channel", caps.get("ad_channels", ()), ""),
            ("amplifier", "Amplifier", caps.get("amplifiers", ()), ""),
            ("hs_index", "Horizontal speed", speeds, " MHz"),
            ("vs_index", "Vertical speed", caps.get("vertical_speeds_us", ()), " µs"),
            ("preamp_index", "Preamp gain", caps.get("preamp_gains", ()), "×"),
        ):
            choices = ["Camera default"] + [f"{i}: {value:g}{suffix}" if isinstance(value, (float, int))
                                            else f"{i}: {value}" for i, value in enumerate(values)]
            choice = wx.Choice(electronics_page, choices=choices)
            value = advanced.get(key)
            choice.SetSelection(0 if value is None else min(value + 1, len(choices) - 1))
            self.selectors[key] = choice
            self._field(electronics_page, electronics_grid, label, choice)
        outer.Add(notebook, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        outer.Add(_label(self, "ROI uses zero-based, half-open detector coordinates. Blank stop means detector edge."),
                  0, wx.ALL, 12)
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(wx.Button(self, wx.ID_OK))
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()
        outer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 12)
        self.SetSizer(outer)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self.mode.Bind(wx.EVT_CHOICE, self._mode_changed)
        self.selectors["ad_channel"].Bind(wx.EVT_CHOICE, self._refresh_hs)
        self.selectors["amplifier"].Bind(wx.EVT_CHOICE, self._refresh_hs)
        self._refresh_hs(None)
        self._mode_changed(None)
        for page in (readout_page, roi_page, electronics_page):
            page.Layout()
            page.FitInside()
        self.CentreOnParent()

    def _page(self, notebook, title):
        page = wx.ScrolledWindow(notebook, style=wx.VSCROLL)
        page.SetScrollRate(0, 12)
        grid = wx.FlexGridSizer(0, 2, 10, 14)
        grid.AddGrowableCol(1, 1)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 16)
        page.SetSizer(box)
        notebook.AddPage(page, title)
        return page, grid

    @staticmethod
    def _field(page, grid, label, control):
        grid.Add(_label(page, label), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(control, 1, wx.EXPAND)

    def _mode_changed(self, event):
        mode = self.mode_values[self.mode.GetSelection()][1]
        enabled = {
            "hstart": mode == ReadoutMode.IMAGE,
            "hend": mode == ReadoutMode.IMAGE,
            "vstart": mode == ReadoutMode.IMAGE,
            "vend": mode == ReadoutMode.IMAGE,
            "vbin": mode == ReadoutMode.IMAGE,
            "track_center": mode == ReadoutMode.SINGLE_TRACK,
            "track_height": mode in (ReadoutMode.SINGLE_TRACK, ReadoutMode.MULTI_TRACK),
            "tracks": mode == ReadoutMode.MULTI_TRACK,
            "track_offset": mode == ReadoutMode.MULTI_TRACK,
            "random_tracks": mode == ReadoutMode.RANDOM_TRACK,
        }
        for key, control in self.fields.items():
            control.Enable(enabled.get(key, True))

    def _refresh_hs(self, event):
        speed_sets = self.caps.get("horizontal_speeds_mhz", {})
        if not speed_sets:
            return
        channel = max(0, self.selectors["ad_channel"].GetSelection() - 1)
        amplifier = max(0, self.selectors["amplifier"].GetSelection() - 1)
        speeds = (speed_sets.get((channel, amplifier))
                  or speed_sets.get(f"{channel}:{amplifier}")
                  or next(iter(speed_sets.values())))
        choice = self.selectors["hs_index"]
        previous = max(0, choice.GetSelection())
        choice.SetItems(["Camera default"] +
                        [f"{index}: {speed:g} MHz" for index, speed in enumerate(speeds)])
        choice.SetSelection(min(previous, len(speeds)))

    def _accept(self, event):
        try:
            values = {}
            for key, control in self.fields.items():
                text = control.GetValue().strip()
                values[key] = text if key == "random_tracks" else (
                    None if not text else int(text))
            tracks = ()
            if values["random_tracks"]:
                tracks = tuple(tuple(int(n) for n in part.split(":"))
                               for part in values["random_tracks"].split(";"))
            self.readout = ReadoutConfig(
                mode=self.mode_values[self.mode.GetSelection()][1],
                hbin=values["hbin"], vbin=values["vbin"],
                hstart=values["hstart"], hend=values["hend"],
                vstart=values["vstart"], vend=values["vend"],
                track_center=values["track_center"], track_height=values["track_height"],
                tracks=values["tracks"], track_offset=values["track_offset"],
                random_tracks=tracks)
            self.advanced = {
                "trigger": self.trigger_values[self.trigger.GetSelection()],
                **{key: (None if control.GetSelection() == 0
                         else control.GetSelection() - 1)
                   for key, control in self.selectors.items()},
            }
        except (ValueError, TypeError) as exc:
            wx.MessageBox(str(exc), "Invalid acquisition setting", wx.OK | wx.ICON_ERROR)
            return
        self.EndModal(wx.ID_OK)


class BatchDialog(wx.Dialog):
    def __init__(self, parent, exposure: float, cycle: float, *,
                 index_offset: int = 0, simulated: bool = False):
        super().__init__(parent, title="Batch capture", size=(680, 700),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        settings = wx.ScrolledWindow(panel, style=wx.VSCROLL)
        settings.SetScrollRate(0, 12)
        settings.SetMinSize((-1, 310))
        grid = wx.FlexGridSizer(0, 2, 8, 12)
        grid.AddGrowableCol(1, 1)
        self.fields = {}
        self._preview_result = None
        self.index_offset = index_offset
        self.raw_extension = ".npz" if simulated else ".sif"
        for name, value in (
            ("Name", "batch"), ("Count", "5"), ("Exposure (s)", str(exposure)),
            ("Cycle (s)", str(cycle)), ("Accumulations", "1"),
            ("Center wavelengths (comma separated)", ""),
            ("Angles (deg, comma separated)", ""),
            ("Wide scan min nm", ""), ("Wide scan max nm", ""),
            ("Overlap fraction", "0.10"),
        ):
            grid.Add(_label(settings, name), 0, wx.ALIGN_CENTER_VERTICAL)
            control = wx.TextCtrl(settings, value=value)
            control.Bind(wx.EVT_TEXT, lambda event: setattr(self, "_preview_result", None))
            self.fields[name] = control
            grid.Add(control, 1, wx.EXPAND)
        settings.SetSizer(grid)
        sizer.Add(settings, 0, wx.ALL | wx.EXPAND, 16)
        sizer.Add(_label(panel, "For a wide scan, enter min/max nm. Count and coordinate lists are ignored."),
                  0, wx.LEFT | wx.BOTTOM, 16)
        self.preview = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.BORDER_SIMPLE)
        for index, (label, width) in enumerate((("Step", 50), ("Raw filename", 210),
                                                  ("Center nm", 110), ("Angle °", 70))):
            self.preview.InsertColumn(index, label, width=width)
        sizer.Add(self.preview, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 16)
        self.estimate = _label(panel, "Preview the sequence before starting.")
        sizer.Add(self.estimate, 0, wx.LEFT | wx.TOP, 16)
        preview_button = wx.Button(panel, label="Preview sequence")
        sizer.Add(preview_button, 0, wx.ALL, 16)
        preview_button.Bind(wx.EVT_BUTTON, self._preview)
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(wx.Button(panel, wx.ID_OK))
        buttons.AddButton(wx.Button(panel, wx.ID_CANCEL))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 12)
        panel.SetSizer(sizer)
        frame = wx.BoxSizer(wx.VERTICAL)
        frame.Add(panel, 1, wx.EXPAND)
        self.SetSizer(frame)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self.result = None

    def _parse(self):
        text = lambda name: self.fields[name].GetValue().strip()
        exposure, cycle = float(text("Exposure (s)")), float(text("Cycle (s)"))
        low, high = text("Wide scan min nm"), text("Wide scan max nm")
        if bool(low) != bool(high):
            raise ValueError("Enter both wide-scan endpoints")
        if low:
            return ("scan", float(low), float(high), float(text("Overlap fraction")),
                    exposure, cycle)
        count = int(text("Count"))
        centers = [float(v) for v in text("Center wavelengths (comma separated)").split(",") if v.strip()]
        angles = [float(v) for v in text("Angles (deg, comma separated)").split(",") if v.strip()]
        steps = repeated_steps(
            text("Name"), count, exposure, cycle, int(text("Accumulations")),
            centers or None, angles or None)
        return ("batch", steps)

    def _preview(self, event):
        try:
            result = self._parse()
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid batch", wx.OK | wx.ICON_ERROR)
            return
        self._preview_result = result
        self.preview.DeleteAllItems()
        if result[0] == "scan":
            self.preview.InsertItem(0, "Scan")
            self.preview.SetItem(0, 1, f"{result[1]:.2f}–{result[2]:.2f} nm")
            self.preview.SetItem(0, 2, f"{result[3]:.0%} overlap")
            self.estimate.SetLabel("Tile count and time are resolved from the connected calibration.")
            return
        for i, step in enumerate(result[1]):
            indexed = Step(step.id, step.index + self.index_offset, step.name,
                           step.center_nm, step.grating, step.angle_deg,
                           step.exposure_s, step.cycle_s, step.accumulations)
            self.preview.InsertItem(i, str(indexed.index))
            self.preview.SetItem(i, 1, indexed.stem + self.raw_extension)
            self.preview.SetItem(i, 2, "" if step.center_nm is None else f"{step.center_nm:g}")
            self.preview.SetItem(i, 3, "" if step.angle_deg is None else f"{step.angle_deg:g}")
        seconds = sum(max(step.exposure_s * step.accumulations, step.cycle_s)
                      for step in result[1])
        self.estimate.SetLabel(
            f"{len(result[1])} steps · at least {seconds:.1f} s exposure/cycle time, "
            "plus readout, motion and file writes")

    def _accept(self, event):
        try:
            self.result = self._preview_result or self._parse()
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid batch", wx.OK | wx.ICON_ERROR)
            return
        self.EndModal(wx.ID_OK)


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Krishnan — Raman Measurement", size=(1480, 920))
        self.SetBackgroundColour(wx.Colour(BG))
        self.worker = InstrumentWorker(self._post)
        self.inventory = None
        self.session = None
        self.readout = ReadoutConfig()
        self.advanced = {}
        self.saved = []
        self.last = None
        self.live_future = None
        self._angular_export_pending = False
        self._build()
        self._set_state("DISCONNECTED")
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll, self.timer)
        self.timer.Start(5000)
        self.Bind(wx.EVT_CLOSE, self._close)
        self.Centre()
        self.Show()

    def _post(self, kind, payload):
        wx.CallAfter(self._event, kind, payload)

    def _button(self, parent, text, callback, *, strong=False):
        button = wx.Button(parent, label=text)
        if strong:
            button.SetBackgroundColour(wx.Colour(ORANGE))
            button.SetForegroundColour(wx.WHITE)
        button.Bind(wx.EVT_BUTTON, callback)
        return button

    def _build(self):
        root = wx.BoxSizer(wx.VERTICAL)
        toolbar = wx.Panel(self)
        toolbar.SetBackgroundColour(wx.Colour(PANEL))
        line = wx.BoxSizer(wx.HORIZONTAL)
        title = _label(toolbar, "KRISHNAN", bold=True)
        title.SetForegroundColour(wx.Colour(ORANGE))
        line.Add(title, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 15)
        self.mode = wx.Choice(toolbar, choices=["Simulator", "Native camera"])
        self.mode.SetSelection(0)
        line.Add(self.mode, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.connect_button = self._button(toolbar, "Connect", self._connect)
        self.session_button = self._button(toolbar, "New/Open session", self._session)
        self.live_button = self._button(toolbar, "Live", self._live, strong=True)
        self.single_button = self._button(toolbar, "Single", self._single)
        self.batch_button = self._button(toolbar, "Batch", self._batch)
        self.snapshot_button = self._button(toolbar, "Capture snapshot", self._snapshot)
        self.stop_button = self._button(toolbar, "Stop", self._stop)
        self.trigger_button = self._button(toolbar, "Software trigger", self._trigger)
        self.export_button = self._button(toolbar, "Export Venkata", self._export)
        self.cosmic_button = self._button(toolbar, "Cosmic review", self._cosmic_review)
        for button in (self.connect_button, self.session_button, self.live_button,
                       self.single_button, self.batch_button, self.snapshot_button,
                       self.stop_button, self.trigger_button, self.export_button,
                       self.cosmic_button):
            line.Add(button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.state_label = _label(toolbar, "DISCONNECTED", bold=True)
        line.AddStretchSpacer()
        line.Add(self.state_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        toolbar.SetSizer(line)
        root.Add(toolbar, 0, wx.EXPAND | wx.BOTTOM, 6)

        split = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self.split = split
        left = wx.Panel(split)
        right = wx.Panel(split)
        left.SetBackgroundColour(wx.Colour(PANEL))
        right.SetBackgroundColour(wx.Colour(PANEL))
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        self.figure = Figure(figsize=(9, 5), facecolor=PANEL)
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasWxAgg(left, -1, self.figure)
        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel("Counts")
        self.ax.grid(alpha=0.16)
        left_sizer.Add(self.canvas, 1, wx.EXPAND | wx.ALL, 8)
        self.strip = WavelengthStrip(left, self._propose_center)
        left_sizer.Add(self.strip, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        left.SetSizer(left_sizer)

        right_sizer = wx.BoxSizer(wx.VERTICAL)
        notebook = wx.Notebook(right)
        self.notebook = notebook
        self.camera_panel = CameraPanel(notebook, lambda: self.session.folder
                                        if self.session is not None else None)
        notebook.AddPage(self.camera_panel, "Camera")
        step_page = wx.Panel(notebook)
        step_sizer = wx.BoxSizer(wx.VERTICAL)
        self.steps_list = wx.ListCtrl(step_page, style=wx.LC_REPORT | wx.BORDER_SIMPLE)
        for i, (label, width) in enumerate((("#", 35), ("Name", 120), ("State", 100),
                                             ("Center", 75))):
            self.steps_list.InsertColumn(i, label, width=width)
        step_sizer.Add(self.steps_list, 1, wx.EXPAND | wx.ALL, 6)
        self.resume_button = self._button(step_page, "Resume incomplete steps", self._resume)
        step_sizer.Add(self.resume_button, 0, wx.ALL, 6)
        step_page.SetSizer(step_sizer)
        notebook.AddPage(step_page, "Steps")
        map_page = wx.Panel(notebook)
        map_sizer = wx.BoxSizer(wx.VERTICAL)
        self.map_view = MapPanel(map_page)
        map_sizer.Add(self.map_view, 1, wx.EXPAND | wx.ALL, 5)
        self.map_fit_button = self._button(
            map_page, "Export angular map for Venkata 2D fitting", self._angular_fit)
        self.map_fit_button.Enable(False)
        map_sizer.Add(self.map_fit_button, 0, wx.ALL, 6)
        map_page.SetSizer(map_sizer)
        notebook.AddPage(map_page, "Map")
        detail_page = wx.Panel(notebook)
        detail_sizer = wx.BoxSizer(wx.VERTICAL)
        self.details = wx.TextCtrl(detail_page, style=wx.TE_MULTILINE | wx.TE_READONLY)
        detail_sizer.Add(self.details, 1, wx.EXPAND | wx.ALL, 6)
        detail_page.SetSizer(detail_sizer)
        notebook.AddPage(detail_page, "Details")
        notebook.ChangeSelection(1)
        notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab_changed)
        right_sizer.Add(notebook, 1, wx.EXPAND)
        right.SetSizer(right_sizer)
        split.SplitVertically(left, right, sashPosition=1080)
        split.SetMinimumPaneSize(250)
        root.Add(split, 1, wx.EXPAND)
        self._bottom(root)
        self.SetSizer(root)
        self.CreateStatusBar()
        self.SetStatusText("Choose Simulator or Native camera, then Connect.")

    def _on_tab_changed(self, event):
        name = self.notebook.GetPageText(event.GetSelection())
        right_width = 960 if name == "Map" else 650 if name == "Camera" else 400
        width = self.split.GetClientSize().width
        if width > 0:
            self.split.SetSashPosition(max(430, width - right_width))
        event.Skip()

    def _bottom(self, root):
        box = wx.Panel(self)
        box.SetBackgroundColour(wx.Colour(PANEL))
        outer = wx.BoxSizer(wx.VERTICAL)
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        for label, default, width in (
            ("Sample", "sample", 130), ("Laser nm", "", 70),
            ("Polarization", "", 95), ("Exposure s", "0.1", 70),
            ("Cycle s", "0.2", 70), ("Accumulations", "1", 45),
        ):
            row1.Add(_label(box, label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            control = wx.TextCtrl(box, value=default, size=(width, -1))
            setattr(self, label.lower().replace(" ", "_"), control)
            row1.Add(control, 0, wx.RIGHT, 12)
        self.axis_choice = wx.Choice(box, choices=["Wavelength nm", "Raman shift cm⁻¹"])
        self.axis_choice.SetSelection(0)
        self.axis_choice.Bind(wx.EVT_CHOICE, lambda e: (self._redraw(), self._map()))
        row1.Add(self.axis_choice, 0, wx.RIGHT, 8)
        row1.Add(self._button(box, "Advanced…", self._advanced), 0)
        outer.Add(row1, 0, wx.EXPAND | wx.ALL, 9)

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(_label(box, "Spectrograph", bold=True), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        row2.Add(_label(box, "Grating"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.grating = wx.Choice(box, choices=[])
        row2.Add(self.grating, 0, wx.RIGHT, 8)
        row2.Add(self._button(box, "Set grating", self._set_grating), 0, wx.RIGHT, 12)
        row2.Add(_label(box, "Center nm"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.center = wx.TextCtrl(box, value="500", size=(75, -1))
        row2.Add(self.center, 0, wx.RIGHT, 5)
        row2.Add(self._button(box, "Move", self._set_center), 0, wx.RIGHT, 12)
        self.optical_readback = _label(box, "No instrument")
        row2.Add(self.optical_readback, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row2.Add(self._button(box, "Set calibration geometry", self._calibration), 0)
        row2.Add(self._button(box, "Accessories…", self._accessories_dialog), 0, wx.LEFT, 6)
        outer.Add(row2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 9)

        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(_label(box, "CCD", bold=True), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self.ccd_status = _label(box, "Disconnected")
        row3.Add(self.ccd_status, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        row3.Add(_label(box, "Setpoint °C"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.setpoint = wx.TextCtrl(box, value="-50", size=(55, -1))
        row3.Add(self.setpoint, 0, wx.RIGHT, 5)
        row3.Add(self._button(box, "Cooler on", lambda e: self._cooler(True)), 0, wx.RIGHT, 5)
        row3.Add(self._button(box, "Cooler off", lambda e: self._cooler(False)), 0, wx.RIGHT, 12)
        row3.Add(_label(box, "Camera shutter"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.shutter = wx.Choice(box, choices=["Closed", "Open", "Automatic"])
        self.shutter.SetSelection(2)
        row3.Add(self.shutter, 0, wx.RIGHT, 5)
        row3.Add(self._button(box, "Apply", self._shutter), 0, wx.RIGHT, 15)
        self.accessories = _label(box, "Accessories: —")
        row3.Add(self.accessories, 1, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(row3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 9)
        future = _label(box, "Sample temperature · magnetic field · angle: controller not configured")
        future.SetForegroundColour(wx.Colour(MUTED))
        outer.Add(future, 0, wx.LEFT | wx.BOTTOM, 9)
        box.SetSizer(outer)
        root.Add(box, 0, wx.EXPAND | wx.TOP, 5)

    def _set_state(self, state):
        self.state_label.SetLabel(state)
        connected = state in ("IDLE", "LIVE", "ACQUIRING", "STOPPING", "FAULT")
        idle = state == "IDLE"
        self.connect_button.SetLabel("Disconnect" if connected else "Connect")
        self.connect_button.Enable(state in ("DISCONNECTED", "IDLE", "FAULT"))
        self.session_button.Enable(state in ("DISCONNECTED", "IDLE", "FAULT"))
        self.live_button.Enable(idle)
        self.single_button.Enable(idle)
        self.batch_button.Enable(idle)
        self.stop_button.Enable(state in ("LIVE", "ACQUIRING", "STOPPING"))
        self.trigger_button.Enable(state == "ACQUIRING"
                                   and self.advanced.get("trigger") == 10)
        self.snapshot_button.Enable(state == "LIVE" and self.session is not None)
        self.resume_button.Enable(idle and self.session is not None)
        self.export_button.Enable(self.session is not None)
        self.cosmic_button.Enable(idle and bool(self.saved))
        self.SetStatusText(state)

    def _submit(self, name, *args, **kwargs):
        future = self.worker.submit(name, *args, **kwargs)
        return future

    def _event(self, kind, payload):
        if kind == "state":
            self._set_state(payload)
        elif kind == "error":
            self._angular_export_pending = False
            self.SetStatusText(payload)
            self.details.AppendText(payload + "\n")
        elif kind == "status":
            self.SetStatusText(payload)
        elif kind == "inventory":
            self.inventory = payload
            self.details.SetValue(str(payload))
            gratings = payload["gratings"]
            self.grating.SetItems([f"{g['index']}: {g['lines_per_mm']:g} l/mm — {g['blaze']}"
                                   for g in gratings])
            selected = payload["optical"]["grating"]
            self.grating.SetSelection(max(0, selected - 1))
            self._optical(payload["optical"])
            cam = payload["camera"]
            temp = payload["temperature"]
            self.ccd_status.SetLabel(
                f"{cam['head_model']} #{cam['serial_number']} · {temp['celsius']:.1f} °C ({temp['status']})")
            installed = payload["accessories"]
            names = [key for key, value in installed.items() if value]
            self.accessories.SetLabel("Accessories: " + (", ".join(names) or "manual / none"))
            if not payload["geometry_matches"]:
                self.SetStatusText("Calibration geometry differs from camera; set it before capture.")
            if self.session is not None:
                self._submit("use_session", self.session)
        elif kind == "optical":
            self._optical(payload)
        elif kind == "wavelength_range":
            limits = payload.get("limits")
            span = payload.get("span")
            if limits is not None:
                self.strip.limits = tuple(sorted(limits))
            self.strip.span = tuple(span) if span is not None else None
            self.strip.Refresh()
        elif kind == "geometry":
            self.SetStatusText(f"Calibration geometry set: {payload}")
        elif kind == "temperature":
            info = payload["reading"]
            self.ccd_status.SetLabel(
                f"{info['celsius']:.1f} °C ({info['status']}); cooler {'on' if payload['cooler_on'] else 'off'}")
        elif kind == "accessory":
            self.SetStatusText(
                f"{payload['kind']} {payload['index']}: {payload['value']} (readback)")
        elif kind == "preview":
            self.last = payload
            self._redraw()
            timings = payload["timings"]
            stats = payload["statistics"]
            self.SetStatusText(
                f"Live: applied exposure {timings.exposure_s:.4g} s · cycle {timings.kinetic_s:.4g} s · "
                f"dropped {stats.sdk_lost + stats.queue_lost}")
        elif kind == "measurement":
            self.last = payload
            self.saved.append(payload)
            self._redraw()
            self._map()
        elif kind == "restored_measurements":
            self.saved = payload
            self.last = payload[-1] if payload else None
            self._redraw()
            self._map()
            self._set_state(self.worker.state)
        elif kind == "steps":
            self.steps_list.DeleteAllItems()
            for i, item in enumerate(payload):
                self.steps_list.InsertItem(i, str(item["index"]))
                self.steps_list.SetItem(i, 1, item["name"])
                self.steps_list.SetItem(i, 2, item["state"])
                self.steps_list.SetItem(i, 3, "" if item["center_nm"] is None
                                        else f"{item['center_nm']:.2f}")
        elif kind == "export":
            paths, h5 = payload
            self.SetStatusText(f"Exported {len(paths)} CSV files and {h5.name}")
            if self._angular_export_pending:
                self._angular_export_pending = False
                wx.MessageBox(
                    f"Open this experiment in Venkata and choose its 2D fitting tool:\n{h5}",
                    "Angular map exported", wx.OK)

    def _optical(self, data):
        center = data.get("wavelength_nm")
        if center is not None:
            self.center.ChangeValue(f"{center:.3f}")
            self.strip.center = center
        self.optical_readback.SetLabel(
            f"SR {data['serial']} · grating {data['grating']} · {center:.3f} nm"
            if center is not None else f"SR {data['serial']} · grating {data['grating']}")
        if self.inventory:
            grating = data["grating"]
            gratings = self.inventory["gratings"]
            if grating and 1 <= grating <= len(gratings):
                self.grating.SetSelection(grating - 1)
        self.strip.Refresh()

    def _axis(self, nm):
        if self.axis_choice.GetSelection() == 0:
            return nm, "Wavelength (nm)"
        try:
            laser = (self.session.data["laser_nm"] if self.session is not None
                     else float(self.laser_nm.GetValue()))
            if laser is None:
                raise ValueError()
            if not np.isfinite(laser) or laser <= 0:
                raise ValueError()
            return raman_shift_cm1(laser, nm), "Raman shift (cm⁻¹)"
        except ValueError:
            self.axis_choice.SetSelection(0)
            self.SetStatusText("Enter a positive laser wavelength to show Raman shift.")
            return nm, "Wavelength (nm)"

    def _redraw(self):
        self.ax.clear()
        series = self.saved[-8:] if self.saved else ([self.last] if self.last else [])
        if self.worker.state == "LIVE" and self.last is not None:
            series = [self.last]
        if not series:
            self.canvas.draw_idle()
            return
        colors = [ORANGE, BLUE, "#6D5F9C", "#3D9578", "#A76437", "#808F32"]
        for i, item in enumerate(series):
            y = np.asarray(item["data"])
            x = np.asarray(item["axis"])
            if y.ndim == 2:
                self.ax.imshow(y, aspect="auto", origin="lower", extent=(x[0], x[-1], 0, y.shape[0]))
                continue
            values, label = self._axis(x)
            name = item.get("step")
            legend = name.stem if name is not None else ("Live" if self.worker.state == "LIVE" else "Stitched")
            self.ax.plot(values, y, lw=0.9, color=colors[i % len(colors)], label=legend)
            self.ax.set_xlabel(label)
            self.ax.set_ylabel("Counts")
        if len(series) > 1:
            self.ax.legend(fontsize=8)
        self.ax.grid(alpha=0.16)
        self.figure.tight_layout()
        self.canvas.draw_idle()
        self.strip.Refresh()

    def _map(self):
        laser = (self.session.data.get("laser_nm") if self.session is not None else None)
        self.map_view.set_measurements(self.saved, laser,
                                       self.axis_choice.GetSelection() == 1)
        data = self.map_view.data
        self.map_fit_button.Enable(bool(data is not None and data.is_angular
                                        and data.intensity.shape[0] >= 2))

    def _propose_center(self, nm):
        self.center.ChangeValue(f"{nm:.3f}")

    def _connect(self, event):
        if self.worker.state != "DISCONNECTED":
            self._submit("disconnect")
            return
        simulation = self.mode.GetSelection() == 0
        sdk_root = None
        if not simulation:
            dialog = wx.TextEntryDialog(self, "Optional Andor SDK root folder (leave blank for discovery)",
                                        "Native connection")
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                sdk_root = dialog.GetValue().strip() or None
            finally:
                dialog.Destroy()
        self._submit("connect", simulation=simulation, sdk_root=sdk_root)

    def _session(self, event):
        with wx.DirDialog(self, "Choose a new or existing Krishnan session folder",
                          style=wx.DD_DEFAULT_STYLE | wx.DD_NEW_DIR_BUTTON) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            folder = Path(dialog.GetPath())
        try:
            if (folder / "session.json").exists():
                session = Session(folder)
            else:
                entries = {}
                for key, label, current in (
                    ("sample", "Sample name", self.sample.GetValue()),
                    ("laser", "Laser wavelength in nm (blank keeps wavelength axis only)",
                     self.laser_nm.GetValue()),
                    ("pol", "Polarization", self.polarization.GetValue()),
                ):
                    with wx.TextEntryDialog(self, label, "New Krishnan session",
                                            value=current) as entry:
                        if entry.ShowModal() != wx.ID_OK:
                            return
                        entries[key] = entry.GetValue().strip()
                laser_text = entries["laser"]
                session = Session(
                    folder, sample=entries["sample"],
                    laser_nm=float(laser_text) if laser_text else None,
                    polarization=entries["pol"])
            prior = session.data.get("device")
            if (prior is not None and self.inventory is not None
                    and any(s["state"] == "complete" for s in session.data["steps"])):
                if (prior["camera"].get("serial_number") !=
                        self.inventory["camera"].get("serial_number")
                        or prior["spectrograph"].get("serial") !=
                        self.inventory["optical"].get("serial")):
                    raise ValueError("Session was captured with different devices")
            self.session = session
            self.saved = []
            self.last = None
            self.sample.ChangeValue(session.data["sample"])
            self.laser_nm.ChangeValue("" if session.data["laser_nm"] is None
                                      else str(session.data["laser_nm"]))
            self.polarization.ChangeValue(session.data["polarization"])
            for field in (self.sample, self.laser_nm, self.polarization):
                field.SetEditable(False)
            self._submit("use_session", session)
            self._submit("load_completed")
            self._set_state(self.worker.state)
            self._event("steps", session.data["steps"])
        except Exception as exc:
            wx.MessageBox(str(exc), "Session error", wx.OK | wx.ICON_ERROR)

    def _numbers(self):
        exposure = float(self.exposure_s.GetValue())
        cycle = float(self.cycle_s.GetValue())
        accumulations = int(self.accumulations.GetValue())
        return exposure, cycle, accumulations

    def _single(self, event):
        if self.session is None:
            self._session(event)
            if self.session is None:
                return
        try:
            exposure, cycle, accumulations = self._numbers()
            step = Step(uuid.uuid4().hex, len(self.session.data["steps"]) + 1,
                        self.sample.GetValue().strip() or "single",
                        exposure_s=exposure, cycle_s=cycle, accumulations=accumulations)
            self.worker.stop_event.clear()
            self._submit("capture", step, self.readout, self.advanced)
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid capture", wx.OK | wx.ICON_ERROR)

    def _batch(self, event):
        try:
            exposure, cycle, _ = self._numbers()
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid timing", wx.OK | wx.ICON_ERROR)
            return
        offset = len(self.session.data["steps"]) if self.session is not None else 0
        with BatchDialog(self, exposure, cycle, index_offset=offset,
                         simulated=self.worker.simulated) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            result = dialog.result
        if self.session is None:
            self._session(event)
            if self.session is None:
                return
        self.worker.stop_event.clear()
        if result[0] == "scan":
            self._submit("scan", *result[1:])
        else:
            steps = result[1]
            offset = len(self.session.data["steps"])
            steps = [Step(s.id, s.index + offset, s.name, s.center_nm, s.grating,
                          s.angle_deg, s.exposure_s, s.cycle_s, s.accumulations)
                     for s in steps]
            self._submit("batch", steps, self.readout, self.advanced)

    def _resume(self, event):
        self.worker.stop_event.clear()
        self._submit("resume_batch", self.readout, self.advanced)

    def _live(self, event):
        try:
            exposure, cycle, _ = self._numbers()
            self.worker.stop_event.clear()
            self.live_future = self._submit("live", exposure, cycle, self.readout)
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid live setting", wx.OK | wx.ICON_ERROR)

    def _stop(self, event):
        threading.Thread(target=self.worker.stop, daemon=True).start()

    def _trigger(self, event):
        if self.worker.camera is None:
            return
        def send():
            try:
                self.worker.camera.software_trigger()
            except Exception as exc:
                self._post("error", f"Software trigger: {exc}")
        threading.Thread(target=send, daemon=True).start()

    def _snapshot(self, event):
        if self.live_future is None:
            return
        try:
            exposure, cycle, accumulations = self._numbers()
            step = Step(uuid.uuid4().hex, len(self.session.data["steps"]) + 1,
                        self.sample.GetValue().strip() or "snapshot",
                        exposure_s=exposure, cycle_s=cycle, accumulations=accumulations)
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid snapshot", wx.OK | wx.ICON_ERROR)
            return
        self._stop(event)
        def after_live(future):
            if self.worker.state == "IDLE":
                self.worker.stop_event.clear()
                self._submit("capture", step, self.readout, self.advanced)
        self.live_future.add_done_callback(after_live)

    def _advanced(self, event):
        with AdvancedDialog(self, self.inventory, self.readout, self.advanced) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.readout, self.advanced = dialog.readout, dialog.advanced

    def _set_grating(self, event):
        if self.grating.GetSelection() != wx.NOT_FOUND:
            self._submit("set_grating", self.grating.GetSelection() + 1)

    def _set_center(self, event):
        try:
            self._submit("set_wavelength", float(self.center.GetValue()))
        except ValueError:
            wx.MessageBox("Enter a wavelength in nm.", "Invalid center", wx.OK | wx.ICON_ERROR)

    def _calibration(self, event):
        self._submit("configure_calibration_geometry")

    def _cooler(self, on):
        try:
            setpoint = int(self.setpoint.GetValue()) if on else None
            self._submit("set_cooler", on, setpoint)
        except ValueError:
            wx.MessageBox("Enter an integer CCD setpoint.", "Invalid setpoint", wx.OK | wx.ICON_ERROR)

    def _shutter(self, event):
        self._submit("set_shutter", [2, 1, 0][self.shutter.GetSelection()])

    def _accessories_dialog(self, event):
        if self.inventory is None or self.worker.state != "IDLE":
            return
        present = self.inventory["accessories"]
        choices = (
            [("slit", index) for index in present["slits"]] +
            ([("spectrograph shutter", 0)] if present["shutter"] else []) +
            ([("filter", 0)] if present["filter"] else []) +
            [("flipper", index) for index in present["flippers"]]
        )
        if not choices:
            wx.MessageBox("No controllable spectrograph accessories were detected. "
                          "Inspect manual slits and optical path on the instrument.",
                          "Spectrograph accessories", wx.OK)
            return
        labels = [f"{kind} {index}" if index else kind for kind, index in choices]
        with wx.SingleChoiceDialog(self, "Installed accessory", "Spectrograph accessories",
                                   labels) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            kind, index = choices[dialog.GetSelection()]
        prompt = {
            "slit": "Slit width in µm (10–2500)",
            "spectrograph shutter": "Shutter mode (0–2; check optical path)",
            "filter": "Filter index (1–6)",
            "flipper": "Port (0 or 1)",
        }[kind]
        with wx.TextEntryDialog(self, prompt, f"Set {kind}") as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            try:
                value = float(dialog.GetValue())
            except ValueError:
                wx.MessageBox("Enter a numeric value.", "Accessory setting", wx.OK | wx.ICON_ERROR)
                return
        self._submit("set_accessory", kind, index, value)

    def _poll(self, event):
        if self.worker.state == "IDLE":
            self._submit("read_temperature")

    def _export(self, event):
        selected = set()
        index = self.steps_list.GetFirstSelected()
        while index != -1:
            if self.session and index < len(self.session.data["steps"]):
                selected.add(self.session.data["steps"][index]["id"])
            index = self.steps_list.GetNextSelected(index)
        self._submit("export", selected or None)

    def _angular_fit(self, event):
        self._angular_export_pending = True
        self._submit("export")

    def _cosmic_review(self, event):
        source = next((item for item in reversed(self.saved)
                       if item.get("step") is not None
                       and np.asarray(item["data"]).ndim == 1), None)
        if source is None:
            return
        try:
            candidates = cosmic_candidates(source["data"])
        except ValueError as exc:
            wx.MessageBox(str(exc), "Cosmic review", wx.OK | wx.ICON_ERROR)
            return
        if not candidates:
            wx.MessageBox("No isolated spikes were detected.", "Cosmic review", wx.OK)
            return
        labels = [
            f"Pixel {index}: {source['axis'][index]:.3f} nm, {source['data'][index]} counts"
            for index in candidates
        ]
        with wx.MultiChoiceDialog(
                self, "Select candidate pixels to replace in a derived copy. "
                      "The raw SIF/NPZ remains unchanged.",
                "Review cosmic-ray candidates", labels) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            selected = [candidates[i] for i in dialog.GetSelections()]
        if selected:
            self._submit("save_cosmic_correction", source["step"].id,
                         source["axis"], source["data"], selected)

    def _close(self, event):
        self.timer.Stop()
        self.camera_panel.shutdown()
        self.Hide()
        threading.Thread(target=self._finish_close, daemon=True).start()

    def _finish_close(self):
        try:
            self.worker.close()
        finally:
            wx.CallAfter(self.Destroy)


def main():
    app = wx.App(False)
    MainFrame()
    app.MainLoop()
