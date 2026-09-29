"""Facade-plane rectification from calibrated camera poses.

Scoped to exactly what previewer needs: previewer is Phase1-only
(CLAUDE.local.md #3.1, one image folder = one facade, no building
footprint), so this module only covers the footprint-free
`facade_plane_from_reconstruction` path -- a segment-based fit (needing a
real footprint FacadeSegment) is out of scope here, see
stitch_engine/README.md.

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


def _estimate_world_up(reconstruction: "pycolmap.Reconstruction") -> np.ndarray:
    """Gravity-up estimate that works even when the reconstruction was never
    passed through align_reconstruction_to_utm (no GPS on any image --
    previewer's own captures never have EXIF GPS, see 2026-09-28
    CLAUDE.local.md). Before this, both callers below used a hardcoded
    world_up=[0,0,1], which is only physically meaningful *after* UTM
    alignment; on a raw/unaligned COLMAP reconstruction, [0,0,1] is just
    whatever axis the mapper's arbitrary seed-pair happened to pick, so the
    rendered facade came out tilted at some random angle (confirmed by the
    user against a properly-aligned reference render, CLAUDE.local.md).

    Drones fly with a gimbal that keeps roughly level roll/pitch relative to
    true gravity throughout a facade pass -- so each registered camera's own
    "up" direction (COLMAP/standard camera convention: X=right, Y=down,
    Z=forward, so camera-local up = -Y), transformed into world coordinates
    via that image's own rotation, is a per-frame gravity estimate that needs
    no GPS at all. Averaging across every registered image cancels out each
    frame's small individual gimbal jitter and leaves a single robust
    world-up vector -- valid whether or not the reconstruction is UTM-aligned
    (post-alignment this converges to very nearly [0,0,1] anyway, since it's
    the same physical gravity direction, just consistently rotated).
    """
    ups = []
    for img in reconstruction.images.values():
        R = img.cam_from_world().rotation.matrix()
        ups.append(R.T @ np.array([0.0, -1.0, 0.0]))
    if not ups:
        return np.array([0.0, 0.0, 1.0])
    up = np.sum(np.array(ups), axis=0)
    norm = np.linalg.norm(up)
    if norm < 1e-6:
        # per-frame ups cancelled out (e.g. a flight that rolled through
        # every orientation) -- no reliable estimate, fall back to the old
        # assumption rather than dividing by ~0.
        return np.array([0.0, 0.0, 1.0])
    return up / norm


def _principal_direction(points_3d: np.ndarray) -> np.ndarray:
    """Largest-variance direction through a point set (e.g. a flight's
    camera centers) via SVD -- the "track" facade_plane_from_reconstruction
    aligns its u-axis to."""
    centered = points_3d - points_3d.mean(axis=0)
    _, _, vt = np.linalg.svd(centered)
    return vt[0]


def _robust_range(values: np.ndarray, k: float = 3.0) -> tuple[float, float]:
    """IQR-based (min, max): excludes values more than `k` interquartile-ranges
    past the nearest quartile before taking the extreme values, so a handful of
    badly-triangulated COLMAP points (a mismatched feature triangulated far off
    the real surface) can't blow up the fitted canvas size the way a plain
    .min()/.max() would -- see facade_plane_from_reconstruction for the real
    run that motivated this (a fixed-distance RANSAC threshold was rejecting
    real wall content on a non-flat building; a distribution-based cutoff
    adapts to whatever this particular point cloud's own spread actually is).
    k=3.0 is the standard "extreme outlier" boxplot threshold (vs 1.5 for a
    plain "outlier") -- deliberately conservative, since clipping real facade
    content is worse than leaving a few meters of genuine margin."""
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    if iqr <= 1e-9:
        return float(values.min()), float(values.max())
    lo, hi = q1 - k * iqr, q3 + k * iqr
    inliers = values[(values >= lo) & (values <= hi)]
    if inliers.size == 0:
        return float(values.min()), float(values.max())
    return float(inliers.min()), float(inliers.max())


_MAX_CANVAS_PIXELS = 400_000_000  # 200m x 200m at 100 px/m -- see rectify_images
_WALL_BAND_SPREAD_LIMIT_M = 5.0  # p10-p90 spread along the normal above which the wall isn't the dominant cluster
_WALL_SLAB_THICKNESS_M = 3.0  # thickness of the densest slab used to re-seed the wall
_WALL_SLAB_MIN_FRACTION = 0.25  # the seed slab must hold at least this share of all points
_WALL_BAND_HALF_WIDTH_M = 1.5  # half-width of the band kept around the re-seeded wall plane


def _filter_points_near_plane(
    points: np.ndarray, centroid: np.ndarray, normal: np.ndarray, k: float = 3.0, max_iters: int = 3
) -> tuple[np.ndarray, np.ndarray]:
    """Drop points whose distance from the fitted plane (along `normal`) puts
    them nowhere near the actual photographed surface, re-estimating the
    centroid from the survivors each pass. An outdoor capture's oblique shots
    also see sky/distant terrain in the background, and COLMAP incidentally
    triangulates some of that too -- at a wildly different depth than the real
    facade standoff, so an iterative IQR trim ON THE DISTANCE-TO-PLANE axis
    (not the final u/v canvas axes -- those can still be dominated by a large,
    widely-spread background cluster even after this) separates the two
    clusters directly, since depth-from-plane is exactly the dimension they
    differ in by orders of magnitude.

    Fallback for when the wall ISN'T the dominant depth cluster (e.g. a flight
    that stayed close to the wall the whole time, so background points
    outnumber real wall points): if the surviving set is still spread over
    several metres along the normal, re-seed from whichever thin slab along
    the normal holds the densest cluster of points (the real wall, on the
    assumption that a wall's own points concentrate far more tightly along
    its own normal than any scattered background does) and keep only points
    within a band of that slab. A well-separated capture never needs this
    fallback, so its result is unaffected."""
    subset = points
    centroid_est = centroid
    for _ in range(max_iters):
        dist = (points - centroid_est) @ normal
        lo, hi = _robust_range(dist, k=k)
        inliers = points[(dist >= lo) & (dist <= hi)]
        if inliers.shape[0] == subset.shape[0] or inliers.shape[0] < 10:
            break
        subset = inliers
        centroid_est = subset.mean(axis=0)

    spread = np.percentile((subset - centroid_est) @ normal, 90) - np.percentile((subset - centroid_est) @ normal, 10)
    if spread <= _WALL_BAND_SPREAD_LIMIT_M:
        return subset, centroid_est

    d_all = (points - centroid_est) @ normal
    order = np.sort(d_all)
    slab_counts = np.searchsorted(order, order + _WALL_SLAB_THICKNESS_M) - np.arange(len(order))
    densest = int(np.argmax(slab_counts))
    if slab_counts[densest] < _WALL_SLAB_MIN_FRACTION * len(points):
        return subset, centroid_est  # no clearly dominant slab either -- keep the IQR-trimmed result as-is

    reseeded_centroid = points[(d_all >= order[densest]) & (d_all <= order[densest] + _WALL_SLAB_THICKNESS_M)].mean(axis=0)
    for _ in range(max_iters):
        band_mask = np.abs((points - reseeded_centroid) @ normal) <= _WALL_BAND_HALF_WIDTH_M
        if band_mask.sum() < 10:
            break
        updated_centroid = points[band_mask].mean(axis=0)
        if np.allclose(updated_centroid, reseeded_centroid):
            break
        reseeded_centroid = updated_centroid
    band_mask = np.abs((points - reseeded_centroid) @ normal) <= _WALL_BAND_HALF_WIDTH_M
    return points[band_mask], reseeded_centroid


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

    Outlier rejection (2026-09-29): `_filter_points_near_plane`, an iterative
    distance-along-normal IQR trim -- previewer's own earlier approach here
    (`_near_camera_track_mask` + `_ransac_plane_inlier_mask`, a fixed 5m
    RANSAC-plane-distance threshold) is still used by `fit_corner_planes`
    below, but turned out wrong for THIS function's job specifically:
    confirmed on a real 68-image facade (this project's own BACK capture)
    that the fixed 5m threshold rejects real, correctly-photographed wall
    content at the outer sections of a curved/staggered panel building (each
    entrance slightly rotated from its neighbors -- common on these
    Soviet-era apartment blocks) just because it's more than 5m off the
    single plane the CENTER section fit best, silently halving the rendered
    canvas width (31m of a true ~61m building -- exactly the "반만 나와요"
    bug the user reported) with no error or warning of any kind. A
    distribution-based (IQR) cutoff instead adapts to this particular point
    cloud's own actual spread rather than assuming a fixed physical
    distance, so it doesn't need to guess the right number in advance:
    confirmed against a known-good reference render for this exact 68-image
    set (canvas 61.19m x 41.96m) -- this approach reproduces that width
    almost exactly, where the old fixed-threshold approach could not
    (confirmed testing 5m through 20m: none reached even 44m).
    """
    points_all = np.array([p.xyz for p in reconstruction.points3D.values()])
    if points_all.shape[0] < 10:
        raise ValueError(f"too few triangulated points ({points_all.shape[0]}) to fit a facade plane")

    centers = np.array([_camera_center(img) for img in reconstruction.images.values()])
    world_up = _estimate_world_up(reconstruction)

    raw_centroid = points_all.mean(axis=0)
    _, _, vt0 = np.linalg.svd(points_all - raw_centroid, full_matrices=False)
    raw_normal = vt0[2]
    # 2026-09-29: with LoFTR matching the ground in front of the building gets triangulated
    # densely too, and on the 429-image BACK facade the all-points SVD normal came out as world-up
    # (the ground plane) -- a 228m x 310m canvas of smeared ground instead of the wall. The
    # cameras' own viewing direction doesn't have that problem: when enough of them look sideways
    # rather than straight down, the wall normal is their mean viewing direction reversed,
    # flattened to horizontal.
    wall_normal = _wall_normal_from_cameras(reconstruction, world_up)
    if wall_normal is not None:
        raw_normal = wall_normal
    points, _ = _filter_points_near_plane(points_all, raw_centroid, raw_normal)
    if points.shape[0] < 10:
        points = points_all

    return fit_plane_from_points(
        points, centers, px_per_m=px_per_m, padding_m=padding_m, world_up=world_up, normal=wall_normal,
    )


