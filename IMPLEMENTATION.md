# Implementation status — 13 October 2026

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

This execution environment lacks wxPython, Matplotlib, pytest, and h5py. The actual GUI window,
Venkata HDF5 round-trip, native SIF readback, motor movement, absolute line
calibration, and cooler/shutter procedure still need Windows workstation
acceptance. The physical SDK and instrument were not accessed in this pass.
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
