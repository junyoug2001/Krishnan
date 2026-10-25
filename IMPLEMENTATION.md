# Implementation status — 14 October 2026

The application code is in [krishnan/](krishnan/) and starts with
`python -m krishnan` from the Krishnan directory after installing the
dependencies in [README.md](README.md).

## Implemented

- Solis-shaped wxPython workspace with a large spectrum, wavelength overview,
  batch step list/map, lower acquisition and spectrograph controls, and
  Venkata's light panel styling and OrRd map palette.
- Simulator/native connection, camera and accessory inventory, explicit
  calibration-geometry action, single/live/snapshot/batch/wide-scan paths,
  explicit CCD cooler/shutter and spectrograph commands.
- Versioned session manifest, raw-file checksums, incomplete-step resume,
  orphan preservation, simulator NPZ, native SIF transaction with SIFReader
  readback, and sidecar wavelength axis.
- Step-and-glue planning and linear overlap blending; non-destructive cosmic
  review; 1D and valid angular-map Venkata export.
- SDK2 continuous cycle request and coordinated SIF save binding in the
  [wrapper](../andor-spectroscopy/src/andor_spectroscopy/).
- Native `SaveAsSif` is bound to the installed SDK2 signature. A camera lock
  covers acquisition through SIF save, while Krishnan verifies the SIF and
  writes the calibrated nm axis to a separate JSON sidecar. Acquisition is
  rejected before any requested optical move if the spectrograph SDK's working
  detector geometry differs from the camera.
- The GUI communicates over a local pipe with a separate device process. The
  backend owns the SDK handles, detects a lost GUI connection and attempts
  stop/close. Live preview and requested cycle are capped at 10 Hz.
- Advanced acquisition now uses separate scrolling Readout, ROI & tracks, and
  Electronics pages with fixed action buttons and mode-aware fields.
- Wavelength guide uses an approximate visible spectrum at physical nm
  coordinates, grey outside the visible band; calibrated detection boundaries
  flank the center value above its pointer, following the supplied Solis view.
- Map tab has Venkata's 2D map and two linked slice plotters, click highlight,
  polar/cartesian row views, binning, colour/contrast controls and zoom/pan.
  Completed captures are restored when a session is opened.
- Camera tab has an independent simulated sample camera with live preview,
  capture to PNG, and settings including a persistent save directory. A source protocol/factory is ready for a
  future LAN adapter after its device identity and SDK are supplied.
- Single and Batch can open a session on demand while using the simulator;
  the Batch dialog is resizable and shows its sequence preview and actions.

## Validation here

Eleven hardware-free tests cover:
planning/blending, manifest resume/orphans, cosmic review, fake SDK2 SIF
ordering and continuous cycle setting, simulated batch/readback, live stop,
simulated wide scan, map rows/slices, wavelength colours, simulated sample
camera, completed-capture restore, and queued Single and Batch captures after
session creation. All new Python modules compile.

The Windows environment now has Python 3.12, wxPython, Matplotlib, NumPy,
pandas, and h5py in the workspace `.venv`. The changed modules compile, a
simulated 1024-pixel capture completed, and the installed SDK2 DLL exports
`SaveAsSif`. The connected native SIF readback, motor movement, absolute line
calibration, and cooler/shutter procedure still need Windows workstation
acceptance. No camera or spectrograph was initialized in this pass.
The separate backend completed simulator connect/live/stop/close and a saved
single capture over the pipe. It exited after a simulated GUI process crash.
The in-app angular fitting button exports a validated angular map to Venkata;
the fit itself is run in Venkata.

## Next connected acceptance

1. Install from [README.md](README.md) on the Windows instrument workstation;
   start in Simulator mode and inspect every dialog and state.
2. Close Solis, connect Native, confirm camera/spectrograph identities and
   accessories, then configure the SDK working calibration geometry if needed.
3. With the site's dark optical path, compare one bounded single capture,
   native SIF and readback with Solis. Confirm requested versus applied timing.
4. Validate live accounting and stop, then a finite batch and interrupted
   resume. Confirm HDF5 import into Venkata.
5. Measure a reference spectral line before using Raman shift and approve
   grating/wavelength movement before a wide scan.
6. Establish the laboratory cooling, shutter, and shutdown procedure before
   using those controls routinely.


## Aravis sample-camera migration (17 October 2026)

The IMC-720G path now uses a local native Aravis runtime, a continuously drained
RGB receiver, authenticated camera-process IPC, and full-resolution TIFF capture.
The Neptune SDK adapter/service and vendor SDK probes were removed. The GUI and
Console use the Aravis backend; legacy camera identity settings migrate without
loading a vendor library. Andor and Thorlabs control paths remain separate.
A temporary camera-only Ethernet/firewall helper restores original state and
checks binding/rule cleanup, including when its backend owner disappears.
It also works when the legacy filter is absent. Already configured networks can
turn off managed setup and run without elevation.

The native acquisition gate passed 4194 full-resolution color frames in five
minutes at 13.978 FPS with zero transport errors while the legacy filter was
detached. GUI acceptance records and a pre-refactor source/config backup
are under ../hardware-checks/aravis-refactor-20261017/. The reference TIFF was
updated after the operator corrected focus and room lighting.
