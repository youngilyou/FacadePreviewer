"""Perspective warp into a shared facade canvas (CLAUDE.local.md #13/#14).

OpenCV does the actual pixel warp here — Kornia owns feature/geometry
(LoFTR + RANSAC + homography), OpenCV owns image warp/blend utility (#2.1/#2.2).
Each image is warped into its own tight local ROI (not the full shared
canvas) purely to keep per-image memory bounded; ROIs are positioned via
`corner` so the blender can still composite them onto one canvas.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class WarpedImage:
    image: np.ndarray  # BGR uint8, local ROI size
    mask: np.ndarray  # uint8 0/255, valid-pixel mask, local ROI size
    corner: tuple[int, int]  # (x, y) top-left of this ROI in shared canvas coords
    size: tuple[int, int]  # (w, h) of this ROI


def _transform_corners(h: int, w: int, H: np.ndarray) -> np.ndarray:
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(corners, H).reshape(-1, 2)


def compute_canvas_bounds(
    images: dict[str, np.ndarray], homographies: dict[str, np.ndarray]
) -> tuple[float, float, float, float]:
    """Bounding box (min_x, min_y, max_x, max_y) of all images warped into the reference frame."""
    min_x = min_y = np.inf
    max_x = max_y = -np.inf
    for image_id, H in homographies.items():
        if image_id not in images:
            continue
        h, w = images[image_id].shape[:2]
        corners = _transform_corners(h, w, H)
        min_x = min(min_x, corners[:, 0].min())
        min_y = min(min_y, corners[:, 1].min())
        max_x = max(max_x, corners[:, 0].max())
        max_y = max(max_y, corners[:, 1].max())
    return min_x, min_y, max_x, max_y


def warp_images(
    images: dict[str, np.ndarray], homographies: dict[str, np.ndarray]
) -> tuple[dict[str, WarpedImage], tuple[int, int]]:
    """Warp every image into the shared reference frame.

    Returns (per-image WarpedImage in its own local ROI, (canvas_w, canvas_h)).
    """
    min_x, min_y, max_x, max_y = compute_canvas_bounds(images, homographies)
    canvas_w = int(np.ceil(max_x - min_x))
    canvas_h = int(np.ceil(max_y - min_y))
    global_shift = np.array([[1, 0, -min_x], [0, 1, -min_y], [0, 0, 1]])

    warped: dict[str, WarpedImage] = {}
    for image_id, H in homographies.items():
        if image_id not in images:
            continue
        img = images[image_id]
        h, w = img.shape[:2]
        H_global = global_shift @ H
        corners = _transform_corners(h, w, H_global)
        local_min_x, local_min_y = corners.min(axis=0)
        local_max_x, local_max_y = corners.max(axis=0)
        local_w = max(1, int(np.ceil(local_max_x - local_min_x)))
        local_h = max(1, int(np.ceil(local_max_y - local_min_y)))

        local_shift = np.array([[1, 0, -local_min_x], [0, 1, -local_min_y], [0, 0, 1]])
        H_local = local_shift @ H_global

        warped_img = cv2.warpPerspective(img, H_local, (local_w, local_h), flags=cv2.INTER_LINEAR)
        src_mask = np.full((h, w), 255, dtype=np.uint8)
        warped_mask = cv2.warpPerspective(src_mask, H_local, (local_w, local_h), flags=cv2.INTER_NEAREST)

        warped[image_id] = WarpedImage(
            image=warped_img,
            mask=warped_mask,
            corner=(int(round(local_min_x)), int(round(local_min_y))),
            size=(local_w, local_h),
        )

    return warped, (canvas_w, canvas_h)
