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
    output_dir = images_dir / "output"
    return sorted(
        p for p in images_dir.rglob("*")
        if p.suffix.lower() in _IMAGE_EXTS and output_dir not in p.parents
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