def _wall_normal_from_cameras(
    reconstruction: pycolmap.Reconstruction,
    world_up: np.ndarray,
    min_oblique: int = 4,
) -> np.ndarray | None:
    """Horizontal wall normal (pointing from the wall toward the cameras) from the registered
    cameras' viewing directions, or None when fewer than `min_oblique` cameras look more than 45
    degrees away from straight down (a roof/nadir capture has no wall to face)."""
    forwards = []
    for img in reconstruction.images.values():
        R = img.cam_from_world().rotation.matrix()
        forwards.append(R.T @ np.array([0.0, 0.0, 1.0]))  # camera +Z (optical axis) in world
    forwards = np.array(forwards)
    oblique = forwards @ world_up > -np.cos(np.deg2rad(45.0))
    if int(oblique.sum()) < min_oblique:
        return None
    normal = -forwards[oblique].mean(axis=0)
    normal = normal - np.dot(normal, world_up) * world_up
    norm = np.linalg.norm(normal)
    if norm < 1e-6:
        return None
    return normal / norm


def fit_plane_from_points(
    points: np.ndarray,
    camera_centers: np.ndarray,
    px_per_m: float = 100.0,
    padding_m: float = 2.0,
    world_up: np.ndarray | None = None,
    normal: np.ndarray | None = None,
) -> FacadePlane:
    """`normal`, if given, is used instead of the SVD normal of `points`
    (facade_plane_from_reconstruction passes the camera-derived wall normal).

    Core SVD plane-fit + orientation logic shared by
    `facade_plane_from_reconstruction` (the front wall, `points` already
    outlier-rejected by `_filter_points_near_plane`) and `fit_corner_planes`
    (a building-corner side/gable wall, `points` are the OUTLIER points from
    the front-plane fit -- see that function's docstring, which still uses
    the older `_near_camera_track_mask`/`_ransac_plane_inlier_mask` pair --
    a small, already-curated corner point set isn't affected by the
    curved-facade failure mode that motivated switching the front wall's own
    filtering, see facade_plane_from_reconstruction). `points`/
    `camera_centers` are assumed already curated by the caller; this function
    only does the geometry (SVD normal, track-aligned+sign-fixed e_u,
    world-anchored e_v, canvas extent via `_robust_range`) -- see
    facade_plane_from_reconstruction for what each step means and why.
    `world_up`: gravity-up estimate (see _estimate_world_up) -- defaults to
    the old fixed [0,0,1] (only valid on a UTM-aligned reconstruction) if the
    caller doesn't have one.
    """
    if world_up is None:
        world_up = np.array([0.0, 0.0, 1.0])
    centroid = points.mean(axis=0)
    # full_matrices=False: this is an (N,3) matrix, and only the (3,3) Vt is ever used (U is
    # discarded) -- the default full_matrices=True still tries to materialize an (N,N) U, which
    # for N in the hundred-thousands is a real crash (observed: 100,048 points -> an attempted
    # 74.6GB allocation for a U this code never even reads).
    _, _, vt = np.linalg.svd(points - centroid, full_matrices=False)
    normal = vt[2] if normal is None else np.asarray(normal, dtype=np.float64)

    is_vertical_wall = abs(float(np.dot(normal, world_up))) < 0.5

    if is_vertical_wall:
        # e_u forced geometrically perpendicular to true world-up (within
        # the plane), instead of trusted from the flight-track PCA below.
        # 2026-09-28: the track-PCA axis turned out to still render facades
        # tilted even with a correct world_up estimate (_estimate_world_up)
        # feeding e_v -- on a small/fragmented registered subset (e.g. 28 of
        # 297 requested images, most dropped by COLMAP's own registration
        # failures) the camera centers' own principal direction isn't a
        # clean horizontal line, so e_u came out tilted regardless of how
        # accurate "up" was, and normal/e_u/e_v are mutually perpendicular
        # by construction so that tilt in e_u alone was enough to skew the
        # whole rendered grid. Deriving e_u directly from cross(world_up,
        # normal) instead guarantees it is exactly horizontal, independent
        # of any noise or partial coverage in the actual flight path.
        e_u = np.cross(world_up, normal)
        if np.linalg.norm(e_u) < 1e-6:
            is_vertical_wall = False  # normal ~= world_up: degenerate, fall through below

    if not is_vertical_wall:
        # rooftop/plan-view (or the above degenerate fallback) -- no natural
        # "up" to anchor e_u to, so it comes from the flight track's own PCA
        # instead (see this function's docstring for why the track is a
        # cleaner "horizontal" than the point cloud's own PCA in this case).
        track = _principal_direction(camera_centers) if camera_centers.shape[0] >= 2 else vt[0]
        e_u = track - np.dot(track, normal) * normal
        if np.linalg.norm(e_u) < 1e-6:
            e_u = vt[0] - np.dot(vt[0], normal) * normal
    e_u = e_u / np.linalg.norm(e_u)

    # SVD/cross-product never fix which of the two opposite directions along
    # e_u's axis is "positive" (2026-09-09 CLAUDE.local.md entry: a real run
    # rendered every source photo left-right mirrored -- confirmed by the
    # user against the drone's own front-facing reference view -- with the
    # mirror direction being whatever np.linalg.svd's internal sign
    # convention happened to produce for that day's point cloud, not a fixed
    # always-flipped bug). Fix the SIGN (not the axis) against an
    # unambiguous physical reference instead: the camera centers sit on the
    # *outward* (viewer) side of the wall by construction, so centroid ->
    # mean-camera-position is a reliable "outward" direction with no sign
    # ambiguity at all. A viewer facing the wall (looking the opposite way,
    # "into" it) with true world-up should see canvas +u as their own right
    # hand -- forward x up, the standard right-handed camera convention.
    if camera_centers.shape[0] >= 1:
        outward = camera_centers.mean(axis=0) - centroid
        forward = -outward
        right_ref = np.cross(forward, world_up)
        right_ref_norm = np.linalg.norm(right_ref)
        if right_ref_norm > 1e-6 and np.dot(e_u, right_ref / right_ref_norm) < 0.0:
            e_u = -e_u

    if is_vertical_wall:
        # v is true world-up, so "up" always renders up regardless of which
        # way the flight track happened to point.
        e_v = -world_up
    else:
        # no natural "up" to anchor to, so v is whatever stays perpendicular
        # to the track-aligned u within the plane.
        e_v = np.cross(normal, e_u)
        e_v = e_v / np.linalg.norm(e_v)

    u = (points - centroid) @ e_u
    v = (points - centroid) @ e_v
    u_min, u_max = _robust_range(u)
    v_min, v_max = _robust_range(v)
    width_m = (u_max - u_min) + 2 * padding_m
    height_m = (v_max - v_min) + 2 * padding_m
    origin = centroid + e_u * (u_min - padding_m) + e_v * (v_min - padding_m)

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
    world_up = _estimate_world_up(reconstruction)
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

        plane = fit_plane_from_points(points_final, corner_centers_arr, world_up=world_up)
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


