# Krishnan

Krishnan is a Raman measurement GUI with Windows hardware support, especially for ANDOR Shamrock and CCD. This requires separate ANDOR python wrapper as andor-spectroscopy, which can be bought from the manufacturer. Its layout follows the supplied
Solis screenshots: a large acquisition plot, wavelength overview, bottom
exposure/readout controls, and a right-hand grating and batch area. It uses
Venkata's light wxPython style and its Run/ExperimentSet data model.
See [DESIGN.md](DESIGN.md) for the full contract and release gates.

## Preview the GUI on macOS

The simulator runs on macOS without an Andor camera or vendor DLL. On an Apple
Silicon Mac, install **Python 3.13 from [python.org](https://www.python.org/downloads/macos/)**
if `python3.13` is unavailable. Python 3.12 also works. In Terminal, change to
the directory containing both `Krishnan/` and `andor-spectroscopy/`, then run:

    bash Krishnan/preview-mac.command

The launcher creates a private virtual environment in
`~/Library/Caches/Krishnan/preview-venv`, installs the local wrapper and GUI,
and opens Krishnan. The first launch downloads Python packages; later launches
reuse the environment. It selects **Simulator** by default. Click **Connect**
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

The **Map** tab expands to show a large 2D map, an angular/row slice, and a
spectral slice. Click a map pixel to highlight it and update both slices.
The tab uses Venkata's `RamanPlotter2d`, `AngularPlotter`, and `SlicePlotter`
visuals, with its map colour choices, linear/log scale, percentile contrast,
polar angular slice, binning, and Matplotlib zoom/pan/save toolbar. It needs
at least two saved 1D captures on the same calibrated wavelength grid.
Angular batch steps form an angle map; other steps form a capture-index map.
Opening a session restores its completed captures for the map. The **Export
angular map for Venkata 2D fitting** button opens the full Venkata analysis
workflow through the exported HDF5 file.

The **Camera** tab sits beside Steps, Map, and Details while the Raman spectrum
stays visible on the left. Play, Stop, Capture, and Configure operate on a
separate simulated sample-view camera. Capture saves a PNG in the session's
`camera` folder by default, or asks for a destination if no session is open.
**Configure…** sets exposure, gain, and a **Save directory** for camera PNGs;
the selected folder is remembered and takes precedence over the session
default. Camera preview does not take control of the Andor spectroscopy camera.

The reported LAN “EGig” device has not been identified, so no network
protocol is assumed. `AuxiliaryCamera` and `LanCameraProfile` reserve the
source interface and configuration fields. Once its manufacturer/model and
SDK are known, a driver can be inserted through `CameraPanel.source_factory`.
If it is a GenICam/GigE Vision device, a vendor GenTL `.cti` producer may be
usable through [Harvester](https://github.com/genicam/harvesters/blob/master/docs/TUTORIAL.rst);
that route requires confirmation from the actual device documentation and a
Windows acceptance test. No LAN connection is attempted in this release.

## Run on the Windows workstation

Use a 64-bit Python 3.12 or newer with a matching wxPython wheel. Install the
wrapper and GUI in the same environment, from the Venkata checkout:

    
    python -m pip install -e .\Krishnan
    cd Krishnan
    python -m krishnan

The **Simulator** option needs no attached camera or vendor DLL. **Native
camera** needs the installed Andor SDK2, Shamrock/ATSpectrograph and ATSIFIO
DLLs. Close Solis before connecting. Choose or create a session folder before
Single or Batch, or follow the folder prompt when either is clicked. Enter the laser wavelength to enable Raman-shift display
and calibrated SIF when the SDK coefficients validate.

If the spectrograph's working calibration geometry differs from the camera,
use **Set calibration geometry** once in the connected session. This sets SDK
working pixel count and pitch; it does not move a motor. Cooler, shutter,
grating, and center wavelength changes require explicit button presses.
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
