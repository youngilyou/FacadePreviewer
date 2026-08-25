"""COLMAP fallback: trigger decision + real SfM execution (CLAUDE.local.md #12).

COLMAP is never required by default (#10, #43.12 says it's a precision path,
not baseline) — `should_run_colmap` decides *when* Phase 1's plain
homography-chain stitch is untrustworthy enough to warrant it.

Only two of #12's four trigger conditions are computed here today:
`global_drift_score` (cycle-consistency check, graph.py) and coverage gap.
Repeated-pattern failure and large-parallax detection need signals this
pipeline doesn't compute yet, so they are not silently assumed false —
`should_run_colmap` says so explicitly rather than pretending coverage.

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
) -> ColmapResult:
    """Run SIFT extraction -> exhaustive matching -> incremental SfM for one
    facade's image set. `image_filenames` are names within `images_dir`
    (matches `pycolmap.extract_features`' `image_names` filter — this lets a
    facade's subset of a shared capture folder be reconstructed without
    copying files). Requires `pycolmap` (pip install pycolmap); raises
    ImportError if it's missing rather than faking a result.

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

    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    database_path = workspace_dir / "database.db"
    if database_path.exists():
        database_path.unlink()  # pycolmap errors on a stale/partial db from a prior run
    sparse_dir = workspace_dir / "sparse"
    sparse_dir.mkdir(exist_ok=True)

    if logger:
        log_event(
            logger, "info", "CM 특징점 추출 시작",
            stage="COLMAP_EXTRACT", facade_id=facade_id, image_count=len(image_filenames),
        )
    # Defaults are num_threads=-1 (one SIFT extractor thread per CPU core) and
    # max_image_size=-1 (no downscale, i.e. full original resolution). For a
    # batch of full-size DJI photos (~20MP each) that combination was observed
    # to balloon past 33GB of RAM on a 33-image facade and crash with a native
    # access violation — this only feeds camera-pose recovery (#12/#13), not
    # the final crack-analysis mosaic, so it doesn't need full resolution the
    # way the stitched output does (#8).
    extraction_options = pycolmap.FeatureExtractionOptions(num_threads=4, max_image_size=3200)
    pycolmap.extract_features(
        database_path=database_path,
        image_path=images_dir,
        image_names=image_filenames,
        extraction_options=extraction_options,
    )

    if logger:
        log_event(logger, "info", "CM 특징점 매칭 시작", stage="COLMAP_MATCH", facade_id=facade_id)
    pycolmap.match_exhaustive(database_path=database_path)

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

    reconstructions = pycolmap.incremental_mapping(
        database_path=database_path,
        image_path=images_dir,
        output_path=sparse_dir,
        next_image_callback=_on_next_image,
    )

    if not reconstructions:
        return ColmapResult(
            facade_id=facade_id,
            num_images_requested=len(image_filenames),
            num_images_registered=0,
        )

    best = max(reconstructions.values(), key=lambda r: r.num_reg_images())
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