def gps_camera_centers(
    by_id: dict[str, ImageMetadata], utm_epsg: int, image_ids,
) -> dict[str, np.ndarray]:
    """{image_id: GPS position as a UTM+altitude camera center} for the given images that have GPS."""
    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    centers: dict[str, np.ndarray] = {}
    for image_id in image_ids:
        meta = by_id.get(image_id)
        if meta is None or meta.gps.latitude is None or meta.gps.altitude_m is None:
            continue
        x, y = transformer.transform(meta.gps.longitude, meta.gps.latitude)
        centers[image_id] = np.array([x, y, meta.gps.altitude_m])
    return centers


def fill_uncovered_pixels(
    image: np.ndarray, observed_mask: np.ndarray, fillers: list[WarpedImage],
) -> int:
    """Pastes each filler (in order) into the pixels of `image` that `observed_mask` still marks as
    not photographed, updating both in place. Returns the number of pixels filled.

    2026-09-29: this app answers "was any part of the wall NOT photographed?", so a photo left out
    of the main mosaic must never turn a photographed spot into a black gap. On the 216-photo BACK
    run the bottom-right corner was photographed only by photos COLMAP had misplaced by a few floors
    (excluded by gps_inconsistent_images), and the mosaic showed a hole there that the capture did
    not have. Fillers only ever go where nothing else landed, so they can't degrade covered areas."""
    filled = 0
    for w in fillers:
        x, y = w.corner
        lw, lh = w.size
        region_obs = observed_mask[y:y + lh, x:x + lw]
        take = (w.mask[:region_obs.shape[0], :region_obs.shape[1]] > 0) & (region_obs == 0)
        n = int(take.sum())
        if n == 0:
            continue
        image[y:y + lh, x:x + lw][take] = w.image[:region_obs.shape[0], :region_obs.shape[1]][take]
        region_obs[take] = 255
        filled += n
    return filled


