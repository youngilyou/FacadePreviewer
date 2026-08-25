"""RANSAC homography estimation (CLAUDE.local.md #2.1, #9).

Uses Kornia's RANSAC (the doc assigns RANSAC/Homography to the Kornia core,
OpenCV stays for image utility/blend/fallback — #2.1/#2.2).
"""

from __future__ import annotations

import numpy as np
import torch
from kornia.geometry.ransac import RANSAC

from src.common.types import GeometryFailureCode, GeometryResult, MatchResult


def estimate_homography(
    match: MatchResult,
    inl_th_px: float = 4.0,
    max_iter: int = 2000,
    confidence: float = 0.99,
    device: str | None = None,
) -> GeometryResult:
    """Estimate H mapping image_a -> image_b from a MatchResult's raw correspondences.

    Only geometric fit is done here; pass/fail against the quality gate
    (#9 thresholds) is decided separately in quality.py so gate tuning never
    requires re-running RANSAC.
    """
    num_matches = match.num_matches
    if num_matches == 0:
        return GeometryResult(
            image_a=match.image_a,
            image_b=match.image_b,
            status=GeometryFailureCode.LOW_MATCH.value,
            num_matches=0,
        )

    dev = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
    kp_a = torch.from_numpy(np.asarray(match.keypoints_a, dtype=np.float32)).to(dev)
    kp_b = torch.from_numpy(np.asarray(match.keypoints_b, dtype=np.float32)).to(dev)

    ransac = RANSAC(model_type="homography", inl_th=inl_th_px, max_iter=max_iter, confidence=confidence)
    try:
        H, inlier_mask = ransac(kp_a, kp_b)
    except Exception:
        return GeometryResult(
            image_a=match.image_a,
            image_b=match.image_b,
            status=GeometryFailureCode.DEGENERATE_HOMOGRAPHY.value,
            num_matches=num_matches,
        )

    H_np = H.detach().cpu().numpy().astype(np.float64)
    inlier_mask_np = inlier_mask.detach().cpu().numpy().reshape(-1).astype(bool)
    num_inliers = int(inlier_mask_np.sum())

    if num_inliers == 0 or not np.isfinite(H_np).all():
        return GeometryResult(
            image_a=match.image_a,
            image_b=match.image_b,
            status=GeometryFailureCode.DEGENERATE_HOMOGRAPHY.value,
            num_matches=num_matches,
            num_inliers=0,
            inlier_ratio=0.0,
        )

    median_err = _median_reprojection_error(
        np.asarray(match.keypoints_a)[inlier_mask_np],
        np.asarray(match.keypoints_b)[inlier_mask_np],
        H_np,
    )

    return GeometryResult(
        image_a=match.image_a,
        image_b=match.image_b,
        status="OK",
        homography=H_np,
        inlier_mask=inlier_mask_np,
        num_matches=num_matches,
        num_inliers=num_inliers,
        inlier_ratio=num_inliers / num_matches,
        median_reprojection_error_px=median_err,
    )


def _median_reprojection_error(pts_a: np.ndarray, pts_b: np.ndarray, H: np.ndarray) -> float:
    ones = np.ones((pts_a.shape[0], 1))
    homog = np.hstack([pts_a, ones])
    projected = homog @ H.T
    projected = projected[:, :2] / projected[:, 2:3]
    errors = np.linalg.norm(projected - pts_b, axis=1)
    return float(np.median(errors))
