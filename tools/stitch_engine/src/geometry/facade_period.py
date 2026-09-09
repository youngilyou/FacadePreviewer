"""Vertical repeat-period (floor spacing) estimation from a single raw
facade photo, even under mild perspective -- a from-scratch Python
reimplementation scoped to exactly what this project needs, inspired by
(but much smaller than) the published literature reviewed 2026-09-09:

  - Wenzel/Drauschke/Foerstner 2008: match a facade image's OWN features
    against each other (not against a second image) to find repeat pairs;
    weight candidate pairs by orientation/scale similarity; accumulate
    displacement vectors in a 2D histogram to find dominant translations.
  - Park/Brocklehurst/Collins/Liu 2010: RANSAC-style consensus voting to
    confirm a candidate translation is a real, well-supported periodic
    structure rather than a handful of coincidental matches.
  - Pritts/Chum/Matas 2014: the general (lattice-free, full-perspective,
    radial-distortion-aware) version of this problem -- NOT reimplemented
    here; this module deliberately skips the projective/generative-model
    machinery, since this project's drone photos are close to fronto-
    parallel already (near-zero gimbal pitch, verified empirically on a
    real capture), and only the VERTICAL period is needed, not a full
    2D lattice + rectification.

This is a real engineering bet, not a guaranteed win -- Wenzel et al.
themselves note their own method is "very sensitive to similar structures
in the neighborhood," which is exactly this project's core difficulty. The
self-consistency check (does the winning period actually explain many
independent pairs, not just one) is the main defense against that failure
mode, but it is not foolproof.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class PeriodEstimate:
    period_px: float | None  # vertical repeat period, in THIS image's own pixel coords
    confidence: float  # 0..1, fraction of candidate pairs consistent with the winning period
    num_candidate_pairs: int
    num_inlier_pairs: int
    harmonic_scores: dict = field(default_factory=dict)  # {multiple: consensus_score}, for diagnostics


def _self_match_displacements(
    gray: np.ndarray,
    max_dx_frac: float = 0.12,
    ratio_thresh: float = 0.85,
    min_separation_px: float = 40.0,
) -> list[tuple[float, float, float]]:
    """Match SIFT features in `gray` against themselves (excluding trivial
    self/near-self matches) and return (dy, weight, scale_ratio) triples for
    candidate pairs that are roughly vertically aligned (|dx| small) --
    exactly the repeats we care about for a floor-spacing estimate, since
    window columns are stacked close to directly above/below each other.
    """
    h, w = gray.shape[:2]
    sift = cv2.SIFT_create(nfeatures=4000, contrastThreshold=0.02)
    kps, des = sift.detectAndCompute(gray, None)
    if des is None or len(kps) < 20:
        return []

    bf = cv2.BFMatcher(cv2.NORM_L2)
    knn = bf.knnMatch(des, des, k=4)  # k=4: index 0 is always self (distance 0), so take 1..3

    max_dx = max_dx_frac * w
    results: list[tuple[float, float, float]] = []
    for matches in knn:
        if len(matches) < 2:
            continue
        query_idx = matches[0].queryIdx
        # matches[0] is the point itself (distance ~0); real candidates start at index 1
        candidates = [m for m in matches[1:] if m.trainIdx != query_idx]
        if len(candidates) < 2:
            continue
        best, second = candidates[0], candidates[1]
        if best.distance > ratio_thresh * second.distance:
            continue  # not a confident match against the *next best* alternative

        i, j = best.queryIdx, best.trainIdx
        if i >= j:
            continue  # dedupe: every pair appears twice (i->j and j->i)
        p_i, p_j = kps[i].pt, kps[j].pt
        dx, dy = p_j[0] - p_i[0], p_j[1] - p_i[1]
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < min_separation_px or abs(dx) > max_dx:
            continue

        # orientation + scale similarity weighting (Wenzel et al. 2008's
        # angle-weight / scale-weight, simplified) -- down-weights pairs
        # that match by descriptor alone but look geometrically implausible
        # as the same repeated motif (e.g. very different apparent size).
        scale_i, scale_j = kps[i].size, kps[j].size
        scale_ratio = min(scale_i, scale_j) / max(scale_i, scale_j, 1e-6)
        angle_diff = abs(((kps[i].angle - kps[j].angle + 180) % 360) - 180)
        angle_weight = max(0.0, np.cos(np.radians(angle_diff)))
        weight = scale_ratio * angle_weight
        if weight < 0.3:
            continue

        results.append((abs(dy), weight, scale_ratio))
    return results


def estimate_vertical_period(
    image_bgr: np.ndarray,
    roi: tuple[float, float, float, float] = (0.1, 0.15, 0.9, 0.95),
    min_period_px: float = 40.0,
    max_period_px_frac: float = 0.5,
    bin_width_px: float = 4.0,
) -> PeriodEstimate:
    """Estimate the dominant vertical repeat period (px, in the ORIGINAL
    image's own coordinates) from a single facade photo.

    `roi` crops to (x0_frac, y0_frac, x1_frac, y1_frac) of the image before
    processing, to stay clear of sky/background the way earlier manual
    crops in this project's diagnostics did.
    """
    h, w = image_bgr.shape[:2]
    x0, y0, x1, y1 = (int(roi[0] * w), int(roi[1] * h), int(roi[2] * w), int(roi[3] * h))
    crop = image_bgr[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    pairs = _self_match_displacements(gray)
    if len(pairs) < 5:
        return PeriodEstimate(None, 0.0, len(pairs), 0)

    dys = np.array([p[0] for p in pairs])
    weights = np.array([p[1] for p in pairs])
    max_period = max_period_px_frac * crop.shape[0]
    mask = (dys >= min_period_px) & (dys <= max_period)
    dys, weights = dys[mask], weights[mask]
    if len(dys) < 5:
        return PeriodEstimate(None, 0.0, len(pairs), 0)

    # Candidate periods: every observed dy is itself a candidate fundamental
    # period OR a harmonic (2x, 3x, ...) of a smaller one. Score each
    # candidate T by how much total weight is "explained" (within tolerance)
    # by T's harmonic series -- the RANSAC-consensus step.
    #
    # max_multiple is deliberately small (2026-09-09: found the hard way --
    # allowing dy to match ANY integer multiple of T lets a small,
    # essentially-arbitrary T "explain" many unrelated large displacements
    # just because small numbers divide more things approximately. Verified
    # by drawing the actual matched point pairs for a case that reported
    # T=40px at 100% confidence: the drawn lines visibly spanned about one
    # full floor (~280px), not 40px -- the real signal was there, but the
    # scoring picked a spurious small T that coincidentally fit many
    # unrelated pairs as high multiples of itself instead. Capping the
    # multiple removes that degree of freedom: a real k=1 (adjacent-floor)
    # match is strong direct evidence, but "this fits as the 15th multiple
    # of some small number" is not.
    candidates = np.unique(np.round(dys / bin_width_px) * bin_width_px)
    best_T, best_score, best_inliers = None, 0.0, 0
    tol = 0.08  # 8% relative tolerance on harmonic multiples
    max_multiple = 3

    for T in candidates:
        if T < min_period_px:
            continue
        multiples = np.round(dys / T)
        multiples = np.clip(multiples, 1, max_multiple)
        expected = multiples * T
        rel_err = np.abs(dys - expected) / expected
        inliers = rel_err < tol
        score = float(weights[inliers].sum())
        if score > best_score:
            best_score, best_T, best_inliers = score, float(T), int(inliers.sum())

    if best_T is None:
        return PeriodEstimate(None, 0.0, len(dys), 0)

    total_weight = float(weights.sum())
    confidence = best_score / total_weight if total_weight > 0 else 0.0

    harmonic_scores = {}
    for k in range(1, 6):
        expected = k * best_T
        rel_err = np.abs(dys - expected) / expected
        harmonic_scores[k] = int((rel_err < tol).sum())

    return PeriodEstimate(best_T, confidence, len(dys), best_inliers, harmonic_scores)
