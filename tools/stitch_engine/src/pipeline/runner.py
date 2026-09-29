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
import numpy as np

from src.capture.image_catalog import build_catalog, filter_wall_like_images
from src.common.config import Config, load_config
from src.common.imageio import imread_unicode, imwrite_unicode
from src.common.logging import get_logger, log_event
from src.common.types import GeometryFailureCode, GeometryResult, ImageMetadata
from src.geometry.facade_period import PeriodEstimate, estimate_vertical_period
from src.geometry.homography import estimate_homography
from src.geometry.quality import apply_quality_gate
from src.geometry.rectification import (
    align_reconstruction_to_utm,
    estimate_utm_epsg,
    facade_plane_from_reconstruction,
    fill_uncovered_pixels,
    gps_camera_centers,
    gps_inconsistent_images,
    rectify_and_blend,
    rectify_images,
)
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

    # Precompute each image's own vertical repeat-period once (2026-09-09,
    # src/geometry/facade_period.py) -- self-matching SIFT within one image,
    # independent of any pairwise match -- so apply_quality_gate can convert
    # a homography's implied shift into "how many floors apart" using THIS
    # image's own measured period (FLOOR_COUNT_MISMATCH check). Computed for
    # every catalog image up front (not lazily per-pair) since most images
    # participate in several pairs and the estimate doesn't depend on which
    # partner it's being checked against.
    t0 = time.time()
    periods: dict[str, PeriodEstimate] = {}
    for meta in catalog:
        img = imread_unicode(meta.file_path, cv2.IMREAD_COLOR)
        if img is None:
            continue
        periods[meta.image_id] = estimate_vertical_period(img)
    log_event(
        logger, "info", "per-image facade period estimation complete",
        stage="PERIOD_ESTIMATED", facade_id=facade_id, image_count=len(periods),
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
            geom = apply_quality_gate(
                geom, cfg, meta_a=by_id.get(pair.image_a), meta_b=by_id.get(pair.image_b),
                period_a=periods.get(pair.image_a),
            )
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

    # Surfaced in the FacadePreviewer UI (scan-results panel) so an operator
    # can select-and-exclude exactly the images that actually caused a
    # problem, instead of guessing from how a photo looks (e.g. "this one's
    # tilted, is that a problem?" -- see CLAUDE.local.md's unmatched-images
    # feature entry). "never_matched" = had zero pairwise geometry edge
    # passing the quality gate with any other image, so it never even
    # reached COLMAP; "colmap_registration_failed" = reached COLMAP but
    # COLMAP itself couldn't register it into the reconstruction.
    unmatched_report: dict[str, list[str]] = {
        "never_matched": sorted(set(by_id.keys()) - set(images.keys())),
        "colmap_registration_failed": [],
    }

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

    result = stitch_facade(
        facade_id, images, geometry_results, cfg, on_preview=_on_preview,
        never_matched_count=len(unmatched_report["never_matched"]),
    )
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
                    colmap_registered_stems = {Path(n).stem for n in colmap_result.registered_image_names}
                    unmatched_report["colmap_registration_failed"] = sorted(
                        set(images.keys()) - colmap_registered_stems
                    )

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

                            # 2026-09-28: GPS-optional -- see _run_colmap_only_pipeline's own
                            # _run_colmap_mapping_only call for the full rationale (FacadePreviewer's
                            # own captures never carry EXIF GPS, so this used to stop dead here
                            # every time and never render anything at all).
                            reconstruction = pycolmap.Reconstruction(colmap_result.sparse_dir)
                            effective_utm_epsg = estimate_utm_epsg(catalog)
                            if effective_utm_epsg is not None:
                                aligned = align_reconstruction_to_utm(reconstruction, by_id, effective_utm_epsg)
                                if not aligned:
                                    log_event(
                                        logger, "warning", "COLMAP reconstruction has too little GPS coverage to align to UTM -- rendering in raw (unaligned) scale instead",
                                        facade_id=facade_id,
                                    )
                            else:
                                log_event(
                                    logger, "info", "no GPS on any image -- rendering in COLMAP's raw (unaligned) reconstruction scale, not real-world meters",
                                    facade_id=facade_id,
                                )
                            plane = facade_plane_from_reconstruction(reconstruction)
                            t_rect = time.time()
                            # rectify_and_blend, not render_sparse_point_splat -- see
                            # _run_colmap_only_pipeline's own call for why (2026-09-28,
                            # colmap.mode: always is the path actually exercised today;
                            # this conditional-fallback path is dormant under that config
                            # but kept consistent in case colmap.mode ever reverts).
                            rect_result = rectify_and_blend(
                                facade_id, reconstruction, plane, colmap_images_dir, cfg,
                                colmap_mean_reprojection_error_px=colmap_result.mean_reprojection_error_px,
                                dense_workspace_dir=output_dir / f"{facade_id}_dense_ws",
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

    with open(output_dir / f"{facade_id}_unmatched_images.json", "w", encoding="utf-8") as f:
        json.dump(unmatched_report, f, indent=2, ensure_ascii=False)

    if preview_state["prev_path"] is not None and preview_state["prev_path"].exists():
        try:
            preview_state["prev_path"].unlink()
        except OSError:
            pass

    log_event(logger, "info", "facade complete", stage="DONE", facade_id=facade_id, output_dir=str(output_dir))
    return output_dir


def _detect_off_wall_images(
    reconstruction: "pycolmap.Reconstruction",
    plane,
    min_gap_ratio: float = 2.5,
    max_exclude_fraction: float = 0.5,
    plane_distance_m: float = 3.0,
) -> set[str]:
    """Ported from the main CheckCrack repo's src/pipeline/runner.py
    (2026-09-12 entry, commit e28a23a) -- verbatim, this function is
    self-contained (pure pycolmap/numpy geometry, no project-specific
    imports) so no adaptation was needed. See that repo's own docstring for
    the full reasoning; short version: a shot aimed mostly at sky/rooftop
    still gets plenty of COLMAP matches/points, just almost none of them
    near the fitted facade plane -- SfM's own triangulation is a more
    reliable "is this image actually looking at the wall" signal than
    coverage-count or pixel-color heuristics (both tried and rejected on
    real data in that session). Looks for the largest RELATIVE gap in
    sorted per-image on-wall-point counts among the lower
    `max_exclude_fraction` of images and cuts there, only if the gap clears
    `min_gap_ratio` -- never excludes anything on a smooth continuum."""
    normal = np.cross(plane.e_u, plane.e_v)
    normal = normal / np.linalg.norm(normal)

    counts: dict[str, int] = {}
    for img in reconstruction.images.values():
        image_id = Path(img.name).stem
        on_wall = 0
        for p in img.points2D:
            if not p.has_point3D() or p.point3D_id not in reconstruction.points3D:
                continue
            point3d = reconstruction.points3D[p.point3D_id]
            if abs(float(np.dot(point3d.xyz - plane.origin, normal))) < plane_distance_m:
                on_wall += 1
        counts[image_id] = on_wall

    sorted_items = sorted(counts.items(), key=lambda item: item[1])
    n = len(sorted_items)
    if n < 4:
        return set()

    max_cut = max(1, int(n * max_exclude_fraction))
    best_gap_ratio = 1.0
    best_cut_idx = 0  # exclude sorted_items[:best_cut_idx]
    for i in range(1, min(max_cut, n - 1) + 1):
        lo = sorted_items[i - 1][1]
        hi = sorted_items[i][1]
        ratio = (hi + 1) / (lo + 1)
        if ratio > best_gap_ratio:
            best_gap_ratio = ratio
            best_cut_idx = i

    if best_gap_ratio < min_gap_ratio or best_cut_idx == 0:
        return set()
    return {image_id for image_id, _ in sorted_items[:best_cut_idx]}


def _run_colmap_mapping_only(
    facade_id: str,
    colmap_images_dir: str,
    colmap_filenames: list[str],
    workspace_dir: Path,
    logger,
    by_id: dict[str, ImageMetadata],
    catalog: list[ImageMetadata],
    cfg: Config | None = None,
):
    """Ported/trimmed from the main repo's same-named function (2026-09-12
    entry) -- COLMAP mapping + UTM alignment + facade-plane fit, WITHOUT
    rectify_and_blend (no seam-finding/blending, the expensive part).
    previewer has no footprint/segment workflow, so unlike the main repo's
    version this always uses facade_plane_from_reconstruction (the only
    plane-fit previewer has ever had -- see run_facade_poc's own history).

    Returns (colmap_result, reconstruction, plane) -- reconstruction/plane
    are None if that stage wasn't reached/didn't succeed. Raises ImportError
    if pycolmap itself isn't installed (caller's concern)."""
    import pycolmap

    # 2026-09-29: LoFTR-backed COLMAP matching (see run_colmap's own comment) needs a
    # TimeoutLoFTRMatcher, catalog and cfg -- only constructed/passed when cfg says to use it, so a
    # caller with no cfg (or use_loftr_matching: false) gets the original SIFT behavior unchanged.
    matcher = None
    use_loftr = cfg is not None and "colmap" in cfg and "use_loftr_matching" in cfg.colmap and bool(cfg.colmap.use_loftr_matching)
    if use_loftr:
        matcher = _make_matcher(cfg)
    try:
        colmap_result = run_colmap(
            facade_id, colmap_images_dir, colmap_filenames,
            workspace_dir=workspace_dir, logger=logger,
            catalog=catalog if use_loftr else None,
            cfg=cfg if use_loftr else None,
            matcher=matcher,
        )
    finally:
        if matcher is not None:
            matcher.close()
    reconstruction = None
    plane = None
    if colmap_result.sparse_dir and colmap_result.num_images_registered >= 4:
        reconstruction, plane = _load_aligned_reconstruction(
            facade_id, colmap_result.sparse_dir, logger, by_id, catalog,
        )
    return colmap_result, reconstruction, plane


def _load_aligned_reconstruction(
    facade_id: str,
    sparse_dir: str | Path,
    logger,
    by_id: dict[str, ImageMetadata],
    catalog: list[ImageMetadata],
):
    """Loads the COLMAP reconstruction at `sparse_dir`, aligns it to UTM when the images carry GPS,
    and fits the facade plane. Returns (reconstruction, plane), both None on failure."""
    import pycolmap

    reconstruction = None
    plane = None
    try:
        reconstruction = pycolmap.Reconstruction(sparse_dir)
        # 2026-09-28: FacadePreviewer's own captures (decoded video frames, see
        # MainViewModel.OnDecodedFrameReceived) carry no EXIF GPS at all -- unlike real DJI
        # photos, so estimate_utm_epsg/align_reconstruction_to_utm never has anything to work
        # with here and this facade_id would previously stop dead right here every single
        # time ("no GPS on any image"), before render_sparse_point_splat/rectify_and_blend
        # ever ran -- meaning NO visual output was ever produced for this app's own capture
        # pipeline, regardless of how fast/successfully COLMAP itself ran. User's explicit
        # call: real-world metric scale/orientation was never the point for an on-site
        # coverage check ("건물의 모습이 나오고, 촬영에서 누락된곳만 보이면 됩니다") -- so when
        # GPS is available, still align to UTM for a properly upright/scaled render (keeps
        # today's behavior for any future caller whose images DO carry GPS); when it isn't,
        # fall back to fitting the plane directly in COLMAP's own raw reconstruction frame.
        # 2026-09-28 update: facade_plane_from_reconstruction no longer trusts a hardcoded
        # [0,0,1] for "up" in this unaligned case (that axis only means gravity after UTM
        # alignment, and using it anyway on a raw/unaligned reconstruction is what rendered a
        # visibly tilted facade the user flagged against a reference photo) -- it now derives
        # "up" from the registered cameras' own poses instead (_estimate_world_up, gravity-
        # stabilized drone gimbal assumption), so the render comes out upright even without GPS.
        effective_utm_epsg = estimate_utm_epsg(catalog)
        if effective_utm_epsg is not None:
            aligned = align_reconstruction_to_utm(reconstruction, by_id, effective_utm_epsg)
            if not aligned:
                log_event(
                    logger, "warning", "COLMAP reconstruction has too little GPS coverage to align to UTM -- rendering in raw (unaligned) scale instead",
                    facade_id=facade_id,
                )
        else:
            log_event(
                logger, "info", "no GPS on any image -- rendering in COLMAP's raw (unaligned) reconstruction scale, not real-world meters",
                facade_id=facade_id,
            )
        plane = facade_plane_from_reconstruction(reconstruction)
    except Exception as exc:
        log_event(logger, "warning", "COLMAP 정렬/평면 계산 실패", facade_id=facade_id, error=str(exc))
        reconstruction = None
        plane = None
    return reconstruction, plane


def _fill_gaps_from_excluded(
    full_reconstruction,
    plane,
    images_dir: str,
    by_id: dict[str, ImageMetadata],
    utm_epsg: int | None,
    filler_ids: list[str],
    misplaced_ids: set[str],
    analysis_image: np.ndarray,
    observed_mask: np.ndarray,
) -> int:
    """Projects the photos left out of the mosaic onto the same canvas and pastes them only where
    nothing else landed (fill_uncovered_pixels). Off-wall photos keep their COLMAP pose; photos COLMAP
    misplaced (gps_inconsistent_images) keep its rotation but sit at their own GPS position, since
    the misplacement was in COLMAP's position, not the camera's orientation."""
    overrides: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    if misplaced_ids and utm_epsg is not None:
        centers = gps_camera_centers(by_id, utm_epsg, misplaced_ids)
        for img in full_reconstruction.images.values():
            image_id = Path(img.name).stem
            if image_id in centers:
                R = img.cam_from_world().rotation.matrix()
                overrides[image_id] = (R, -R @ centers[image_id])
    ids = [i for i in filler_ids if i not in misplaced_ids or i in overrides]
    warped, _ = rectify_images(
        full_reconstruction, plane, images_dir, None, image_names=set(ids), pose_overrides=overrides,
    )
    return fill_uncovered_pixels(analysis_image, observed_mask, [warped[i] for i in ids if i in warped])


def _thin_by_gps_spacing(
    ordered_filenames: list[str],
    by_id: dict[str, ImageMetadata],
    catalog: list[ImageMetadata],
    min_spacing_m: float,
) -> list[str]:
    """Keeps a photo (in the given capture order) only if its GPS position is at least
    `min_spacing_m` (3D, UTM + altitude) from the last photo kept. Photos without GPS are always
    kept; with no GPS at all the list is returned unchanged."""
    utm_epsg = estimate_utm_epsg(catalog)
    if utm_epsg is None:
        return ordered_filenames
    import pyproj

    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    kept: list[str] = []
    last: np.ndarray | None = None
    for name in ordered_filenames:
        meta = by_id.get(Path(name).stem)
        if meta is None or meta.gps.latitude is None or meta.gps.altitude_m is None:
            kept.append(name)
            continue
        x, y = transformer.transform(meta.gps.longitude, meta.gps.latitude)
        p = np.array([x, y, meta.gps.altitude_m])
        if last is None or np.linalg.norm(p - last) >= min_spacing_m:
            kept.append(name)
            last = p
    return kept


def _restrict_reconstruction_to_images(sparse_dir: str | Path, keep_names: set[str], out_dir: Path) -> Path:
    """Writes a copy of the (unaligned) reconstruction at `sparse_dir` holding only the images in
    `keep_names`, poses unchanged, and returns its directory. Deregistering in memory is not
    enough: the image stays in `reconstruction.images` without a pose and every later loop over
    the images (plane fit, rectify_images) would fail on it -- writing and reloading drops it."""
    import shutil

    import pycolmap

    rec = pycolmap.Reconstruction(sparse_dir)
    for image_id in list(rec.images.keys()):
        img = rec.images[image_id]
        if img.has_pose and img.name not in keep_names:
            rec.deregister_frame(img.frame_id)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    rec.write(out_dir)
    return out_dir


def _run_colmap_only_pipeline(
    facade_id: str,
    catalog: list[ImageMetadata],
    cfg: Config,
    output_root: Path,
    logger,
    output_dir_override: Path | None = None,
) -> Path | None:
    """`colmap.mode: always` -- skip 1st-stage LoFTR matching + pairwise-
    homography stitching entirely and go straight to COLMAP.

    2026-09-09: on this project's real captures, COLMAP (a) never reuses the
    1st stage's LoFTR-based geometry_results at all -- it always redoes its
    own SIFT matching from scratch -- and (b) every real run so far has
    triggered needs_colmap_fallback anyway (repetitive-facade texture makes
    the pairwise LoFTR+RANSAC chain fragile in a way COLMAP's own global
    bundle adjustment isn't, per the session's extensive findings). So the
    ~6 minutes of LoFTR matching + stitching was pure waste on every real
    run.

    2026-09-29: output is `{facade_id}_analysis_colmap.tif` only (no
    visual/observed-mask/plain-name copies, no crack detection), and photos
    `_detect_off_wall_images` flags are now left out of the mosaic -- still
    from this single pass's reconstruction, not a 2nd COLMAP run. Reference
    result: UE_TemImg/output/V005/BACK_analysis_colmap.tif.

    2026-09-13: single COLMAP pass, always -- no automatic 2nd pass. An
    earlier version of this function did a mapping-only stage 1 -> auto
    off-wall detection -> conditional from-scratch stage 2 re-run (ported
    from the main CheckCrack repo's 2026-09-12 stage1/filter/stage2
    restructure, commit e28a23a). User's own call: this app already lets
    the operator review/remove bad captured frames by hand in the left
    sidebar BEFORE clicking "스캔 시작" (MainViewModel's CapturedFrames
    review UI) -- if that manual pass already happened, an automatic
    2nd COLMAP re-run is redundant cost for no benefit, and removing it
    guarantees exactly ONE COLMAP run every time (never a conditional
    two), which is strictly faster than the old two-pass version even in
    its own best case. `_detect_off_wall_images` is still run once, for
    free, against this single pass's own reconstruction -- but now purely
    as an operator-facing WARNING (`{facade_id}_off_wall_warning.json`),
    never as a trigger to re-run COLMAP: it tells the operator which
    frames looked like they barely saw the wall, so they can delete those
    and hit "스캔 시작" again if they want a cleaner result, same manual
    loop as reviewing blurry frames. (Superseded 2026-09-29, see above.)
    """
    output_dir = output_dir_override if output_dir_override is not None else output_root / facade_id / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    by_id = {m.image_id: m for m in catalog}

    unmatched_report: dict[str, list[str]] = {"never_matched": [], "colmap_registration_failed": []}

    source_dirs = {str(Path(m.file_path).parent) for m in catalog}
    if len(source_dirs) != 1:
        log_event(
            logger, "warning", "facade images span multiple source dirs, cannot run COLMAP-only mode",
            facade_id=facade_id, source_dirs=list(source_dirs),
        )
        return None
    colmap_images_dir = next(iter(source_dirs))
    colmap_filenames = [Path(m.file_path).name for m in catalog]

    # 2026-09-28: pre-filter obvious non-wall frames (sky/mountains/ground, a serpentine flight's
    # column-turn frames) before COLMAP ever sees them -- see filter_wall_like_images's own
    # docstring for why (root-caused a real 499-image run whose incremental_mapping fragmented
    # into repeated from-scratch restarts, 932+ next_image_callback firings for 499 actual images,
    # still not done after ~15 minutes). Config-gated (wall_filter.enabled) so it can be turned
    # off without a code change if a future facade's legitimate wall content is itself low-texture
    # (e.g. a plain unpainted concrete wall) and starts getting wrongly excluded.
    wall_filter_cfg = cfg.wall_filter if "wall_filter" in cfg else None
    excluded_non_wall: list[str] = []
    if wall_filter_cfg is None or bool(wall_filter_cfg.enabled if "enabled" in wall_filter_cfg else True):
        threshold = (
            float(wall_filter_cfg.min_laplacian_variance)
            if wall_filter_cfg is not None and "min_laplacian_variance" in wall_filter_cfg
            else 800.0
        )
        colmap_filenames, excluded_non_wall = filter_wall_like_images(
            colmap_images_dir, colmap_filenames, min_laplacian_variance=threshold,
        )
        if excluded_non_wall:
            log_event(
                logger, "info", "하늘/원경 등 벽면이 아닌 프레임 사전 제외",
                stage="WALL_FILTER", facade_id=facade_id,
                excluded_count=len(excluded_non_wall), kept_count=len(colmap_filenames),
            )
            with open(output_dir / f"{facade_id}_wall_filter_excluded.json", "w", encoding="utf-8") as f:
                json.dump(sorted(excluded_non_wall), f, indent=2, ensure_ascii=False)

    # colmap.min_photo_spacing_m (2026-09-29): in capture order, skip a photo that is closer (3D GPS
    # distance) than this to the last photo kept. The serpentine flight takes 10+ nearly identical
    # photos while hovering at each column's top and bottom, but climbs/descends ~5.5m per photo.
    # A plain "every 2nd photo" cut (tried first) kept half of the hover duplicates and left the
    # climbing photos 11m (3-4 floors) apart, and on the repetitive facade COLMAP then placed those
    # 17-19m too low -- they got excluded and the upper floors went missing (coverage 0.64). 1.5m
    # keeps 216 of 429 on BACK (same count, same speed) with at most 6.2m between kept photos.
    # Skipped photos are listed in {facade}_unmatched_images.json, not as registration failures.
    min_spacing = float(cfg.colmap.min_photo_spacing_m) if "min_photo_spacing_m" in cfg.colmap else 0.0
    skipped_by_spacing: list[str] = []
    if min_spacing > 0:
        before = colmap_filenames
        colmap_filenames = _thin_by_gps_spacing(sorted(colmap_filenames), by_id, catalog, min_spacing)
        kept = set(colmap_filenames)
        skipped_by_spacing = [n for n in before if n not in kept]
        log_event(
            logger, "info", f"촬영 위치가 {min_spacing}m 안쪽으로 겹치는 사진 제외",
            stage="PHOTO_SPACING", facade_id=facade_id,
            kept_count=len(colmap_filenames), skipped_count=len(skipped_by_spacing),
        )

    # pycolmap.extract_features treats an empty image_names list as "every image under
    # images_dir", recursing into output/ -- the 2026-09-28 run where wall_filter excluded all
    # 429 photos reconstructed its own old output files instead.
    if not colmap_filenames:
        log_event(logger, "warning", "COLMAP에 넣을 사진이 없음", facade_id=facade_id)
        return None

    try:
        import pycolmap  # noqa: F401  -- import-availability check only, used inside the helpers below
    except ImportError:
        log_event(logger, "warning", "pycolmap not installed, cannot run COLMAP-only mode", facade_id=facade_id)
        return None

    # === Single COLMAP pass: mapping, on whatever images are in the (operator-curated)
    # capture folder right now. ===
    t_colmap = time.time()
    colmap_result, reconstruction, plane = _run_colmap_mapping_only(
        facade_id, colmap_images_dir, colmap_filenames,
        output_dir / f"{facade_id}_colmap", logger, by_id, catalog, cfg,
    )
    log_event(
        logger, "info", "COLMAP-only run complete",
        stage="COLMAP_ONLY", facade_id=facade_id,
        elapsed_s=round(time.time() - t_colmap, 2),
        num_images_requested=colmap_result.num_images_requested,
        num_images_registered=colmap_result.num_images_registered,
    )
    with open(output_dir / f"{facade_id}_colmap_report.json", "w", encoding="utf-8") as f:
        json.dump(asdict(colmap_result), f, indent=2, ensure_ascii=False)
    colmap_registered_stems = {Path(n).stem for n in colmap_result.registered_image_names}
    excluded_non_wall_stems = {Path(n).stem for n in excluded_non_wall}
    skipped_stems = {Path(n).stem for n in skipped_by_spacing}
    unmatched_report["colmap_registration_failed"] = sorted(
        set(by_id.keys()) - colmap_registered_stems - excluded_non_wall_stems - skipped_stems
    )
    unmatched_report["excluded_non_wall"] = sorted(excluded_non_wall_stems)
    unmatched_report["skipped_by_spacing"] = sorted(skipped_stems)

    if reconstruction is None or plane is None:
        # Too few images registered, or no GPS to align to UTM -- nothing to rectify.
        with open(output_dir / f"{facade_id}_unmatched_images.json", "w", encoding="utf-8") as f:
            json.dump(unmatched_report, f, indent=2, ensure_ascii=False)
        return output_dir

    # Photos that barely see the wall (sky/rooftop shots), and photos COLMAP placed far from their
    # own GPS position (gps_inconsistent_images), are left out of the mosaic -- both come from this
    # same single COLMAP pass, so no second COLMAP run is needed. The reconstruction is cut down to
    # the remaining photos, then re-aligned and the plane re-fitted.
    full_reconstruction = reconstruction  # still holds the excluded photos, for fill_uncovered_pixels below
    filler_ids: list[str] = []
    off_wall_ids = _detect_off_wall_images(reconstruction, plane)
    utm_epsg = estimate_utm_epsg(catalog)
    misplaced = gps_inconsistent_images(reconstruction, by_id, utm_epsg) if utm_epsg is not None else {}
    if misplaced:
        unmatched_report["excluded_gps_mismatch"] = sorted(misplaced)
        log_event(
            logger, "info", "COLMAP 위치가 GPS와 크게 다른 사진 제외",
            stage="GPS_MISMATCH_DETECTED", facade_id=facade_id, excluded_count=len(misplaced),
            max_error_m=round(max(misplaced.values()), 1), excluded_image_ids=sorted(misplaced),
        )
    excluded_ids = off_wall_ids | set(misplaced)
    if excluded_ids:
        keep_names = {
            name for name in colmap_result.registered_image_names if Path(name).stem not in excluded_ids
        }
        restricted_dir = _restrict_reconstruction_to_images(
            colmap_result.sparse_dir, keep_names, output_dir / f"{facade_id}_colmap" / "sparse" / "on_wall",
        )
        restricted, restricted_plane = _load_aligned_reconstruction(
            facade_id, restricted_dir, logger, by_id, catalog,
        )
        if restricted is not None and restricted_plane is not None:
            reconstruction, plane = restricted, restricted_plane
            # correctly placed ones first, then the GPS-repositioned ones
            filler_ids = sorted(off_wall_ids - set(misplaced)) + sorted(misplaced)
            unmatched_report["excluded_off_wall"] = sorted(off_wall_ids)
            log_event(
                logger, "info", "벽면이 거의 안 보이는 사진 제외",
                stage="OFF_WALL_DETECTED", facade_id=facade_id,
                excluded_count=len(off_wall_ids), kept_count=len(keep_names),
                excluded_image_ids=sorted(off_wall_ids),
            )
        else:
            log_event(
                logger, "warning", "사진 제외 후 재정렬 실패 -- 전체 사진으로 진행",
                stage="OFF_WALL_DETECTED", facade_id=facade_id, excluded_count=len(excluded_ids),
            )

    # 2026-09-28: back to rectify_and_blend, NOT render_sparse_point_splat -- tried the sparse
    # patch-splat first (faster in theory, real per-source-pixel patches pasted at each point's
    # projected plane location), but the result was still just scattered fragments with black gaps
    # between them, not something a human could actually recognize as a building ("등신아 이것으로
    # 건물이라는 것을 어떻게 알아", fair criticism). Re-examined the actual cost breakdown:
    # pipeline.yaml's dense.enable was ALREADY false (a 2026-09-09 finding, unrelated to anything
    # from today) -- so GPU dense-stereo, which this file's own docstring called "the expensive
    # part", was never actually running in ANY of today's slow test runs. The real bottlenecks were
    # the SfM-stage ones already fixed above (GPU/WDDM SIFT stall, exhaustive matching, mapping
    # fragmentation, wall-filter, bounded max_runtime_seconds) -- rectify_and_blend's own per-image
    # homography warp + seam/blend was never what made this slow, so there's no longer a speed
    # reason to avoid it, and it gives an actual seamless photographic mosaic instead of gappy dots.
    t_rect = time.time()
    try:
        rect_result = rectify_and_blend(
            facade_id, reconstruction, plane, colmap_images_dir, cfg,
            colmap_mean_reprojection_error_px=colmap_result.mean_reprojection_error_px,
            dense_workspace_dir=output_dir / f"{facade_id}_dense_ws",
        )
    except ValueError as exc:  # rectify_images' implausible-canvas guard
        log_event(logger, "error", "정사영상 생성 실패 -- COLMAP 재구성 이상", facade_id=facade_id, error=str(exc))
        with open(output_dir / f"{facade_id}_unmatched_images.json", "w", encoding="utf-8") as f:
            json.dump(unmatched_report, f, indent=2, ensure_ascii=False)
        return None

    if filler_ids and rect_result.analysis_image is not None:
        filled_px = _fill_gaps_from_excluded(
            full_reconstruction, plane, colmap_images_dir, by_id, utm_epsg, filler_ids, set(misplaced),
            rect_result.analysis_image, rect_result.observed_mask,
        )
        rect_result.quality.coverage_ratio = float(np.count_nonzero(rect_result.observed_mask)) / float(
            rect_result.observed_mask.size
        )
        log_event(
            logger, "info", "제외한 사진으로 빈 곳 채움",
            stage="GAP_FILL", facade_id=facade_id, filler_count=len(filler_ids), filled_px=filled_px,
            coverage_ratio=rect_result.quality.coverage_ratio,
        )

    # The mosaic is the one image this mode produces (FacadePreviewer shows it after 스캔 시작).
    if rect_result.analysis_image is not None:
        imwrite_unicode(output_dir / f"{facade_id}_analysis_colmap.tif", rect_result.analysis_image)
    with open(output_dir / f"{facade_id}_quality_report_colmap.json", "w", encoding="utf-8") as f:
        json.dump(asdict(rect_result.quality), f, indent=2, ensure_ascii=False)
    log_event(
        logger, "info", "COLMAP-rectified mosaic complete",
        stage="RECTIFIED_COLMAP", facade_id=facade_id,
        elapsed_s=round(time.time() - t_rect, 2),
        coverage_ratio=rect_result.quality.coverage_ratio,
        image_count=rect_result.quality.image_count,
    )

    source_images = [{"image_id": m.image_id, "file_path": m.file_path} for m in catalog]
    with open(output_dir / f"{facade_id}_source_images.json", "w", encoding="utf-8") as f:
        json.dump(source_images, f, indent=2, ensure_ascii=False)
    with open(output_dir / f"{facade_id}_unmatched_images.json", "w", encoding="utf-8") as f:
        json.dump(unmatched_report, f, indent=2, ensure_ascii=False)

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

    colmap_mode = str(cfg.colmap.mode) if "mode" in cfg.colmap else "fallback"
    if colmap_mode == "always":
        # Skip the LoFTR matcher entirely -- COLMAP-only mode never uses it
        # (see _run_colmap_only_pipeline's docstring for why).
        return _run_colmap_only_pipeline(
            facade_id, catalog, cfg, output_root, logger,
            output_dir_override=Path(output_dir) if output_dir is not None else None,
        )

    matcher = _make_matcher(cfg)
    try:
        return _run_facade_pipeline(
            facade_id, catalog, matcher, cfg, output_root, logger, run_colmap_fallback,
            output_dir_override=Path(output_dir) if output_dir is not None else None,
        )
    finally:
        matcher.close()
