"""One worker owns devices and serializes acquisition, motion and file commits."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import json
import os
import threading
import time
from typing import Callable

import numpy as np

from .bootstrap import prepare_paths
from .core import (
    Session, Step, export_venkata, sha256, step_glue_centers,
    stitch_tiles, utc_now, write_txt,
)
from .processing import correct_cosmics

prepare_paths()

from andor_spectroscopy.camera import Camera
from andor_spectroscopy.errors import AcquisitionCancelledError
from andor_spectroscopy.instrument import Spectrometer
from andor_spectroscopy.models import (
    AcquisitionConfig, AcquisitionMode, ReadoutConfig, TriggerMode,
)
from andor_spectroscopy.sif import SIFReader
from andor_spectroscopy.simulator import SimulatedCamera
from andor_spectroscopy.spectrograph import SimulatedSpectrograph, Spectrograph
from andor_spectroscopy.storage import load_npz, save_npz


class InstrumentWorker:
    def __init__(self, emit: Callable[[str, object], None]):
        self.emit = emit
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="krishnan-device")
        self.camera = None
        self.optics = None
        self.instrument = None
        self.session: Session | None = None
        self.simulated = False
        self.state = "DISCONNECTED"
        self.stop_event = threading.Event()
        self._live = None
        self._operation_lock = threading.Lock()
        self._batch_running = False

    def _state(self, value: str) -> None:
        self.state = value
        self.emit("state", value)

    def submit(self, method: str, *args, **kwargs):
        return self.pool.submit(self._execute, method, args, kwargs)

    def _execute(self, method, args, kwargs):
        try:
            return getattr(self, method)(*args, **kwargs)
        except Exception as exc:
            if self.stop_event.is_set() and isinstance(
                    exc, (AcquisitionCancelledError, InterruptedError)):
                self.emit("status", "Acquisition stopped.")
            else:
                self.emit("error", f"{type(exc).__name__}: {exc}")
            if self.state not in ("DISCONNECTED", "FAULT"):
                self._state("FAULT" if getattr(self.camera, "state", None) == "FAULT"
                            else "IDLE")
            raise

    def connect(self, *, simulation: bool, camera_index: int = 0,
                spectrograph_index: int = 0, sdk_root: str | None = None,
                backend: str = "shamrock") -> None:
        if self.state != "DISCONNECTED":
            raise RuntimeError("Disconnect before connecting again")
        self._state("CONNECTING")
        camera = optics = None
        try:
            camera = (SimulatedCamera(width=1024, height=127, seed=7)
                      if simulation else Camera(index=camera_index, sdk_root=sdk_root))
            camera.open()
            optics = (SimulatedSpectrograph(detector_pixels=camera.detector[0],
                                            pixel_width_um=camera.pixel_pitch_um[0])
                      if simulation else Spectrograph.open(
                          device=spectrograph_index, backend=backend, sdk_root=sdk_root))
            self.camera, self.optics = camera, optics
            self.instrument = Spectrometer(camera, optics)
            self.simulated = simulation
            geometry = optics.calibration_geometry()
            matching = geometry[0] == camera.detector[0] and np.isclose(
                geometry[1], camera.pixel_pitch_um[0], rtol=1e-6)
            inventory = {
                "camera": asdict(camera.identity),
                "temperature": asdict(camera.temperature()),
                "cooler_on": camera.cooler_is_on(),
                "gratings": [asdict(g) for g in optics.gratings()],
                "optical": asdict(optics.snapshot()),
                "calibration_geometry": geometry,
                "geometry_matches": bool(matching),
                "accessories": {
                    "slits": [n for n in range(1, 5) if optics.slit_present(n)],
                    "shutter": optics.shutter_present(),
                    "filter": optics.filter_present(),
                    "flippers": [n for n in (1, 2) if optics.flipper_present(n)],
                },
                "readout": asdict(camera.capabilities()),
                "triggers": [int(mode) for mode in TriggerMode
                             if camera.trigger_available(mode)],
                "simulation": simulation,
            }
            self.emit("inventory", inventory)
            self._emit_wavelength_range()
            self._state("IDLE")
        except BaseException:
            if optics is not None:
                optics.close()
            if camera is not None:
                camera.close()
            self.camera = self.optics = self.instrument = None
            self._state("DISCONNECTED")
            raise

    def disconnect(self) -> None:
        if self.state not in ("IDLE", "FAULT"):
            raise RuntimeError("Stop the current acquisition first")
        if self.optics is not None:
            self.optics.close()
        if self.camera is not None:
            self.camera.close()
        self.camera = self.optics = self.instrument = None
        self._state("DISCONNECTED")

    def use_session(self, session: Session) -> None:
        if self.state not in ("IDLE", "DISCONNECTED"):
            raise RuntimeError("Cannot switch sessions while acquiring")
        if self.camera is not None:
            prior = session.data.get("device")
            if prior is not None and any(
                    item["state"] == "complete" for item in session.data["steps"]):
                old_camera = prior["camera"].get("serial_number")
                old_optics = prior["spectrograph"].get("serial")
                if (old_camera != self.camera.identity.serial_number
                        or old_optics != self.optics.serial):
                    raise ValueError(
                        "This session belongs to different camera/spectrograph identities")
            capabilities = asdict(self.camera.capabilities())
            capabilities["horizontal_speeds_mhz"] = {
                f"{channel}:{amplifier}": list(values)
                for (channel, amplifier), values
                in capabilities["horizontal_speeds_mhz"].items()
            }
            session.set_device(self.camera, self.optics, capabilities)
        self.session = session
        self.emit("session", session.data)

    def load_completed(self) -> None:
        """Restore saved spectra for plotting without modifying raw acquisitions."""
        if self.session is None:
            raise RuntimeError("Open a session first")
        damaged = self.session.verify_completed()
        if damaged:
            raise IOError(f"Completed raw files failed verification: {damaged}")
        restored = []
        for item in self.session.data["steps"]:
            if item["state"] != "complete":
                continue
            raw_path = self.session.folder / item["raw"]["path"]
            sidecar = self.session.folder / item["calibration"]["path"]
            axis = np.asarray(json.loads(sidecar.read_text(encoding="utf-8"))["axis_nm"],
                              dtype=float)
            if item["raw"]["format"] == "native-sif":
                data = SIFReader(raw_path).read().frames[0]
            else:
                data = load_npz(raw_path).data
            step = Step(item["id"], item["index"], item["name"], item["center_nm"],
                        item["grating"], item["angle_deg"], item["exposure_s"],
                        item["cycle_s"], item["accumulations"])
            restored.append({"step": step, "axis": axis, "data": data,
                             "applied": item.get("applied") or {}})
        self.emit("restored_measurements", restored)

    def configure_calibration_geometry(self) -> None:
        self._need_idle()
        camera, optics = self.camera, self.optics
        optics.configure_calibration_geometry(camera.detector[0], camera.pixel_pitch_um[0])
        self.emit("geometry", optics.calibration_geometry())
        self._emit_wavelength_range()

    def _need_idle(self) -> None:
        if self.state != "IDLE" or self.camera is None or self.optics is None:
            raise RuntimeError("Connect and wait for idle")

    def set_grating(self, grating: int) -> None:
        self._need_idle()
        self.instrument.set_grating(grating)
        self.emit("optical", asdict(self.optics.snapshot()))
        self._emit_wavelength_range()

    def set_wavelength(self, nm: float) -> None:
        self._need_idle()
        self.instrument.set_wavelength(nm)
        self.emit("optical", asdict(self.optics.snapshot()))
        self._emit_wavelength_range()

    def _emit_wavelength_range(self) -> None:
        """Report SDK calibrated pixel range without moving either instrument."""
        grating = self.optics.get_grating()
        limits = self.optics.wavelength_limits(grating)
        try:
            width = self.camera.detector[0]
            pitch = self.camera.pixel_pitch_um[0]
            axis = np.asarray(self.optics.calibration(width, pitch).full_axis_nm,
                              dtype=float)
            finite = axis[np.isfinite(axis)]
            if finite.size < 2:
                raise ValueError("No finite CCD calibration")
            span = (float(np.min(finite)), float(np.max(finite)))
            error = None
        except Exception as exc:
            span = None
            error = str(exc)
        self.emit("wavelength_range", {"limits": limits, "span": span,
                                        "error": error})

    def set_cooler(self, on: bool, setpoint: int | None = None) -> None:
        self._need_idle()
        if setpoint is not None:
            self.camera.set_temperature(setpoint)
        self.camera.cooler_on() if on else self.camera.cooler_off()
        self.emit("temperature", {
            "reading": asdict(self.camera.temperature()),
            "cooler_on": self.camera.cooler_is_on(),
        })

    def set_shutter(self, mode: int) -> None:
        self._need_idle()
        self.camera.set_shutter(mode)
        self.emit("shutter", mode)

    def read_temperature(self) -> None:
        self._need_idle()
        self.emit("temperature", {
            "reading": asdict(self.camera.temperature()),
            "cooler_on": self.camera.cooler_is_on(),
        })

    def set_accessory(self, kind: str, index: int, value: float) -> None:
        self._need_idle()
        optics = self.optics
        if kind == "slit":
            if not optics.slit_present(index):
                raise ValueError("Motorized slit is not installed")
            optics.set_slit_width(index, float(value))
            readback = optics.get_slit_width(index)
        elif kind == "spectrograph shutter":
            if not optics.shutter_present():
                raise ValueError("Spectrograph shutter is not installed")
            optics.set_shutter(int(value))
            readback = optics.get_shutter()
        elif kind == "filter":
            if not optics.filter_present():
                raise ValueError("Filter wheel is not installed")
            optics.set_filter(int(value))
            readback = optics.get_filter()
        elif kind == "flipper":
            if not optics.flipper_present(index):
                raise ValueError("Flipper is not installed")
            optics.set_flipper_port(index, int(value))
            readback = optics.get_flipper_port(index)
        else:
            raise ValueError("Unknown accessory")
        self.emit("accessory", {"kind": kind, "index": index, "value": readback})

    def _config(self, step: Step, readout: ReadoutConfig,
                advanced: dict | None = None) -> AcquisitionConfig:
        from andor_spectroscopy.models import ReadoutSelection, TriggerMode
        values = advanced or {}
        mode = AcquisitionMode.ACCUMULATE if step.accumulations > 1 else AcquisitionMode.SINGLE
        selection = ReadoutSelection(
            values.get("ad_channel"), values.get("amplifier"),
            values.get("hs_index"), values.get("vs_index"), values.get("preamp_index"))
        if all(v is None for v in asdict(selection).values()):
            selection = None
        return AcquisitionConfig(
            step.exposure_s, mode=mode, readout=readout,
            trigger=TriggerMode(values.get("trigger", 0)),
            accumulations=step.accumulations,
            accumulation_cycle_s=step.cycle_s,
            readout_selection=selection,
        )

    def _save(self, step: Step, config: AcquisitionConfig):
        session = self.session
        assert session is not None
        extension = ".npz" if self.simulated else ".sif"
        raw = session.folder / "raw" / (step.stem + extension)
        if raw.exists():
            raise FileExistsError(raw)
        laser = session.data["laser_nm"]
        if self.simulated:
            measurement = self.instrument.capture(config, timeout_s=max(30, step.exposure_s * step.accumulations + 10))
            save_npz(raw, measurement)
            stored = load_npz(raw)
            if not np.array_equal(stored.data, measurement.data):
                raise IOError("Simulated NPZ failed raw readback")
            calibrated = False
        else:
            temporary = raw.with_name(raw.stem + ".partial.sif")
            if temporary.exists():
                raise FileExistsError(temporary)
            try:
                measurement, calibrated = self.instrument.capture_to_sif(
                    config, str(temporary), laser_nm=laser,
                    timeout_s=max(30, step.exposure_s * step.accumulations + 10))
                saved = SIFReader(temporary).read()
                expected = measurement.data.astype(np.float32).reshape(-1)
                if saved.frame_count != 1 or saved.frame_size != expected.size:
                    raise IOError("SIF frame dimensions do not match acquisition")
                if not np.allclose(saved.frames[0], expected, rtol=0, atol=0.5):
                    raise IOError("SIF pixels do not match captured data")
                if calibrated and (
                    saved.x_calibration is None
                    or saved.x_calibration.shape != measurement.wavelength_nm.shape
                    or not np.allclose(
                        saved.x_calibration, measurement.wavelength_nm, rtol=0, atol=0.02
                    )
                ):
                    raise IOError("Calibrated SIF wavelength axis failed readback")
                os.link(temporary, raw)
            finally:
                temporary.unlink(missing_ok=True)
        sidecar = session.folder / "calibration" / (step.stem + ".json")
        with sidecar.open("x", encoding="utf-8") as stream:
            json.dump({
                "axis_nm": measurement.wavelength_nm.tolist(),
                "optical": asdict(measurement.optical_snapshot),
                "calibrated_sif": calibrated,
                "laser_nm": laser,
            }, stream, ensure_ascii=False, allow_nan=False)
        return measurement, {
            "path": str(raw.relative_to(session.folder)),
            "format": "simulation-npz" if self.simulated else "native-sif",
            "sha256": sha256(raw),
        }, {
            "path": str(sidecar.relative_to(session.folder)),
            "unit": "nm", "source": "spectrograph-sdk" if not self.simulated else "simulator",
            "calibrated_sif": calibrated,
        }

    def capture(self, step: Step, readout: ReadoutConfig | None = None,
                advanced: dict | None = None, *, _from_batch: bool = False):
        if not _from_batch:
            self._need_idle()
        elif not self._batch_running or self.camera is None:
            raise RuntimeError("No batch is running")
        if self.session is None:
            raise RuntimeError("Create or open a session first")
        readout = readout or ReadoutConfig()
        entry = next((s for s in self.session.data["steps"] if s["id"] == step.id), None)
        if entry is None:
            self.session.add_steps([step])
        elif entry["state"] == "complete":
            raise ValueError("Step is already complete")
        config = self._config(step, readout, advanced)
        self._state("ACQUIRING")
        self.session.update(step.id, "configuring", started_utc=utc_now(),
                            requested={"step": asdict(step), "acquisition": asdict(config)})
        try:
            if step.grating is not None and step.grating != self.optics.get_grating():
                self.session.update(step.id, "moving")
                self.instrument.set_grating(step.grating)
            if step.center_nm is not None:
                self.session.update(step.id, "moving")
                self.instrument.set_wavelength(step.center_nm)
            if step.grating is not None or step.center_nm is not None:
                self.emit("optical", asdict(self.optics.snapshot()))
                self._emit_wavelength_range()
            if self.stop_event.is_set():
                raise InterruptedError("Stopped before capture")
            self.session.update(step.id, "acquiring")
            measurement, raw, calibration = self._save(step, config)
            self.session.update(step.id, "verifying")
            applied = {
                "exposure_s": measurement.frame.timings.exposure_s,
                "accumulation_cycle_s": measurement.frame.timings.accumulation_s,
                "kinetic_cycle_s": measurement.frame.timings.kinetic_s,
                "grating": self.optics.get_grating(),
                "center_nm": self.optics.get_wavelength(),
                "temperature": asdict(self.camera.temperature()),
                "readout": asdict(measurement.frame.readout),
                "trigger": int(config.trigger),
                "readout_selection": (
                    asdict(measurement.frame.readout_selection)
                    if measurement.frame.readout_selection is not None else None),
                "optical": asdict(measurement.optical_snapshot),
            }
            self.session.update(step.id, "complete", applied=applied, raw=raw,
                                calibration=calibration, failure=None)
            self.emit("measurement", {
                "step": step, "axis": measurement.wavelength_nm,
                "data": measurement.data, "applied": applied,
            })
            self.emit("steps", self.session.data["steps"])
            return measurement
        except Exception as exc:
            state = "interrupted" if self.stop_event.is_set() or isinstance(exc, InterruptedError) else "failed"
            self.session.update(step.id, state, failure=str(exc))
            self.emit("steps", self.session.data["steps"])
            raise
        finally:
            self._state("FAULT" if getattr(self.camera, "state", None) == "FAULT"
                        else "ACQUIRING" if _from_batch else "IDLE")

    def batch(self, steps: list[Step], readout: ReadoutConfig | None = None,
              advanced: dict | None = None) -> None:
        self._need_idle()
        if self.session is None:
            raise RuntimeError("Create or open a session first")
        existing = {s["id"] for s in self.session.data["steps"]}
        self.session.add_steps([s for s in steps if s.id not in existing])
        self.emit("steps", self.session.data["steps"])
        self.stop_event.clear()
        self._batch_running = True
        self._state("ACQUIRING")
        try:
            for step in steps:
                if self.stop_event.is_set():
                    break
                item = next(s for s in self.session.data["steps"] if s["id"] == step.id)
                if item["state"] == "complete":
                    continue
                started = time.monotonic()
                self.capture(step, readout, advanced, _from_batch=True)
                if step.cycle_s > 0:
                    self.stop_event.wait(max(0.0, step.cycle_s - (time.monotonic() - started)))
        finally:
            self._batch_running = False
            self._state("FAULT" if getattr(self.camera, "state", None) == "FAULT" else "IDLE")

    def resume_batch(self, readout: ReadoutConfig | None = None,
                     advanced: dict | None = None) -> None:
        self._need_idle()
        if self.session is None:
            raise RuntimeError("Open a session first")
        damaged = self.session.verify_completed()
        if damaged:
            raise IOError(f"Completed raw files failed verification: {damaged}")
        orphans = self.session.quarantine_incomplete_files()
        if orphans:
            self.emit("status", f"Preserved {len(orphans)} incomplete files in orphans folders.")
        steps = [
            Step(item["id"], item["index"], item["name"], item["center_nm"],
                 item["grating"], item["angle_deg"], item["exposure_s"],
                 item["cycle_s"], item["accumulations"])
            for item in self.session.data["steps"] if item["state"] != "complete"
        ]
        self.batch(steps, readout, advanced)

    def scan(self, start_nm: float, end_nm: float, overlap: float,
             exposure_s: float, cycle_s: float = 0.0) -> None:
        self._need_idle()
        if self.session is None:
            raise RuntimeError("Create or open a session first")
        width = self.camera.detector[0]
        pitch = self.camera.pixel_pitch_um[0]
        calibration = self.optics.calibration(width, pitch)
        grating = self.optics.get_grating()
        centers = step_glue_centers(
            start_nm, end_nm, calibration.full_axis_nm,
            self.optics.wavelength_limits(grating), overlap)
        steps = [
            Step(__import__("uuid").uuid4().hex, i + 1 + len(self.session.data["steps"]),
                 "wide_scan", center_nm=center, grating=grating,
                 exposure_s=exposure_s, cycle_s=cycle_s)
            for i, center in enumerate(centers)
        ]
        self.session.add_steps(steps)
        self.stop_event.clear()
        tiles = []
        self._batch_running = True
        self._state("ACQUIRING")
        try:
            for step in steps:
                if self.stop_event.is_set():
                    return
                started = time.monotonic()
                measurement = self.capture(step, _from_batch=True)
                if measurement.data.ndim != 1:
                    raise ValueError("Step and Glue requires a 1D spectral readout")
                tiles.append((measurement.wavelength_nm, measurement.data))
                if step.cycle_s > 0:
                    self.stop_event.wait(max(0.0, step.cycle_s - (time.monotonic() - started)))
        finally:
            self._batch_running = False
            self._state("FAULT" if getattr(self.camera, "state", None) == "FAULT" else "IDLE")
        axis, counts = stitch_tiles(tiles, start_nm, end_nm)
        path = self.session.folder / "derived" / (
            f"wide_scan_{len(self.session.data['derived']) + 1:03d}.txt")
        write_txt(path, axis, counts)
        self.session.data["derived"].append({
            "type": "step-and-glue", "path": str(path.relative_to(self.session.folder)),
            "source_steps": [step.id for step in steps],
            "range_nm": [start_nm, end_nm], "overlap": overlap,
            "processing": "linear overlap weights on common calibrated nm grid",
            "created_utc": utc_now(),
        })
        self.session._write()
        self.emit("measurement", {"step": None, "axis": axis, "data": counts,
                                  "applied": {"derived": "Step and Glue"}})

    def export(self, selected: set[str] | None = None):
        if self.session is None:
            raise RuntimeError("Open a session first")
        damaged = self.session.verify_completed()
        if damaged:
            raise IOError(f"Raw files failed verification: {damaged}")
        measurements = []
        for item in self.session.data["steps"]:
            if item["state"] != "complete":
                continue
            path = self.session.folder / item["raw"]["path"]
            axis_path = self.session.folder / item["calibration"]["path"]
            axis = np.asarray(json.loads(axis_path.read_text(encoding="utf-8"))["axis_nm"])
            if item["raw"]["format"] == "native-sif":
                data = SIFReader(path).read().frames[0]
            else:
                data = load_npz(path).data
            if data.ndim == 1 and data.shape[0] == axis.size:
                measurements.append((item, axis, data))
        result = export_venkata(self.session, measurements, selected=selected)
        self.emit("export", result)
        return result

    def save_cosmic_correction(self, step_id: str, axis: np.ndarray,
                               counts: np.ndarray, indices: list[int]) -> Path:
        if self.session is None:
            raise RuntimeError("Open a session first")
        source = next((s for s in self.session.data["steps"]
                       if s["id"] == step_id and s["state"] == "complete"), None)
        if source is None:
            raise ValueError("Cosmic correction requires a completed raw step")
        corrected = correct_cosmics(counts, indices)
        path = self.session.folder / "derived" / (source["stem"] + "_cosmic.txt")
        write_txt(path, axis, corrected)
        self.session.data["derived"].append({
            "type": "cosmic-correction", "path": str(path.relative_to(self.session.folder)),
            "source_steps": [step_id], "indices": sorted(set(indices)),
            "processing": "linear interpolation of operator-approved isolated pixels",
            "created_utc": utc_now(),
        })
        self.session._write()
        self.emit("measurement", {"step": None, "axis": axis, "data": corrected,
                                  "applied": {"derived": "Cosmic correction"}})
        return path

    def live(self, exposure_s: float, cycle_s: float,
             readout: ReadoutConfig | None = None) -> None:
        self._need_idle()
        self.stop_event.clear()
        config = AcquisitionConfig(
            exposure_s, mode=AcquisitionMode.CONTINUOUS,
            kinetic_cycle_s=cycle_s, readout=readout or ReadoutConfig())
        self._state("LIVE")
        last_emit = 0.0
        try:
            with self.instrument.stream(config, timeout_s=30, queue_size=32,
                                        loss_policy="preview") as stream:
                self._live = stream
                for measurement in stream:
                    if self.stop_event.is_set():
                        break
                    now = time.monotonic()
                    if now - last_emit >= 0.1:
                        self.emit("preview", {
                            "axis": measurement.wavelength_nm, "data": measurement.data,
                            "timings": measurement.frame.timings,
                            "statistics": stream.statistics,
                        })
                        last_emit = now
        except AcquisitionCancelledError:
            if not self.stop_event.is_set():
                raise
        finally:
            self._live = None
            self._state("FAULT" if getattr(self.camera, "state", None) == "FAULT" else "IDLE")

    def stop(self) -> None:
        self.stop_event.set()
        if self.state in ("LIVE", "ACQUIRING"):
            self.emit("state", "STOPPING")
            if self.camera is not None:
                self.camera.cancel()

    def close(self) -> None:
        self.stop()
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.camera is not None and self.state in ("IDLE", "FAULT"):
            self.disconnect()
