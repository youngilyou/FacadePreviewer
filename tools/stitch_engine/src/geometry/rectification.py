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

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pycolmap
import pyproj

from src.common.imageio import imread_unicode
from src.common.types import ImageMetadata, StitchQualityReport
from src.stitching.blend import blend_analysis, blend_visual, compute_seam_masks
from src.stitching.mosaic import MosaicResult, paste_max
from src.stitching.warp import WarpedImage


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

    Orientation: if the fitted normal is mostly horizontal, this is the
    common facade case -- e_v is forced to true world-up rather than
    trusting SVD's second axis, which has no reason to already be vertical.
    Either way, e_u follows the flight track's own principal direction (PCA
    on the registered camera centers, projected onto the plane) rather than
    the point cloud's own PCA axis -- a drone pass flies roughly parallel to
    whatever it's photographing (a wall's edge, or a roof's long side), so
    the track is a far more natural "horizontal" than an axis derived purely
    from the (capture-direction-agnostic) point cloud shape.

    PoC-level: no outlier rejection on the point cloud yet (a few badly
    triangulated points could skew the fit).
    """
    points = np.array([p.xyz for p in reconstruction.points3D.values()])
    if points.shape[0] < 10:
        raise ValueError(f"too few triangulated points ({points.shape[0]}) to fit a facade plane")

    centroid = points.mean(axis=0)
    _, _, vt = np.linalg.svd(points - centroid)
    normal = vt[2]

    centers = np.array([_camera_center(img) for img in reconstruction.images.values()])
    track = _principal_direction(centers) if centers.shape[0] >= 2 else vt[0]
    e_u = track - np.dot(track, normal) * normal
    if np.linalg.norm(e_u) < 1e-6:
        e_u = vt[0] - np.dot(vt[0], normal) * normal
    e_u = e_u / np.linalg.norm(e_u)

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
) -> tuple[dict[str, WarpedImage], tuple[int, int]]:
    """Undistort + plane-project every registered image onto one fixed,
    plane-sized canvas (every image lands at corner (0,0), full canvas size
    -- unlike warp.py's per-image local-ROI trick, which exists specifically
    to bound a canvas that a drifting homography *chain* could blow up
    arbitrarily; that risk doesn't apply here since the canvas is fixed by
    the known facade span, not derived from where images project to)."""
    canvas_w = max(1, int(round(plane.width_m * plane.px_per_m)))
    canvas_h = max(1, int(round(plane.height_m * plane.px_per_m)))

    warped: dict[str, WarpedImage] = {}
    for img in reconstruction.images.values():
        image_id = Path(img.name).stem
        raw = imread_unicode(Path(images_dir) / img.name, cv2.IMREAD_COLOR)
        if raw is None:
            continue

        cam = img.camera
        K = cam.calibration_matrix()
        if cam.model.name == "SIMPLE_RADIAL" and abs(float(cam.params[3])) > 1e-9:
            k = float(cam.params[3])
            raw = cv2.undistort(raw, K, np.array([k, 0.0, 0.0, 0.0], dtype=np.float64))

        pose = img.cam_from_world()
        R = pose.rotation.matrix()
        t = np.asarray(pose.translation)
        H = _camera_to_facade_homography(K, R, t, plane)

        warped_img = cv2.warpPerspective(raw, H, (canvas_w, canvas_h), flags=cv2.INTER_LINEAR)
        src_mask = np.full(raw.shape[:2], 255, dtype=np.uint8)
        warped_mask = cv2.warpPerspective(src_mask, H, (canvas_w, canvas_h), flags=cv2.INTER_NEAREST)

        warped[image_id] = WarpedImage(image=warped_img, mask=warped_mask, corner=(0, 0), size=(canvas_w, canvas_h))

    return warped, (canvas_w, canvas_h)


def rectify_and_blend(
    facade_id: str,
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path,
    cfg,
    colmap_mean_reprojection_error_px: float | None = None,
) -> MosaicResult:
    """Full COLMAP-pose-rectified facade mosaic: plane-project -> seam ->
    blend, reusing the same seam/blend code the homography-chain path uses
    (stitching/blend.py) -- the two paths only disagree about where each
    image's pixels land on the canvas, not about how to combine them once
    there. `plane` is already fully built by the caller
    (facade_plane_from_reconstruction, previewer's only path).
    """
    warped, canvas_size = rectify_images(reconstruction, plane, images_dir)

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
