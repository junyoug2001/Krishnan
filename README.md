# Krishnan

Krishnan is a Raman measurement GUI with Windows hardware support for the Andor Shamrock spectrograph and SDK2 CCD. It uses the adjacent custom `andor-spectroscopy` Python wrapper from the earlier SDK work, which calls the installed vendor DLLs. It does not import Andor's `pyAndorSDK2` Python package for normal operation. Its layout follows the supplied
Solis screenshots: a large acquisition plot, wavelength overview, bottom
exposure/readout controls, and a right-hand grating and batch area. It uses
Venkata's light wxPython style and its Run/ExperimentSet data model.
See [DESIGN.md](DESIGN.md) for the full contract and release gates.

## Device backend and reconnect

The control path is **Krishnan GUI → independent Krishnan backend → adjacent
`andor-spectroscopy` wrapper → installed Andor SDK DLLs → camera/spectrograph**.
Andor, Aravis and Thorlabs each run in their own windowless hardware process.
There is no independent manager process and no backend window. The right-side
**Console** tab shows each process's PID, IPC connection, device state and cleanup
phase. It provides **Stop safely**, **Disconnect**, **Close backend**, and
**Reconnect**. Every safe control is sent through that backend's protocol;
none force-kill a process. Andor Disconnect retains its cooler and temperature
interlocks. Explicit Close uses the existing manual safe shutdown: cooler off,
temperature checks, and SDK shutdown only when the detector is at least -20 °C.
An error keeps the backend alive rather than forcing shutdown.

Current state is obtained from backend IPC, with stale or unavailable status
shown explicitly. The combined log view is history and can be filtered by
backend. Per-run timestamped UTF-8 files remain under `logs/andor`,
`logs/aravis`, and `logs/thorlabs` in the Krishnan folder, even after the GUI
loses contact. **Log folder** opens that history. Each backend's existing main
connection remains independent of its read-only Console status endpoint.
Reconnect attaches to a surviving process and restores its state; it never
homes a motor or starts Aravis acquisition. If an older backend is already
running, safely close that existing backend once before loading this update.
Its existing cleanup rules still apply until it has been restarted.

The GUI connects to the backend through an authenticated localhost socket. A
per-checkout process lock prevents two backends from opening the same devices
through this installation. The connection file and diagnostics are in a
checkout-specific folder under `%TEMP%\krishnan-backend` on this PC. Run
`.venv\Scripts\python.exe -c "from krishnan.process_worker import _runtime_dir; print(_runtime_dir())"`
from the shared project folder to find the exact path. The connection
file contains a local authentication secret and should not be shared.

Closing or crashing the GUI leaves the backend and device session running.
Relaunch `launch-windows.cmd` within 30 minutes to reattach; the backend
restores connected state and the active session. After 30 minutes without a
GUI, it stops acquisition and switches the cooler off if it was on. It then
allows at least another 30 minutes for warm-up before SDK shutdown. If the
detector remains below -20 °C, it keeps checking; on a cleanup error it keeps
the backend alive and records the failure instead of forcing shutdown. An
explicit **Disconnect** refuses SDK shutdown while cooling is on or the
detector is below -20 °C. This persistence covers GUI failures, not a backend
crash, operating-system shutdown, or loss of computer power.

For the installed DU401_BVF, captures explicitly select the Solis-matched 1×
preamp gain and 8.25 µs vertical shift when those choices are available. An
Advanced selection overrides either value. The camera shutter control starts
at **Unchanged** because its prior SDK state cannot be read back; selecting
Automatic/Closed/Open and clicking Apply sends that command to the SDK.
The CCD temperature display includes the age of its last confirmed readback
and marks it stale after 15 seconds. Idle polling is limited to once per 10
seconds. This DU401 does not advertise SDK2 temperature reads during an active
acquisition, so Live ends a short stream approximately every 10 seconds,
reads temperature while the camera is idle, and resumes acquisition. A long
single exposure cannot provide a fresh temperature until it completes; its
display stays marked stale. Cooler changes attempted during acquisition are
rejected immediately, never queued to run after Stop. The backend logs
cooler state changes and detector crossings of -50, 0, 10, and 20 °C rather
than every temperature sample.
`settings.json` controls the temperature interval (10–60 seconds) and the
thresholds logged by a newly started backend. The CCD display also shows the
last measured SDK temperature/readback call duration in milliseconds. This
does not include the time needed to pause and resume Live acquisition; actual
device timing must be observed on hardware.

## Preview the GUI on macOS

