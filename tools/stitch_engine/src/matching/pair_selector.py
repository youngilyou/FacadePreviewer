"""Candidate image-pair selection (CLAUDE.local.md #7).

Full N^2 matching is explicitly forbidden (#43.4). Candidates come from two
cheap sources instead:
  1. temporal neighbors within the same flight (sequential capture order)
  2. spatial proximity via a KD-tree radius query (O(N log N), not O(N^2))
Both are filtered by a view-angle-delta gate before being kept.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.spatial import cKDTree

from src.common.config import Config
from src.common.geo import angle_delta_deg
from src.common.types import ImageMetadata, PairCandidate


def _local_enu_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Small-area equirectangular projection (meters), good enough for a KD-tree radius query."""
    r = 6371000.0
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * r
    y = math.radians(lat - lat0) * r
    return x, y


def select_pairs(catalog: list[ImageMetadata], cfg: Config) -> list[PairCandidate]:
    mcfg = cfg.matching
    k = int(mcfg.temporal_neighbor_count)
    max_gps_m = float(mcfg.max_gps_distance_m)
    max_view_deg = float(mcfg.max_view_angle_delta_deg)

    pairs: dict[tuple[str, str], PairCandidate] = {}

    def add(a: ImageMetadata, b: ImageMetadata, reason: str) -> None:
        gps_dist = _gps_distance(a, b)
        view_delta = angle_delta_deg(a.gimbal_pose.yaw_deg, b.gimbal_pose.yaw_deg)
        if gps_dist is not None and gps_dist > max_gps_m:
            return
        if view_delta is not None and view_delta > max_view_deg:
            return
        key = tuple(sorted((a.image_id, b.image_id)))
        if key in pairs:
            return
        pairs[key] = PairCandidate(
            image_a=key[0],
            image_b=key[1],
            gps_distance_m=gps_dist,
            view_angle_delta_deg=view_delta,
            reason=reason,
        )

    # 1) temporal neighbors, grouped by flight and sorted by image_id
    by_flight: dict[str, list[ImageMetadata]] = {}
    for meta in catalog:
        by_flight.setdefault(meta.mission.flight_id or "", []).append(meta)
    for images in by_flight.values():
        images.sort(key=lambda m: m.image_id)
        for i, meta in enumerate(images):
            for j in range(i + 1, min(i + 1 + k, len(images))):
                add(meta, images[j], "temporal_neighbor")

    # 2) spatial proximity via KD-tree (skips images without GPS)
    located = [m for m in catalog if m.gps.latitude is not None and m.gps.longitude is not None]
    if len(located) >= 2:
        lat0 = sum(m.gps.latitude for m in located) / len(located)
        lon0 = sum(m.gps.longitude for m in located) / len(located)
        xy = np.array([_local_enu_xy(m.gps.latitude, m.gps.longitude, lat0, lon0) for m in located])
        tree = cKDTree(xy)
        neighbor_pairs = tree.query_pairs(r=max_gps_m)
        for i, j in neighbor_pairs:
            add(located[i], located[j], "gps_proximity")

    return list(pairs.values())


def _gps_distance(a: ImageMetadata, b: ImageMetadata):
    from src.common.geo import haversine_distance_m

    return haversine_distance_m(a.gps.latitude, a.gps.longitude, b.gps.latitude, b.gps.longitude)
