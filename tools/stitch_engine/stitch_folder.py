"""Stitch one captured facade folder and report unphotographed spots.

Usage:
    python stitch_folder.py <images_folder> [facade_name]

This is previewer's own entry point -- self-contained, does not import
anything from the main CheckCrack repo (see previewer/CLAUDE.local.md).
Meant to be invoked by FacadePreviewer's "스캔 시작" button as a subprocess,
the same way CheckCrackViewer already shells out to tools/stitch_folder.py
in the main project.

Wraps src.pipeline.runner.run_facade_poc with previewer's own config
(config/pipeline.yaml next to this script). Output goes to
<images_folder>/output/ by default, containing:
  - <facade_name>_analysis.tif       -- stitched mosaic
  - <facade_name>_observed_mask.tif  -- which pixels were actually photographed
  - <facade_name>_quality_report.json -- coverage_ratio is the number that
    answers "did we miss part of the wall?"
  - <facade_name>_colmap_report.json  -- present only if COLMAP fallback ran
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_ENGINE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ENGINE_ROOT))

from src.pipeline.runner import run_facade_poc  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 1:
        print("usage: python stitch_folder.py <images_folder> [facade_name]")
        sys.exit(1)

    images_dir = Path(args[0])
    if not images_dir.is_dir():
        print(f"not a folder: {images_dir}")
        sys.exit(1)

    facade_name = args[1] if len(args) > 1 else images_dir.name
    config_path = _ENGINE_ROOT / "config" / "pipeline.yaml"
    output_dir = images_dir / "output"

    print(f"stitching '{images_dir}' as facade '{facade_name}' ...")
    out = run_facade_poc(
        facade_id=facade_name,
        images_dir=images_dir,
        output_root=str(images_dir),
        config_path=str(config_path),
        output_dir=output_dir,
    )
    if out is None:
        print("failed: no image pair passed the geometry quality gate")
        sys.exit(1)

    print(f"done: {out}")
    print(f"  - {facade_name}_analysis.tif")
    print(f"  - {facade_name}_observed_mask.tif")
    print(f"  - {facade_name}_quality_report.json (check coverage_ratio / needs_colmap_fallback)")


if __name__ == "__main__":
    main()
