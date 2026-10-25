"""Bounded real-camera acceptance through Krishnan's Camera tab and backend.

Only the sample camera is opened. Andor and Thorlabs are never instantiated.
The normal camera backend temporarily prepares/restores its Ethernet session.
"""
import argparse
import faulthandler
import json
from pathlib import Path
import time

import wx
from PIL import Image
from krishnan.camera_view import CameraPanel


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=60)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--simulator',action='store_true',help='Offline GUI/shutdown check; no hardware process')
    args=parser.parse_args()
    faulthandler.enable()
    faulthandler.dump_traceback_later(args.seconds+330)
    if not 5<=args.seconds<=300:parser.error('Use 5..300 seconds')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    report={'success':False,'requested_seconds':args.seconds}
    app=wx.App(False)
    frame=wx.Frame(None,title='Krishnan Aravis camera acceptance — automatic bounded test',size=(800,740))

    class ObservedPanel(CameraPanel):
        def _show_frame(self, camera_frame):
            super()._show_frame(camera_frame)
            displayed.append(time.perf_counter())

    displayed=[]
    panel=ObservedPanel(frame,lambda:None)
    panel.source_choice.SetSelection(1 if args.simulator else 0)
    panel.preview_fps=14;panel.fps_control.SetValue(14)
    panel.save_directory=output
    root=wx.BoxSizer(wx.VERTICAL);root.Add(panel,1,wx.EXPAND);frame.SetSizer(root)
    timer=wx.Timer(frame)
    started_at=None
    setup_deadline=time.perf_counter()+300
    stopping=False
    capture_requested=False

    def finish(error=None):
        timer.Stop()
        statistics=panel.aravis.last_statistics
        if displayed:
            duration=displayed[-1]-displayed[0]
            report.update(displayed_frames=len(displayed),display_fps=(len(displayed)-1)/duration if duration else 0)
            full_windows=range(0,int(duration)-4,5)
            report['five_second_display_fps']=[sum(a<=t-displayed[0]<a+5 for t in displayed)/5 for a in full_windows]
        report['acquisition']=statistics
        report['error']=error or panel._last_error
        captures=list(output.glob('camera_*.tif'))
        report['captures']=[str(p) for p in captures]
        if panel._last_full_frame is not None:
            image=Image.fromarray(panel._last_full_frame.rgb)
            image.save(output/'last-rgb.tif',compression='raw')
            image.save(output/'last-rgb.png')
            report['full_resolution_size']=list(image.size)
        if args.simulator:
            report['success']=bool(not report['error'] and displayed and captures)
        else:
            report['success']=bool(not report['error'] and report.get('display_fps',0)>10 and
                statistics.get('rgb_fps',0)>=13.3 and statistics.get('incomplete',1)==0 and
                statistics.get('missing_ids',1)==0 and statistics.get('image_timeouts',1)==0 and
                report.get('full_resolution_size')==[1624,1228] and captures)
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2),flush=True)
        panel.shutdown()
        panel.pool.shutdown(wait=True)
        # Let wx process deferred window destruction and leave MainLoop normally.
        frame.Destroy()
        def leave_loop():
            windows=list(wx.GetTopLevelWindows())
            print('Deferred GUI teardown:',[(w.GetClassName(),w.GetName(),w.GetTitle(),w.IsShown()) for w in windows],flush=True)
            for window in windows:window.Destroy()
        wx.CallLater(500,leave_loop)

    def tick(_event):
        nonlocal started_at,stopping,capture_requested
        now=time.perf_counter()
        if panel._last_error and not panel.running:
            finish(panel._last_error);return
        if started_at is None:
            if displayed:started_at=displayed[0]
            elif now>setup_deadline:
                panel.stop();finish('Camera startup timed out');return
        elif not stopping:
            elapsed=now-started_at
            if elapsed>3 and not capture_requested:
                panel.capture();capture_requested=True
            if elapsed>=args.seconds:
                panel.stop();stopping=True
        elif not panel.running:
            finish()

    def user_close(event):
        nonlocal stopping
        panel.stop();stopping=True
        if not panel.running:finish('Acceptance window closed early')
        else:event.Veto()

    frame.Bind(wx.EVT_TIMER,tick,timer)
    frame.Bind(wx.EVT_CLOSE,user_close)
    frame.Show()
    app.SetTopWindow(frame)
    timer.Start(100)
    wx.CallLater(200,panel.play)
    app.MainLoop()
    faulthandler.cancel_dump_traceback_later()
    return 0 if report['success'] else 1


if __name__=='__main__':raise SystemExit(main())
