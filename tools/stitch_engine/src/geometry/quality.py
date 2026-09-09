"""Homography quality gate (CLAUDE.local.md #9)."""

from __future__ import annotations

import numpy as np

from src.common.config import Config
from src.common.types import GeometryFailureCode, GeometryResult, ImageMetadata
from src.geometry.facade_period import PeriodEstimate


def _implied_vertical_shift_px(homography: np.ndarray, width: int, height: int) -> float:
    """How far image A's own center point moves vertically once mapped
    through the homography into B's frame -- the same measurement used to
    empirically derive/validate this check (see 2026-09-08/09 CLAUDE.local.md
    entry). Uses the actual homography (not just its raw translation term)
    so rotation/perspective in the fit is accounted for, not just translation.
    """
    p = homography @ np.array([width / 2.0, height / 2.0, 1.0])
    cy = p[1] / p[2]
    return float(cy - height / 2.0)


def _gps_pose_consistent(
    result: GeometryResult, meta_a: ImageMetadata | None, meta_b: ImageMetadata | None,
    min_dz_m: float = 2.0, min_shift_px: float = 100.0,
) -> bool:
    """Sign-only cross-check: a level camera captured higher up must see any
    fixed wall point lower in ITS OWN frame -- purely geometric, holds
    regardless of flight direction (ascending vs descending pass) or which
    of the two images was captured first, since it only depends on the two
    cameras' actual relative altitude (see 2026-09-08/09 CLAUDE.local.md
    entry). Deliberately sign-only, not magnitude -- the real capture showed
    up to ~17x variation in apparent-shift-per-meter-of-altitude between
    different height bands (likely drone standoff-distance drift over a long
    flight, not confirmed), so a magnitude/scale check isn't safe to enforce
    yet without falsely rejecting good matches; sign disagreement has no such
    ambiguity.

    min_dz_m=2.0 and min_shift_px=100.0 aren't arbitrary -- a real capture's
    OWN good (OK-status) edges showed ~216px residual std around the
    dz->shift trend line even for genuinely consistent matches (homography
    estimation noise, not a bug), so checking small dz/small implied shift
    pairs would mostly just be flagging that noise, not real inconsistency.
    Both thresholds sit comfortably above that noise floor -- at dz=2m the
    expected shift (~112px/m, from that same real capture) is already
    similar in size to the noise std, so this stays conservative by design;
    it should catch only unambiguous, large disagreements, e.g. what an
    actual wrong-floor aliasing match would look like, not borderline cases.

    Returns True (allow) whenever there isn't enough information to check
    (no metadata, no altitude, altitude difference or implied shift too
    small to have a reliable sign) -- this check should only ever REJECT on
    a clear disagreement, never withhold a pass for lack of data.
    """
    if meta_a is None or meta_b is None:
        return True
    if meta_a.gps.altitude_m is None or meta_b.gps.altitude_m is None:
        return True
    dz = meta_b.gps.altitude_m - meta_a.gps.altitude_m
    if abs(dz) < min_dz_m:
        return True
    width = meta_a.width or 5280
    height = meta_a.height or 3956
    ty = _implied_vertical_shift_px(result.homography, width, height)
    if abs(ty) < min_shift_px:
        return True
    return (dz > 0) == (ty > 0)


