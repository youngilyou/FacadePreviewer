"""Debug tooling for tracing a mosaic pixel back to its source photo(s).

2026-09-09 CLAUDE.local.md entry -- an operator pointing at a suspicious
region of a mosaic had no way to say "which original photo is this". A first
attempt rendered every source image's placement as a labeled rectangle onto
one static overlay image, but with 140 heavily-overlapping images (many
frames per pass, each covering most of that pass's full height) later-drawn
rectangles completely hid earlier ones -- most labels were simply invisible.
A coordinate lookup is the right shape for this instead: given one canvas
(x, y), return every image_id whose warped placement actually covers it,
ordered by area (smallest/most specific first) so the most likely primary
contributor is listed first.
"""

from __future__ import annotations


def find_images_at(warped: dict, x: int, y: int, top_n: int = 5) -> list[str]:
    """Up to `top_n` image_ids (smallest-area first) whose WarpedImage.corner/
    size covers canvas pixel (x, y). A real capture has deliberately heavy
    overlap between consecutive frames in the same pass, so most points are
    covered by 10-30+ images' bounding boxes -- and each box is the AXIS-
    ALIGNED bound of a (possibly grazing-angle-distorted) warped quad, which
    can be considerably bigger than that image's actual useful footprint. Not
    every hit is an equally good answer to "which photo is this really from"
    -- smallest-area-first is a heuristic (a tighter box is usually a more
    fronto-parallel, less-stretched, more specific match), and `top_n` caps
    the result to the handful most likely to actually matter, rather than
    dumping every loosely-overlapping candidate. This is a tracing aid, not
    a substitute for the real per-pixel seam decision (stitching/blend.py).
    """
    hits = []
    for image_id, w in warped.items():
        cx, cy = w.corner
        cw, ch = w.size
        if cx <= x < cx + cw and cy <= y < cy + ch:
            hits.append((cw * ch, image_id))
    hits.sort()
    return [image_id for _, image_id in hits[:top_n]]
