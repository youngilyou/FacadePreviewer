"""Seam + blend (CLAUDE.local.md #14).

Both outputs share the same seam decision (which source image owns each
canvas pixel) computed once via OpenCV's graph-cut seam finder. They differ
only in how hard that seam edge is: `blend_analysis` pastes with no
cross-fade to keep crack detail sharp, `blend_visual` runs exposure
compensation + multi-band blending for a presentable mosaic.
"""

from __future__ import annotations

import cv2
import numpy as np

from src.stitching.warp import WarpedImage

# OpenCV's detail Blender/SeamFinder transparently use OpenCL (UMat) when
# available. On this machine that OpenCL context fights the PyTorch CUDA
# context (LoFTR) for the same GPU and intermittently fails with
# CL_OUT_OF_RESOURCES on nothing-special buffer sizes (crashed a real run
# on a 34-image facade's blender.prepare()). This is a CPU-bound step
# regardless; force plain Mat processing instead of chasing GPU contention.
cv2.ocl.setUseOpenCL(False)


def compute_seam_masks(
    warped: dict[str, WarpedImage], canvas_size: tuple[int, int], seam_max_dim: int = 1600
) -> dict[str, np.ndarray]:
    """Partition each canvas pixel to exactly one source image.

    The seam finder itself produces a clean partition at its own (small)
    resolution. Resizing each image's small seam mask back up to full
    resolution *independently* — the original approach — round-trips
    through a different rounding for every image, so two adjacent images'
    upscaled masks can disagree by a pixel or two along the boundary and
    leave a thin strip neither claims. Composited, that strip is background
    (black) — a hairline that follows real image content and reads exactly
    like a crack, which is the worst possible artifact for this project.
    Routing every image's small seam mask through one shared small-scale
    canvas label map before the single upscale removes that disagreement.
    """
    canvas_w, canvas_h = canvas_size
    scale = min(1.0, seam_max_dim / max(canvas_w, canvas_h, 1))

    ids = list(warped.keys())
    small_images, small_masks, small_corners, small_sizes = [], [], [], []
    for image_id in ids:
        w = warped[image_id]
        small_w = max(1, int(round(w.size[0] * scale)))
        small_h = max(1, int(round(w.size[1] * scale)))
        small_img = cv2.resize(w.image, (small_w, small_h), interpolation=cv2.INTER_AREA)
        small_mask = cv2.resize(w.mask, (small_w, small_h), interpolation=cv2.INTER_NEAREST)
        small_images.append(small_img.astype(np.float32) / 255.0)
        small_masks.append(small_mask)
        small_corners.append((int(round(w.corner[0] * scale)), int(round(w.corner[1] * scale))))
        small_sizes.append((small_w, small_h))

    finder = cv2.detail.GraphCutSeamFinder("COST_COLOR")
    small_seam_masks = finder.find(small_images, small_corners, small_masks)

    small_canvas_w = max(1, int(round(canvas_w * scale)))
    small_canvas_h = max(1, int(round(canvas_h * scale)))
    label_canvas = np.full((small_canvas_h, small_canvas_w), -1, dtype=np.int16)
    for idx, ((cx, cy), (sw, sh), seam) in enumerate(zip(small_corners, small_sizes, small_seam_masks)):
        if isinstance(seam, cv2.UMat):
            seam = seam.get()
        x0, y0 = max(0, cx), max(0, cy)
        x1, y1 = min(small_canvas_w, cx + sw), min(small_canvas_h, cy + sh)
        if x1 <= x0 or y1 <= y0:
            continue
        seam_crop = np.asarray(seam)[y0 - cy : y1 - cy, x0 - cx : x1 - cx]
        region = label_canvas[y0:y1, x0:x1]
        region[seam_crop > 0] = idx

    label_full = cv2.resize(label_canvas, (canvas_w, canvas_h), interpolation=cv2.INTER_NEAREST)

    seam_masks: dict[str, np.ndarray] = {}
    for idx, image_id in enumerate(ids):
        w = warped[image_id]
        x, y = w.corner
        ww, hh = w.size
        full_seam = np.zeros((hh, ww), dtype=np.uint8)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(canvas_w, x + ww), min(canvas_h, y + hh)
        if x1 > x0 and y1 > y0:
            crop = (label_full[y0:y1, x0:x1] == idx).astype(np.uint8) * 255
            full_seam[y0 - y : y1 - y, x0 - x : x1 - x] = crop
        seam_masks[image_id] = cv2.bitwise_and(full_seam, w.mask)

    _fill_residual_gaps(warped, seam_masks, canvas_size)
    return seam_masks


