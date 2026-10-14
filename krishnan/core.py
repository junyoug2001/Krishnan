"""Session persistence, sequence planning, scan processing and Venkata export."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import os
import re
import uuid

import numpy as np

from .bootstrap import prepare_paths

prepare_paths()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    if not result:
        raise ValueError("Enter a sample or step name")
    return result[:80]


@dataclass(frozen=True)
class Step:
    id: str
    index: int
    name: str
    center_nm: float | None = None
    grating: int | None = None
    angle_deg: float | None = None
    exposure_s: float = 0.1
    cycle_s: float = 0.0
    accumulations: int = 1

    def __post_init__(self) -> None:
        clean_name(self.name)
        if self.index < 1 or self.accumulations < 1:
            raise ValueError("Step index and accumulations must be positive")
        if not math.isfinite(self.exposure_s) or self.exposure_s <= 0:
            raise ValueError("Exposure must be finite and positive")
        if not math.isfinite(self.cycle_s) or self.cycle_s < 0:
            raise ValueError("Cycle must be finite and nonnegative")
        for value in (self.center_nm, self.angle_deg):
            if value is not None and not math.isfinite(value):
                raise ValueError("Scan coordinates must be finite")

    @property
    def stem(self) -> str:
        return f"{self.index:04d}_{clean_name(self.name)}_{self.id[:8]}"


def repeated_steps(name: str, count: int, exposure_s: float, cycle_s: float = 0.0,
                   accumulations: int = 1, centers: list[float] | None = None,
                   angles: list[float] | None = None) -> list[Step]:
    if not 1 <= count <= 100_000:
        raise ValueError("Count must be between 1 and 100000")
    if centers is not None and len(centers) != count:
        raise ValueError("Wavelength list must match step count")
    if angles is not None and len(angles) != count:
        raise ValueError("Angle list must match step count")
    return [
        Step(str(uuid.uuid4()), i + 1, name,
             center_nm=None if centers is None else centers[i],
             angle_deg=None if angles is None else angles[i],
             exposure_s=exposure_s, cycle_s=cycle_s, accumulations=accumulations)
        for i in range(count)
    ]


def step_glue_centers(start_nm: float, end_nm: float, current_axis_nm: np.ndarray,
                      limits_nm: tuple[float, float], overlap: float = 0.10) -> list[float]:
    """Plan centers from current dispersion; actual tile axes are checked after moves."""
    axis = np.asarray(current_axis_nm, dtype=float)
    if axis.ndim != 1 or axis.size < 2 or not np.isfinite(axis).all():
        raise ValueError("A finite calibrated CCD axis is required")
    if not (math.isfinite(start_nm) and math.isfinite(end_nm) and
            start_nm < end_nm and 0 < overlap < 0.8):
        raise ValueError("Invalid scan range or overlap")
    lo, hi = sorted((float(axis[0]), float(axis[-1])))
    span = hi - lo
    if span <= 0:
        raise ValueError("Calibration has zero span")
    half = span / 2
    low_center, high_center = limits_nm
    if start_nm < low_center - half or end_nm > high_center + half:
        raise ValueError("Requested span is outside grating reach")
    first = max(low_center, min(high_center, start_nm + half))
    last = max(low_center, min(high_center, end_nm - half))
    stride = span * (1 - overlap)
    centers = [first]
    while centers[-1] + half < end_nm - 1e-9:
        candidate = min(last, centers[-1] + stride)
        if candidate <= centers[-1] + 1e-9:
            raise ValueError("The requested range cannot be covered")
        centers.append(candidate)
        if len(centers) > 10000:
            raise ValueError("Too many scan tiles")
    return centers


def stitch_tiles(tiles: list[tuple[np.ndarray, np.ndarray]],
                 start_nm: float, end_nm: float) -> tuple[np.ndarray, np.ndarray]:
    """Blend adjacent calibrated tiles on a common wavelength grid."""
    if not tiles:
        raise ValueError("No tiles to stitch")
    prepared = []
    pitches = []
    for axis, values in tiles:
        x, y = np.asarray(axis, dtype=float), np.asarray(values, dtype=float)
        if x.ndim != 1 or y.shape != x.shape or x.size < 2:
            raise ValueError("Each tile must contain one matching spectrum")
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ValueError("Tiles must be finite")
        order = np.argsort(x)
        x, y = x[order], y[order]
        if np.any(np.diff(x) <= 0):
            raise ValueError("Tile wavelength axis must be unique")
        pitches.append(float(np.median(np.diff(x))))
        prepared.append((x, y))
    prepared.sort(key=lambda pair: pair[0][0])
    for (left, _), (right, _) in zip(prepared, prepared[1:]):
        if right[0] >= left[-1]:
            raise ValueError("Adjacent scan tiles have no overlap")
    pitch = max(pitches)
    grid = np.arange(start_nm, end_nm + pitch * 0.25, pitch, dtype=float)
    grid = grid[grid <= end_nm + 1e-9]
    accum = np.zeros_like(grid)
    weights = np.zeros_like(grid)
    for i, (x, y) in enumerate(prepared):
        mask = (grid >= x[0]) & (grid <= x[-1])
        w = np.ones(mask.sum(), dtype=float)
        gx = grid[mask]
        if i > 0:
            previous = prepared[i - 1][0]
            a, b = x[0], min(previous[-1], x[-1])
            if b > a:
                w *= np.clip((gx - a) / (b - a), 0, 1)
        if i + 1 < len(prepared):
            next_axis = prepared[i + 1][0]
            a, b = next_axis[0], min(x[-1], next_axis[-1])
            if b > a:
                w *= np.clip((b - gx) / (b - a), 0, 1)
        accum[mask] += np.interp(gx, x, y) * w
        weights[mask] += w
    if np.any(weights <= 0):
        raise ValueError("Scan has uncovered wavelengths")
    return grid, accum / weights


class Session:
    """Versioned, atomic manifest. Complete steps are immutable on resume."""

    def __init__(self, folder: str | Path, *, sample: str | None = None,
                 laser_nm: float | None = None, polarization: str = ""):
        self.folder = Path(folder).expanduser().resolve()
        self.path = self.folder / "session.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("schema_version") != 1:
                raise ValueError("Unsupported Krishnan session version")
        else:
            if sample is None:
                raise ValueError("Sample is required for a new session")
            if laser_nm is not None and (not math.isfinite(laser_nm) or laser_nm <= 0):
                raise ValueError("Laser wavelength must be positive")
            self.folder.mkdir(parents=True, exist_ok=True)
            self.data = {
                "schema_version": 1, "id": str(uuid.uuid4()), "created_utc": utc_now(),
                "sample": sample, "laser_nm": laser_nm, "polarization": polarization,
                "device": None, "steps": [], "derived": [], "orphans": [], "notes": "",
            }
            self._write()
        for part in ("raw", "calibration", "derived", "exports"):
            (self.folder / part).mkdir(exist_ok=True)

    def _write(self) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def set_device(self, camera, optics, capabilities: dict | None = None) -> None:
        self.data["device"] = {
            "camera": asdict(camera.identity),
            "spectrograph": {"serial": optics.serial, "backend": optics.backend},
            "capabilities": capabilities or {},
        }
        self._write()

    def add_steps(self, steps: list[Step]) -> None:
        known = {item["id"] for item in self.data["steps"]}
        paths = {item["stem"] for item in self.data["steps"]}
        for step in steps:
            if step.id in known or step.stem in paths:
                raise ValueError("Duplicate step ID or filename")
            if any((self.folder / "raw" / (step.stem + suffix)).exists()
                   for suffix in (".sif", ".npz", ".partial.sif")):
                raise FileExistsError(step.stem)
        self.data["steps"].extend({
            **asdict(step), "stem": step.stem, "state": "planned",
            "requested": None, "applied": None, "raw": None,
            "calibration": None, "failure": None, "started_utc": None,
            "ended_utc": None,
        } for step in steps)
        self._write()

    def update(self, step_id: str, state: str, **fields) -> dict:
        item = next((entry for entry in self.data["steps"] if entry["id"] == step_id), None)
        if item is None:
            raise KeyError(step_id)
        if item["state"] == "complete":
            raise ValueError("Completed raw steps cannot be changed")
        item["state"] = state
        item.update(fields)
        if state in ("complete", "failed", "interrupted"):
            item["ended_utc"] = utc_now()
        self._write()
        return item

    def verify_completed(self) -> list[str]:
        missing = []
        for item in self.data["steps"]:
            if item["state"] != "complete":
                continue
            raw = item.get("raw") or {}
            path = self.folder / raw.get("path", "")
            if not path.is_file() or sha256(path) != raw.get("sha256"):
                missing.append(item["id"])
        return missing

    def quarantine_incomplete_files(self) -> list[str]:
        """Preserve orphaned raw data from a crash before reacquiring a step."""
        moved = []
        for item in self.data["steps"]:
            if item["state"] == "complete":
                continue
            for directory, suffix in (
                ("raw", ".sif"), ("raw", ".npz"), ("raw", ".partial.sif"),
                ("calibration", ".json"),
            ):
                source = self.folder / directory / (item["stem"] + suffix)
                if not source.exists():
                    continue
                destination_dir = self.folder / directory / "orphans"
                destination_dir.mkdir(exist_ok=True)
                destination = destination_dir / (
                    item["stem"] + "_" + uuid.uuid4().hex[:8] + suffix)
                os.replace(source, destination)
                relative = str(destination.relative_to(self.folder))
                self.data.setdefault("orphans", []).append({
                    "step_id": item["id"], "path": relative,
                    "reason": "incomplete step before resume", "moved_utc": utc_now(),
                })
                moved.append(relative)
        if moved:
            self._write()
        return moved


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_txt(path: Path, axis: np.ndarray, intensity: np.ndarray,
              *, shift: bool = False) -> None:
    if path.exists():
        raise FileExistsError(path)
    label = "Raman Shift (cm-1)" if shift else "Wavelength (nm)"
    np.savetxt(path, np.column_stack((axis, intensity)), delimiter="\t",
               header=f"{label}\tIntensity (count)", comments="")


def export_venkata(session: Session, measurements: list[tuple[dict, np.ndarray, np.ndarray]],
                   *, selected: set[str] | None = None) -> tuple[list[Path], Path]:
    from data_structure import ExperimentSet, Run, RunType, raman_shift_cm1

    experiment = ExperimentSet(id=session.data["id"], metadata={
        "sample": session.data["sample"], "laser_nm": session.data["laser_nm"],
        "polarization": session.data["polarization"],
        "krishnan_manifest": str(session.path),
    })
    csv_paths = []
    angular = []
    for step, axis, counts in measurements:
        if counts.ndim != 1:
            continue
        laser = session.data["laser_nm"]
        shift = None if laser is None else raman_shift_cm1(laser, axis)
        run = Run(
            id=step["id"], source_path=str(session.folder / step["raw"]["path"]),
            wl_nm=np.asarray(axis), shift_cm1=shift, intensity=np.asarray(counts),
            intensity_unit="count", run_type=RunType.RUN_1D,
            metadata={"nickname": step["stem"], "sample": session.data["sample"],
                      "pol": session.data["polarization"], "krishnan_step": step["id"]},
        )
        experiment.add_run(run)
        if step.get("angle_deg") is not None:
            angular.append((float(step["angle_deg"]), axis, counts, step["id"]))
        if selected is None or step["id"] in selected:
            csv_path = session.folder / "exports" / (step["stem"] + ".csv")
            run.export_csv_1d(base_filepath=str(csv_path))
            csv_paths.append(csv_path)
    if len(angular) >= 2:
        reference = angular[0][1]
        if all(axis.shape == reference.shape and np.allclose(axis, reference, atol=0.01)
               for _, axis, _, _ in angular):
            angular.sort(key=lambda item: item[0])
            laser = session.data["laser_nm"]
            map_run = Run(
                id="angular_map_" + session.data["id"].replace("-", "_"),
                source_path=str(session.path),
                wl_nm=np.asarray(reference),
                shift_cm1=None if laser is None else raman_shift_cm1(laser, reference),
                intensity_2d=np.vstack([item[2] for item in angular]),
                angle_values=np.asarray([item[0] for item in angular]),
                intensity_unit="count", run_type=RunType.RUN_2D,
                metadata={"nickname": "Angular map", "source_steps": [item[3] for item in angular]},
            )
            experiment.add_run(map_run)
    for index, derived in enumerate(session.data.get("derived", [])):
        if derived.get("type") not in ("step-and-glue", "cosmic-correction"):
            continue
        path = session.folder / derived["path"]
        table = np.loadtxt(path, skiprows=1)
        if table.ndim != 2 or table.shape[1] != 2:
            raise ValueError(f"Derived scan is malformed: {path}")
        axis, counts = table[:, 0], table[:, 1]
        laser = session.data["laser_nm"]
        experiment.add_run(Run(
            id=f"derived_{index + 1}_{session.data['id'].replace('-', '_')}",
            source_path=str(path), wl_nm=axis,
            shift_cm1=None if laser is None else raman_shift_cm1(laser, axis),
            intensity=counts, intensity_unit="count", run_type=RunType.RUN_1D,
            metadata={"nickname": f"{derived['type']} {index + 1}",
                      "source_steps": derived["source_steps"],
                      "processing": derived["processing"]},
        ))
    h5_path = session.folder / "exports" / "experiment.h5"
    experiment.export_hdf5(str(h5_path))
    return csv_paths, h5_path
