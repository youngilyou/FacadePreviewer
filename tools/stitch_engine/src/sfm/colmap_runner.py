"""COLMAP fallback: trigger decision + real SfM execution (CLAUDE.local.md #12).

COLMAP is never required by default (#10, #43.12 says it's a precision path,
not baseline) — `should_run_colmap` decides *when* Phase 1's plain
homography-chain stitch is untrustworthy enough to warrant it.

Three of #12's four trigger conditions are computed here today:
`global_drift_score` (cycle-consistency check, graph.py), coverage gap, and
(2026-09-09) unreachable_ratio -- images the 2D chain can't place at all
(no matching path to the reference, or zero passing edges to anything) are
a real, silent data-loss failure mode coverage_gap_ratio alone can miss,
since that's computed only over the placed subset's own (auto-shrunk)
canvas. Repeated-pattern failure and large-parallax detection still need
signals this pipeline doesn't compute yet, so they are not silently assumed
false — `should_run_colmap` says so explicitly rather than pretending
coverage.

`run_colmap` does real SfM via pycolmap (SIFT extraction -> exhaustive
matching -> incremental mapping + bundle adjustment) and returns actual
recovered camera poses/points, never a fabricated result. It stops at
producing the reconstruction (#12's "intrinsics, extrinsics, sparse
points, bundle-adjusted poses") — feeding those poses back into facade
rectification (#13) to replace the 2D homography chain is further work,
not done by this function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.common.config import Config
from src.common.types import StitchQualityReport


def should_run_colmap(quality: StitchQualityReport, cfg: Config) -> tuple[bool, list[str]]:
    """Return (needs_colmap, reasons). Empty reasons means no trigger fired."""
    ccfg = cfg.colmap
    reasons: list[str] = []

    if not bool(ccfg.enabled):
        return False, reasons

    max_drift = float(ccfg.max_drift_px)
    if quality.global_drift_score_px is not None and quality.global_drift_score_px > max_drift:
        reasons.append(
            f"global_drift_score_px={quality.global_drift_score_px:.1f} > max_drift_px={max_drift}"
        )

    # A facade can pass the *mean* drift check while its single worst seam
    # is still badly misaligned (observed: mean=9.15px passed a 10px gate,
    # but max=26.9px produced a clearly visible wavy/bent line in the
    # mosaic) — mean alone hides exactly that kind of one-bad-seam defect.
    max_seam_drift = float(ccfg.max_seam_drift_px) if "max_seam_drift_px" in ccfg else max_drift * 2
    if quality.max_drift_score_px is not None and quality.max_drift_score_px > max_seam_drift:
        reasons.append(
            f"max_drift_score_px={quality.max_drift_score_px:.1f} > max_seam_drift_px={max_seam_drift}"
        )

    max_gap = float(ccfg.max_coverage_gap_ratio)
    if quality.coverage_ratio is not None:
        gap = 1.0 - quality.coverage_ratio
        if gap > max_gap:
            reasons.append(f"coverage_gap_ratio={gap:.2f} > max_coverage_gap_ratio={max_gap}")

    # 2026-09-09: images the 2D homography chain simply can't place at all
    # (no matching path to the reference image) are silently dropped from
    # the mosaic entirely -- not a spatial gap on a shrunk canvas the way
    # coverage_gap_ratio measures, but a real photo that never appears in
    # the output at all. This can slip past both checks above: coverage_ratio
    # is computed only over the *placed* subset's own canvas, so it can look
    # perfectly healthy while a whole side of the building silently vanished.
    # Two distinct ways an image never makes it in (see mosaic.py/runner.py):
    # never_matched_count (zero passing edge to *anything*, never even a
    # graph node) and unreachable_image_ids (had edges, just to a different,
    # smaller connected component than the one holding the reference).
    max_unreachable = float(ccfg.max_unreachable_ratio) if "max_unreachable_ratio" in ccfg else 0.03
    total_images = quality.image_count + quality.never_matched_count
    if total_images > 0:
        dropped = len(quality.unreachable_image_ids) + quality.never_matched_count
        unreachable_ratio = dropped / total_images
        if unreachable_ratio > max_unreachable:
            reasons.append(
                f"unreachable_ratio={unreachable_ratio:.2f} > max_unreachable_ratio={max_unreachable} "
                f"({dropped}/{total_images} images have no path to the reference image and would "
                f"otherwise be silently dropped from the mosaic entirely)"
            )

    # #12 also lists repeated-pattern failure and large camera-distance/
    # parallax change as triggers. Neither is detected yet (would need
    # window-pattern-aware match auditing and per-pair baseline/parallax
    # estimates this pipeline doesn't compute), so they cannot fire here —
    # documented instead of silently treated as "not present".

    return len(reasons) > 0, reasons


@dataclass
class ColmapResult:
    facade_id: str
    num_images_requested: int
    num_images_registered: int
    registered_image_names: list = field(default_factory=list)
    num_points3d: int = 0
    mean_reprojection_error_px: float | None = None
    sparse_dir: str | None = None


def run_colmap(
    facade_id: str,
    images_dir: str | Path,
    image_filenames: list[str],
    workspace_dir: str | Path,
    logger=None,
    catalog: list | None = None,
    cfg=None,
    matcher=None,
) -> ColmapResult:
    """Run feature extraction -> matching -> incremental SfM for one facade's
    image set. `image_filenames` are names within `images_dir` (matches
    `pycolmap.extract_features`' `image_names` filter — this lets a facade's
    subset of a shared capture folder be reconstructed without copying
    files). Requires `pycolmap` (pip install pycolmap); raises ImportError if
    it's missing rather than faking a result.

    Matching backend (2026-09-29): plain `pycolmap.match_sequential` (SIFT) by
    default. If `catalog`, `cfg`, and `matcher` (a caller-owned
    `TimeoutLoFTRMatcher`) are all given AND `cfg.colmap.use_loftr_matching`
    is truthy, matching goes through `src/matching/loftr_colmap_bridge.py`
    instead — ported from the main CheckCrack repo, where this is the
    confirmed fix for the exact repetitive-facade mismatch problem this app's
    own SIFT-only pipeline fought all session on this same BACK facade
    (registration stuck around 10-30%, or a geometrically degenerate result
    once more of it registered): SIFT confuses visually-identical repeated
    features (windows/vents/balconies) into spurious matches; LoFTR doesn't.
    Confirmed on this same building's FRONT facade in the main repo: 422/422
    images registered. Any caller that omits catalog/cfg/matcher gets the
    original SIFT behavior unchanged.

    `logger`, if given, gets phase-boundary events (extraction/matching/
    mapping start) plus a per-image event during incremental mapping via
    pycolmap's `next_image_callback` — that callback fires once per image
    registered *after* the initial seed pair (confirmed empirically: 11
    images registered fired it 9 times) and carries no image identity, just
    "another one landed", so the progress string is a plain counter, not a
    named image. Without a logger this runs exactly as before (silent,
    caller only sees the final ColmapResult) — CheckCrackViewer only shows
    live COLMAP progress for callers that pass one.
    """
    import pycolmap

    from src.common.logging import log_event

    use_loftr = (
        catalog is not None and cfg is not None and matcher is not None
        and "colmap" in cfg and "use_loftr_matching" in cfg.colmap and bool(cfg.colmap.use_loftr_matching)
    )

    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    sparse_dir = workspace_dir / "sparse"
    sparse_dir.mkdir(exist_ok=True)

    # num_threads=4 and max_image_size=3200 were both added after a batch of
    # full-size DJI photos (~20MP each) ballooned past 33GB of RAM on a
    # 33-image facade and crashed with a native access violation.
    # max_image_size stays as a harmless no-op safety net for anyone who
    # eventually calls this with larger source photos than FacadePreviewer's
    # own 640x640 captures; num_threads=4 turned out not to be the real lever
    # for THIS caller's slowness at all (tried -1 on 2026-09-28, no better).
    #
    # use_gpu=False (2026-09-28): pycolmap.FeatureExtractionOptions defaults
    # use_gpu=True ("Creating SIFT GPU feature extractor" in the log). On this
    # dev machine (RTX 4080, WDDM driver mode -- required since it's also the
    # display GPU) that made extraction stall to a near-halt on ~1000 tiny
    # 640x640 frames: `nvidia-smi` showed the python process holding an open
    # CUDA context (~6GB VRAM) but GPU-Util pinned at 0-2%, while the process
    # still burned real CPU time (confirmed via Get-Process, not a deadlock).
    # WDDM adds significant per-kernel-launch scheduling overhead versus
    # Linux/TCC mode, and SIFT on a tiny image is many small, fast kernel
    # launches -- for images this small that dispatch overhead apparently
    # dominates over whatever the GPU actually saves, unlike the
    # normal-sized-or-larger photos GPU SIFT is usually a clear win for.
    # Plain CPU extraction (num_threads=4) doesn't hit this ceiling.
    extraction_options = pycolmap.FeatureExtractionOptions(
        num_threads=4, max_image_size=3200, use_gpu=False,
    )

    if logger:
        log_event(logger, "info", "CM SfM 재구성 시작", stage="COLMAP_MAPPING", facade_id=facade_id)

    registered = {"n": 0}

    def _on_next_image() -> None:
        registered["n"] += 1
        if logger:
            log_event(
                logger, "info", "CM 이미지 등록 중",
                stage="COLMAP_MAPPING_PROGRESS", facade_id=facade_id,
                progress=f"~{registered['n'] + 2}/{len(image_filenames)}",  # +2: the unreported initial seed pair
            )

    # 2026-09-28: bounded mapping options -- confirmed on a real 297-image capture that
    # incremental_mapping's defaults (max_num_models=50, max_runtime_seconds=-1/unlimited) let it
    # spend 20 minutes restarting from new seed pairs (progress counter reached ~1472 registration
    # events for 297 actual images) when a facade's repeating window/floor pattern makes SIFT
    # matches ambiguous between non-corresponding-but-visually-similar windows -- a classic
    # repetitive-structure SfM failure mode that wall_filter (filters non-wall frames, a different
    # problem) doesn't address. Capping both gives a hard worst-case bound regardless of whether
    # that ambiguity gets resolved: max_num_models cuts off the restart-from-scratch churn directly
    # (5 attempts is generous for a single facade that should ideally need just 1), and
    # max_runtime_seconds is the final backstop so a pathological case still returns whatever
    # partial reconstruction it has rather than running unbounded. A registered_image count well
    # below num_images_requested after this is a real, visible signal (ColmapResult/
    # {facade}_colmap_report.json) that this facade's capture likely needs a slower/more careful
    # re-run -- not a silently swallowed failure.
    # use_prior_position (2026-09-28): real SD-card DJI photos carry EXIF GPS (confirmed:
    # extract_features' own log prints "GPS: LAT=.. LON=.. ALT=.." per image once it's in the
    # database) -- previewer's own frame_*.jpg video captures never have this, so this was never
    # available to try before. Feeds each image's GPS position into the mapper as a soft prior
    # (regularization term in bundle adjustment, not a hard constraint) instead of registering
    # purely from feature matches -- should narrow the seed-pair/registration search space directly
    # with real position information, on a repetitive-facade texture that otherwise leaves SIFT
    # matching itself ambiguous about which of several similar-looking windows is the real match.
    # use_robust_loss_on_prior_position=True guards against consumer GPS's well-known weak point,
    # barometric/GPS altitude easily ±10m+ off: a robust loss down-weights a badly-off prior instead
    # of the default quadratic penalty pulling the solution toward it. Even a noisy prior is still
    # far tighter than the true failure mode here (confusing entire floors metrs apart), so this is
    # expected to help disambiguate without needing the altitude to be exactly right.
    # 2026-09-29: the caps above only apply to the SIFT path. With LoFTR matches the mapper runs
    # with no runtime/model cap (GPS prior: see below) -- the 240s cap cut the
    # LoFTR run on the 429-image BACK facade off at 92 registered images, while the same LoFTR
    # matches mapped with default options register every image (target output:
    # UE_TemImg/output/V005/BACK_analysis_colmap.tif).
    if use_loftr:
        # Each photo's EXIF GPS (written into the database by extract_features) is a soft prior in
        # bundle adjustment. Without it, the 215-photo BACK run (colmap.image_stride 2) came out
        # bent along the flight: median camera-vs-GPS error 100m in one run, 7.4km in a repeat. With
        # it, two repeats both gave 0.56-0.61m. Robust loss keeps a bad GPS fix from pulling the
        # reconstruction.
        mapping_options = pycolmap.IncrementalPipelineOptions(
            use_prior_position=True, use_robust_loss_on_prior_position=True,
        )
        # colmap.fast_bundle_adjustment: global bundle adjustment less often and with fewer
        # refinement rounds. Measured on BACK (215 photos): mapping 4.4 -> 1.6 min, same
        # registration and mosaic (coverage 0.937 vs 0.940).
        if "fast_bundle_adjustment" in cfg.colmap and bool(cfg.colmap.fast_bundle_adjustment):
            mapping_options.ba_global_frames_ratio = 1.4
            mapping_options.ba_global_points_ratio = 1.4
            mapping_options.ba_global_max_refinements = 2
            mapping_options.ba_local_max_refinements = 1
    else:
        mapping_options = pycolmap.IncrementalPipelineOptions(
            max_num_models=8, max_runtime_seconds=240,
            use_prior_position=True, use_robust_loss_on_prior_position=True,
        )

    # 2026-09-28: incremental_mapping's own seed-pair/RANSAC choices are randomized, and on a
    # weakly-textured, marginally-connected facade (this app's usual case, see wall_filter's own
    # docstring) that randomness dominates the result far more than any of the tuning above --
    # confirmed empirically on this exact dataset (297 images, identical inputs, 5 back-to-back
    # full extract+match+map runs): registered counts of [18, 17, 162, 18, 18]. Four attempts
    # stalled early at the same ~17-18 (whatever seed pair they happened to draw led into a dead
    # end almost immediately), but one drew a seed pair that grew into a real 162-image
    # reconstruction -- more than double the previously-accepted single-shot result (64/297) that
    # produced the tilted/curved facade the user flagged (2026-09-28 CLAUDE.local.md). A single
    # mapping attempt is therefore a coin flip on whether the whole facade comes out usable at all,
    # not just on registration percentage. User's explicit choice (asked directly, given the
    # ~1.5-3min-per-attempt cost): retry up to 5 times and keep whichever attempt registered the
    # most images, rather than accept the first attempt's roll of the dice.
    #
    # IMPORTANT (2026-09-28, found via a live-app run that got 19/297 despite this retry loop
    # already being in place): re-running ONLY incremental_mapping against one shared, already-
    # extracted-and-matched database.db does NOT reproduce the [18, 17, 162, 18, 18] spread above --
    # tested directly (3 mapping-only calls on one fixed db/matches): [18, 13, 18], every one stuck
    # in the bad range, never close to 64 let alone 162. The real source of the run-to-run variance
    # is upstream of the mapper -- extract_features/match_sequential themselves aren't perfectly
    # reproducible (multi-threaded SIFT extraction ordering, RANSAC draws inside match verification)
    # -- so a fixed set of matches structurally caps every subsequent mapping attempt at whatever
    # that one extraction/matching pass happened to support. Each retry attempt below therefore
    # redoes extraction AND matching from scratch into its own fresh database, not just mapping --
    # the ~5s/attempt this costs is negligible against the 90-190s mapping stage.
    # 2026-09-28 (real SD-card photos, 15-min field-use budget): extract+match is no longer
    # negligible per-attempt against real 20MP photos (~86s at max_image_size=3200 for a ~430-image
    # facade, vs ~5s for previewer's tiny 320px video frames) -- 5 attempts at the old 180s mapping
    # cap could reach ~22min worst case. Trimmed to 3 attempts x 120s cap (worst case
    # 3*(86+120)=~618s=~10.3min, leaving headroom under rectify_and_blend for the 15min target) --
    # a real tradeoff (fewer rolls of the dice than the video-frame case's user-approved 5), but
    # use_prior_position above should make each individual attempt less of a coin flip to begin
    # with on this dataset, so fewer attempts should still be needed on average.
    # 2026-09-29: user's priority shifted from the 15-min field-use budget to actually covering the
    # WHOLE building -- 43/429 registered (3 attempts, 120s cap) was good quality (0.56px reprojection
    # error) but visibly just a fragment. Raised both knobs to spend more time chasing a bigger,
    # more complete reconstruction instead of stopping early.
    # 2026-09-29: LoFTR matching is expensive (real GPU work per pair) and, unlike SIFT, doesn't
    # need to be REDONE per attempt to get independent draws -- the run-to-run randomness that
    # required fresh extract+match per attempt above was traced specifically to SIFT's own
    # multi-threaded extraction ordering, which doesn't apply here. So for LoFTR: extract+match
    # ONCE into a base database, then copy it fresh into each attempt (incremental_mapping's own
    # seed-pair/RANSAC randomness is the only thing retries are still guarding against). Far fewer
    # attempts needed given LoFTR's own much higher match quality (confirmed in the main repo:
    # 422/422 registered on this building's FRONT facade).
    import shutil

    base_db_path = None
    if use_loftr:
        from src.matching.loftr_colmap_bridge import match_database_with_loftr

        base_db_path = workspace_dir / "database_loftr_base.db"
        # A base database from an earlier scan may hold a different image set (e.g. all 429
        # photos before colmap.image_stride), and extract_features only adds to it -- start clean.
        for stale in (base_db_path, Path(f"{base_db_path}-wal"), Path(f"{base_db_path}-shm")):
            if stale.exists():
                stale.unlink()
        pycolmap.extract_features(
            database_path=base_db_path,
            image_path=images_dir,
            image_names=image_filenames,
            extraction_options=extraction_options,
        )
        loftr_stats = match_database_with_loftr(
            database_path=base_db_path,
            images_dir=images_dir,
            image_filenames=image_filenames,
            catalog=catalog,
            cfg=cfg,
            matcher=matcher,
            workspace_dir=workspace_dir,
            logger=logger,
        )
        if logger:
            log_event(
                logger, "info", "LoFTR 매칭 통계", stage="COLMAP_LOFTR_MATCH",
                facade_id=facade_id, **loftr_stats,
            )

    best: "pycolmap.Reconstruction | None" = None
    # 2026-09-29: 92/429 registered with the previous 3-attempt/240s cap left visibly real content
    # unregistered (8258 real LoFTR pairs attempted -- match coverage isn't the bottleneck, mapping
    # time/attempts was). LoFTR's own matching cost is now a one-time sunk cost per run_colmap call
    # (each attempt only copies the already-matched database, no re-extraction/re-matching), so
    # more attempts here are cheap relative to the ~11min LoFTR match phase -- worth spending more
    # of the budget on mapping specifically.
    # An uncapped LoFTR mapping pass already registers the whole facade, so it gets one attempt;
    # a second one would only repeat the same long mapping.
    max_attempts = 1 if use_loftr else 8
    early_stop_ratio = 0.9
    for attempt in range(max_attempts):
        attempt_dir = sparse_dir / f"attempt_{attempt}"
        attempt_dir.mkdir(exist_ok=True)
        attempt_db_path = attempt_dir / "database.db"
        if logger:
            log_event(
                logger, "info", "CM SfM 재시도",
                stage="COLMAP_MAPPING_ATTEMPT", facade_id=facade_id,
                attempt=attempt + 1, max_attempts=max_attempts,
            )
        if use_loftr:
            shutil.copy(base_db_path, attempt_db_path)
        else:
            pycolmap.extract_features(
                database_path=attempt_db_path,
                image_path=images_dir,
                image_names=image_filenames,
                extraction_options=extraction_options,
            )
            pycolmap.match_sequential(
                database_path=attempt_db_path,
                pairing_options=pycolmap.SequentialPairingOptions(overlap=15, loop_detection=False),
            )
        registered["n"] = 0
        reconstructions = pycolmap.incremental_mapping(
            database_path=attempt_db_path,
            image_path=images_dir,
            output_path=attempt_dir,
            options=mapping_options,
            next_image_callback=_on_next_image,
        )
        attempt_best = max(reconstructions.values(), key=lambda r: r.num_reg_images()) if reconstructions else None
        if attempt_best is not None and (best is None or attempt_best.num_reg_images() > best.num_reg_images()):
            best = attempt_best
        if best is not None and best.num_reg_images() >= early_stop_ratio * len(image_filenames):
            break

    if best is None:
        return ColmapResult(
            facade_id=facade_id,
            num_images_requested=len(image_filenames),
            num_images_registered=0,
        )

    best_dir = sparse_dir / "best"
    best_dir.mkdir(exist_ok=True)
    best.write(best_dir)  # persist chosen reconstruction for provenance (#39)

    errors = [p.error for p in best.points3D.values() if p.has_error]
    mean_error = sum(errors) / len(errors) if errors else None

    return ColmapResult(
        facade_id=facade_id,
        num_images_requested=len(image_filenames),
        num_images_registered=best.num_reg_images(),
        registered_image_names=sorted(img.name for img in best.images.values()),
        num_points3d=best.num_points3D(),
        mean_reprojection_error_px=mean_error,
        sparse_dir=str(best_dir),
    )