def _fill_residual_gaps(
    warped: dict[str, WarpedImage], seam_masks: dict[str, np.ndarray], canvas_size: tuple[int, int]
) -> None:
    """Claim any canvas pixel a source image is valid at but no seam mask
    claimed (mutates `seam_masks` in place). The shared-label upscale above
    removes cross-image disagreement, but a source's *own* small-scale mask
    can still drop a few true-edge pixels during downscaling (thin/jagged
    warp boundaries) — those would otherwise render as background (black),
    i.e. more crack-look-alike hairlines."""
    canvas_w, canvas_h = canvas_size
    claimed = np.zeros((canvas_h, canvas_w), dtype=bool)
    bounds: dict[str, tuple[int, int, int, int]] = {}
    for image_id, w in warped.items():
        x, y = w.corner
        ww, hh = w.size
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(canvas_w, x + ww), min(canvas_h, y + hh)
        bounds[image_id] = (x0, y0, x1, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        claimed[y0:y1, x0:x1] |= seam_masks[image_id][y0 - y : y1 - y, x0 - x : x1 - x] > 0

    for image_id, w in warped.items():
        x, y = w.corner
        x0, y0, x1, y1 = bounds[image_id]
        if x1 <= x0 or y1 <= y0:
            continue
        valid = w.mask[y0 - y : y1 - y, x0 - x : x1 - x] > 0
        gap = valid & ~claimed[y0:y1, x0:x1]
        if gap.any():
            seam_masks[image_id][y0 - y : y1 - y, x0 - x : x1 - x][gap] = 255
            claimed[y0:y1, x0:x1] |= gap


def blend_analysis(
    warped: dict[str, WarpedImage], seam_masks: dict[str, np.ndarray], canvas_size: tuple[int, int]
) -> np.ndarray:
    """Minimal-blend composite: each canvas pixel is a hard copy from its seam-assigned source."""
    canvas_w, canvas_h = canvas_size
    blender = cv2.detail.Blender_createDefault(cv2.detail.Blender_NO)
    blender.prepare((0, 0, canvas_w, canvas_h))
    for image_id, w in warped.items():
        blender.feed(w.image.astype(np.int16), seam_masks[image_id], w.corner)
    dst, _ = blender.blend(None, None)
    return cv2.convertScaleAbs(dst)


def blend_visual(
    warped: dict[str, WarpedImage],
    seam_masks: dict[str, np.ndarray],
    canvas_size: tuple[int, int],
    num_bands: int = 5,
) -> np.ndarray:
    canvas_w, canvas_h = canvas_size
    ids = list(warped.keys())
    corners = [warped[i].corner for i in ids]
    images = [warped[i].image for i in ids]
    valid_masks = [warped[i].mask for i in ids]

    # Whole-image gain (not GAIN_BLOCKS): block compensation solves a linear
    # system sized (num_images * blocks_per_image)^2, and a single warped
    # ROI here can already approach the full canvas size (oblique homography
    # perspective stretches a source image's ROI well past its own
    # resolution), which blew that system up to hundreds of GB in practice.
    compensator = cv2.detail.ExposureCompensator_createDefault(cv2.detail.ExposureCompensator_GAIN)
    compensator.feed(corners, images, valid_masks)

    blender = cv2.detail_MultiBandBlender()
    blender.setNumBands(num_bands)
    blender.prepare((0, 0, canvas_w, canvas_h))

    for idx, image_id in enumerate(ids):
        w = warped[image_id]
        compensated = compensator.apply(idx, w.corner, w.image, seam_masks[image_id])
        if isinstance(compensated, cv2.UMat):
            compensated = compensated.get()
        blender.feed(compensated.astype(np.int16), seam_masks[image_id], w.corner)

    dst, _ = blender.blend(None, None)
    return cv2.convertScaleAbs(dst)
