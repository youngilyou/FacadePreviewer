"""Facade-level stitch orchestration: graph -> warp -> seam -> blend -> quality report."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

from src.common.config import Config
from src.common.types import GeometryResult, StitchQualityReport
from src.stitching.blend import blend_analysis, blend_visual, compute_seam_masks
from src.stitching.graph import (
    build_stitch_graph,
    compute_drift_score,
    compute_global_homographies,
    count_connected_components,
    pick_reference,
)
from src.stitching.warp import warp_images
from src.sfm.colmap_runner import should_run_colmap


@dataclass
class MosaicResult:
    analysis_image: np.ndarray | None
    visual_image: np.ndarray | None
    observed_mask: np.ndarray
    quality: StitchQualityReport


def _safe_blend(blend_fn, *args, **kwargs) -> np.ndarray | None:
    try:
        return blend_fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — OpenCV raises its own cv2.error, plus MemoryError etc.
        import logging

        logging.getLogger(__name__).warning(
            "%s failed on a degenerate warp (%s: %s) — mosaic image will be missing but "
            "drift/coverage numbers below are unaffected, so COLMAP fallback can still run",
            blend_fn.__name__, type(exc).__name__, exc,
        )
        return None


def paste_max(canvas: np.ndarray, patch: np.ndarray, corner: tuple[int, int]) -> None:
    x, y = corner
    h, w = patch.shape[:2]
    y0, x0 = max(0, y), max(0, x)
    y1, x1 = min(canvas.shape[0], y + h), min(canvas.shape[1], x + w)
    if y1 <= y0 or x1 <= x0:
        return
    patch_crop = patch[y0 - y : y1 - y, x0 - x : x1 - x]
    roi = canvas[y0:y1, x0:x1]
    np.maximum(roi, patch_crop, out=roi)


def _emit_incremental_preview(
    warped: dict, canvas_size: tuple[int, int], on_preview: Callable[[np.ndarray, int, int], None]
) -> None:
    """Paste each already-warped image onto a small shared canvas one at a
    time and hand it to `on_preview` after every addition, so a caller can
    show the mosaic "filling in" image by image — CLAUDE.local.md #21 says
    don't shrink the real analysis mosaic, but this is a separate, disposable
    progress-preview artifact, so it's built at a capped size from the start
    rather than holding a full-resolution (possibly tens-of-thousands-of-px)
    color canvas just to look at it.
    """
    canvas_w, canvas_h = canvas_size
    preview_max_dim = 1400
    scale = min(1.0, preview_max_dim / max(canvas_w, canvas_h, 1))
    pv_w, pv_h = max(1, int(canvas_w * scale)), max(1, int(canvas_h * scale))
    preview_canvas = np.zeros((pv_h, pv_w, 3), dtype=np.uint8)

    for i, w in enumerate(warped.values(), start=1):
        if scale < 1.0:
            patch = cv2.resize(w.image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            corner = (int(round(w.corner[0] * scale)), int(round(w.corner[1] * scale)))
        else:
            patch, corner = w.image, w.corner
        paste_max(preview_canvas, patch, corner)
        on_preview(preview_canvas, i, len(warped))


def stitch_facade(
    facade_id: str,
    images: dict[str, np.ndarray],
    geometry_results: list[GeometryResult],
    cfg: Config,
    on_preview: Callable[[np.ndarray, int, int], None] | None = None,
) -> MosaicResult:
    graph = build_stitch_graph(geometry_results)
    reference = pick_reference(graph)
    if reference is None:
        raise RuntimeError(f"facade {facade_id}: no geometry edge passed the quality gate")

    homographies, unreachable = compute_global_homographies(graph, reference)
    sizes = {iid: (img.shape[1], img.shape[0]) for iid, img in images.items()}
    mean_drift_px, max_drift_px, cycle_edge_count = compute_drift_score(graph, reference, homographies, sizes)

    warped, canvas_size = warp_images(images, homographies)

    if on_preview is not None:
        _emit_incremental_preview(warped, canvas_size, on_preview)

    seam_masks = compute_seam_masks(warped, canvas_size)

    # A degenerate/near-extreme homography in this chain (more likely on a
    # sparse, low-redundancy graph — exactly what drives needs_colmap_fallback
    # below) can stretch one image's warped ROI enormously and crash OpenCV's
    # blender outright. That must not swallow the drift/coverage numbers this
    # facade's COLMAP-fallback decision depends on, so blend failures are
    # caught per-output rather than aborting the whole facade.
    scfg = cfg.stitch
    analysis_image = _safe_blend(blend_analysis, warped, seam_masks, canvas_size) if scfg.generate_analysis_mosaic else None
    visual_image = (
        _safe_blend(blend_visual, warped, seam_masks, canvas_size, num_bands=int(scfg.multiband_num_bands))
        if scfg.generate_visual_mosaic
        else None
    )

    canvas_w, canvas_h = canvas_size
    observed_mask = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    for w in warped.values():
        paste_max(observed_mask, w.mask, w.corner)
    coverage_ratio = float(np.count_nonzero(observed_mask)) / float(observed_mask.size)

    ok_results = [r for r in geometry_results if r.status == "OK"]
    quality = StitchQualityReport(
        facade_id=facade_id,
        image_count=len(images),
        matched_pair_count=len(ok_results),
        failed_pair_count=len(geometry_results) - len(ok_results),
        mean_inlier_ratio=float(np.mean([r.inlier_ratio for r in ok_results])) if ok_results else None,
        median_reprojection_error_px=(
            float(np.median([r.median_reprojection_error_px for r in ok_results]))
            if ok_results
            else None
        ),
        coverage_ratio=coverage_ratio,
        disconnected_components=count_connected_components(graph),
        reference_image_id=reference,
        unreachable_image_ids=unreachable,
        global_drift_score_px=mean_drift_px,
        max_drift_score_px=max_drift_px,
        cycle_edge_count=cycle_edge_count,
    )
    needs_colmap, reasons = should_run_colmap(quality, cfg)
    quality.needs_colmap_fallback = needs_colmap
    quality.colmap_fallback_reasons = reasons

    return MosaicResult(
        analysis_image=analysis_image,
        visual_image=visual_image,
        observed_mask=observed_mask,
        quality=quality,
    )