The simulator runs on macOS without an Andor camera or vendor DLL. On an Apple
Silicon Mac, install **Python 3.13 from [python.org](https://www.python.org/downloads/macos/)**
if `python3.13` is unavailable. Python 3.12 also works. In Terminal, change to
the directory containing both `Krishnan/` and `andor-spectroscopy/`, then run:

    bash Krishnan/preview-mac.command

The launcher creates a private virtual environment in
`~/Library/Caches/Krishnan/preview-venv`, installs the local wrapper and GUI,
and opens Krishnan. The first launch downloads Python packages; later launches
reuse the environment. Select **Simulator** explicitly, then click **Connect**
to populate the display with the simulated camera and spectrograph. No hardware
or Andor DLL is touched in Simulator mode.

### Try Single and Batch on the Mac

1. Click **Connect** with **Simulator** selected. The simulated spectrum and
   spectrograph become available.
2. Set **Exposure s** (for example `0.01`), **Cycle s** (for example `0.05`),
   and **Accumulations** in the lower controls. Click **Single**. If no session
   is open, choose a new folder and enter a sample name; the laser wavelength
   may be left blank. The spectrum appears on the left, and the completed step
   appears under **Steps**.
3. Click **Batch**, set a name and count (for example `3`), then click
   **Preview sequence** and **OK**. The simulator runs the steps in order. The
   **Steps** tab shows progress; after two compatible captures, **Map** shows
   the linked 2D map and slices. **Stop** interrupts a batch, and **Resume
   incomplete steps** continues it.

The session folder contains `session.json`, simulated `.npz` raw captures in
`raw/`, and wavelength calibration sidecars in `calibration/`. Select an
existing session with **New/Open session** to restore its completed spectra.
Simulator captures are test data, not measurements from the Andor devices.

The macOS system `python3` may be an Xcode Command Line Tools stub rather than
a usable Python installation. The launcher uses `python3.13` or `python3.12`
and a prebuilt wxPython wheel. Native camera connection remains a
Windows-only path; this Mac launch is for interface and workflow review.

## Reviewing the map and sample camera

The **Map** tab shows a large 2D map above an angular/row slice and a
spectral slice. Click a map pixel to highlight it and update both slices.
The tab uses Venkata's `RamanPlotter2d`, `AngularPlotter`, and `SlicePlotter`
visuals, with its map colour choices, linear/log scale, percentile contrast,
polar angular slice, binning, and Matplotlib zoom/pan/save toolbar. It needs
at least two saved 1D captures on the same calibrated wavelength grid.
Angular batch steps form an angle map; other steps form a capture-index map.
Opening a session restores its completed captures for the map. The **Export
angular map for Venkata 2D fitting** button opens the full Venkata analysis
workflow through the exported HDF5 file.

The **Camera** tab uses **Aravis IMC-720G** by default, with an explicit
**Simulator** option. It has no Neptune SDK, Neptune DLL, or vendor GenTL
producer dependency. The camera process uses a locally installed native Aravis GigE
Vision interface. Andor and Thorlabs retain their existing separate backends.

Play starts a windowless camera process and may request administrator approval
for camera Ethernet setup. A session helper allows UDP only from the configured
camera to the backend executable on the camera's Ethernet interface. If an old
Neptune network filter is still bound there, it temporarily detaches that filter.
Stop restores the original filter binding and removes the firewall exception.
Wi-Fi, FTDI, driver files, NIC tuning, firmware and permanent IP configuration
are not changed. An independent helper watches the backend process and performs
network cleanup even if that process exits unexpectedly. Session diagnostics
are retained under `%TEMP%/krishnan-aravis-network-*`. This compatibility cleanup
is skipped when that old filter is absent; Aravis does not require it installed.
`aravis_manage_network=false` is available for an already configured network.

The native receiver drains camera buffers continuously and converts every good
frame to full-resolution RGB, independently of GUI timing. Preview is reduced
for IPC; Capture saves the unscaled 1624 x 1228 image as an uncompressed RGB TIFF.
The default preview cap is 14 FPS (adjustable 1-16). The Camera tab reports actual
display FPS, while Console IPC exposes acquisition FPS and incomplete/missing
frame counts. A slower preview can skip visual updates without blocking camera
reception. The current exposure, gain, ROI, pixel format, gamma and channel gains
are preserved. Acquisition temporarily uses 14 FPS, 20 us packet pacing, and a
five-second warmup followed by frozen white balance. Camera settings are restored
and read back when the device closes. Color conversion uses the camera's PFNC
Bayer phase, bilinear interpolation, RGB channel order and the scan-line order
of the user's reference TIFF. No software white balancing or brightness fitting
is applied. Matching a reference image does not establish absolute calibration.

The service listens on authenticated localhost IPC. Stop closes the camera and
exits the camera service. GUI connection loss does the same; the independent
network helper restores its state after backend loss. Console reconnect attaches
an idle service and does not start acquisition. Configure sets the TIFF folder;
exposure/gain controls there apply to the simulator. The camera process never
sends Andor cooler, spectrograph or motor commands.

Camera identity, host/camera IP, Aravis DLL directory, FPS, pacing and warmup are
in local `settings.json`. Set the camera serial and host/camera IP addresses
for your workstation there. Install the Aravis runtime under
`native/aravis/runtime/ucrt64/bin` by running
`..\.venv\Scripts\python.exe tools/setup-aravis.py` from the Krishnan folder.
The installer downloads official MSYS2 packages, checks SHA256 hashes and extracts
only project files. It changes no system driver, global PATH or registry setting.
Dependency licenses and a package manifest accompany the runtime. Network helper
scripts use a process-scoped execution-policy flag; system policy is unchanged.

The initial acceptance ran for five minutes at 13.978 FPS with 4,194 complete
full-color frames and no incomplete frames, missing IDs, timeouts or underruns
while the old filter was detached. Keeping that filter bound produced packet loss.
The corrected Camera tab passed a 60-second live test on 2026-10-25 at 13.983
acquisition FPS and 13.879 display FPS, with no missing packets or incomplete
frames, a full-resolution TIFF save, normal process exit, and verified network
restoration. After the user switched illumination on, a further 20-second run
passed at 13.992 acquisition FPS and 13.859 display FPS, again with zero packet
loss and normal cleanup. The sample was visible in the correct orientation.
It was bluer than the 2026-10-17 reference; exact color/focus equivalence under
the current optical conditions has not been established. Images were not fitted
to force a match with that reference.
This supports the selected receive path, not a guarantee against all kernel
crashes. The installed filter still exists after session cleanup and remains
bound on other interfaces. The FTDI-related crash evidence is a separate concern.

## Run on the Windows workstation

Use a 64-bit Python 3.12 or newer with a matching wxPython wheel. Install the
wrapper and GUI in the same environment, from the Venkata checkout:

    
    python -m pip install -e .\Krishnan
    cd Krishnan
    python -m krishnan

The **Simulator** option needs no attached camera or vendor DLL. **Native
camera** needs the installed Andor SDK2, Shamrock/ATSpectrograph and ATSIFIO
DLLs. Close Solis before connecting. Choose or create a session folder before
Single or Batch, or follow the folder prompt when either is clicked. Enter the
laser wavelength to enable Raman-shift display.

The included `settings.example.json` selects **Simulator**; local
`settings.json` can select Native. The workstation's SDK folder is saved
in `settings.json` as `sdk_root`; **SDK folder…** changes it. If that folder
does not exist, Connect asks for a new one. The fallback discovery checks
`ANDOR_SDK_ROOT` and installed DLL locations only while choosing a folder,
not during temperature reads. The **Details** tab shows the exact camera and
spectrograph DLLs loaded. Configure `sdk_root` for your installation.

`settings.json` also stores the CCD setpoint, exposure, cycle time,
accumulations, window size, temperature interval, and log thresholds. The
**Cooler on after new Native connection** checkbox saves an explicit
preference, initially off. When checked, the GUI requests cooling only after
a new native connection succeeds; reconnecting the GUI to an already running
backend does not repeat it. The current cooler on/off readback is never saved
as a preference. Always use the displayed CCD readback for current state.

For the observed DU401_BVF / SR750-A pair, a fresh spectrograph SDK session
starts with calibration geometry 0 pixels / 0 µm. On Native connection,
Krishnan supplies and verifies the camera's 1024-pixel / 26-µm geometry as
SDK working inputs. This does not move the spectrograph or edit its EEPROM.
Native captures currently use the SDK's
uncalibrated SIF writer and save the measured wavelength axis in a JSON
sidecar. Calibrated SIF coefficients have not yet been validated on hardware.

By default the camera cooler stays unchanged on connection and ordinary GUI
close. The optional checkbox above deliberately changes that connection
behavior. A device-backend crash can skip the normal SDK abort
and shutdown calls. If a crash occurs while the camera is cooling or acquiring,
leave the camera powered, regain control with the laboratory's approved
software, check cooler/temperature and acquisition status, and follow the
site's warm-up procedure before removing power. Do not assume that closing
Python made the physical device idle or warm. One connected backend capture
has verified native SIF saving and normal shutdown on this workstation;
backend crashes and cooling recovery have not been exercised.

The GUI and instrument worker run in separate Python processes. A local
socket carries commands and at most ten preview updates per second. The native
continuous cycle request is at least 0.1 s; actual cadence is also limited by
camera readout. If the GUI exits, the backend waits up to 30 minutes for it to
reconnect, then begins the cooler shutdown and warm-up sequence described
above. A backend crash, stuck native call, or power loss still needs the
site's recovery procedure. Keep Solis closed while Krishnan owns the devices.

The **Set calibration geometry** button can restore the SDK working pixel count
and pitch if another client changes them during a connected session. It does
not move a motor. Cooler changes require an explicit button press unless the
saved automatic cooling preference is enabled. Shutter, grating, and center
wavelength changes require explicit button presses.
Follow the laboratory's cooling and optical movement procedure.

Physical captures are native SIF, with a JSON nm-axis sidecar. The simulator
saves NPZ clearly marked as simulation. **Export Venkata** writes one CSV per
1D capture and an HDF5 experiment file to the session's exports folder.
Raw files are never silently replaced. A stopped batch retains completed
steps; **Resume incomplete steps** checks their hashes before continuing.

## Current validation

The modules compile and the simulator/backend checks can run without wx.
The wx window on macOS and the physical save path still need workstation
acceptance.
Read [DESIGN.md](DESIGN.md) for the checks still needed for native SIF,
spectrograph motion, spectral accuracy, and site cooling policy.

## Thorlabs motors

The right-side **Motors** tab contains separate K10CR2 cards for the serials
configured in local `settings.json`. Close Kinesis before selecting **Connect motors**.
Connection loads each assigned Kinesis stage profile, verifies the degree
conversion and 0–360° range, and applies the saved motion preferences. It never
homes or moves a mount automatically. The position is the controller's position
count; it is not an independent measurement of the mounted optic.

Each card has an absolute degree target, **Move**, **Home**, an immediate **STOP**,
positive/negative jog, four selectable drive speeds, **Enable/Disable**, and
**Identify**. Values outside 0–360° disable Move and are rejected again by the
backend. An unhomed mount must be explicitly homed before Move, Jog, or Drive.
Homing finds the hardware reference and can rotate through a full turn; check
clearance before pressing Home.

**Settings** controls move/jog speed and acceleration, home speed, jog step,
single-step/held/continuous jogging, stop mode, backlash, move direction, and
drive speeds. Defaults are 5°/s and 5°/s². Speed is capped at 15°/s, acceleration
at 10°/s², and home speed at 10°/s. These are application ceilings, not a guarantee
that every mounted load can use those rates. **Apply and save** writes
`motor-settings.json`; it does not persist settings to the controller's flash.
The SDK folder and serial list are in `settings.json` as `kinesis_sdk_root` and
`kinesis_serials`.

Drive arrows always require holding. Jog arrows follow the chosen jog mode.
Held motion requires a GUI heartbeat every 0.2 s and requests an immediate stop
if that heartbeat is absent for 0.8 s. Continuous motion has a 60 s timeout and
requests stopping before the 0/360° boundary using speed and stopping distance.
The red STOP buttons always request an immediate stop. Software boundaries
cannot detect collisions or cable interference, and the vendor Home operation
ignores software travel limits.

Kinesis runs in its own windowless process, independent of both the GUI and
CCD backend. On GUI exit or connection loss it immediately requests a stop,
verifies stopped status, releases controller handles, and exits without a
reconnect grace. Console safe-close uses the same procedure. A failed stop
verification keeps the backend in cleanup rather than reporting success.
A backend crash or a hung SDK call cannot guarantee delivery of a stop command.

Andor's automatic 30-minute reconnect grace and subsequent 30-minute warm-up
remain unchanged. Progress messages appear approximately every ten minutes,
plus a final warning with one minute remaining; temperature polling and safety
deadlines are unchanged. The Console countdown is calculated from the deadline
reported by the backend.

The offline motor checks cover target validation, unit conversion, completion
readback, jog boundaries, heartbeat expiry, detachment, IPC, and the wx controls:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Krishnan\tests -p test_motors.py -v
```

Run all motor and Console checks without physical devices with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Krishnan\tests -p "test_*.py" -v
```

Console checks use separate simulated backend processes, shortened test deadlines,
and fake camera/motor calls. They verify reattachment, safe-close IPC, temperature
interlocks, cleanup on GUI loss, log cadence, launch flags and preserved preview
payloads without moving hardware or changing cooler/optics state.
