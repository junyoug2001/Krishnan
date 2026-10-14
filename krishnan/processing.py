"""Reviewable derived corrections; source arrays remain unchanged."""

from __future__ import annotations

import numpy as np


def cosmic_candidates(values: np.ndarray, *, threshold_sigma: float = 8.0) -> list[int]:
    """Find isolated positive one-pixel spikes for operator review.

    A candidate is never corrected automatically. This intentionally
    conservative detector is distinct from Venkata's merged-run comparison.
    """
    y = np.asarray(values, dtype=float)
    if y.ndim != 1 or y.size < 7 or not np.isfinite(y).all():
        raise ValueError("Cosmic review needs a finite 1D spectrum")
    baseline = np.median(np.stack([np.roll(y, i) for i in (-2, -1, 0, 1, 2)]), axis=0)
    residual = y - baseline
    median = np.median(residual[2:-2])
    sigma = 1.4826 * np.median(np.abs(residual[2:-2] - median))
    if sigma <= 0:
        sigma = 1.0
    result = []
    for index in range(2, len(y) - 2):
        if residual[index] > threshold_sigma * sigma and (
            y[index] > max(y[index - 1], y[index + 1])):
            result.append(index)
    return result


def correct_cosmics(values: np.ndarray, indices: list[int]) -> np.ndarray:
    raw = np.asarray(values)
    result = np.asarray(raw, dtype=float).copy()
    chosen = sorted(set(indices))
    for index in chosen:
        if not 0 < index < len(result) - 1:
            raise ValueError("Cosmic correction index is outside the interior")
        left = index - 1
        right = index + 1
        while left in chosen:
            left -= 1
        while right in chosen:
            right += 1
        if left < 0 or right >= len(result):
            raise ValueError("Cosmic cluster has no neighbors")
        result[index] = raw[left] + (raw[right] - raw[left]) * (index - left) / (right - left)
    return result
