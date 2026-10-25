"""Map above linked angular/row and spectral slices."""

from __future__ import annotations

import numpy as np
import wx
from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg, NavigationToolbar2WxAgg
from matplotlib.figure import Figure

from .bootstrap import prepare_paths
from .map_data import MapData, build_map, map_slices

prepare_paths()
from data_structure import raman_shift_cm1
from plotting import AngularPlotter, RamanPlotter2d, SlicePlotter
from row_axis import RowAxisSpec


class MapPanel(wx.Panel):
    """Large map with click-selected row and spectral slices."""

    def __init__(self, parent):
        super().__init__(parent)
        self.data: MapData | None = None
        self.selection: tuple[int, int] | None = None
        self.spectral_x = None
        self.spectral_label = "Wavelength (nm)"
        self.map_plotter = self.row_plotter = self.spectral_plotter = None

        layout = wx.BoxSizer(wx.VERTICAL)
        controls = wx.BoxSizer(wx.VERTICAL)
        first_row = wx.BoxSizer(wx.HORIZONTAL)
        second_row = wx.BoxSizer(wx.HORIZONTAL)
        self.polar = wx.CheckBox(self, label="Polar angle slice")
        self.polar.SetValue(True)
        self.x_bin = wx.SpinCtrl(self, min=1, max=999, initial=1, size=(65, -1))
        self.y_bin = wx.SpinCtrl(self, min=1, max=999, initial=1, size=(65, -1))
        self.cmap = wx.Choice(self, choices=["OrRd", "viridis", "magma", "gray"])
        self.cmap.SetSelection(0)
        self.log = wx.CheckBox(self, label="Log colour")
        self.highlight = wx.Choice(self, choices=["Click highlight", "No highlight"])
        self.highlight.SetSelection(0)
        self.low = wx.SpinCtrl(self, min=0, max=49, initial=0, size=(65, -1))
        self.high = wx.SpinCtrl(self, min=51, max=100, initial=100, size=(65, -1))
        for label, control in (("", self.polar), ("λ bin", self.x_bin),
                               ("Row bin", self.y_bin), ("Colour", self.cmap)):
            if label:
                first_row.Add(wx.StaticText(self, label=label), 0,
                             wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 3)
            first_row.Add(control, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 9)
        for label, control in (("", self.highlight), ("", self.log),
                               ("Low %", self.low), ("High %", self.high)):
            if label:
                second_row.Add(wx.StaticText(self, label=label), 0,
                               wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 3)
            second_row.Add(control, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 9)
        controls.Add(first_row, 0, wx.BOTTOM, 4)
        controls.Add(second_row, 0)
        layout.Add(controls, 0, wx.EXPAND | wx.ALL, 5)
        # wx uses the canvas' initial pixel dimensions as its minimum size.
        # Keep that minimum below the narrow measurement sidebar width.
        self.figure = Figure(figsize=(5, 5), facecolor="white")
        self.canvas = FigureCanvasWxAgg(self, -1, self.figure)
        self.canvas.SetMinSize((240, 220))
        self.toolbar = NavigationToolbar2WxAgg(self.canvas)
        self.toolbar.Realize()
        layout.Add(self.toolbar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        layout.Add(self.canvas, 1, wx.EXPAND | wx.ALL, 5)
        self.message = wx.StaticText(self, label="Capture at least two spectra on the same calibrated axis to form a map.")
        layout.Add(self.message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(layout)

        self.canvas.mpl_connect("button_press_event", self._clicked)
        for control in (self.polar, self.x_bin, self.y_bin, self.cmap, self.log,
                        self.highlight,
                        self.low, self.high):
            control.Bind(wx.EVT_CHECKBOX if isinstance(control, wx.CheckBox)
                         else wx.EVT_CHOICE if isinstance(control, wx.Choice)
                         else wx.EVT_SPINCTRL, self._settings_changed)
        self._empty()

    def _empty(self):
        self.figure.clear()
        grid = self._grid()
        panels = ((self.figure.add_subplot(grid[0, :2]), "2D Raman map"),
                  (self.figure.add_subplot(grid[1, 0]), "Polar / row slice"),
                  (self.figure.add_subplot(grid[1, 1]), "Spectral slice"))
        for ax, title in panels:
            ax.set_title(title)
            ax.set_xticks([])
            ax.set_yticks([])
            ax.text(0.5, 0.5, "Awaiting captures", ha="center", va="center",
                    transform=ax.transAxes, color="#60717D")
        self.canvas.draw_idle()

    def _grid(self):
        return self.figure.add_gridspec(
            2, 3, height_ratios=(2.35, 1), width_ratios=(1, 1, 0.055),
            left=0.12, right=0.89, bottom=0.11, top=0.94,
            wspace=0.32, hspace=0.53)

    def set_measurements(self, saved: list[dict], laser_nm: float | None,
                         raman_axis: bool = False) -> None:
        previous = self.data
        self.data = build_map(saved)
        data = self.data
        if data is None or data.intensity.shape[0] < 2:
            self.selection = None
            self.message.SetLabel("Capture at least two spectra on the same calibrated axis to form a map.")
            self._empty()
            return
        if raman_axis and laser_nm is not None and np.isfinite(laser_nm) and laser_nm > 0:
            self.spectral_x = raman_shift_cm1(laser_nm, data.wavelength_nm)
            self.spectral_label = "Raman shift (cm⁻¹)"
        else:
            self.spectral_x = data.wavelength_nm
            self.spectral_label = "Wavelength (nm)"
        if previous is None or self.selection is None:
            self.selection = (data.intensity.shape[1] // 2, data.intensity.shape[0] - 1)
        else:
            ix, iy = self.selection
            self.selection = (min(ix, data.intensity.shape[1] - 1),
                              min(iy, data.intensity.shape[0] - 1))
        self.polar.Enable(data.is_angular)
        self._render()

    def _settings_changed(self, event):
        if self.data is not None and self.data.intensity.shape[0] >= 2:
            self._render()

    def _render(self):
        data = self.data
        if data is None:
            return
        self.figure.clear()
        grid = self._grid()
        map_ax = self.figure.add_subplot(grid[0, :2])
        colorbar_ax = self.figure.add_subplot(grid[0, 2])
        row_ax = self.figure.add_subplot(grid[1, 0], projection=(
            "polar" if data.is_angular and self.polar.GetValue() else None))
        spectral_ax = self.figure.add_subplot(grid[1, 1])
        self.map_plotter = RamanPlotter2d(map_ax)
        self.row_plotter = AngularPlotter(row_ax)
        self.spectral_plotter = SlicePlotter(spectral_ax)
        row_axis = (RowAxisSpec(kind="angle", unit="deg", label="Angle (°)",
                                major_step=None) if data.is_angular else
                    RowAxisSpec(kind="index", unit="", label="Capture",
                                major_step=None,
                                tick_positions=data.rows if len(data.rows) <= 12 else None,
                                tick_labels=data.row_labels if len(data.rows) <= 12 else None))
        self.map_plotter.render(
            self.spectral_x, data.rows, data.intensity,
            title="Raman map", xlabel=self.spectral_label,
            row_axis=row_axis, cmap=self.cmap.GetStringSelection(),
            color_scale="log" if self.log.GetValue() else "linear",
            colorbar_ax=colorbar_ax)
        self.map_plotter.set_highlight_mode(
            "click" if self.highlight.GetSelection() == 0 else "none")
        self.map_plotter.set_contrast(self.low.GetValue(), self.high.GetValue())
        self._select(*self.selection)

    def _select(self, ix: int, iy: int):
        data = self.data
        if data is None or self.map_plotter is None:
            return
        self.selection = (ix, iy)
        x = float(self.spectral_x[ix])
        row = float(data.rows[iy])
        row_profile, spectrum = map_slices(
            data.intensity, ix, iy, self.x_bin.GetValue(), self.y_bin.GetValue())
        show_highlight = self.highlight.GetSelection() == 0
        self.map_plotter.set_highlight(x, row, visible=show_highlight)
        self.row_plotter.render(
            data.rows, row_profile,
            mode="polar" if data.is_angular and self.polar.GetValue() else "cartesian",
            title=f"Row slice at {x:.2f}",
            xlabel="Angle (°)" if data.is_angular else "Capture")
        if not data.is_angular and len(data.rows) <= 12:
            self.row_plotter.ax.set_xticks(data.rows)
        self.row_plotter.set_highlight(row, visible=show_highlight)
        self.spectral_plotter.render(
            self.spectral_x, spectrum,
            title=f"Spectrum at {data.row_labels[iy]}",
            xlabel=self.spectral_label, ylabel="Counts")
        self.spectral_plotter.set_highlight(x, visible=show_highlight)
        self.message.SetLabel(
            f"Selected {x:.3f} {self.spectral_label.split('(')[-1].rstrip(')')} · "
            f"{data.row_labels[iy]} · {data.intensity[iy, ix]:.5g} counts. "
            "Click the map to update both slices; toolbar zoom and pan are available.")
        self.canvas.draw_idle()

    def _clicked(self, event):
        if (self.map_plotter is None or event.inaxes is not self.map_plotter.ax
                or event.button != 1 or self.toolbar.mode):
            return
        indices = self.map_plotter.get_index_at(event.xdata, event.ydata)
        if indices is not None:
            self._select(*indices)