def gps_inconsistent_images(
    reconstruction: pycolmap.Reconstruction,
    by_id: dict[str, ImageMetadata],
    utm_epsg: int,
    min_error_m: float = 3.0,
    median_multiple: float = 10.0,
) -> dict[str, float]:
    """{image_id: error_m} for registered images whose camera position in an already
    UTM-aligned reconstruction is far from their own GPS position -- threshold is
    max(`min_error_m`, `median_multiple` x the median error of all images).

    2026-09-29, 429-image BACK facade with LoFTR matching: every image registered and the
    median camera-vs-GPS error was 0.26m, but a group of rooftop-level photos sat 17-19m too low
    -- the repetitive floor pattern let COLMAP chain them onto the wall a few floors down, and
    rectify_images then pasted their sky/roof content into the middle of the facade. Their own
    GPS disagrees with that placement by far more than the rest of the flight does."""
    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    errors: dict[str, float] = {}
    for img in reconstruction.images.values():
        image_id = Path(img.name).stem
        meta = by_id.get(image_id)
        if meta is None or meta.gps.latitude is None or meta.gps.altitude_m is None:
            continue
        x, y = transformer.transform(meta.gps.longitude, meta.gps.latitude)
        errors[image_id] = float(np.linalg.norm(_camera_center(img) - np.array([x, y, meta.gps.altitude_m])))
    if not errors:
        return {}
    threshold = max(min_error_m, median_multiple * float(np.median(list(errors.values()))))
    return {image_id: e for image_id, e in errors.items() if e > threshold}


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
    pose_overrides: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
) -> tuple[dict[str, WarpedImage], tuple[int, int]]:
    """`pose_overrides` {image_id: (R, t)} projects that image with this cam_from_world
    rotation/translation instead of its reconstruction pose (see fill_uncovered_pixels).

    Undistort + plane-project every registered image onto one fixed,
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
    # 2026-09-29: a bent COLMAP reconstruction (215-photo BACK run, cameras up to 2km off their GPS)
    # stretched the fitted plane until a single canvas-sized warp needed 4.5GB and OpenCV died with
    # an out-of-memory error. No single facade is anywhere near this size, so stop with a readable
    # error instead.
    if canvas_w * canvas_h > _MAX_CANVAS_PIXELS:
        raise ValueError(
            f"facade canvas {plane.width_m:.0f}m x {plane.height_m:.0f}m is implausibly large -- "
            "the COLMAP reconstruction is most likely broken"
        )
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

        if pose_overrides and image_id in pose_overrides:
            R, t = pose_overrides[image_id]
        else:
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


def render_sparse_point_splat(
    facade_id: str,
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path | None = None,
    patch_radius_px: int = 6,
) -> MosaicResult:
    """Fast coverage-shape preview: projects COLMAP's own triangulated sparse points onto the
    fitted facade plane and, for each one, pastes a small real-pixel patch sampled from wherever
    that point was actually observed (its COLMAP track's first (image, point2D) observation --
    Point2D.xy is the exact pixel it was detected at, no reprojection math needed) instead of a
    flat single-color dot. No per-image homography warp, no seam-finding/blending, no dense
    stereo -- this still skips everything rectify_and_blend does except the plane projection
    itself, which is why it's orders of magnitude cheaper, but small real patches read as
    recognizable wall texture (window edges, floor lines) in a way flat dots never could
    (2026-09-28, explicit user call after seeing the flat-dot version: "점에 이미지를 붙이라고").

    `images_dir` is required to get patches (falls back to flat Point3D.color dots, the original
    2026-09-28 behavior, if not given or a source file can't be read -- e.g. a caller that only
    has the reconstruction, not the original capture folder, still gets a usable result).

    Deliberately not attempting rectify_and_blend's output quality: this app's stated purpose is
    an on-site "did I cover the whole wall" check, not final crack-level imagery (see
    pipeline.yaml's colmap.mode comment, and the main CheckCrack repo's full-res pipeline is where
    that actually happens) -- a recognizable point-cloud silhouette of the wall satisfies that,
    not a seamless photographic mosaic.

    Returns the same MosaicResult shape rectify_and_blend does, so
    _run_colmap_only_pipeline needs no changes beyond which function it calls.
    coverage_ratio is measured the same way observed_mask always has been (fraction of canvas
    pixels marked observed), just cheaper to produce: each splatted patch's footprint IS the "was
    this bit of wall photographed" signal here, in place of rectify_and_blend's per-pixel warped
    image coverage.
    """
    width_px = max(1, int(round(plane.width_m * plane.px_per_m)))
    height_px = max(1, int(round(plane.height_m * plane.px_per_m)))
    canvas = np.zeros((height_px, width_px, 3), dtype=np.uint8)
    mask = np.zeros((height_px, width_px), dtype=np.uint8)

    images_dir = Path(images_dir) if images_dir is not None else None
    # Every point observed by the same image re-reads that one file -- caching avoids re-decoding
    # a 297-frame facade's JPEGs thousands of times over (once per point, not once per image).
    source_image_cache: dict[str, np.ndarray | None] = {}

    def _load_source(name: str) -> np.ndarray | None:
        if name not in source_image_cache:
            source_image_cache[name] = imread_unicode(str(images_dir / name)) if images_dir is not None else None
        return source_image_cache[name]

    ps = patch_radius_px
    for p in reconstruction.points3D.values():
        rel = p.xyz - plane.origin
        u = float(np.dot(rel, plane.e_u))
        v = float(np.dot(rel, plane.e_v))
        px = int(round(u * plane.px_per_m))
        py = int(round(v * plane.px_per_m))
        if not (0 <= px < width_px and 0 <= py < height_px):
            continue

        patch = None
        if p.track.elements:
            elem = p.track.elements[0]
            if elem.image_id in reconstruction.images:
                src_image = reconstruction.images[elem.image_id]
                src = _load_source(src_image.name)
                if src is not None:
                    sx, sy = src_image.point2D(elem.point2D_idx).xy
                    sx, sy = int(round(sx)), int(round(sy))
                    y0, y1 = max(0, sy - ps), min(src.shape[0], sy + ps + 1)
                    x0, x1 = max(0, sx - ps), min(src.shape[1], sx + ps + 1)
                    if y1 > y0 and x1 > x0:
                        patch = src[y0:y1, x0:x1]

        cy0, cy1 = max(0, py - ps), min(height_px, py + ps + 1)
        cx0, cx1 = max(0, px - ps), min(width_px, px + ps + 1)
        if cy1 <= cy0 or cx1 <= cx0:
            continue
        if patch is not None:
            ch, cw = cy1 - cy0, cx1 - cx0
            canvas[cy0:cy1, cx0:cx1] = cv2.resize(patch, (cw, ch), interpolation=cv2.INTER_AREA)
        else:
            color_bgr = (int(p.color[2]), int(p.color[1]), int(p.color[0]))  # RGB -> cv2's BGR
            canvas[cy0:cy1, cx0:cx1] = color_bgr
        mask[cy0:cy1, cx0:cx1] = 255

    coverage_ratio = float(mask.mean()) / 255.0 if mask.size else 0.0
    quality = StitchQualityReport(
        facade_id=facade_id,
        image_count=len(reconstruction.images),
        coverage_ratio=coverage_ratio,
    )
    return MosaicResult(analysis_image=canvas, visual_image=canvas, observed_mask=mask, quality=quality)


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
    # 2026-09-28: user's explicit call for the 15-minute field-use budget ("blend 안해도 됨") --
    # blend_visual is real extra cost beyond blend_analysis (exposure-compensation solves a linear
    # system over every warped image, then a full cv2.detail_MultiBandBlender Laplacian-pyramid
    # blend), on top of the seam-finding both share. blend_analysis already IS a correct "no blend"
    # hard-seam composite (cv2.detail.Blender_NO -- literally the OpenCV no-op blender), so
    # `fast_paste_only` just reuses that result for visual_image too instead of also paying for
    # blend_visual's extra passes. Coverage-check use doesn't need the exposure-matched/feathered
    # look blend_visual exists for.
    fast_paste_only = bool(scfg.fast_paste_only) if "fast_paste_only" in scfg else False
    if fast_paste_only:
        visual_image = analysis_image.copy() if analysis_image is not None else None
    else:
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
