"""Build an image catalog (metadata table) for a set of DJI still images.

Output mirrors CLAUDE.local.md #28: ``metadata/images.parquet``.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd

from src.capture.dji_metadata import parse_dji_image
from src.common.types import ImageMetadata

_IMAGE_EXTS = {".jpg", ".jpeg"}


def scan_images(images_dir: str | Path) -> list[Path]:
    images_dir = Path(images_dir)
    # The Viewer's "--in-place" flow (tools/stitch_folder.py) writes this
    # facade's own results, including numbered *.jpg live-preview snapshots,
    # to <images_dir>/output/ — a subfolder of the very directory this rglob
    # scans. Without this exclusion, re-running (or a crash leaving stale
    # preview files behind) feeds the pipeline's own output back in as if it
    # were source photos on the next run, corrupting image_count/matching.
    #
    # <images_dir>/excluded/ is FacadePreviewer's "선택 제외" button
    # (MainViewModel.RemoveExcludedFrames / CapturedFrameItem's own doc
    # comment) -- it physically moves an operator-unchecked frame's file
    # there rather than deleting it, specifically so a later rglob-based scan
    # like this one skips it. That comment's claim went unverified until
    # 2026-09-28: this exclusion was missing entirely, so an excluded frame's
    # file was still picked up here, making _run_colmap_only_pipeline see
    # >1 source dir (images_dir AND images_dir/excluded) and refuse to run
    # ("facade images span multiple source dirs") the moment any frame had
    # ever been excluded -- 선택 제외 silently broke the whole COLMAP path.
    output_dir = images_dir / "output"
    excluded_dir = images_dir / "excluded"
    return sorted(
        p for p in images_dir.rglob("*")
        if p.suffix.lower() in _IMAGE_EXTS
        and output_dir not in p.parents
        and excluded_dir not in p.parents
    )


def build_catalog(images_dir: str | Path) -> list[ImageMetadata]:
    return [parse_dji_image(p) for p in scan_images(images_dir)]


def catalog_to_dataframe(catalog: list[ImageMetadata]) -> pd.DataFrame:
    rows = [_flatten(meta) for meta in catalog]
    return pd.DataFrame(rows)


def _flatten(meta: ImageMetadata) -> dict:
    row = {
        "image_id": meta.image_id,
        "file_path": meta.file_path,
        "timestamp_utc": meta.timestamp_utc,
        "drone_model": meta.drone_model,
        "camera_model": meta.camera_model,
        "width": meta.width,
        "height": meta.height,
    }
    for prefix, obj in (
        ("gps", meta.gps),
        ("drone_pose", meta.drone_pose),
        ("gimbal_pose", meta.gimbal_pose),
        ("camera", meta.camera),
        ("mission", meta.mission),
    ):
        for k, v in asdict(obj).items():
            row[f"{prefix}.{k}"] = v
    return row


def save_catalog(catalog: list[ImageMetadata], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = catalog_to_dataframe(catalog)
    df.to_parquet(out_path, index=False)
    return out_path


def load_catalog(images_dir: str | Path, cache_path: str | Path | None = None) -> list[ImageMetadata]:
    """Build (and optionally cache) an image catalog for `images_dir`.

    Re-parses from source images every call unless a valid parquet cache is
    given via `cache_path` — kept simple since Phase 1 catalogs are small
    (tens-hundreds of images).
    """
    catalog = build_catalog(images_dir)
    if cache_path is not None:
        save_catalog(catalog, cache_path)
    return catalog


def filter_wall_like_images(
    images_dir: str | Path,
    filenames: list[str],
    min_laplacian_variance: float = 800.0,
) -> tuple[list[str], list[str]]:
    """Returns (kept, excluded). Excludes frames whose grayscale Laplacian variance (a standard
    blur/texture-density metric -- higher means more fine detail/edges) falls below the threshold:
    a wide/distant view (sky, mountains, ground far below, a serpentine flight's column-turn
    frames) has much less high-frequency detail than a close-up shot of an actual textured
    building wall (windows, balconies, floor lines).

    Why this matters (2026-09-28): COLMAP's incremental_mapping doesn't just skip images it can't
    confidently place -- when enough of a facade's frames are low-texture/non-wall, matches
    between them and the real wall frames are too weak or absent, and the mapper repeatedly
    abandons and restarts from a new seed pair trying to grow a usable model. Confirmed on a real
    499-image capture: its next_image_callback fired 932+ times (nearly double the actual image
    count) before being killed after ~15 minutes still not done -- each restart re-pays full
    registration + bundle-adjustment cost from scratch. Filtering the obvious non-wall shots out
    before COLMAP ever sees them cuts that fragmentation off at the source, instead of trying to
    make the mapper more tolerant of it.

    Threshold calibrated empirically against that same real capture: pure landscape/aerial frames
    measured ~250-800, a frame showing even a partial close wall section measured ~900-1000+, and
    a full close-up facade frame measured ~1900. 800 sits in the gap between those, erring toward
    keeping a borderline frame (COLMAP just gets little/no use out of it, a harmless no-op) over
    excluding one with real, useful wall content (which would create an actual, invisible coverage
    gap) -- see pipeline.yaml's wall_filter section to retune without a code change.
    """
    import cv2

    from src.common.imageio import imread_unicode

    images_dir = Path(images_dir)
    kept: list[str] = []
    excluded: list[str] = []
    for name in filenames:
        img = imread_unicode(str(images_dir / name))
        if img is None:
            kept.append(name)  # let downstream code report an unreadable file, not this filter
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        (excluded if lap_var < min_laplacian_variance else kept).append(name)
    return kept, excluded