def _floor_count_consistent(
    result: GeometryResult, meta_a: ImageMetadata | None, meta_b: ImageMetadata | None,
    period_a: "PeriodEstimate | None",
    real_floor_height_m: float = 2.51, min_confidence: float = 0.6,
    min_dz_m: float = 1.0, max_floor_disagreement: float = 0.75,
) -> bool:
    """Magnitude-aware sibling of `_gps_pose_consistent` -- the scale check
    that was deliberately deferred there (2026-09-08/09 entry: a real
    capture showed ~17x variation in raw apparent-shift-per-meter, so no
    single global px/m scale was safe to enforce). `period_a`
    (src/geometry/facade_period.py) sidesteps that: instead of one global
    scale for the whole capture, it measures THIS SPECIFIC image's own
    facade repeat-period directly from its own pixel content, so the
    "floors apart" conversion is self-calibrating per image rather than
    assuming one capture-wide constant.

    real_floor_height_m=2.51 comes from the vertical-autocorrelation
    measurement on this capture's own rectified COLMAP mosaic (2026-09-09
    entry) -- a real, verified number for this specific building, not a
    generic assumption; a different building would need its own value.

    Compares expected_floors (from GPS dz / real_floor_height_m) against
    observed_floors (from the homography's implied shift / this image's own
    measured period) -- these are two INDEPENDENT estimates of "how many
    floors apart are these two shots" (one from GPS, one from image
    content), and should agree. A disagreement of a full floor or more is
    exactly what a wrong-floor aliasing match looks like, even when
    _gps_pose_consistent's sign check alone would pass it.

    Returns True (allow) whenever there isn't enough information or
    confidence to check -- only ever rejects on a clear, large disagreement.
    """
    if period_a is None or period_a.period_px is None or period_a.confidence < min_confidence:
        return True
    if meta_a is None or meta_b is None:
        return True
    if meta_a.gps.altitude_m is None or meta_b.gps.altitude_m is None:
        return True
    dz = meta_b.gps.altitude_m - meta_a.gps.altitude_m
    if abs(dz) < min_dz_m:
        return True
    width = meta_a.width or 5280
    height = meta_a.height or 3956
    ty = _implied_vertical_shift_px(result.homography, width, height)
    expected_floors = dz / real_floor_height_m
    observed_floors = ty / period_a.period_px
    return abs(expected_floors - observed_floors) <= max_floor_disagreement


def apply_quality_gate(
    result: GeometryResult, cfg: Config,
    meta_a: ImageMetadata | None = None, meta_b: ImageMetadata | None = None,
    period_a: "PeriodEstimate | None" = None,
) -> GeometryResult:
    """Return `result` with `.status` set to "OK" or a GeometryFailureCode.

    Does not mutate the RANSAC fit — only decides pass/fail so gate
    thresholds can be retuned without recomputing homographies.

    `meta_a`/`meta_b` are optional (existing callers/tests that don't pass
    them just skip the GPS pose-consistency check below) -- when given, they
    let this catch an internally-consistent-but-wrong-floor inlier set that
    every other check above accepts. `period_a` (optional) additionally
    enables the magnitude-aware floor-count check -- see
    `_floor_count_consistent`.
    """
    if result.status != "OK" and result.status != GeometryFailureCode.DEGENERATE_HOMOGRAPHY.value:
        # status already carries a pre-RANSAC failure (e.g. LOW_MATCH from zero matches)
        return result
    if result.status == GeometryFailureCode.DEGENERATE_HOMOGRAPHY.value:
        return result

    gcfg = cfg.geometry
    if result.num_matches < int(gcfg.min_matches):
        result.status = GeometryFailureCode.LOW_MATCH.value
        return result
    if result.num_inliers < int(gcfg.min_inliers) or result.inlier_ratio < float(gcfg.min_inlier_ratio):
        result.status = GeometryFailureCode.LOW_INLIER.value
        return result
    max_err = float(gcfg.max_median_reprojection_error_px)
    if result.median_reprojection_error_px is None or result.median_reprojection_error_px > max_err:
        result.status = GeometryFailureCode.HIGH_REPROJECTION_ERROR.value
        return result
    if not _gps_pose_consistent(result, meta_a, meta_b):
        result.status = GeometryFailureCode.POSE_INCONSISTENT.value
        return result
    if not _floor_count_consistent(result, meta_a, meta_b, period_a):
        result.status = GeometryFailureCode.FLOOR_COUNT_MISMATCH.value
        return result

    result.status = "OK"
    return result
