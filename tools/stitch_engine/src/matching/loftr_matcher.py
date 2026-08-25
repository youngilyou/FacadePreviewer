"""LoFTR feature matching via Kornia (CLAUDE.local.md #8).

Images are downscaled only for LoFTR's own inference input; matched keypoint
coordinates are always rescaled back to original-resolution pixel space
before being returned (#8: "원본 이미지를 LoFTR 입력 크기로 영구 downscale하지 않는다").
"""

from __future__ import annotations

import multiprocessing as mp
import queue as queue_module
from pathlib import Path

import cv2
import kornia.feature as KF
import numpy as np
import torch

from src.common.config import Config
from src.common.imageio import imread_unicode
from src.common.types import MatchResult

_PAD_MULTIPLE = 8  # LoFTR's coarse stage downsamples by 8x; H/W must be divisible by it.


class LoFTRMatcher:
    def __init__(self, cfg: Config, device: str | None = None):
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.model = KF.LoFTR(pretrained=cfg.loftr.pretrained).to(self.device).eval()
        self.min_confidence = float(cfg.loftr.min_confidence)
        self.max_side = int(cfg.loftr.max_image_side)
        # Pair selection reuses each image across several pairs (temporal
        # neighbors + GPS-proximity candidates); without this, every pair
        # re-reads and re-decodes both full-resolution JPEGs from disk. For
        # a facade's image count (tens-hundreds) the downscaled tensors are
        # small enough this is cheap to keep for the matcher's lifetime.
        self._tensor_cache: dict[str, tuple[torch.Tensor, float, tuple[int, int]]] = {}

    def _load_gray_tensor(self, path: str | Path) -> tuple[torch.Tensor, float, tuple[int, int]]:
        key = str(path)
        if key in self._tensor_cache:
            return self._tensor_cache[key]

        img = imread_unicode(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"could not read image: {path}")
        h, w = img.shape
        scale = min(1.0, self.max_side / max(h, w))
        if scale < 1.0:
            new_w, new_h = int(round(w * scale)), int(round(h * scale))
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        else:
            scale = 1.0
            new_h, new_w = h, w

        pad_h = (-new_h) % _PAD_MULTIPLE
        pad_w = (-new_w) % _PAD_MULTIPLE
        if pad_h or pad_w:
            img = cv2.copyMakeBorder(img, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=0)

        tensor = torch.from_numpy(img).float()[None, None] / 255.0
        result = (tensor.to(self.device), scale, (new_h, new_w))
        self._tensor_cache[key] = result
        return result

    def match(self, path_a: str | Path, path_b: str | Path) -> MatchResult:
        tensor_a, scale_a, size_a = self._load_gray_tensor(path_a)
        tensor_b, scale_b, size_b = self._load_gray_tensor(path_b)

        with torch.inference_mode():
            out = self.model({"image0": tensor_a, "image1": tensor_b})

        conf = out["confidence"].detach().cpu().numpy()
        keep = conf >= self.min_confidence

        pts_a = out["keypoints0"].detach().cpu().numpy()[keep]
        pts_b = out["keypoints1"].detach().cpu().numpy()[keep]
        conf = conf[keep]

        # Drop matches that fell in the zero-padded border, then rescale to
        # original resolution.
        valid = (
            (pts_a[:, 0] < size_a[1])
            & (pts_a[:, 1] < size_a[0])
            & (pts_b[:, 0] < size_b[1])
            & (pts_b[:, 1] < size_b[0])
        )
        pts_a = pts_a[valid] / scale_a
        pts_b = pts_b[valid] / scale_b
        conf = conf[valid]

        return MatchResult(
            image_a=Path(path_a).stem,
            image_b=Path(path_b).stem,
            num_matches=int(len(conf)),
            keypoints_a=pts_a.astype(np.float64),
            keypoints_b=pts_b.astype(np.float64),
            confidence=conf.astype(np.float64),
        )


class MatchTimeoutError(RuntimeError):
    """A single pair's LoFTR match didn't return within TimeoutLoFTRMatcher's timeout."""


def _worker_main(cfg_dict: dict, device: str | None, task_q, result_q) -> None:
    """Entry point for TimeoutLoFTRMatcher's worker subprocess. Owns its own
    CUDA context -- runs LoFTRMatcher.match() requests until it receives a
    None sentinel (clean shutdown) or is killed by the parent (timeout)."""
    matcher = LoFTRMatcher(Config(cfg_dict), device)
    while True:
        task = task_q.get()
        if task is None:
            return
        path_a, path_b = task
        try:
            result = matcher.match(path_a, path_b)
            result_q.put(("ok", result))
        except Exception as exc:  # report back instead of crashing silently
            result_q.put(("error", repr(exc)))


class TimeoutLoFTRMatcher:
    """Runs LoFTRMatcher in a dedicated worker subprocess so one pathological
    pair can't stall an entire facade's pair loop.

    Ported from the main CheckCrack pipeline's src/matching/loftr_matcher.py,
    where this was added after confirming live (2026-08-15/16) that
    LoFTRMatcher.match() can hang 10+ minutes on a specific pair while every
    other pair in the same batch matches in ~1-2s -- consistent with
    CLAUDE.local.md #9's own warning that repetitive window patterns ("반복
    창문 패턴") cause matching trouble. A same-process timeout (e.g. a
    background thread with future.result(timeout=...)) can't actually recover
    from this: CUDA calls queued from different host threads still serialize
    on the same GPU context, so an abandoned thread's stuck kernel keeps
    blocking every pair after it anyway. Only killing the whole process tears
    down that CUDA context, so a timeout here kills the worker process and
    starts a fresh one -- the timed-out pair is then reported as
    MATCH_TIMEOUT and the facade's pair loop moves on to the next pair.
    """

    def __init__(self, cfg: Config, device: str | None = None, timeout_s: float = 60.0):
        self._cfg_dict = cfg.to_dict()
        self._device = device
        self._timeout_s = timeout_s
        self._ctx = mp.get_context("spawn")
        self._task_q = self._ctx.Queue()
        self._result_q = self._ctx.Queue()
        self._process: mp.process.BaseProcess | None = None
        self._spawn_worker()

    def _spawn_worker(self) -> None:
        self._process = self._ctx.Process(
            target=_worker_main,
            args=(self._cfg_dict, self._device, self._task_q, self._result_q),
            daemon=True,
        )
        self._process.start()

    def match(self, path_a: str | Path, path_b: str | Path) -> MatchResult:
        self._task_q.put((str(path_a), str(path_b)))
        try:
            status, payload = self._result_q.get(timeout=self._timeout_s)
        except queue_module.Empty:
            self._kill_and_respawn()
            raise MatchTimeoutError(
                f"LoFTR match timed out after {self._timeout_s:.0f}s: {path_a} <-> {path_b}"
            )
        if status == "error":
            raise RuntimeError(f"LoFTR match failed in worker process: {payload}")
        return payload

    def _kill_and_respawn(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=5)
        # Drop anything left in the queues from the killed worker before reuse.
        for q in (self._task_q, self._result_q):
            while True:
                try:
                    q.get_nowait()
                except queue_module.Empty:
                    break
        self._spawn_worker()

    def close(self) -> None:
        if self._process is not None and self._process.is_alive():
            try:
                self._task_q.put(None)
                self._process.join(timeout=5)
            except Exception:
                pass
            if self._process.is_alive():
                self._process.kill()
        self._process = None
