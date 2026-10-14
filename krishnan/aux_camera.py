"""Independent sample-view camera contract and hardware-free implementation.

The LAN device model and protocol have not yet been identified. A future
transport adapter implements this interface without changing the GUI or the
Andor spectroscopy worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import time

import numpy as np


@dataclass(frozen=True)
class CameraFrame:
    rgb: np.ndarray
    sequence: int
    timestamp: float


@dataclass(frozen=True)
class CameraSettings:
    exposure_ms: float = 30.0
    gain: float = 1.0

    def __post_init__(self) -> None:
        if not (np.isfinite(self.exposure_ms) and 0.1 <= self.exposure_ms <= 10_000):
            raise ValueError("Camera exposure must be 0.1–10000 ms")
        if not (np.isfinite(self.gain) and 0.1 <= self.gain <= 32):
            raise ValueError("Camera gain must be 0.1–32×")


class AuxiliaryCamera(Protocol):
    def open(self) -> None: ...
    def configure(self, settings: CameraSettings) -> None: ...
    def start(self) -> None: ...
    def read_frame(self, timeout_s: float = 1.0) -> CameraFrame: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class SimulatedAuxiliaryCamera:
    """Deterministic moving sample image for testing the separate camera tab."""

    def __init__(self, width: int = 640, height: int = 480):
        self.width, self.height = width, height
        self.settings = CameraSettings()
        self.sequence = 0
        self.running = False
        self.opened = False
        y, x = np.mgrid[:height, :width]
        self.x, self.y = x.astype(np.float32), y.astype(np.float32)

    def open(self) -> None:
        self.opened = True

    def configure(self, settings: CameraSettings) -> None:
        if not self.opened or self.running:
            raise RuntimeError("Configure the camera while it is open and stopped")
        self.settings = settings

    def start(self) -> None:
        if not self.opened:
            raise RuntimeError("Open the camera first")
        self.running = True

    def read_frame(self, timeout_s: float = 1.0) -> CameraFrame:
        if not self.running:
            raise RuntimeError("Camera is stopped")
        time.sleep(min(0.10, max(0.001, timeout_s)))
        self.sequence += 1
        x, y = self.x, self.y
        phase = self.sequence * 0.045
        cx = self.width * (0.50 + 0.14 * np.sin(phase))
        cy = self.height * (0.52 + 0.10 * np.cos(phase * 0.7))
        spot = np.exp(-(((x - cx) / 55) ** 2 + ((y - cy) / 42) ** 2) / 2)
        rings = 0.5 + 0.5 * np.cos(np.hypot(x - self.width / 2, y - self.height / 2) / 11)
        exposure_scale = min(3.0, self.settings.exposure_ms / 30.0)
        base = 28 + 15 * rings + 180 * spot * exposure_scale * min(2, self.settings.gain)
        rgb = np.stack((base * 0.92, base * 0.98, base * 1.04), axis=2)
        return CameraFrame(np.clip(rgb, 0, 255).astype(np.uint8), self.sequence,
                           time.time())

    def stop(self) -> None:
        self.running = False

    def close(self) -> None:
        self.running = False
        self.opened = False


@dataclass(frozen=True)
class LanCameraProfile:
    """Identification required before selecting a real LAN transport adapter."""

    manufacturer: str = ""
    model: str = ""
    address: str = ""
    sdk_or_cti_path: str = ""

    @property
    def ready_for_driver_selection(self) -> bool:
        return bool(self.manufacturer.strip() and self.model.strip()
                    and self.sdk_or_cti_path.strip())
