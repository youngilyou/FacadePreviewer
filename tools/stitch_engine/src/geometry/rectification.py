"""Facade-plane rectification from calibrated camera poses.

Ported from the main CheckCrack repo's src/geometry/rectification.py
(CLAUDE.local.md #13), trimmed to exactly what previewer needs: previewer is
Phase1-only (CLAUDE.local.md #3.1, one image folder = one facade, no
building footprint), so only the footprint-free `facade_plane_from_reconstruction`
path is ported -- `facade_plane_from_segment` (needs a real footprint
FacadeSegment) is out of scope here, see stitch_engine/README.md.

The pairwise-homography chain (stitching/graph.py + warp.py) has no way to
enforce *global* consistency -- each pair only agrees locally, so alignment
error accumulates hop by hop (global_drift_score, graph.py). That's exactly
what triggers the COLMAP fallback (should_run_colmap, sfm/colmap_runner.py).
This module is the other half of that fallback: once COLMAP has recovered
real per-image camera poses and calibrated intrinsics, every registered
image's pixel->facade-plane mapping is a single closed-form homography
computed straight from geometry -- there is no chain to drift, because each
image is placed independently against the *plane*, not against its
neighbors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pycolmap
import pyproj

from src.common.imageio import imread_unicode
from src.common.types import ImageMetadata, StitchQualityReport
from src.geometry.dense_depth import ImageDepthInfo, apply_depth_correction, is_gpu_dense_available, load_depth_corrections, run_dense_stereo
from src.stitching.blend import blend_analysis, blend_visual, compute_seam_masks
from src.stitching.mosaic import MosaicResult, paste_max
from src.stitching.warp import WarpedImage, _transform_corners

logger = logging.getLogger(__name__)


@dataclass
class FacadePlane:
    origin: np.ndarray  # (3,) UTM (x, y, z) meters — the facade-local (0,0)
    e_u: np.ndarray  # (3,) unit vector, horizontal along the facade
    e_v: np.ndarray  # (3,) unit vector, "down" in the rendered canvas
    px_per_m: float
    width_m: float  # canvas u-extent
    height_m: float  # canvas v-extent


def estimate_utm_epsg(catalog: list[ImageMetadata]) -> int | None:
    """Standard 6-degree WGS84 UTM zone from the average GPS position of a
    set of images. previewer has no operator-supplied utm_epsg (no footprint
    workflow), so this derives one from each image's own EXIF GPS instead of
    requiring the operator to look up a zone number by hand. Returns None
    (never a guessed zone) if no image in the catalog has GPS at all."""
    lats = [m.gps.latitude for m in catalog if m.gps.latitude is not None]
    lons = [m.gps.longitude for m in catalog if m.gps.longitude is not None]
    if not lats or not lons:
        return None
    lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _camera_center(img: "pycolmap.Image") -> np.ndarray:
    """World-frame camera center from cam_from_world (X_cam = R@X_world + t
    means the camera sits at R.T @ (-t) in world coordinates) -- same R/t
    extraction rectify_images already uses below."""
    pose = img.cam_from_world()
    R = pose.rotation.matrix()
    t = np.asarray(pose.translation)
    return R.T @ (-t)


def _principal_direction(points_3d: np.ndarray) -> np.ndarray:
    """Largest-variance direction through a point set (e.g. a flight's
    camera centers) via SVD -- the "track" facade_plane_from_reconstruction
    aligns its u-axis to."""
    centered = points_3d - points_3d.mean(axis=0)
    _, _, vt = np.linalg.svd(centered)
    return vt[0]


def _near_camera_track_mask(
    points: np.ndarray,
    camera_centers: np.ndarray,
    standoff_multiple: float = 4.0,
    absolute_cap_m: float = 150.0,
) -> np.ndarray:
    """Coarse pre-filter: keep only points within a plausible shooting
    distance of *some* camera, before RANSAC ever sees them.

    A pure "most-inlier-count" RANSAC plane fit (see _ransac_plane_inlier_mask)
    has its own failure mode discovered while testing this exact fix: if the
    background (a mountainside, in this dataset) happens to be bigger and
    more coherently planar than the actual facade, RANSAC's max-inlier
    criterion can pick the *mountain* as the best-supported plane instead of
    the wall the drone was actually circling -- which reproduced the same
    giant-canvas OOM this whole fix exists to prevent, just one step later
    (confirmed: the fixed pipeline still grew to >23GB RSS with no plane
    ever getting rejected, because the facade points were themselves treated
    as the minority "noise" against a huge mountain-plane consensus).

    A drone photographing a wall stays close to it (typical standoff is
    single-digit to a few tens of meters); a mountain in the background sits
    hundreds of meters to kilometers away regardless of how planar it is.
    That's a much stronger, camera-geometry-grounded signal than plane
    coherence alone, so it runs *first*: for each 3D point, take its distance
    to the nearest camera center, use `standoff_multiple` times the median of
    that per-point value across the whole cloud as the cutoff (so it adapts
    to however close/far this particular flight actually was), but never
    let that adaptive cutoff exceed absolute_cap_m regardless of what the
    median says -- if most of the reconstruction IS the mountain (median
    itself already huge), the adaptive cutoff alone could still wave the
    background through.
    """
    # Chunked to bound peak memory: a full (num_points x num_cameras)
    # distance matrix for a large cloud/flight would itself be sizeable.
    chunk = 20_000
    nearest_dist = np.empty(points.shape[0], dtype=np.float64)
    for start in range(0, points.shape[0], chunk):
        end = min(start + chunk, points.shape[0])
        diffs = points[start:end, None, :] - camera_centers[None, :, :]
        nearest_dist[start:end] = np.sqrt((diffs**2).sum(axis=2)).min(axis=1)

    cutoff = min(float(np.median(nearest_dist)) * standoff_multiple, absolute_cap_m)
    return nearest_dist < cutoff


def _ransac_plane_inlier_mask(
    points: np.ndarray,
    max_point_plane_dist_m: float = 5.0,
    num_iterations: int = 2000,
    seed: int = 0,
) -> np.ndarray:
    """Boolean inlier mask for the largest-support single plane in `points`.

    facade_plane_from_reconstruction used to feed *every* triangulated point
    straight into an SVD plane fit and a min/max bounding box with no
    rejection at all. A real run showed why that breaks: repeated
    window/balcony patterns fooled the matcher into a few bad correspondences,
    COLMAP triangulated those as points off in the background (mountains/sky)
    instead of on the wall -- 12.5% of 118k points sat >100m from the main
    cluster, one point 1.2km out -- and that alone widened the fitted facade
    to roughly 1km x 1km. At this module's 100 px/m that's an ~11GB canvas,
    which crashed cv2.warpPerspective with an out-of-memory error in
    rectify_images below (silently swallowed by pipeline/runner.py's broad
    except, so the tool just fell back to the *already-broken* pairwise-chain
    mosaic without ever showing why the COLMAP-rectified one never appeared).

    Plain RANSAC: sample 3 points, fit the plane through them, count points
    within max_point_plane_dist_m of it, keep the plane with the most
    support. 5m is a deliberately generous default -- big enough to keep
    genuine facade relief (balconies, AC units, recessed windows) as inliers,
    while still rejecting anything actually off the building.
    """
    n = points.shape[0]
    if n < 3:
        return np.ones(n, dtype=bool)

    rng = np.random.default_rng(seed)
    best_mask = np.ones(n, dtype=bool)
    best_count = -1
    for _ in range(num_iterations):
        p0, p1, p2 = points[rng.choice(n, size=3, replace=False)]
        normal = np.cross(p1 - p0, p2 - p0)
        normal_len = np.linalg.norm(normal)
        if normal_len < 1e-9:  # near-collinear sample, degenerate plane
            continue
        normal = normal / normal_len
        dist = np.abs((points - p0) @ normal)
        mask = dist < max_point_plane_dist_m
        count = int(mask.sum())
        if count > best_count:
            best_count, best_mask = count, mask
    return best_mask


def facade_plane_from_reconstruction(
    reconstruction: pycolmap.Reconstruction,
    px_per_m: float = 100.0,
    padding_m: float = 2.0,
) -> FacadePlane:
    """Footprint-free facade plane fit, for previewer's Phase1-only
    `run_facade_poc` which has no footprint file to take a local coordinate
    frame from at all. Fits the plane straight from COLMAP's own
    triangulated 3D points (already bundle-adjusted, representing the actual
    photographed surface) via SVD -- the least-variance direction is the
    plane normal. Only meaningful once `reconstruction` has already been
    aligned to real, metric, gravity-aligned UTM+altitude coordinates via
    align_reconstruction_to_utm; this fit is purely geometric and has no
    other source of scale or orientation.

    2026-09-09/10: `px_per_m` was briefly raised to 300 chasing a corner
    "double edge" defect, on the theory that px_per_m=100 was downsampling
    each ~5280x3956 source photo by ~10x. That whole investigation (like the
    later per-image-roll one on 09-10) was run on a reconstruction that was
    never actually passed through align_reconstruction_to_utm -- its width
    came out as a physically-impossible ~21.8m for a 13-floor building
    (~1m/floor). The REAL, aligned building is ~63.5m wide; at that true
    scale, px_per_m=100 downsamples each source photo by under 2x, not 10x,
    so the "severe aliasing" theory doesn't hold up either once measured
    correctly. Reverted to 100 -- the corner defect needs to be re-examined
    against a properly aligned reconstruction before concluding anything
    about its cause (not done as of this entry).

    Orientation: if the fitted normal is mostly horizontal, this is the
    common facade case -- e_v is forced to true world-up rather than
    trusting SVD's second axis, which has no reason to already be vertical.
    Either way, e_u follows the flight track's own principal direction (PCA
    on the registered camera centers, projected onto the plane) rather than
    the point cloud's own PCA axis -- a drone pass flies roughly parallel to
    whatever it's photographing (a wall's edge, or a roof's long side), so
    the track is a far more natural "horizontal" than an axis derived purely
    from the (capture-direction-agnostic) point cloud shape.

    Outlier rejection (two stages, see each helper's docstring): first
    _near_camera_track_mask drops points nowhere near where the drone
    actually flew, then _ransac_plane_inlier_mask fits a plane to what's left
    and drops whatever still doesn't lie on it. Before this function's own
    centroid/SVD/bounding-box math ever ran, a few badly triangulated points
    (or, with RANSAC alone and no camera-distance stage, an entire coherent
    background structure like a mountainside) could balloon the fitted
    facade to ~1km and crash rectify_images with an out-of-memory error.
    """
    points_all = np.array([p.xyz for p in reconstruction.points3D.values()])
    if points_all.shape[0] < 10:
        raise ValueError(f"too few triangulated points ({points_all.shape[0]}) to fit a facade plane")

    centers = np.array([_camera_center(img) for img in reconstruction.images.values()])

    # Two-stage outlier rejection -- order matters. Camera-distance first
    # (cheap, and grounded in where the drone actually flew, so it can't be
    # outvoted by a big coherent background structure), THEN plane-coherence
    # RANSAC on what's left (catches remaining noise/mismatches that are
    # merely near the drone but still not on the wall). See both helpers'
    # docstrings for why running RANSAC alone, first, was not enough.
    if centers.shape[0] >= 1:
        near_mask = _near_camera_track_mask(points_all, centers)
        points_near = points_all[near_mask]
    else:
        points_near = points_all
    if points_near.shape[0] < 10:
        points_near = points_all

    inlier_mask = _ransac_plane_inlier_mask(points_near)
    points = points_near[inlier_mask]
    if points.shape[0] < 10:
        # RANSAC couldn't find a coherent plane at all (degenerate scene) --
        # fall back to the camera-distance-filtered set rather than raising,
        # matching this function's original (pre-outlier-rejection) behavior.
        points = points_near

    return fit_plane_from_points(points, centers, px_per_m=px_per_m, padding_m=padding_m)


def fit_plane_from_points(
    points: np.ndarray, camera_centers: np.ndarray, px_per_m: float = 100.0, padding_m: float = 2.0,
) -> FacadePlane:
    """Core SVD plane-fit + orientation logic shared by
    `facade_plane_from_reconstruction` (the front wall, `points` already
    outlier-rejected by its two RANSAC stages) and `fit_corner_planes` (a
    building-corner side/gable wall, `points` are the OUTLIER points from the
    front-plane fit -- see that function's docstring). `points`/
    `camera_centers` are assumed already curated by the caller; this function
    only does the geometry (SVD normal, track-aligned+sign-fixed e_u,
    world-anchored e_v, canvas extent) -- see facade_plane_from_reconstruction
    for what each step means and why.
    """
    centroid = points.mean(axis=0)
    # full_matrices=False: this is an (N,3) matrix, and only the (3,3) Vt is ever used (U is
    # discarded) -- the default full_matrices=True still tries to materialize an (N,N) U, which
    # for N in the hundred-thousands is a real crash (observed: 100,048 points -> an attempted
    # 74.6GB allocation for a U this code never even reads).
    _, _, vt = np.linalg.svd(points - centroid, full_matrices=False)
    normal = vt[2]

    track = _principal_direction(camera_centers) if camera_centers.shape[0] >= 2 else vt[0]
    e_u = track - np.dot(track, normal) * normal
    if np.linalg.norm(e_u) < 1e-6:
        e_u = vt[0] - np.dot(vt[0], normal) * normal
    e_u = e_u / np.linalg.norm(e_u)

    # e_u's AXIS comes from the flight track's own SVD above, which is exactly
    # what we want (the track is a cleaner "horizontal" than the point
    # cloud's own PCA -- see this function's docstring) -- but SVD never
    # fixes which of the two opposite directions along that axis is "positive"
    # (2026-09-09 CLAUDE.local.md entry: a real run rendered every source
    # photo left-right mirrored -- confirmed by the user against the drone's
    # own front-facing reference view -- with the mirror direction being
    # whatever np.linalg.svd's internal sign convention happened to produce
    # for that day's point cloud, not a fixed always-flipped bug). Fix the
    # SIGN (not the axis) against an unambiguous physical reference instead:
    # the camera centers sit on the *outward* (viewer) side of the wall by
    # construction, so centroid -> mean-camera-position is a reliable
    # "outward" direction with no SVD sign ambiguity at all. A viewer facing
    # the wall (looking the opposite way, "into" it) with true world-up
    # should see canvas +u as their own right hand -- forward x up, the
    # standard right-handed camera convention.
    if camera_centers.shape[0] >= 1:
        outward = camera_centers.mean(axis=0) - centroid
        forward = -outward
        world_up_ref = np.array([0.0, 0.0, 1.0])
        right_ref = np.cross(forward, world_up_ref)
        right_ref_norm = np.linalg.norm(right_ref)
        if right_ref_norm > 1e-6 and np.dot(e_u, right_ref / right_ref_norm) < 0.0:
            e_u = -e_u

    world_up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(normal, world_up))) < 0.5:
        # vertical wall -- v is true world-up, so "up" always renders up
        # regardless of which way the flight track happened to point.
        e_v = np.array([0.0, 0.0, -1.0])
    else:
        # rooftop/plan-view -- no natural "up" to anchor to, so v is
        # whatever stays perpendicular to the track-aligned u within the plane.
        e_v = np.cross(normal, e_u)
        e_v = e_v / np.linalg.norm(e_v)

    u = (points - centroid) @ e_u
    v = (points - centroid) @ e_v
    width_m = float(u.max() - u.min()) + 2 * padding_m
    height_m = float(v.max() - v.min()) + 2 * padding_m
    origin = centroid + e_u * (float(u.min()) - padding_m) + e_v * (float(v.min()) - padding_m)

    return FacadePlane(origin=origin, e_u=e_u, e_v=e_v, px_per_m=px_per_m, width_m=width_m, height_m=height_m)


def compute_image_valid_x_ranges(
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    max_point_plane_dist_m: float = 2.0,
    min_points_for_split: int = 30,
    min_purity: float = 0.9,
    min_excluded_width_frac: float = 0.10,
    max_excluded_width_frac: float = 0.90,
) -> dict[str, tuple[float, str] | None]:
    """2026-09-09 CLAUDE.local.md entry ("B" -- per-image valid-region
    masking): a drone pass that turns a building corner captures the front
    wall and the perpendicular end/gable wall IN THE SAME FRAME (confirmed by
    directly opening DJI_0045.JPG/DJI_0184.JPG and seeing exactly this) --
    forcing that end-wall content through the front plane's single homography
    produces severe, image-to-image-inconsistent stretching (the "corner
    wall too big and inconsistent" / "warped blue blob" defects the user
    found). Rather than discarding these straddling images entirely (losing
    real front-wall coverage right at the corners), this finds, PER IMAGE,
    whether its own 2D keypoints split cleanly into an on-plane cluster and
    an off-plane cluster, and if so, a horizontal (image-x) cutoff so only
    the on-plane side gets warped onto the canvas.

    Uses each image's own COLMAP 2D<->3D correspondences (`img.points2D`,
    already computed by SfM, no dense reconstruction needed) -- a 3D point's
    distance to the *front* plane (this function's own `plane` argument,
    already fit by `facade_plane_from_reconstruction`) says whether that
    keypoint belongs to the front wall or not. `max_point_plane_dist_m=2.0`
    is tighter than `_ransac_plane_inlier_mask`'s 5.0 (which needs to be
    generous enough to keep real relief like balconies as *plane* inliers)
    -- here the goal is specifically to isolate an entirely different,
    ~perpendicular surface, which sits much further than 2m from the front
    plane almost everywhere except right at the corner seam itself.

    Returns, per image_id: None (no clean split found -- most images; use
    the whole image unchanged, exactly like before) or (threshold_x,
    'keep_left' | 'keep_right') meaning "only pixels with x < threshold_x
    (or >=, respectively) are on the front wall, in this image's own
    ORIGINAL (pre-undistort) pixel coordinates."

    The split-quality bar (`min_points_for_split` on each side,
    `min_purity=0.9`) is intentionally strict: a plain front-wall image with
    a handful of noisy/mismatched 3D points must NOT get spuriously masked
    just because a few of its points happen to be far from the plane -- only
    an image with a real, cleanly-separable second cluster (i.e., a genuine
    corner-straddling shot) qualifies.

    `min_excluded_width_frac`/`max_excluded_width_frac` guard against a
    second, independent failure mode found the hard way on a real run: point
    COUNT purity alone doesn't see image-space geometry, so a handful of
    stray/mismatched points sitting near one edge of an otherwise-normal
    image could reach >90% purity at a threshold just a few pixels in from
    that edge -- technically "pure" but keeping essentially the WHOLE image
    while nominally "excluding" almost nothing, or (mirrored) excluding
    almost EVERYTHING to chase a few outliers. A real corner-straddling shot
    excludes a substantial, non-trivial fraction of the frame (in this
    project's own data, roughly a third to two-thirds) -- so any split whose
    excluded width falls outside [10%, 90%] of the image's own width is
    treated as spurious and discarded (falls back to using the whole image,
    same as an image with no split at all).
    """
    normal = np.cross(plane.e_u, plane.e_v)
    normal = normal / np.linalg.norm(normal)

    results: dict[str, tuple[float, str] | None] = {}
    for img in reconstruction.images.values():
        stem = Path(img.name).stem
        xs_in: list[float] = []
        xs_out: list[float] = []
        for p in img.points2D:
            if not p.has_point3D():
                continue
            pt3d = reconstruction.points3D[p.point3D_id].xyz
            dist = abs(float(np.dot(pt3d - plane.origin, normal)))
            (xs_in if dist < max_point_plane_dist_m else xs_out).append(float(p.xy[0]))

        if len(xs_in) < min_points_for_split or len(xs_out) < min_points_for_split:
            results[stem] = None
            continue

        xs_in_sorted = np.sort(np.array(xs_in))
        xs_out_sorted = np.sort(np.array(xs_out))
        candidates = np.unique(np.concatenate([xs_in_sorted, xs_out_sorted]))
        n_in, n_out = len(xs_in_sorted), len(xs_out_sorted)
        in_left = np.searchsorted(xs_in_sorted, candidates, side="left")
        out_left = np.searchsorted(xs_out_sorted, candidates, side="left")
        # option 1: outliers cluster on the LEFT (x < t), inliers on the RIGHT -- keep the right side
        purity_keep_right = (out_left + (n_in - in_left)) / (n_in + n_out)
        # option 2: outliers cluster on the RIGHT (x >= t), inliers on the LEFT -- keep the left side
        purity_keep_left = ((n_out - out_left) + in_left) / (n_in + n_out)

        i1, i2 = int(np.argmax(purity_keep_right)), int(np.argmax(purity_keep_left))
        if purity_keep_right[i1] >= purity_keep_left[i2]:
            best_purity, best_thresh, best_side = float(purity_keep_right[i1]), float(candidates[i1]), "keep_right"
        else:
            best_purity, best_thresh, best_side = float(purity_keep_left[i2]), float(candidates[i2]), "keep_left"

        if best_purity < min_purity:
            results[stem] = None
            continue

        image_width = float(img.camera.width)
        excluded_frac = (best_thresh / image_width) if best_side == "keep_right" else (1.0 - best_thresh / image_width)
        if not (min_excluded_width_frac <= excluded_frac <= max_excluded_width_frac):
            results[stem] = None
            continue

        # 2026-09-09: tried a per-image safety margin here (pulling the
        # cutoff further into the confidently-flat interior) as a candidate
        # fix for a "double edge"/ghosting defect seen right around this
        # boundary -- ruled out by direct measurement: margins up to 2000px
        # (near half the image width) made zero visible difference. The
        # split itself was never the problem (different straddling images'
        # thresholds already agree to ~1 canvas px when projected).
        # blend_analysis (hard seam, no blending) also renders this exact
        # spot cleanly, and a follow-up objective check (gradient-peak
        # profile at the defect's exact pixel row) found the seam-owned
        # region assignment itself is essentially identical whether
        # blend_visual's multiband_num_bands is 5 or 1 -- so num_bands is
        # NOT the cause either (an earlier note here claiming it was is
        # wrong; caught by that follow-up check, not by a fresh capture).
        # True root cause is still open as of this entry.
        results[stem] = (best_thresh, best_side)

    return results


@dataclass
class CornerPlaneGroup:
    plane: FacadePlane
    image_names: set[str]  # image_ids (stems) that see this corner
    valid_x_ranges: dict[str, tuple[float, str]]  # INVERTED vs the front plane's -- keeps the corner side


def fit_corner_planes(
    reconstruction: pycolmap.Reconstruction,
    front_plane: FacadePlane,
    valid_x_ranges: dict[str, tuple[float, str] | None],
    min_images_per_corner: int = 3,
    min_points_per_corner: int = 10,
) -> dict[str, CornerPlaneGroup]:
    """"C" (2026-09-09 CLAUDE.local.md entry): rather than just excluding a
    corner-straddling image's off-plane side (compute_image_valid_x_ranges,
    "B" -- loses that content entirely), fit that excluded content its OWN
    plane and rectify it separately, so the perpendicular end/gable wall at
    each building corner renders correctly too (not forced through the front
    wall's homography) instead of being thrown away.

    Groups the images `compute_image_valid_x_ranges` already flagged by
    their `side` label -- 'keep_right' images all excluded their LEFT
    portion (one corner), 'keep_left' images all excluded their RIGHT
    portion (the other corner) -- exactly two possible corners, no separate
    spatial clustering needed. For each group, gathers the 3D points behind
    each image's own EXCLUDED-side 2D keypoints (these are, by construction,
    the off-front-plane point cloud -- the corner wall's own geometry) and
    fits a plane to them with the same `fit_plane_from_points` core used for
    the front wall.

    A corner group with too few contributing images/points
    (`min_images_per_corner`/`min_points_per_corner`) is dropped rather than
    fit from a handful of noisy points -- a real second plane needs real
    support, same philosophy as compute_image_valid_x_ranges' own purity bar.
    """
    by_side: dict[str, list] = {"keep_right": [], "keep_left": []}
    for img in reconstruction.images.values():
        stem = Path(img.name).stem
        split = valid_x_ranges.get(stem)
        if split is not None:
            by_side[split[1]].append(img)

    groups: dict[str, CornerPlaneGroup] = {}
    for corner_name, imgs in by_side.items():
        if len(imgs) < min_images_per_corner:
            continue

        corner_points: list[np.ndarray] = []
        corner_centers: list[np.ndarray] = []
        inverted_ranges: dict[str, tuple[float, str]] = {}
        for img in imgs:
            stem = Path(img.name).stem
            thresh_x, side = valid_x_ranges[stem]
            # invert: render what the front plane excluded
            inverted_ranges[stem] = (thresh_x, "keep_left" if side == "keep_right" else "keep_right")
            corner_centers.append(_camera_center(img))
            for p in img.points2D:
                if not p.has_point3D():
                    continue
                on_excluded_side = (p.xy[0] < thresh_x) if side == "keep_right" else (p.xy[0] >= thresh_x)
                if on_excluded_side:
                    corner_points.append(reconstruction.points3D[p.point3D_id].xyz)

        if len(corner_points) < min_points_per_corner:
            continue

        # Same two-stage outlier rejection as the front plane (see
        # facade_plane_from_reconstruction's docstring) -- without it, a real
        # run's corner point set (raw, unfiltered "excluded side" 3D points)
        # produced a >600m-wide "plane" (a >4-gigapixel canvas) because a
        # handful of badly-triangulated/background points sneak in here just
        # as easily as they do for the front wall. RANSAC alone (no camera-
        # distance stage) has the same "background can outvote the real
        # surface" failure mode documented there, so run both stages.
        corner_points_arr = np.array(corner_points)
        corner_centers_arr = np.array(corner_centers)
        near_mask = _near_camera_track_mask(corner_points_arr, corner_centers_arr)
        points_near = corner_points_arr[near_mask] if near_mask.sum() >= 10 else corner_points_arr
        inlier_mask = _ransac_plane_inlier_mask(points_near)
        points_final = points_near[inlier_mask] if inlier_mask.sum() >= 10 else points_near
        if points_final.shape[0] < min_points_per_corner:
            continue

        plane = fit_plane_from_points(points_final, corner_centers_arr)
        groups[corner_name] = CornerPlaneGroup(
            plane=plane,
            image_names={Path(img.name).stem for img in imgs},
            valid_x_ranges=inverted_ranges,
        )

    return groups


def align_reconstruction_to_utm(
    reconstruction: pycolmap.Reconstruction,
    by_id: dict[str, ImageMetadata],
    utm_epsg: int,
    min_common_images: int = 3,
) -> bool:
    """Align COLMAP's arbitrary-frame reconstruction to real-world UTM+altitude
    meters using each registered image's own GPS as a location prior.
    Mutates `reconstruction` in place. Returns False (reconstruction is left
    untouched) if there isn't enough GPS coverage or alignment fails -- never
    proceeds with an unaligned/unscaled reconstruction, since every downstream
    plane-projection distance would silently be wrong.
    """
    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    names: list[str] = []
    locations: list[list[float]] = []
    for img in reconstruction.images.values():
        meta = by_id.get(Path(img.name).stem)
        if meta is None or meta.gps.latitude is None or meta.gps.altitude_m is None:
            continue
        x, y = transformer.transform(meta.gps.longitude, meta.gps.latitude)
        names.append(img.name)
        locations.append([x, y, meta.gps.altitude_m])

    if len(names) < min_common_images:
        return False

    sim3d = pycolmap.align_reconstruction_to_locations(
        reconstruction, names, np.array(locations), min_common_images, pycolmap.RANSACOptions()
    )
    if sim3d is None:
        return False
    reconstruction.transform(sim3d)
    return True


# 2026-09-10: a visibly wavy/leaning building silhouette was first (wrongly)
# diagnosed as several degrees of per-image COLMAP roll noise, "confirmed"
# against DJI's own XMP GimbalRollDegree and "fixed" with a per-image
# Rz(delta) roll correction here -- which made the mosaic dramatically
# WORSE, not better. Root cause of the wave was something else entirely:
# every scratch script used to investigate it that day (unlike this
# module's real caller, runner.py) skipped align_reconstruction_to_utm
# before calling facade_plane_from_reconstruction, so "world up" was never
# actually aligned with the fitted plane/camera poses at all -- comparing
# COLMAP's roll (measured in that unaligned, arbitrary-orientation frame)
# against the gimbal's roll (always relative to true gravity) compared two
# unrelated references. Once properly aligned, COLMAP's own roll matched
# the gimbal to within ~0.08 degrees, std ~0.003, across all 140 images --
# no real roll problem ever existed, and the wave disappeared with no
# per-image correction of any kind once alignment was done correctly. The
# gimbal-roll-correction code (read XMP GimbalRollDegree, measure roll
# against true world-up, apply Rz(delta) before the flat-plane homography)
# was removed entirely rather than left in place unused, since it never
# addressed a real defect -- see this date's CLAUDE.local.md entry before
# reintroducing anything similar.


def _camera_to_facade_homography(K: np.ndarray, R: np.ndarray, t: np.ndarray, plane: FacadePlane) -> np.ndarray:
    """Closed-form undistorted-image-pixel -> facade-plane-pixel homography.

    For X_world = origin + u*e_u + v*e_v (the plane, parameterized in
    facade-canvas pixels), a calibrated pinhole camera images it as
    K @ (R @ X_world + t). Collecting the u, v and constant terms into
    columns gives the facade->image homography directly; we want the
    inverse direction for warpPerspective(src=image, ..., dst=facade canvas).
    """
    e_u_px = plane.e_u / plane.px_per_m
    e_v_px = plane.e_v / plane.px_per_m
    col_u = K @ (R @ e_u_px)
    col_v = K @ (R @ e_v_px)
    col_o = K @ (R @ plane.origin + t)
    facade_to_image = np.column_stack([col_u, col_v, col_o])
    return np.linalg.inv(facade_to_image)


def rectify_images(
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path,
    valid_x_ranges: dict[str, tuple[float, str] | None] | None = None,
    image_names: set[str] | None = None,
    depth_corrections: dict[str, ImageDepthInfo] | None = None,
    depth_deviation_threshold_m: float = 0.15,
    depth_max_deviation_m: float = 3.0,
) -> tuple[dict[str, WarpedImage], tuple[int, int]]:
    """Undistort + plane-project every registered image onto one fixed,
    plane-sized canvas. Each image is warped into its own tight local ROI
    and carries a `corner` offset, not a full canvas-sized buffer -- the same
    memory-bounding trick warp.py's homography-chain path already uses (see
    WarpedImage there).

    This function originally warped every image straight onto the full
    canvas at corner (0,0), reasoning that "the canvas is fixed by the known
    facade span, not derived from where images project to" so there was
    nothing to blow up the way a drifting chain could. True, but a single
    drone photo only ever covers a small patch of the wall -- with ~165
    full-canvas copies (each mostly empty padding) all held in memory at
    once for blending, that alone pushed this process past 24GB RSS on a
    real run, and it had to be killed before OpenCV even got a chance to OOM
    on its own the way the *pre-outlier-rejection* bug this whole fix
    started from did. ROI-cropping per image the way warp.py does keeps each
    one's footprint close to what it actually covers instead of the whole
    wall.

    `valid_x_ranges` (from `compute_image_valid_x_ranges`, optional) masks
    out the off-plane (e.g. building-corner side-wall) portion of a
    straddling image BEFORE it's warped, so only its genuine front-wall
    content contributes to the canvas -- see that function's docstring.

    `depth_corrections` (from `dense_depth.load_depth_corrections`, optional
    -- GPU-only, silently absent on CPU-only machines) re-projects pixels
    that are genuinely off the fitted plane (balconies, ledges) using their
    real dense-stereo depth instead of the flat-plane homography every pixel
    otherwise gets; on-plane pixels are untouched. See dense_depth.py.
    """
    canvas_w = max(1, int(round(plane.width_m * plane.px_per_m)))
    canvas_h = max(1, int(round(plane.height_m * plane.px_per_m)))
    valid_x_ranges = valid_x_ranges or {}

    warped: dict[str, WarpedImage] = {}
    for img in reconstruction.images.values():
        image_id = Path(img.name).stem
        if image_names is not None and image_id not in image_names:
            continue
        raw = imread_unicode(Path(images_dir) / img.name, cv2.IMREAD_COLOR)
        if raw is None:
            continue

        raw_h, raw_w = raw.shape[:2]
        keep_mask = np.full((raw_h, raw_w), 255, dtype=np.uint8)
        split = valid_x_ranges.get(image_id)
        if split is not None:
            thresh_x, side = split
            cutoff = int(round(thresh_x))
            if side == "keep_right":
                keep_mask[:, :max(0, cutoff)] = 0
            else:
                keep_mask[:, max(0, cutoff):] = 0

        cam = img.camera
        K = cam.calibration_matrix()
        if cam.model.name == "SIMPLE_RADIAL" and abs(float(cam.params[3])) > 1e-9:
            k = float(cam.params[3])
            dist_coeffs = np.array([k, 0.0, 0.0, 0.0], dtype=np.float64)
            raw = cv2.undistort(raw, K, dist_coeffs)
            keep_mask = cv2.undistort(keep_mask, K, dist_coeffs)

        pose = img.cam_from_world()
        R = pose.rotation.matrix()
        t = np.asarray(pose.translation)
        H = _camera_to_facade_homography(K, R, t, plane)

        h, w = raw.shape[:2]
        corners = _transform_corners(h, w, H)
        min_x, min_y = corners.min(axis=0)
        max_x, max_y = corners.max(axis=0)
        # Clamp to the canvas -- an extreme grazing-angle pose could in
        # principle still project outside it; this way a single image can
        # never allocate more than the canvas itself regardless.
        min_x, min_y = max(0.0, float(min_x)), max(0.0, float(min_y))
        max_x, max_y = min(float(canvas_w), float(max_x)), min(float(canvas_h), float(max_y))
        if max_x <= min_x or max_y <= min_y:
            continue  # projects entirely outside the canvas
        # floor (not round) the corner so it never rounds up past min_x, then
        # clamp local_w/local_h against the canvas from that exact corner --
        # rounding corner and size independently (as a first pass at this did)
        # can round the corner up by up to 1px while size is computed from the
        # unrounded float extent, so corner+size can land 1px past canvas_w/h.
        # cv2.detail's blender does not tolerate a fed ROI that exceeds the
        # canvas it was cv2.detail.Blender.prepare()'d with -- it throws an
        # opaque "Unknown C++ exception" rather than clipping, which is
        # exactly what a real run hit here.
        corner_x, corner_y = int(np.floor(min_x)), int(np.floor(min_y))
        local_w = min(canvas_w - corner_x, max(1, int(np.ceil(max_x - min_x))))
        local_h = min(canvas_h - corner_y, max(1, int(np.ceil(max_y - min_y))))
        if local_w <= 0 or local_h <= 0:
            continue

        local_shift = np.array([[1, 0, -corner_x], [0, 1, -corner_y], [0, 0, 1]])
        H_local = local_shift @ H

        warped_img = cv2.warpPerspective(raw, H_local, (local_w, local_h), flags=cv2.INTER_LINEAR)
        warped_mask = cv2.warpPerspective(keep_mask, H_local, (local_w, local_h), flags=cv2.INTER_NEAREST)

        depth_info = depth_corrections.get(image_id) if depth_corrections else None
        if depth_info is not None:
            try:
                apply_depth_correction(
                    warped_img, warped_mask, corner_x, corner_y, depth_info,
                    R, t, plane.origin, plane.e_u, plane.e_v, plane.px_per_m,
                    depth_deviation_threshold_m, depth_max_deviation_m,
                )
            except Exception:
                logger.warning("depth correction failed for %s -- keeping flat-plane-only projection", image_id, exc_info=True)

        warped[image_id] = WarpedImage(
            image=warped_img,
            mask=warped_mask,
            corner=(corner_x, corner_y),
            size=(local_w, local_h),
        )

    return warped, (canvas_w, canvas_h)


def _run_dense_depth_stage(
    reconstruction: pycolmap.Reconstruction,
    images_dir: str | Path,
    cfg,
    dense_workspace_dir: str | Path | None,
) -> dict[str, ImageDepthInfo]:
    """GPU-only (see dense_depth.is_gpu_dense_available). Returns {} -- never
    raises -- on any CPU-only machine, missing/disabled config, or failure,
    so the caller's flat-plane-only behavior is the unconditional fallback.

    `reconstruction` must already be the UTM-aligned one (post
    align_reconstruction_to_utm, exactly the object `plane` was fit from):
    COLMAP reconstructions carry no inherent metric scale on their own, and
    align_reconstruction_to_utm's GPS-based similarity transform is what
    fixes that -- writing and dense-stereo-ing this *aligned* copy (instead
    of the original on-disk sparse_dir) is what makes patch_match_stereo's
    depth values come out directly in the same real-world meters as `plane`,
    with no separate scale factor to track by hand.
    """
    dense_cfg = cfg.dense if "dense" in cfg else None
    if dense_cfg is None or not bool(dense_cfg.enable) or dense_workspace_dir is None:
        return {}
    if not is_gpu_dense_available():
        logger.info("dense.enable is true but no CUDA device is available -- flat-plane-only rectification")
        return {}
    try:
        dense_workspace_dir = Path(dense_workspace_dir)
        aligned_sparse_dir = dense_workspace_dir / "aligned_sparse"
        aligned_sparse_dir.mkdir(parents=True, exist_ok=True)
        reconstruction.write(str(aligned_sparse_dir))
        ws = run_dense_stereo(
            sparse_dir=aligned_sparse_dir,
            images_dir=images_dir,
            workspace_dir=dense_workspace_dir / "stereo_ws",
            max_image_size=int(dense_cfg.max_image_size) if "max_image_size" in dense_cfg else 1600,
            geom_consistency=bool(dense_cfg.geom_consistency) if "geom_consistency" in dense_cfg else True,
            gpu_index=int(dense_cfg.gpu_index) if "gpu_index" in dense_cfg else 0,
        )
        if ws is None:
            return {}
        image_name_by_id = {Path(img.name).stem: img.name for img in reconstruction.images.values()}
        corrections = load_depth_corrections(ws, reconstruction, image_name_by_id)
        logger.info("GPU dense-stereo depth loaded for %d/%d images", len(corrections), len(image_name_by_id))
        return corrections
    except Exception:
        logger.warning("GPU dense-stereo depth stage failed -- continuing with flat-plane-only rectification", exc_info=True)
        return {}


def rectify_and_blend(
    facade_id: str,
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path,
    cfg,
    colmap_mean_reprojection_error_px: float | None = None,
    dense_workspace_dir: str | Path | None = None,
) -> MosaicResult:
    """Full COLMAP-pose-rectified facade mosaic: plane-project -> seam ->
    blend, reusing the same seam/blend code the homography-chain path uses
    (stitching/blend.py) -- the two paths only disagree about where each
    image's pixels land on the canvas, not about how to combine them once
    there. `plane` is already fully built by the caller
    (facade_plane_from_reconstruction, previewer's only path).

    `dense_workspace_dir`, if given, opts into GPU dense-stereo depth
    correction of off-plane content (balconies etc, see dense_depth.py) --
    gated on both `cfg.dense.enable` and actual CUDA availability, so this
    is always safe to pass unconditionally from the caller.

    `reconstruction` must already be UTM-aligned (align_reconstruction_to_utm,
    caller's responsibility -- see runner.py) before being passed in here:
    without it, "world up" has no relationship to the actual fitted plane or
    camera poses at all (2026-09-10 CLAUDE.local.md entry -- an unaligned
    reconstruction was mistaken for a real per-image camera-roll defect one
    whole investigation deep before this was caught).
    """
    valid_x_ranges = compute_image_valid_x_ranges(reconstruction, plane)
    depth_corrections = _run_dense_depth_stage(reconstruction, images_dir, cfg, dense_workspace_dir)
    dense_cfg = cfg.dense if "dense" in cfg else None
    deviation_threshold_m = (
        float(dense_cfg.deviation_threshold_m) if dense_cfg is not None and "deviation_threshold_m" in dense_cfg else 0.15
    )
    max_deviation_m = (
        float(dense_cfg.max_deviation_m) if dense_cfg is not None and "max_deviation_m" in dense_cfg else 3.0
    )
    warped, canvas_size = rectify_images(
        reconstruction, plane, images_dir, valid_x_ranges,
        depth_corrections=depth_corrections, depth_deviation_threshold_m=deviation_threshold_m,
        depth_max_deviation_m=max_deviation_m,
    )

    seam_masks = compute_seam_masks(warped, canvas_size)
    scfg = cfg.stitch
    analysis_image = blend_analysis(warped, seam_masks, canvas_size) if scfg.generate_analysis_mosaic else None
    visual_image = (
        blend_visual(warped, seam_masks, canvas_size, num_bands=int(scfg.multiband_num_bands))
        if scfg.generate_visual_mosaic
        else None
    )

    canvas_w, canvas_h = canvas_size
    observed_mask = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    for w in warped.values():
        paste_max(observed_mask, w.mask, w.corner)
    coverage_ratio = float(np.count_nonzero(observed_mask)) / float(observed_mask.size)

    quality = StitchQualityReport(
        facade_id=facade_id,
        image_count=len(warped),
        matched_pair_count=0,  # not applicable — no pairwise graph in this path
        failed_pair_count=0,
        mean_inlier_ratio=None,
        median_reprojection_error_px=colmap_mean_reprojection_error_px,
        coverage_ratio=coverage_ratio,
        disconnected_components=1,
        reference_image_id=None,
        unreachable_image_ids=[],
        global_drift_score_px=None,  # no chain to drift — every image is placed independently
        max_drift_score_px=None,
        cycle_edge_count=0,
        needs_colmap_fallback=False,
    )

    return MosaicResult(
        analysis_image=analysis_image, visual_image=visual_image, observed_mask=observed_mask, quality=quality
    )
