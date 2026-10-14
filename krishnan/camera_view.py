"""Concurrent sample-camera preview kept separate from the spectroscopy SDK."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import threading
from typing import Callable

import wx

from .aux_camera import AuxiliaryCamera, CameraFrame, CameraSettings, SimulatedAuxiliaryCamera


class CameraDisplay(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.bitmap = None
        self.Bind(wx.EVT_PAINT, self._paint)

    def show_frame(self, frame: CameraFrame):
        image = wx.Image(frame.rgb.shape[1], frame.rgb.shape[0],
                         frame.rgb.tobytes())
        self.bitmap = wx.Bitmap(image)
        self.Refresh(False)

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour("#20252A")))
        dc.Clear()
        if self.bitmap is None:
            dc.SetTextForeground(wx.Colour("#C5CDD2"))
            dc.DrawText("Press Play to preview the sample camera", 18, 18)
            return
        width, height = self.GetClientSize()
        iw, ih = self.bitmap.GetSize()
        factor = min(width / iw, height / ih)
        target_w, target_h = max(1, int(iw * factor)), max(1, int(ih * factor))
        scaled = self.bitmap.ConvertToImage().Scale(target_w, target_h,
                                                     wx.IMAGE_QUALITY_HIGH)
        dc.DrawBitmap(wx.Bitmap(scaled), (width - target_w) // 2,
                      (height - target_h) // 2)


class CameraPanel(wx.Panel):
    def __init__(self, parent, session_folder,
                 source_factory: Callable[[], AuxiliaryCamera] = SimulatedAuxiliaryCamera):
        super().__init__(parent)
        self.session_folder = session_folder
        self.source_factory = source_factory
        self.settings = CameraSettings()
        self.config = wx.Config("Krishnan")
        saved_directory = self.config.Read("sample_camera_save_directory", "")
        self.save_directory = Path(saved_directory).expanduser() if saved_directory else None
        self.frame: CameraFrame | None = None
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sample-camera")
        self.running = False
        self.closed = False
        layout = wx.BoxSizer(wx.VERTICAL)
        controls = wx.BoxSizer(wx.HORIZONTAL)
        self.play_button = wx.Button(self, label="Play")
        self.stop_button = wx.Button(self, label="Stop")
        self.capture_button = wx.Button(self, label="Capture")
        self.configure_button = wx.Button(self, label="Configure…")
        for button in (self.play_button, self.stop_button, self.capture_button,
                       self.configure_button):
            controls.Add(button, 0, wx.RIGHT, 5)
        layout.Add(controls, 0, wx.ALL, 7)
        self.display = CameraDisplay(self)
        layout.Add(self.display, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 7)
        self.status = wx.StaticText(self, label="Sample camera: simulator ready. LAN adapter awaits device model/SDK.")
        layout.Add(self.status, 0, wx.ALL, 7)
        self.SetSizer(layout)
        self.play_button.Bind(wx.EVT_BUTTON, self.play)
        self.stop_button.Bind(wx.EVT_BUTTON, self.stop)
        self.capture_button.Bind(wx.EVT_BUTTON, self.capture)
        self.configure_button.Bind(wx.EVT_BUTTON, self.configure)
        self._buttons()

    def _buttons(self):
        self.play_button.Enable(not self.running)
        self.stop_button.Enable(self.running)
        self.capture_button.Enable(self.frame is not None)
        self.configure_button.Enable(not self.running)

    def play(self, event=None):
        if self.running or self.closed:
            return
        self.stop_event.clear()
        self.running = True
        self._buttons()
        self.pool.submit(self._stream)

    def _stream(self):
        source = self.source_factory()
        try:
            source.open()
            source.configure(self.settings)
            source.start()
            while not self.stop_event.is_set():
                frame = source.read_frame(timeout_s=0.2)
                wx.CallAfter(self._show_frame, frame)
        except Exception as exc:
            wx.CallAfter(self._set_status, f"Camera error: {exc}")
        finally:
            source.stop()
            source.close()
            wx.CallAfter(self._stopped)

    def _show_frame(self, frame):
        if self.closed or self.stop_event.is_set():
            return
        self.frame = frame
        self.display.show_frame(frame)
        self.status.SetLabel(f"Simulator · frame {frame.sequence} · {self.settings.exposure_ms:g} ms")
        self._buttons()

    def _set_status(self, text):
        if not self.closed:
            self.status.SetLabel(text)

    def _stopped(self):
        if not self.closed:
            self.running = False
            self._buttons()
            self.status.SetLabel("Sample camera stopped. Last frame is available for Capture.")

    def stop(self, event=None):
        self.stop_event.set()

    def configure(self, event=None):
        with wx.Dialog(self, title="Sample camera", size=(620, 370),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER) as dialog:
            box = wx.BoxSizer(wx.VERTICAL)
            box.Add(wx.StaticText(dialog, label="Source: simulated sample camera"),
                    0, wx.ALL, 12)
            box.Add(wx.StaticText(dialog, label=(
                "LAN camera support is reserved until its manufacturer, model,\n"
                "transport protocol, and SDK/GenTL producer are known.")),
                0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            rows = wx.FlexGridSizer(0, 2, 8, 8)
            rows.AddGrowableCol(1)
            exposure = wx.TextCtrl(dialog, value=f"{self.settings.exposure_ms:g}")
            gain = wx.TextCtrl(dialog, value=f"{self.settings.gain:g}")
            for label, control in (("Exposure (ms)", exposure), ("Gain (×)", gain)):
                rows.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                rows.Add(control, 1, wx.EXPAND)
            box.Add(rows, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
            box.Add(wx.StaticText(dialog, label="Save directory for camera PNGs"),
                    0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
            directory_row = wx.BoxSizer(wx.HORIZONTAL)
            session = self.session_folder()
            default_directory = (self.save_directory or
                                 (Path(session) / "camera" if session is not None else
                                  Path.home() / "Pictures" / "Krishnan"))
            save_directory = wx.TextCtrl(dialog, value=str(default_directory))
            directory_row.Add(save_directory, 1, wx.EXPAND | wx.RIGHT, 8)
            browse = wx.Button(dialog, label="Browse…")
            directory_row.Add(browse, 0)
            box.Add(directory_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

            def choose_directory(_event):
                current = Path(save_directory.GetValue()).expanduser()
                starting = current
                while not starting.is_dir() and starting != starting.parent:
                    starting = starting.parent
                with wx.DirDialog(dialog, "Choose camera image directory",
                                  defaultPath=str(starting),
                                  style=wx.DD_DEFAULT_STYLE | wx.DD_NEW_DIR_BUTTON) as picker:
                    if picker.ShowModal() == wx.ID_OK:
                        save_directory.ChangeValue(picker.GetPath())

            browse.Bind(wx.EVT_BUTTON, choose_directory)
            box.Add(dialog.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL),
                    0, wx.EXPAND | wx.ALL, 12)
            dialog.SetSizer(box)
            dialog.SetMinSize((520, 340))
            dialog.Layout()

            def apply_settings(_event):
                try:
                    settings = CameraSettings(float(exposure.GetValue()),
                                              float(gain.GetValue()))
                    selected = save_directory.GetValue().strip()
                    if not selected:
                        raise ValueError("Choose a camera save directory")
                    destination = Path(selected).expanduser().resolve()
                    destination.mkdir(parents=True, exist_ok=True)
                    if not destination.is_dir():
                        raise ValueError("Camera save path is not a directory")
                    self.settings = settings
                    self.save_directory = destination
                    self.config.Write("sample_camera_save_directory", str(destination))
                    self.config.Flush()
                    self.status.SetLabel(f"Camera captures: {destination}")
                    dialog.EndModal(wx.ID_OK)
                except (ValueError, OSError) as exc:
                    wx.MessageBox(str(exc), "Invalid camera settings",
                                  wx.OK | wx.ICON_ERROR)

            dialog.Bind(wx.EVT_BUTTON, apply_settings, id=wx.ID_OK)
            dialog.ShowModal()

    def capture(self, event=None):
        if self.frame is None:
            return
        default_name = datetime.now().strftime("camera_%Y%m%d_%H%M%S_%f.png")
        folder = self.session_folder()
        if self.save_directory is not None or folder is not None:
            destination = (self.save_directory if self.save_directory is not None
                           else Path(folder) / "camera")
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                wx.MessageBox(str(exc), "Camera save error", wx.OK | wx.ICON_ERROR)
                return
            path = destination / default_name
        else:
            with wx.FileDialog(self, "Save camera frame", wildcard="PNG image (*.png)|*.png",
                               defaultFile=default_name, style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = Path(dialog.GetPath())
        image = wx.Image(self.frame.rgb.shape[1], self.frame.rgb.shape[0],
                         self.frame.rgb.tobytes())
        if not image.SaveFile(str(path), wx.BITMAP_TYPE_PNG):
            wx.MessageBox("Could not save camera image.", "Capture", wx.OK | wx.ICON_ERROR)
            return
        self.status.SetLabel(f"Saved {path}")

    def shutdown(self):
        self.closed = True
        self.stop_event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
