"""Backend IPC state, safe controls and a combined persistent log history."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import time

import wx
from wx.lib.scrolledpanel import ScrolledPanel

from .backend_console import monitor_request, log_directory
from .process_worker import _runtime_dir
from .aravis_camera import _runtime_dir as aravis_runtime

RUNTIMES = {"Andor": (_runtime_dir(), "backend.json"),
            "Aravis": (aravis_runtime(), "service.json"),
            "Thorlabs": (_runtime_dir("motors"), "backend.json")}


def log_history(selected="All"):
    records = []
    stamp = re.compile(r"^\[(\d{4}-\d\d-\d\d [\d:]+)")
    for backend, (runtime, _) in RUNTIMES.items():
        if selected not in ("All", backend):
            continue
        paths = sorted(log_directory(backend).glob("*.log"))[-8:]
        paths += sorted((runtime / "logs").glob("*.log"))[-8:]
        old = runtime / ("service.log" if backend == "Aravis" else "backend.log")
        if old.exists():
            paths.insert(0, old)
        for path in paths:
            try:
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 98304))
                    chunk = stream.read().decode("utf-8", errors="replace")
                lines = chunk.splitlines()[1:] if size > 98304 else chunk.splitlines()
                key = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
                for i, line in enumerate(lines):
                    found = stamp.match(line)
                    if found:
                        key = found.group(1)
                    records.append((key, path.name, i, f"[{backend}] {line}"))
            except OSError:
                continue
    return "\n".join(x[3] for x in sorted(records)[-3000:])


class ConsolePanel(wx.Panel):
    def __init__(self, parent, reconnect):
        super().__init__(parent)
        self.reconnect = reconnect
        self.closed = False
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="backend-console")
        self.pending = set()
        self.last = {}
        self.last_ok = {}
        self.log_pending = False
        self.last_logs = 0.0
        self.log_text = ""
        self.controls = {}
        root = wx.BoxSizer(wx.VERTICAL)
        self.cards = cards = ScrolledPanel(self, size=(-1, 305))
        card_sizer = wx.BoxSizer(wx.VERTICAL)
        for name in RUNTIMES:
            group = wx.StaticBoxSizer(wx.VERTICAL, cards, name)
            box = group.GetStaticBox()
            label = wx.StaticText(box, label="Checking backend IPC…")
            label.SetMinSize((1, -1))
            group.Add(label, 0, wx.ALL | wx.EXPAND, 6)
            row = wx.BoxSizer(wx.HORIZONTAL)
            buttons = {}
            for title, action in (("Stop safely", "stop"), ("Disconnect", "disconnect"),
                                  ("Close backend", "close"), ("Reconnect", "reconnect")):
                button = wx.Button(box, label=title)
                button.Bind(wx.EVT_BUTTON, lambda _e, n=name, a=action: self.action(n, a))
                row.Add(button, 0, wx.RIGHT, 4)
                buttons[action] = button
            group.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
            card_sizer.Add(group, 0, wx.EXPAND | wx.ALL, 5)
            self.controls[name] = (label, buttons)
        cards.SetSizer(card_sizer)
        cards.SetupScrolling(scroll_x=False)
        root.Add(cards, 0, wx.EXPAND)
        self.message = wx.StaticText(self, label="State comes from backend IPC. Logs below are history.",
                                      style=wx.ST_ELLIPSIZE_END)
        self.message.SetMinSize((1, -1))
        root.Add(self.message, 0, wx.ALL | wx.EXPAND, 6)
        options = wx.BoxSizer(wx.HORIZONTAL)
        options.Add(wx.StaticText(self, label="Log filter"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.filter = wx.Choice(self, choices=["All", *RUNTIMES])
        self.filter.SetSelection(0)
        self.filter.Bind(wx.EVT_CHOICE, lambda _e: self.refresh_logs(force=True))
        options.Add(self.filter, 0, wx.RIGHT, 8)
        self.follow = wx.CheckBox(self, label="Follow latest")
        self.follow.SetValue(True)
        options.Add(self.follow, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        folder = wx.Button(self, label="Log folder")
        folder.Bind(wx.EVT_BUTTON, self.open_folder)
        options.Add(folder, 0)
        root.Add(options, 0, wx.ALL, 6)
        self.logs = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.logs.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        root.Add(self.logs, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)
        self.SetSizer(root)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.poll, self.timer)
        self.timer.Start(1000)
        wx.CallAfter(self.poll)

    def poll(self, _event=None):
        if self.closed:
            return
        for name, (runtime, info) in RUNTIMES.items():
            if name in self.pending:
                continue
            self.pending.add(name)
            future = self.pool.submit(monitor_request, runtime, info_name=info)
            def done(result, n=name):
                try:
                    payload = result.result()
                except Exception as exc:
                    payload = {"available": False, "reason": str(exc)}
                wx.CallAfter(self.display, n, payload)
            future.add_done_callback(done)
        self.refresh_logs()

    def display(self, name, payload):
        if self.closed:
            return
        self.pending.discard(name)
        self.last[name] = payload
        label, buttons = self.controls[name]
        if payload.get("available"):
            self.last_ok[name] = payload
            age = time.time() - payload.get("updated", 0)
            stale = " · STALE" if age > 3 else ""
            text = f"PID {payload['pid']} · IPC connected{stale} · {payload.get('device_state', 'Unknown')}"
            text += f"\n{payload.get('phase', 'Unknown')} · cleanup: {payload.get('cleanup', 'None')}"
            if payload.get("deadline") is not None:
                remaining = max(0, int(payload["deadline"] - time.time()))
                text += f" · {remaining // 60:02d}:{remaining % 60:02d} remaining"
            if payload.get("error"):
                text += f"\nLast error: {payload['error']}"
            camera = payload.get('camera_statistics') or {}
            if camera:
                text += (f"\nRGB {camera.get('rgb_fps',0):.2f} FPS"
                         f" · incomplete {camera.get('incomplete',0)}"
                         f" · missing IDs {camera.get('missing_ids',0)}"
                         f" · preview skips {camera.get('preview_skipped',0)}")
            temperature = payload.get("temperature") or {}
            if temperature.get("reading"):
                text += f"\nCCD {temperature['reading']['celsius']:g} °C · cooler {'on' if payload.get('cooler_on') else 'off'} (last readback)"
            if payload.get("devices"):
                text += "\n" + " · ".join(f"{d['serial']}: {d['position']:.3f}° {d['operation']}" if d.get("connected")
                                              else f"{d['serial']}: disconnected" for d in payload["devices"])
            for action, button in buttons.items():
                button.Enable(action == "reconnect" or action in payload.get("controls", []))
        else:
            text = payload.get("reason", "IPC unavailable")
            previous = self.last_ok.get(name)
            if previous:
                text += f"\nLast IPC confirmation: {previous.get('device_state')} / {previous.get('phase')}"
            text += "\nCurrent hardware state is unconfirmed."
            for action, button in buttons.items():
                button.Enable(action == "reconnect")
        label.SetLabel(text)
        label.Wrap(max(300, self.GetClientSize().width - 40))
        self.cards.Layout()
        self.cards.FitInside()
        self.Layout()

    def action(self, name, action):
        if action == "reconnect":
            try:
                result = self.reconnect[name]()
                if result is not None and hasattr(result, "add_done_callback"):
                    def done(future):
                        try:
                            future.result()
                        except Exception as exc:
                            if not self.closed:
                                wx.CallAfter(self.message.SetLabel, str(exc))
                    result.add_done_callback(done)
                self.message.SetLabel(f"{name}: reconnect requested; waiting for IPC confirmation")
            except Exception as exc:
                self.message.SetLabel(str(exc))
            return
        runtime, info = RUNTIMES[name]
        future = self.pool.submit(monitor_request, runtime, action, info_name=info)
        def done(result):
            try:
                payload = result.result()
                text = (f"{name}: safe {action} queued; watch the IPC cleanup state"
                        if payload.get("accepted") else payload.get("reason", "Request failed"))
            except Exception as exc:
                text = str(exc)
            if not self.closed:
                wx.CallAfter(self.message.SetLabel, text)
        future.add_done_callback(done)

    def refresh_logs(self, force=False):
        if self.closed or self.log_pending or not force and time.monotonic() - self.last_logs < 2:
            return
        self.log_pending = True
        selected = self.filter.GetStringSelection()
        future = self.pool.submit(log_history, selected)
        def done(result):
            try:
                text = result.result()
            except Exception as exc:
                text = str(exc)
            wx.CallAfter(self.show_logs, selected, text)
        future.add_done_callback(done)

    def show_logs(self, selected, text):
        if self.closed:
            return
        self.log_pending = False
        self.last_logs = time.monotonic()
        if selected != self.filter.GetStringSelection():
            self.refresh_logs(force=True)
            return
        if text != self.log_text:
            position = self.logs.GetInsertionPoint()
            self.logs.ChangeValue(text)
            self.logs.ShowPosition(self.logs.GetLastPosition() if self.follow.GetValue() else position)
            self.log_text = text

    def open_folder(self, _event):
        selected = self.filter.GetStringSelection()
        path = log_directory(selected if selected in RUNTIMES else "Andor")
        path.mkdir(parents=True, exist_ok=True)
        wx.LaunchDefaultApplication(str(path))

    def shutdown(self):
        self.closed = True
        self.timer.Stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
