"""GPU dense-stereo depth for true per-pixel facade projection.

`rectify_images` (rectification.py) places every registered image on the
facade canvas with a single closed-form homography per image
(`_camera_to_facade_homography`), which is only correct for pixels that
actually lie *on* the fitted facade plane. That is a fine assumption for the
wall itself, but wrong for anything standing proud of it -- a balcony, a
ledge, an AC unit -- whose pixels get sheared/stretched to wherever the
flat-plane homography happens to send them instead of to where they really
are (the "blue blob" balcony-warp defect flagged from real mosaic output).

This module runs COLMAP's own CUDA dense-stereo (`patch_match_stereo`) to
recover a true per-pixel depth for each image, so those genuinely off-plane
pixels can be re-projected using their own real 3D position (found via
camera ray + depth, then dropped orthogonally onto the canvas plane) instead
of the flat-plane assumption. On-plane pixels are left completely untouched
by this module -- they already get the correct answer from the existing
homography path, and re-deriving them from noisier per-pixel depth would
only add risk for zero benefit.

COLMAP's dense-stereo module is CUDA-only (there is no CPU implementation in
COLMAP itself -- confirmed via upstream: `pycolmap.PatchMatchOptions` only
exposes a `gpu_index` selector, never a CPU mode). `is_gpu_dense_available()`
detects this cleanly via `pycolmap.has_cuda`/`get_num_cuda_devices()`; every
public function here degrades to a no-op (returns None / skips correction)
rather than raising when GPU dense stereo isn't available, so a CPU-only
machine silently keeps using the flat-homography-only path it always used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pycolmap

logger = logging.getLogger(__name__)


def is_gpu_dense_available() -> bool:
    """True iff this machine can actually run pycolmap's CUDA dense stereo.

    COLMAP's dense module (patch_match_stereo / stereo_fusion) has no CPU
    fallback, so this is also the single gate for "is Option D (real depth)
    available at all on this machine" throughout the pipeline.
    """
    try:
        return bool(pycolmap.has_cuda) and pycolmap.get_num_cuda_devices() > 0
    except Exception:
        logger.warning("pycolmap CUDA capability check failed", exc_info=True)
        return False


@dataclass
class ImageDepthInfo:
    """Everything `rectification.py` needs to depth-correct one image, all
    already resolved to the exact pixel grid the depth map itself uses
    (COLMAP's PatchMatch internally works at `max_image_size`, which is
    normally *smaller* than the image's true undistorted resolution -- see
    `_load_one_depth` for why color and depth are both resampled to this
    shared grid rather than trying to upsample depth to full resolution).
    """

    depth_map: np.ndarray  # (h, w) float32 meters, 0.0 = no valid estimate
    color_bgr: np.ndarray  # (h, w, 3) uint8, resampled to match depth_map
    K: np.ndarray  # (3, 3) intrinsics for this exact (h, w) grid


def run_dense_stereo(
    sparse_dir: str | Path,
    images_dir: str | Path,
    workspace_dir: str | Path,
    max_image_size: int = 1600,
    geom_consistency: bool = True,
    gpu_index: int = 0,
) -> Path | None:
    """Undistort the registered images and run CUDA patch_match_stereo into
    `workspace_dir`. Returns `workspace_dir` on success, or None (never
    raises) if GPU dense stereo isn't available or the run fails for any
    reason -- callers are expected to fall back to the flat-plane-only path
    exactly as if this module didn't exist.
    """
    if not is_gpu_dense_available():
        logger.info("GPU dense stereo unavailable (no CUDA device found) -- skipping depth correction")
        return None

    workspace_dir = Path(workspace_dir)
    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        pycolmap.undistort_images(
            output_path=str(workspace_dir),
            input_path=str(sparse_dir),
            image_path=str(images_dir),
        )
        opts = pycolmap.PatchMatchOptions()
        opts.max_image_size = int(max_image_size)
        opts.geom_consistency = bool(geom_consistency)
        opts.gpu_index = str(gpu_index)
        pycolmap.patch_match_stereo(str(workspace_dir), options=opts)
    except Exception:
        logger.warning("GPU dense stereo run failed -- falling back to flat-plane-only rectification", exc_info=True)
        return None
    return workspace_dir


def _read_depth_map(workspace_dir: Path, image_name: str) -> np.ndarray | None:
    """Prefer the geometrically-filtered depth map (cross-checked against
    neighboring views, per COLMAP docs the recommended one to consume) and
    fall back to the photometric-only one if geometric consistency was
    disabled or that file is missing for this particular image."""
    depth_dir = workspace_dir / "stereo" / "depth_maps"
    for suffix in (".geometric.bin", ".photometric.bin"):
        path = depth_dir / f"{image_name}{suffix}"
        if path.exists():
            dm = pycolmap.DepthMap()
            dm.read(str(path))
            return dm.to_array()
    return None


def _load_one_depth(workspace_dir: Path, reconstruction: pycolmap.Reconstruction, image_name: str) -> ImageDepthInfo | None:
    depth_map = _read_depth_map(workspace_dir, image_name)
    if depth_map is None or not np.any(depth_map > 0):
        return None

    undist_image_path = workspace_dir / "images" / image_name
    color_full = cv2.imread(str(undist_image_path), cv2.IMREAD_COLOR)
    if color_full is None:
        return None

    undist_recon = pycolmap.Reconstruction(str(workspace_dir / "sparse"))
    undist_img = next((im for im in undist_recon.images.values() if im.name == image_name), None)
    if undist_img is None:
        return None
    cam = undist_img.camera
    K_full = cam.calibration_matrix()

    dm_h, dm_w = depth_map.shape
    # PatchMatch works at its own internal (typically downsized) resolution,
    # independent of the full undistorted image size undistort_images wrote
    # to disk -- resample color down to the depth grid (rather than trying
    # to upsample depth to full color resolution) so every pixel in
    # ImageDepthInfo refers to the exact same physical sample point.
    scale = dm_w / float(cam.width)
    color_small = cv2.resize(color_full, (dm_w, dm_h), interpolation=cv2.INTER_AREA)
    K_scaled = K_full.copy()
    K_scaled[0, :] *= scale
    K_scaled[1, :] *= scale

    return ImageDepthInfo(depth_map=depth_map, color_bgr=color_small, K=K_scaled)


def load_depth_corrections(
    workspace_dir: Path,
    reconstruction: pycolmap.Reconstruction,
    image_names: dict[str, str],
) -> dict[str, ImageDepthInfo]:
    """Load per-image depth for every image that has a usable depth map.

    `image_names` maps image_id (Path(name).stem, `rectify_images`'s own
    keying) -> the original registered file name (e.g. "DJI_0045.JPG"),
    which is also the name `undistort_images` wrote its copy under.
    """
    out: dict[str, ImageDepthInfo] = {}
    for image_id, name in image_names.items():
        try:
            info = _load_one_depth(workspace_dir, reconstruction, name)
        except Exception:
            logger.warning("failed to load dense depth for %s -- keeping flat-plane-only projection", name, exc_info=True)
            continue
        if info is not None:
            out[image_id] = info
    return out


def apply_depth_correction(
    dst_image: np.ndarray,
    dst_mask: np.ndarray,
    corner_x: int,
    corner_y: int,
    depth_info: ImageDepthInfo,
    R: np.ndarray,
    t: np.ndarray,
    plane_origin: np.ndarray,
    plane_e_u: np.ndarray,
    plane_e_v: np.ndarray,
    px_per_m: float,
    deviation_threshold_m: float,
    max_deviation_m: float = 3.0,
) -> None:
    """Overwrite the off-plane pixels of one image's already-warped local
    canvas buffer (`dst_image`/`dst_mask`, exactly what `rectify_images`
    produced via the flat-plane homography) with their true depth-projected
    position, in place. On-plane pixels of the same image are left
    completely untouched -- only pixels whose real 3D position deviates from
    the fitted plane by more than `deviation_threshold_m` are ever moved.

    `R`, `t` are the SAME cam_from_world pose already used for this image's
    flat homography (not re-derived from the undistorted workspace's own
    sparse model) so that, for a hypothetical pixel exactly on the plane,
    this method and the flat homography agree to floating-point precision --
    no seam can appear at the boundary between "corrected" and
    "not corrected" pixels of the same image.

    `max_deviation_m` bounds correction to genuine near-wall protrusions
    (balconies, ledges -- realistically well under a few meters proud of the
    wall). Without this, sky/distant-terrain pixels (which the *existing*
    flat-homography path already tolerates, because a near-grazing camera
    ray intersected with the infinite plane typically lands far outside any
    image's local canvas ROI and gets clipped) can slip through here
    instead: orthographic projection has no such natural divergence, so a
    background point can coincidentally land *inside* the canvas footprint
    even at 30-100m from the wall -- confirmed on real data, ~5-30% of
    flagged "off-plane" background pixels landed in-bounds. Anything beyond
    this range is left exactly as the flat homography already rendered it.
    """
    depth_map = depth_info.depth_map
    valid = depth_map > 0
    if not np.any(valid):
        return

    ys, xs = np.nonzero(valid)
    depths = depth_map[ys, xs].astype(np.float64)

    K_inv = np.linalg.inv(depth_info.K)
    pix_h = np.stack([xs.astype(np.float64), ys.astype(np.float64), np.ones(xs.shape[0])], axis=0)
    rays_cam = K_inv @ pix_h  # (3, N), z-component == 1 for every ray
    X_cam = rays_cam * depths[None, :]  # COLMAP's PatchMatch depth is camera-space Z, so this is exact
    X_world = R.T @ (X_cam - t[:, None])  # (3, N)

    normal = np.cross(plane_e_u, plane_e_v)
    normal = normal / np.linalg.norm(normal)
    delta = X_world - plane_origin[:, None]  # (3, N)
    dist_from_plane = normal @ delta  # (N,)

    abs_dist = np.abs(dist_from_plane)
    off_plane = (abs_dist > deviation_threshold_m) & (abs_dist <= max_deviation_m)
    if not np.any(off_plane):
        return  # this image has nothing genuinely (near-wall) off-plane -- nothing to correct

    u_px = (plane_e_u @ delta) * px_per_m
    v_px = (plane_e_v @ delta) * px_per_m
    local_u = np.round(u_px - corner_x).astype(np.int64)
    local_v = np.round(v_px - corner_y).astype(np.int64)

    local_h, local_w = dst_image.shape[:2]
    sel = off_plane & (local_u >= 0) & (local_u < local_w) & (local_v >= 0) & (local_v < local_h)
    if not np.any(sel):
        return

    lu, lv = local_u[sel], local_v[sel]
    colors = depth_info.color_bgr[ys[sel], xs[sel]]

    scratch_image = np.zeros((local_h, local_w, 3), dtype=np.uint8)
    scratch_mask = np.zeros((local_h, local_w), dtype=np.uint8)
    scratch_image[lv, lu] = colors
    scratch_mask[lv, lu] = 255

    # Forward-scattering points (as opposed to warpPerspective's
    # backward/gather sampling) can leave 1-2px sub-pixel gaps wherever the
    # canvas's px_per_m is finer than the depth map's own (typically ~800px
    # wide) working resolution. A single cheap dilation pass closes those
    # without materially affecting the shape of the corrected region -- any
    # gap this doesn't close just keeps its original flat-homography pixel,
    # which is a safe degradation (same result as before this correction
    # existed), never a new artifact.
    kernel = np.ones((3, 3), np.uint8)
    filled_mask = cv2.dilate(scratch_mask, kernel, iterations=1)
    filled_image = cv2.dilate(scratch_image, kernel, iterations=1)
    write = filled_mask > 0
    dst_image[write] = filled_image[write]
    dst_mask[write] = 255
