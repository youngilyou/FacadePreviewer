"""Incrementally extract SIFT features for whatever frame_*.jpg files currently exist in a
capture folder, into the same database.db that run_colmap (stitch_folder.py -> ... ->
colmap_runner.py) will later reuse. See colmap_runner.py's 2026-09-28 reuse-database comment for
the full rationale -- this is the "extraction happens during capture" half of that.

Usage:
    python incremental_extract.py <images_dir> <workspace_dir>

Safe to call repeatedly against a growing images_dir: pycolmap.extract_features only does real
work for images it hasn't already stored features for in this database.db (confirmed empirically
idempotent, 2026-09-28), so each call's cost is proportional to just the *new* frames since the
last call. Meant to be invoked periodically by FacadePreviewer's capture loop
(MainViewModel.MaybeTriggerIncrementalExtraction), not by a human.
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: python incremental_extract.py <images_dir> <workspace_dir>")
        sys.exit(1)

    images_dir = Path(args[0])
    workspace_dir = Path(args[1])
    if not images_dir.is_dir():
        print(f"not a folder: {images_dir}")
        sys.exit(1)

    image_names = sorted(f.name for f in images_dir.glob("frame_*.jpg"))
    if not image_names:
        print("no frame_*.jpg found yet, nothing to extract")
        return

    import pycolmap

    workspace_dir.mkdir(parents=True, exist_ok=True)
    database_path = workspace_dir / "database.db"
    # Same options as colmap_runner.run_colmap -- see its own comment for why (RAM-safety
    # inherited from full-size-photo callers, use_gpu=False because this app's tiny 640x640
    # frames stall almost to a halt on this machine's WDDM-mode GPU, confirmed 2026-09-28).
    options = pycolmap.FeatureExtractionOptions(num_threads=4, max_image_size=3200, use_gpu=False)
    pycolmap.extract_features(
        database_path=database_path,
        image_path=images_dir,
        image_names=image_names,
        extraction_options=options,
    )
    print(f"extracted features for up to {len(image_names)} frame(s) into {database_path}")


if __name__ == "__main__":
    main()
