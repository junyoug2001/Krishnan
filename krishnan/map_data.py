"""Build selected map and slices with Venkata's spectral/row conventions."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class MapData:
    wavelength_nm: np.ndarray
    rows: np.ndarray
    intensity: np.ndarray
    row_labels: tuple[str, ...]
    is_angular: bool


def build_map(saved: list[dict]) -> MapData | None:
    spectra = [item for item in saved if item.get("step") is not None
               and np.asarray(item["data"]).ndim == 1]
    if not spectra:
        return None
    axis = np.asarray(spectra[0]["axis"], dtype=float)
    if axis.ndim != 1 or axis.size < 2 or not np.isfinite(axis).all():
        return None
    compatible = [item for item in spectra
                  if np.asarray(item["axis"]).shape == axis.shape
                  and np.allclose(item["axis"], axis, atol=0.01, rtol=0)]
    if not compatible:
        return None
    matrix = np.vstack([np.asarray(item["data"], dtype=float) for item in compatible])
    angles = [item["step"].angle_deg for item in compatible]
    angular = (all(angle is not None and np.isfinite(angle) for angle in angles)
               and len(set(angles)) == len(angles))
    if angular:
        order = np.argsort(angles, kind="stable")
        matrix = matrix[order]
        rows = np.asarray(angles, dtype=float)[order]
        labels = tuple(f"{value:g}°" for value in rows)
    else:
        rows = np.arange(1, len(compatible) + 1, dtype=float)
        labels = tuple(item["step"].name for item in compatible)
    return MapData(axis.copy(), rows, matrix, labels, angular)


def slice_window(center: int, width: int, size: int) -> slice:
    width = max(1, int(width))
    center = max(0, min(int(center), size - 1))
    return slice(max(0, center - (width - 1) // 2),
                 min(size, center + width // 2 + 1))


def map_slices(matrix: np.ndarray, ix: int, iy: int, x_bin: int = 1,
               y_bin: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """Return row and spectral profiles at selected map pixel."""
    matrix = np.asarray(matrix, dtype=float)
    if matrix.ndim != 2 or not matrix.size:
        raise ValueError("A nonempty 2D map is required")
    if not (0 <= ix < matrix.shape[1] and 0 <= iy < matrix.shape[0]):
        raise IndexError("Map selection outside data")

    def finite_mean(values: np.ndarray, axis: int) -> np.ndarray:
        valid = np.isfinite(values)
        counts = valid.sum(axis=axis)
        total = np.where(valid, values, 0.0).sum(axis=axis)
        result = np.full(total.shape, np.nan, dtype=float)
        np.divide(total, counts, out=result, where=counts > 0)
        return result

    row = finite_mean(matrix[:, slice_window(ix, x_bin, matrix.shape[1])], 1)
    spectral = finite_mean(matrix[slice_window(iy, y_bin, matrix.shape[0]), :], 0)
    return row, spectral
