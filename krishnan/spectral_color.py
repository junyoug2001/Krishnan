"""Approximate visible-light colours at physical wavelength coordinates."""

from __future__ import annotations

import math


DARK_GREY = (51, 55, 60)


def wavelength_rgb(nm: float) -> tuple[int, int, int]:
    """Return a display RGB colour for 380–780 nm, grey outside visible light.

    The piecewise sRGB approximation is intended as a wavelength guide, not
    a radiometric or colour matching calibration of the monitor.
    """
    if not math.isfinite(nm) or nm < 380.0 or nm > 780.0:
        return DARK_GREY
    if nm < 440:
        rgb = (-(nm - 440) / 60, 0.0, 1.0)
    elif nm < 490:
        rgb = (0.0, (nm - 440) / 50, 1.0)
    elif nm < 510:
        rgb = (0.0, 1.0, -(nm - 510) / 20)
    elif nm < 580:
        rgb = ((nm - 510) / 70, 1.0, 0.0)
    elif nm < 645:
        rgb = (1.0, -(nm - 645) / 65, 0.0)
    else:
        rgb = (1.0, 0.0, 0.0)
    if nm < 420:
        attenuation = 0.3 + 0.7 * (nm - 380) / 40
    elif nm > 700:
        attenuation = 0.3 + 0.7 * (780 - nm) / 80
    else:
        attenuation = 1.0
    return tuple(round(255 * (max(0.0, min(1.0, c)) * attenuation) ** 0.8)
                 for c in rgb)
