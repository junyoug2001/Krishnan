"""Opt-in sample controller contract for future temperature/field/angle devices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ControllerCapabilities:
    axis: str
    unit: str
    minimum: float
    maximum: float
    supports_stabilization: bool


class SampleController(Protocol):
    def identity(self) -> str: ...

    def capabilities(self) -> ControllerCapabilities: ...

    def readback(self) -> float: ...

    def set_target(self, value: float) -> None: ...

    def wait_stable(self, tolerance: float, dwell_s: float, timeout_s: float) -> float: ...

    def safe_stop(self) -> None: ...


_registry: dict[str, type[SampleController]] = {}


def register_controller(name: str, implementation: type[SampleController]) -> None:
    if not name or name in _registry:
        raise ValueError("Controller name must be nonempty and unique")
    _registry[name] = implementation


def available_controllers() -> tuple[str, ...]:
    return tuple(sorted(_registry))
