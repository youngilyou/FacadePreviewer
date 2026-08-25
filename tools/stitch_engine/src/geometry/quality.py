"""Homography quality gate (CLAUDE.local.md #9)."""

from __future__ import annotations

from src.common.config import Config
from src.common.types import GeometryFailureCode, GeometryResult


def apply_quality_gate(result: GeometryResult, cfg: Config) -> GeometryResult:
    """Return `result` with `.status` set to "OK" or a GeometryFailureCode.

    Does not mutate the RANSAC fit — only decides pass/fail so gate
    thresholds can be retuned without recomputing homographies.
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

    result.status = "OK"
    return result
