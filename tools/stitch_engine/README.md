# previewer's stitch engine

Self-contained Kornia LoFTR + RANSAC/homography + COLMAP-fallback stitch
pipeline, vendored from the main CheckCrack repo's `src/` (see file
headers for exactly which module each file came from) so previewer never
imports across repo boundaries (previewer/CLAUDE.local.md).

Purpose here is narrower than the main repo's: **find unphotographed spots
on the building**, not precision crack measurement. No crack/tiling/
measurement stages are included.

## What's different from the main repo's pipeline

- `src/sfm/colmap_runner.py` uses `pycolmap`, same as the main repo --
  previewer's earlier native `colmap.exe` CLI subprocess (`tools/colmap_deps/`)
  was removed after it turned out to silently under-report registered images
  (it looked for a `images.txt` model file `colmap.exe mapper` never
  actually writes) and never fed the recovered poses back into a corrected
  mosaic in the first place.
- `src/pipeline/runner.py` has `run_facade_poc` (one image folder = one
  facade, CLAUDE.local.md #3.1) plus the COLMAP-pose facade-plane
  rectification fallback (`src/geometry/rectification.py`,
  `facade_plane_from_reconstruction` -- the footprint-free variant, since
  previewer has no building footprint to classify against). The
  footprint-based `run_building_poc` path and `facade_plane_from_segment`
  are still out of scope -- previewer captures one segment at a time
  already.
- `config/pipeline.yaml` is tuned separately: `loftr.max_image_side: 640`
  matches previewer's capture-time downscale (see previewer's capture-only
  redesign) so LoFTR never resizes down further, and
  `stitch.generate_visual_mosaic` is off (skips the multi-band blend pass
  a coverage-gap tool doesn't need).

## Setup

```
pip install -r requirements.txt
```

## Usage

```
python stitch_folder.py <images_folder> [facade_name]
```

Output goes to `<images_folder>/output/`:
- `<facade_name>_analysis.tif` -- stitched mosaic (homography-chain)
- `<facade_name>_observed_mask.tif` -- which pixels were actually photographed
- `<facade_name>_quality_report.json` -- `coverage_ratio` answers "did we
  miss part of the wall?"; `needs_colmap_fallback` / COLMAP report present
  if the homography chain drifted too much
- `<facade_name>_analysis_colmap.tif` / `_visual_colmap.tif` /
  `_observed_mask_colmap.tif` / `_quality_report_colmap.json` -- present only
  when the COLMAP fallback actually ran and registered enough images (>=4):
  a second mosaic rectified straight from COLMAP's recovered camera poses
  (no homography chain to drift), which should be preferred over the plain
  `_analysis.tif` whenever it exists

Meant to be invoked by FacadePreviewer's "스캔 시작" button as a subprocess
(same pattern as the main repo's CheckCrackViewer -> `tools/stitch_folder.py`).
