"""Small great-circle geometry helpers shared by capture/matching stages."""

from __future__ import annotations

import math
from typing import Optional

_EARTH_RADIUS_M = 6371000.0


def haversine_distance_m(
    lat1: Optional[float], lon1: Optional[float], lat2: Optional[float], lon2: Optional[float]
) -> Optional[float]:
    if None in (lat1, lon1, lat2, lon2):
        return None
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def angle_delta_deg(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Smallest absolute difference between two angles in degrees, wrapped to [0, 180]."""
    if a is None or b is None:
        return None
    diff = (a - b) % 360.0
    if diff > 180.0:
        diff = 360.0 - diff
    return diff
