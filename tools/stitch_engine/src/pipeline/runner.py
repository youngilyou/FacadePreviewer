"""previewer's own stitch pipeline entry point.

Trimmed copy of the main CheckCrack project's src/pipeline/runner.py,
scoped to exactly what previewer needs: `run_facade_poc` (CLAUDE.local.md
#3.1 "1 Facade = 1 Flight" -- one image folder is one facade, no building
footprint). The footprint-based `run_building_poc` path is dropped entirely
rather than vendored unused -- previewer's job is finding unphotographed
spots on one already-known facade capture, not building-wide facade
classification. The COLMAP-pose facade-plane rectification fallback *is*
included (src.geometry.rectification, footprint-free
facade_plane_from_reconstruction variant only) -- see that module and
CLAUDE.local.md's 2026-08-22 entry for why this was added.

COLMAP, when triggered, runs via previewer's own src.sfm.colmap_runner
(pycolmap-based, same as the main CheckCrack pipeline -- see
CLAUDE.local.md's 2026-08-22 entry for why the earlier native colmap.exe
CLI subprocess design was replaced).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import cv2

from src.capture.image_catalog import build_catalog
from src.common.config import Config, load_config
from src.common.imageio import imread_unicode, imwrite_unicode
from src.common.logging import get_logger, log_event
from src.common.types import GeometryFailureCode, GeometryResult, ImageMetadata
from src.geometry.homography import estimate_homography
from src.geometry.quality import apply_quality_gate
from src.geometry.rectification import align_reconstruction_to_utm, estimate_utm_epsg, facade_plane_from_reconstruction, rectify_and_blend
from src.matching.loftr_matcher import MatchTimeoutError, TimeoutLoFTRMatcher
from src.matching.pair_selector import select_pairs
from src.sfm.colmap_runner import run_colmap
from src.stitching.mosaic import stitch_facade


def _run_facade_pipeline(
    facade_id: str,
    catalog: list[ImageMetadata],
    matcher: TimeoutLoFTRMatcher,
    cfg: Config,
    output_root: Path,
    logger,
    run_colmap_fallback: bool = True,
    output_dir_override: Path | None = None,
) -> Path | None:
    """MATCHED -> GEOMETRY_SOLVED -> STITCHED for one facade's image set.
    Returns the output dir, or None if there weren't enough passing pairs
    to stitch anything."""
    output_dir = output_dir_override if output_dir_override is not None else output_root / facade_id / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    by_id = {m.image_id: m for m in catalog}

    t0 = time.time()
    pairs = select_pairs(catalog, cfg)
    log_event(
        logger, "info", "pair graph built",
        stage="PAIR_GRAPH_BUILT", facade_id=facade_id, pair_count=len(pairs),
        elapsed_s=round(time.time() - t0, 2),
    )

    geometry_results: list[GeometryResult] = []
    t0 = time.time()
    for i, pair in enumerate(pairs):
        path_a = by_id[pair.image_a].file_path
        path_b = by_id[pair.image_b].file_path
        try:
            match = matcher.match(path_a, path_b)
        except MatchTimeoutError as exc:
            geom = GeometryResult(
                image_a=pair.image_a, image_b=pair.image_b,
                status=GeometryFailureCode.MATCH_TIMEOUT.value,
            )
            geometry_results.append(geom)
            log_event(
                logger, "warning", "pair match timed out, skipping",
                stage="MATCH_GEOMETRY", facade_id=facade_id,
                image_a=pair.image_a, image_b=pair.image_b,
                status=geom.status, progress=f"{i + 1}/{len(pairs)}", error=str(exc),
            )
            continue

        if match.num_matches < int(cfg.geometry.min_matches):
            geom = GeometryResult(
                image_a=pair.image_a, image_b=pair.image_b,
                status=GeometryFailureCode.LOW_MATCH.value, num_matches=match.num_matches,
            )
        else:
            geom = estimate_homography(match, inl_th_px=float(cfg.geometry.ransac_reproj_threshold_px))
            geom = apply_quality_gate(geom, cfg)
        geometry_results.append(geom)

        log_event(
            logger, "info", "pair processed",
            stage="MATCH_GEOMETRY", facade_id=facade_id,
            image_a=pair.image_a, image_b=pair.image_b,
            matches=geom.num_matches, inliers=geom.num_inliers,
            inlier_ratio=round(geom.inlier_ratio, 4),
            median_reproj_px=(
                round(geom.median_reprojection_error_px, 3)
                if geom.median_reprojection_error_px is not None
                else None
            ),
            status=geom.status,
            progress=f"{i + 1}/{len(pairs)}",
        )
    log_event(
        logger, "info", "matching + geometry complete",
        stage="GEOMETRY_SOLVED", facade_id=facade_id,
        ok_count=sum(1 for g in geometry_results if g.status == "OK"),
        failed_count=sum(1 for g in geometry_results if g.status != "OK"),
        elapsed_s=round(time.time() - t0, 2),
    )

    t0 = time.time()
    needed_ids = {g.image_a for g in geometry_results if g.status == "OK"} | {
        g.image_b for g in geometry_results if g.status == "OK"
    }
    if not needed_ids:
        log_event(
            logger, "warning", "facade has no geometry edge passing the quality gate, skipping stitch",
            stage="FAILED_GEOMETRY", facade_id=facade_id,
        )
        return None

    images = {}
    for image_id in needed_ids:
        img = imread_unicode(by_id[image_id].file_path, cv2.IMREAD_COLOR)
        if img is None:
            log_event(logger, "warning", "failed to read image", image_id=image_id)
            continue
        images[image_id] = img

    preview_state = {"prev_path": None}

    def _on_preview(canvas, i: int, total: int) -> None:
        new_path = output_dir / f"{facade_id}_live_preview_{i:03d}.jpg"
        imwrite_unicode(new_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
        prev_path = preview_state["prev_path"]
        if prev_path is not None and prev_path.exists():
            try:
                prev_path.unlink()
            except OSError:
                pass
        preview_state["prev_path"] = new_path
        log_event(
            logger, "info", "미리보기 갱신",
            stage="PREVIEW_UPDATED", facade_id=facade_id,
            progress=f"{i}/{total}", preview_path=str(new_path),
        )

    result = stitch_facade(facade_id, images, geometry_results, cfg, on_preview=_on_preview)
    log_event(
        logger, "info", "facade stitched",
        stage="STITCHED",
        elapsed_s=round(time.time() - t0, 2), **asdict(result.quality),
    )
    if result.quality.needs_colmap_fallback:
        log_event(
            logger, "warning", "facade needs COLMAP fallback, stitch is NEEDS_MANUAL_REVIEW",
            stage="NEEDS_MANUAL_REVIEW", facade_id=facade_id,
            reasons=result.quality.colmap_fallback_reasons,
        )
        if run_colmap_fallback:
            t_colmap = time.time()
            source_dirs = {str(Path(by_id[iid].file_path).parent) for iid in images.keys()}
            if len(source_dirs) != 1:
                log_event(
                    logger, "warning", "facade images span multiple source dirs, skipping COLMAP",
                    facade_id=facade_id, source_dirs=list(source_dirs),
                )
            else:
                colmap_images_dir = next(iter(source_dirs))
                colmap_filenames = [Path(by_id[iid].file_path).name for iid in images.keys()]
                try:
                    colmap_result = run_colmap(
                        facade_id, colmap_images_dir, colmap_filenames,
                        workspace_dir=output_dir.parent / "colmap", logger=logger,
                    )
                    log_event(
                        logger, "info", "COLMAP fallback complete",
                        stage="COLMAP_FALLBACK", facade_id=facade_id,
                        elapsed_s=round(time.time() - t_colmap, 2),
                        num_images_requested=colmap_result.num_images_requested,
                        num_images_registered=colmap_result.num_images_registered,
                    )
                    with open(output_dir / f"{facade_id}_colmap_report.json", "w", encoding="utf-8") as f:
                        json.dump(asdict(colmap_result), f, indent=2, ensure_ascii=False)

                    # Use the recovered poses to rectify onto the real facade plane
                    # instead of the drifting homography chain -- needs enough images
                    # registered to trust the plane fit, and *some* way to get the
                    # reconstruction into real, gravity-aligned UTM+altitude meters
                    # (align_reconstruction_to_utm's own requirement). previewer has
                    # no operator-supplied utm_epsg (no footprint workflow), so it
                    # derives a UTM zone from the images' own GPS instead.
                    if colmap_result.sparse_dir and colmap_result.num_images_registered >= 4:
                        try:
                            import pycolmap

                            effective_utm_epsg = estimate_utm_epsg(catalog)
                            if effective_utm_epsg is None:
                                log_event(
                                    logger, "warning", "no GPS on any image, cannot align COLMAP reconstruction for rectification",
                                    facade_id=facade_id,
                                )
                            else:
                                reconstruction = pycolmap.Reconstruction(colmap_result.sparse_dir)
                                aligned = align_reconstruction_to_utm(reconstruction, by_id, effective_utm_epsg)
                                if not aligned:
                                    log_event(
                                        logger, "warning", "COLMAP reconstruction has too little GPS coverage to align to UTM, skipping rectification",
                                        facade_id=facade_id,
                                    )
                                else:
                                    plane = facade_plane_from_reconstruction(reconstruction)
                                    t_rect = time.time()
                                    rect_result = rectify_and_blend(
                                        facade_id, reconstruction, plane, colmap_images_dir, cfg,
                                        colmap_mean_reprojection_error_px=colmap_result.mean_reprojection_error_px,
                                    )
                                    if rect_result.analysis_image is not None:
                                        imwrite_unicode(output_dir / f"{facade_id}_analysis_colmap.tif", rect_result.analysis_image)
                                    if rect_result.visual_image is not None:
                                        imwrite_unicode(output_dir / f"{facade_id}_visual_colmap.tif", rect_result.visual_image)
                                    imwrite_unicode(output_dir / f"{facade_id}_observed_mask_colmap.tif", rect_result.observed_mask)
                                    with open(output_dir / f"{facade_id}_quality_report_colmap.json", "w", encoding="utf-8") as f:
                                        json.dump(asdict(rect_result.quality), f, indent=2, ensure_ascii=False)
                                    log_event(
                                        logger, "info", "COLMAP-rectified mosaic complete",
                                        stage="RECTIFIED_COLMAP", facade_id=facade_id,
                                        elapsed_s=round(time.time() - t_rect, 2),
                                        coverage_ratio=rect_result.quality.coverage_ratio,
                                        image_count=rect_result.quality.image_count,
                                    )
                        except Exception as exc:
                            log_event(
                                logger, "warning", "COLMAP-pose rectification failed",
                                facade_id=facade_id, error=str(exc),
                            )
                except ImportError:
                    log_event(
                        logger, "warning", "pycolmap not installed, cannot run COLMAP fallback",
                        facade_id=facade_id,
                    )
                except Exception as exc:
                    log_event(
                        logger, "warning", "COLMAP fallback failed",
                        facade_id=facade_id, error=str(exc),
                    )

    if result.analysis_image is not None:
        imwrite_unicode(output_dir / f"{facade_id}_analysis.tif", result.analysis_image)
    if result.visual_image is not None:
        imwrite_unicode(output_dir / f"{facade_id}_visual.tif", result.visual_image)
    imwrite_unicode(output_dir / f"{facade_id}_observed_mask.tif", result.observed_mask)

    with open(output_dir / f"{facade_id}_quality_report.json", "w", encoding="utf-8") as f:
        json.dump(asdict(result.quality), f, indent=2, ensure_ascii=False)

    failed_pairs = [_asdict_pair(g) for g in geometry_results if g.status != "OK"]
    with open(output_dir / f"{facade_id}_failed_pairs.json", "w", encoding="utf-8") as f:
        json.dump(failed_pairs, f, indent=2, ensure_ascii=False)

    source_images = [
        {"image_id": iid, "file_path": by_id[iid].file_path} for iid in sorted(images.keys())
    ]
    with open(output_dir / f"{facade_id}_source_images.json", "w", encoding="utf-8") as f:
        json.dump(source_images, f, indent=2, ensure_ascii=False)

    if preview_state["prev_path"] is not None and preview_state["prev_path"].exists():
        try:
            preview_state["prev_path"].unlink()
        except OSError:
            pass

    log_event(logger, "info", "facade complete", stage="DONE", facade_id=facade_id, output_dir=str(output_dir))
    return output_dir


def _asdict_pair(g: GeometryResult) -> dict:
    return {
        "image_a": g.image_a, "image_b": g.image_b, "status": g.status,
        "num_matches": g.num_matches, "num_inliers": g.num_inliers,
        "inlier_ratio": round(g.inlier_ratio, 4),
    }


def _make_matcher(cfg: Config) -> TimeoutLoFTRMatcher:
    timeout_s = float(cfg.loftr.pair_timeout_s) if "pair_timeout_s" in cfg.loftr else 60.0
    return TimeoutLoFTRMatcher(cfg, timeout_s=timeout_s)


def run_facade_poc(
    facade_id: str,
    images_dir: str | Path,
    output_root: str | Path,
    config_path: str | Path = "config/pipeline.yaml",
    limit: int | None = None,
    run_colmap_fallback: bool = True,
    output_dir: str | Path | None = None,
) -> Path | None:
    """Whole `images_dir` is one facade (CLAUDE.local.md #3.1). `output_dir`,
    if given, overrides the usual `output_root/facade_id/output` layout and
    writes straight there instead."""
    cfg = load_config(config_path)
    output_root = Path(output_root)
    logger = get_logger("pipeline", log_dir="logs")

    t0 = time.time()
    catalog = build_catalog(images_dir)
    if limit is not None:
        catalog = catalog[:limit]
    for meta in catalog:
        meta.mission.facade_hint = facade_id
    log_event(
        logger, "info", "metadata parsed",
        stage="METADATA_PARSED", facade_id=facade_id, image_count=len(catalog),
        elapsed_s=round(time.time() - t0, 2),
    )

    matcher = _make_matcher(cfg)
    try:
        return _run_facade_pipeline(
            facade_id, catalog, matcher, cfg, output_root, logger, run_colmap_fallback,
            output_dir_override=Path(output_dir) if output_dir is not None else None,
        )
    finally:
        matcher.close()
