"""Unicode-safe replacements for cv2.imread/imwrite.

cv2.imread/imwrite go through OpenCV's internal ANSI (not UTF-8, not
wide-char) file path handling on Windows, so any path containing non-ASCII
characters — e.g. this project's Korean direction-group folder names
앞/뒤/좌/우 (CLAUDE.local.md #4.1) — silently mangles and fails with
"can't open/read file", even though the path is completely valid.
np.fromfile/ndarray.tofile go through Python's own (Unicode-correct) file
APIs, so decoding/encoding through an in-memory buffer sidesteps the bug
entirely. Always use these instead of cv2.imread/imwrite directly.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np


def imread_unicode(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path: str | Path, image: np.ndarray, params: list[int] | None = None) -> bool:
    """2026-09-08: a real 168-image FRONT run hit `OSError: [Errno 22] Invalid
    argument` on this exact write (a small, ~78MB-raw analysis mosaic -- not a
    2GB-plus-buffer case) right after a 20+ minute COLMAP fallback finished,
    discarding that entire run's output. Re-running just this write against the
    same freshly-produced image immediately after succeeded with no code
    change, so this was a one-off transient OS-level failure (most likely
    something else briefly holding the destination path open -- e.g. an
    Explorer thumbnail/preview handle or AV scan on the previous version of the
    same output file -- not a reproducible bug in the encode/write itself).
    Losing 20+ minutes of matching+SfM to a one-off OS hiccup on the very last
    write is not an acceptable failure mode regardless of root cause, so retry
    with a short backoff before giving up for real.
    """
    path = Path(path)
    ext = path.suffix if path.suffix else ".png"
    ok, buf = cv2.imencode(ext, image, params or [])
    if not ok:
        return False
    last_err: OSError | None = None
    for attempt in range(5):
        try:
            buf.tofile(str(path))
            return True
        except OSError as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise last_err
