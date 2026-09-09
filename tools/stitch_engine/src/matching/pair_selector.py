"""Candidate image-pair selection (CLAUDE.local.md #7).

Full N^2 matching is explicitly forbidden (#43.4). Candidates come from two
cheap sources instead:
  1. temporal neighbors within the same flight (sequential capture order)
  2. spatial proximity via a KD-tree radius query (O(N log N), not O(N^2))
Both are filtered by a view-angle-delta gate before being kept.

Distance here is full 3D (horizontal GPS + altitude), not horizontal-only.
A bottom-to-top zigzag facade capture moves mainly in ALTITUDE, not
horizontal position -- a real run had 168 images spanning 35.5m of altitude
but with most shots landing within a couple of meters of each other
*horizontally* (the drone barely moves sideways while stepping up a floor).
Horizontal-only distance was measuring the wrong thing: of the pairs it
called "close" (<1m horizontal), 82% actually differed by >5m in altitude --
different floors of the building, entirely non-overlapping, but confidently
offered up as match candidates anyway. Against a facade with a repeating
window/balcony pattern, that is not a harmless inefficiency: it is directly
handing the matcher pairs where a wrong-floor false match looks just as
plausible as a right-floor true one (2026-09-08 CLAUDE.local.md entry).

The spatial-proximity radius is auto-calibrated per capture from that
capture's OWN nearest-neighbor 3D distance distribution (see
_auto_calibrate_radius), not a single fixed constant -- deliberately, since
today's GPS is plain consumer-grade (a real run's own nearest-neighbor
spacing already varies with capture density/altitude precision) and an RTK
GPS upgrade (centimeter-level) is planned for later. RTK will simply make
the measured nearest-neighbor distances tighter and more consistent, which
this auto-calibration picks up on its own -- the same "trust roughly 3x the
typical real neighbor spacing" margin holds either way, with no code or
config change needed when RTK lands. cfg.matching.max_gps_distance_m still
exists as the clamp/fallback (upper bound on the auto-computed radius, and
the value used outright if there isn't enough located imagery to calibrate
from) -- see 2026-09-08 CLAUDE.local.md entry.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.spatial import cKDTree

from src.common.config import Config
from src.common.geo import angle_delta_deg
from src.common.types import ImageMetadata, PairCandidate


def _local_enu_xyz(lat: float, lon: float, alt_m: float | None, lat0: float, lon0: float) -> tuple[float, float, float]:
    """Small-area equirectangular projection (meters) plus altitude, good enough for a KD-tree
    radius query. alt_m=None (no altitude on this image) falls back to 0.0 -- consistent with
    _gps_distance_3d likewise degrading to horizontal-only when either side lacks altitude,
    rather than silently excluding images that have GPS but not altitude."""
    r = 6371000.0
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * r
    y = math.radians(lat - lat0) * r
    return x, y, float(alt_m) if alt_m is not None else 0.0


def _auto_calibrate_radius(
    tree: "cKDTree", num_points: int, fallback_m: float, multiplier: float = 3.0,
) -> float:
    """Derive the spatial-proximity radius from THIS capture's own nearest-neighbor 3D
    distance distribution instead of trusting one fixed constant to fit every capture
    (see module docstring for why -- GPS quality today vs. after the planned RTK
    upgrade, and capture density/altitude precision both vary run to run).

    Uses the 90th percentile of each point's nearest-neighbor distance (robust to a
    handful of genuinely isolated shots skewing a mean/max) times a fixed multiplier
    (3x -- verified empirically on a real 168-image capture: true neighbor spacing
    clustered under ~2.5m at p90, while different-floor pairs started at 5m+, so 3x
    lands solidly inside that gap without needing to retune per capture). Clamped to
    fallback_m as an upper bound, so a sparse or oddly-shaped capture can't calibrate
    itself into an unreasonably large radius that re-admits the cross-floor pairs this
    whole change exists to exclude.
    """
    if num_points < 2:
        return fallback_m
    dists, _ = tree.query(tree.data, k=2)
    nn_dist = dists[:, 1]
    calibrated = float(np.percentile(nn_dist, 90)) * multiplier
    return min(calibrated, fallback_m)


def select_pairs(catalog: list[ImageMetadata], cfg: Config) -> list[PairCandidate]:
    mcfg = cfg.matching
    k = int(mcfg.temporal_neighbor_count)
    fallback_gps_m = float(mcfg.max_gps_distance_m)
    max_view_deg = float(mcfg.max_view_angle_delta_deg)

    # Build the 3D KD-tree (and auto-calibrate the radius from it) up front -- both the
    # temporal-neighbor pass and the spatial-proximity pass below gate on this same radius,
    # so it needs to exist before either one runs, not just before step 2 as before.
    located = [m for m in catalog if m.gps.latitude is not None and m.gps.longitude is not None]
    tree = None
    max_gps_m = fallback_gps_m
    if len(located) >= 2:
        lat0 = sum(m.gps.latitude for m in located) / len(located)
        lon0 = sum(m.gps.longitude for m in located) / len(located)
        xyz = np.array([
            _local_enu_xyz(m.gps.latitude, m.gps.longitude, m.gps.altitude_m, lat0, lon0)
            for m in located
        ])
        tree = cKDTree(xyz)
        max_gps_m = _auto_calibrate_radius(tree, len(located), fallback_gps_m)

    pairs: dict[tuple[str, str], PairCandidate] = {}

    def add(a: ImageMetadata, b: ImageMetadata, reason: str, require_distance_check: bool = True) -> None:
        gps_dist = _gps_distance_3d(a, b)
        view_delta = angle_delta_deg(a.gimbal_pose.yaw_deg, b.gimbal_pose.yaw_deg)
        # Temporal neighbors (reason="temporal_neighbor") skip this -- see the call site below
        # for why. gps_dist is still computed and stored either way, purely as metadata.
        if require_distance_check and gps_dist is not None and gps_dist > max_gps_m:
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

    # 1) temporal neighbors, grouped by flight and sorted by image_id. NOT gated on GPS distance
    # (require_distance_check=False) -- these exist specifically to guarantee the whole capture
    # stays connected regardless of GPS noise or the auto-calibrated radius above. A real run
    # showed why that guarantee matters: with the distance check also applied here, a zigzag
    # pass-to-pass transition (bigger jump between the end of one vertical pass and the start of
    # the next than the auto-calibrated "same-floor" threshold) severed the temporal chain at
    # every such transition, splitting 168 images into 20 disconnected islands -- only a ~28-image
    # band near the reference stayed reachable, everything before/after in capture order did not.
    # Consecutive shots in a systematic photogrammetry capture overlap by construction (that's the
    # point of flying a planned pattern), so capture order alone is trusted here; view-angle is
    # still checked (a real yaw change between shots, e.g. repositioning at a pass turnaround, is
    # a different signal than "physically far apart" and can still mean genuinely no overlap).
    by_flight: dict[str, list[ImageMetadata]] = {}
    for meta in catalog:
        by_flight.setdefault(meta.mission.flight_id or "", []).append(meta)
    for images in by_flight.values():
        images.sort(key=lambda m: m.image_id)
        for i, meta in enumerate(images):
            for j in range(i + 1, min(i + 1 + k, len(images))):
                add(meta, images[j], "temporal_neighbor", require_distance_check=False)

    # 2) spatial proximity via KD-tree (skips images without GPS), 3D (horizontal + altitude)
    if tree is not None:
        neighbor_pairs = tree.query_pairs(r=max_gps_m)
        for i, j in neighbor_pairs:
            add(located[i], located[j], "gps_proximity")

    return list(pairs.values())


def _gps_distance_3d(a: ImageMetadata, b: ImageMetadata):
    """Horizontal (haversine) + altitude combined via Euclidean sum -- see this module's
    docstring for why altitude can't be dropped for a facade capture. Falls back to
    horizontal-only when either image lacks altitude, rather than refusing to compare them."""
    from src.common.geo import haversine_distance_m

    horiz = haversine_distance_m(a.gps.latitude, a.gps.longitude, b.gps.latitude, b.gps.longitude)
    if horiz is None:
        return None
    if a.gps.altitude_m is None or b.gps.altitude_m is None:
        return horiz
    vert = abs(a.gps.altitude_m - b.gps.altitude_m)
    return math.hypot(horiz, vert)
