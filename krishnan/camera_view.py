"""Concurrent sample-camera preview kept separate from the spectroscopy SDK."""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Callable

import wx
from PIL import Image

from .aux_camera import AuxiliaryCamera, CameraFrame, CameraSettings, SimulatedAuxiliaryCamera
from .aravis_camera import AravisAuxiliaryCamera


class CameraDisplay(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.bitmap = None
        self.Bind(wx.EVT_PAINT, self._paint)

    def show_frame(self, frame: CameraFrame):
        bitmap = wx.Bitmap.FromBuffer(frame.rgb.shape[1], frame.rgb.shape[0],
                                      frame.rgb.tobytes())
        if bitmap.GetWidth() > 1024:
            image = bitmap.ConvertToImage()
            width = 1024
            height = max(1, round(image.GetHeight() * width / image.GetWidth()))
            image = image.Scale(width, height, wx.IMAGE_QUALITY_NORMAL)
            bitmap = wx.Bitmap(image)
        self.bitmap = bitmap
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
                                                     wx.IMAGE_QUALITY_NORMAL)
        dc.DrawBitmap(wx.Bitmap(scaled), (width - target_w) // 2,
                      (height - target_h) // 2)


class CameraPanel(wx.Panel):
    def __init__(self, parent, session_folder,
                 source_factory: Callable[[], AuxiliaryCamera] | None = None):
        super().__init__(parent)
        self.session_folder = session_folder
        self.source_factory = source_factory
        self.settings = CameraSettings()
        self.config = wx.Config("Krishnan")
        saved_directory = self.config.Read("sample_camera_save_directory", "")
        self.save_directory = Path(saved_directory).expanduser() if saved_directory else None
        self.frame: CameraFrame | None = None
        self._last_full_frame: CameraFrame | None = None
        self._frame_lock = threading.Lock()
        self._pending_frame = False
        self._latest_frame: CameraFrame | None = None
        self._pending_capture_path: Path | None = None
        self._capture_busy = False
        self._last_error: str | None = None
        self._source_name = "Aravis IMC-720G"
        try:
            saved_fps = int(self.config.Read("aravis_preview_fps", "14"))
        except ValueError:
            saved_fps = 14
        self.preview_fps = min(16, max(1, saved_fps))
        self._frame_times: deque[float] = deque()
        self._fps_label_at = 0.0
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sample-camera")
        self.running = False
        self.closed = False
        self._detach_on_shutdown = False
        self.aravis = AravisAuxiliaryCamera()
        layout = wx.BoxSizer(wx.VERTICAL)
        controls = wx.BoxSizer(wx.HORIZONTAL)
        self.source_choice = wx.Choice(self, choices=["Aravis IMC-720G", "Simulator"])
        saved_source = self.config.Read("sample_camera_source", "aravis")
        self.source_choice.SetSelection(1 if saved_source == "simulator" else 0)
        controls.Add(self.source_choice, 0, wx.RIGHT, 8)
        self.play_button = wx.Button(self, label="Play")
        self.stop_button = wx.Button(self, label="Stop")
        self.capture_button = wx.Button(self, label="Capture")
        self.configure_button = wx.Button(self, label="Configure…")
        for button in (self.play_button, self.stop_button, self.capture_button,
                       self.configure_button):
            controls.Add(button, 0, wx.RIGHT, 5)
        layout.Add(controls, 0, wx.ALL, 7)
        rate_row = wx.BoxSizer(wx.HORIZONTAL)
        rate_row.Add(wx.StaticText(self, label="Preview cap (FPS)"),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 7)
        self.fps_control = wx.SpinCtrl(self, min=1, max=16,
                                       initial=self.preview_fps, size=(65, -1))
        rate_row.Add(self.fps_control, 0)
        layout.Add(rate_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        self.display = CameraDisplay(self)
        layout.Add(self.display, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 7)
        initial_status = ("Simulator ready." if self.source_choice.GetSelection() == 1 else
                          "Aravis ready; Play may request Windows administrator approval.")
        self.status = wx.StaticText(self, label=initial_status)
        layout.Add(self.status, 0, wx.ALL, 7)
        self.SetSizer(layout)
        self.play_button.Bind(wx.EVT_BUTTON, self.play)
        self.stop_button.Bind(wx.EVT_BUTTON, self.stop)
        self.capture_button.Bind(wx.EVT_BUTTON, self.capture)
        self.configure_button.Bind(wx.EVT_BUTTON, self.configure)
        self.source_choice.Bind(wx.EVT_CHOICE, self._source_changed)
        self.fps_control.Bind(wx.EVT_SPINCTRL, self._fps_changed)
        self.fps_control.Bind(wx.EVT_TEXT, self._fps_changed)
        self._buttons()

    def _buttons(self):
        self.play_button.Enable(not self.running)
        self.stop_button.Enable(self.running)
        full_frame_available = (self._source_name != "Aravis IMC-720G" or
                                self.running or self._last_full_frame is not None)
        self.capture_button.Enable(self.frame is not None and full_frame_available
                                   and not self._capture_busy)
        self.configure_button.Enable(not self.running)
        self.source_choice.Enable(not self.running)

    def _source_changed(self, _event):
        selected = "simulator" if self.source_choice.GetSelection() == 1 else "aravis"
        self.config.Write("sample_camera_source", selected)
        self.config.Flush()
        self.status.SetLabel("Simulator ready." if selected == "simulator" else
                             "Aravis ready; Play may request Windows administrator approval.")

    def _fps_changed(self, _event):
        value = self.fps_control.GetValue()
        if not 1 <= value <= 16 or value == self.preview_fps:
            return
        self.preview_fps = value
        self.config.Write("aravis_preview_fps", str(value))
        self.config.Flush()

    def play(self, event=None):
        if self.running or self.closed:
            return
        self.stop_event.clear()
        self._last_error = None
        self.frame = None
        self._last_full_frame = None
        with self._frame_lock:
            self._latest_frame = None
            self._pending_frame = False
        self._frame_times.clear()
        self._fps_label_at = 0.0
        self._source_name = self.source_choice.GetStringSelection()
        self.status.SetLabel(f"Connecting {self._source_name}…")
        self.running = True
        self._buttons()
        self.pool.submit(self._stream)

    def _stream(self):
        source = None
        try:
            source = (self.source_factory() if self.source_factory is not None else
                      SimulatedAuxiliaryCamera() if self._source_name == "Simulator" else
                      self.aravis)
            source.open()
            if self.stop_event.is_set():
                return
            source.configure(self.settings)
            source.start()
            while not self.stop_event.is_set():
                if hasattr(source, "preview_fps"):
                    source.preview_fps = self.preview_fps
                frame = source.read_frame(timeout_s=0.2)
                with self._frame_lock:
                    self._latest_frame = frame
                    needs_delivery = not self._pending_frame
                    if needs_delivery:
                        self._pending_frame = True
                if needs_delivery:
                    wx.CallAfter(self._deliver_frame)
                if isinstance(source, AravisAuxiliaryCamera):
                    self._save_pending_aravis_capture(source)
        except Exception as exc:
            self._last_error = f"Camera error: {exc}"
            wx.CallAfter(self._set_status, self._last_error)
        finally:
            if source is not None:
                try:
                    if isinstance(source, AravisAuxiliaryCamera):
                        self._save_pending_aravis_capture(source)
                    if self._detach_on_shutdown and isinstance(source, AravisAuxiliaryCamera):
                        source.detach()
                    else:
                        source.close()
                except Exception as exc:
                    if self._last_error is None:
                        self._last_error = f"Camera cleanup error: {exc}"
                finally:
                    if isinstance(source, AravisAuxiliaryCamera):
                        self._last_full_frame = source.last_full_frame
            with self._frame_lock:
                pending = self._pending_capture_path
                self._pending_capture_path = None
            if pending is not None:
                wx.CallAfter(self._capture_done, None,
                             "Camera stopped before the full-resolution capture was available")
            wx.CallAfter(self._stopped)

    def _save_pending_aravis_capture(self, source: AravisAuxiliaryCamera) -> None:
        with self._frame_lock:
            path = self._pending_capture_path
            self._pending_capture_path = None
        if path is None:
            return
        try:
            frame = source.capture_frame()
            self._last_full_frame = frame
            Image.fromarray(frame.rgb).save(path, format="TIFF", compression="raw")
        except Exception as exc:
            wx.CallAfter(self._capture_done, None, str(exc))
        else:
            wx.CallAfter(self._capture_done, path, None)

    def _capture_done(self, path: Path | None, error: str | None) -> None:
        self._capture_busy = False
        if self.closed:
            return
        self._buttons()
        if error is not None:
            wx.MessageBox(error, "Camera TIFF save error", wx.OK | wx.ICON_ERROR)
        elif path is not None:
            self.status.SetLabel(f"Saved {path}")

    def _deliver_frame(self):
        with self._frame_lock:
            frame = self._latest_frame
            self._latest_frame = None
            self._pending_frame = False
        if frame is not None:
            self._show_frame(frame)

    def _show_frame(self, frame):
        if self.closed or self.stop_event.is_set():
            return
        first_frame = self.frame is None
        self.frame = frame
        self.display.show_frame(frame)
        now = time.perf_counter()
        self._frame_times.append(now)
        while self._frame_times and now - self._frame_times[0] > 2.0:
            self._frame_times.popleft()
        if now - self._fps_label_at >= 0.25:
            if len(self._frame_times) >= 2:
                elapsed = now - self._frame_times[0]
                measured_fps = (len(self._frame_times) - 1) / elapsed if elapsed > 0 else 0.0
                self.status.SetLabel(f"{self._source_name} · {measured_fps:.1f} FPS "
                                     f"(cap {self.preview_fps})")
            else:
                self.status.SetLabel(f"{self._source_name} · measuring FPS "
                                     f"(cap {self.preview_fps})")
            self._fps_label_at = now
        if first_frame:
            self._buttons()

    def _set_status(self, text):
        if not self.closed:
            self.status.SetLabel(text)

    def _stopped(self):
        if not self.closed:
            self.running = False
            self._buttons()
            available = (self.frame is not None and
                         (self._source_name != "Aravis IMC-720G" or
                          self._last_full_frame is not None))
            self.status.SetLabel(self._last_error or
                                 ("Sample camera stopped. Last frame is available for Capture."
                                  if available else "Sample camera stopped."))

    def stop(self, event=None):
        self.stop_event.set()

    def configure(self, event=None):
        with wx.Dialog(self, title="Sample camera", size=(620, 370),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER) as dialog:
            box = wx.BoxSizer(wx.VERTICAL)
            simulated = self.source_choice.GetSelection() == 1
            box.Add(wx.StaticText(dialog, label=f"Source: {self.source_choice.GetStringSelection()}"),
                    0, wx.ALL, 12)
            box.Add(wx.StaticText(dialog, label=(
                "Aravis preview uses the camera's current exposure and gain.\n"
                "Acquisition uses 14 FPS, stable packet pacing and frozen white balance;\n"
                "original camera and Ethernet settings return when the camera closes."
                if not simulated else "Simulator exposure and gain can be changed below.")),
                0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            rows = wx.FlexGridSizer(0, 2, 8, 8)
            rows.AddGrowableCol(1)
            exposure = wx.TextCtrl(dialog, value=f"{self.settings.exposure_ms:g}")
            gain = wx.TextCtrl(dialog, value=f"{self.settings.gain:g}")
            exposure.Enable(simulated)
            gain.Enable(simulated)
            for label, control in (("Exposure (ms)", exposure), ("Gain (×)", gain)):
                rows.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                rows.Add(control, 1, wx.EXPAND)
            box.Add(rows, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
            box.Add(wx.StaticText(dialog, label="Save directory for camera TIFFs"),
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
        if self.frame is None or self._capture_busy:
            return
        default_name = datetime.now().strftime("camera_%Y%m%d_%H%M%S_%f.tif")
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
            with wx.FileDialog(self, "Save camera frame", wildcard="TIFF image (*.tif)|*.tif",
                               defaultFile=default_name, style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = Path(dialog.GetPath())
                if path.suffix.lower() not in (".tif", ".tiff"):
                    path = path.with_suffix(".tif")
        if self._source_name == "Aravis IMC-720G" and self.running:
            self._capture_busy = True
            with self._frame_lock:
                self._pending_capture_path = path
            self._buttons()
            self.status.SetLabel("Saving full-resolution Aravis TIFF…")
            return
        frame = (self._last_full_frame if self._source_name == "Aravis IMC-720G"
                 else self.frame)
        if frame is None:
            wx.MessageBox("No full-resolution Aravis frame is available",
                          "Camera TIFF save error", wx.OK | wx.ICON_ERROR)
            return
        try:
            Image.fromarray(frame.rgb).save(path, format="TIFF", compression="raw")
        except (OSError, ValueError) as exc:
            wx.MessageBox(str(exc), "Camera TIFF save error", wx.OK | wx.ICON_ERROR)
            return
        self.status.SetLabel(f"Saved {path}")

    def shutdown(self):
        self._detach_on_shutdown = self.running and not self.stop_event.is_set()
        self.closed = True
        self.stop_event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.aravis.detach()

    def connect_backend(self):
        """Attach an idle Aravis service without starting camera acquisition."""
        if self.running:
            return
        def attach():
            self.aravis.open()
            if self.closed:
                self.aravis.detach()
        return self.pool.submit(attach)
