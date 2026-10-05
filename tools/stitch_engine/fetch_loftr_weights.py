"""Pre-downloads kornia's LoFTR "outdoor" weights into the torch hub cache (run by Setup-StitchEngine.ps1).

kornia downloads them on first use from http://cmp.felk.cvut.cz/~mishkdmy/models/loftr_outdoor.ckpt,
and that server stops answering at times (2026-10-03: connection timeout from a fresh field laptop,
which failed setup). So the file is fetched from kornia's own Hugging Face repo first and placed
exactly where torch.hub looks for it; kornia then finds it in the cache and never contacts the
original server. The original URL stays as a fallback.
"""

import sys
import urllib.request
from pathlib import Path

import torch

FILE_NAME = "loftr_outdoor.ckpt"
SOURCES = [
    "https://huggingface.co/kornia/loftr/resolve/main/loftr_outdoor.ckpt",
    "http://cmp.felk.cvut.cz/~mishkdmy/models/loftr_outdoor.ckpt",
]
MIN_BYTES = 40 * 1024 * 1024  # the real file is ~46MB; anything much smaller is an error page


def main() -> int:
    target = Path(torch.hub.get_dir()) / "checkpoints" / FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)

    if not (target.exists() and target.stat().st_size >= MIN_BYTES):
        for url in SOURCES:
            tmp = target.with_suffix(".part")
            try:
                print(f"downloading {url}")
                with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
                if tmp.stat().st_size < MIN_BYTES:
                    raise IOError(f"too small ({tmp.stat().st_size} bytes)")
                tmp.replace(target)
                break
            except Exception as exc:  # try the next source
                print(f"  failed: {exc}")
                tmp.unlink(missing_ok=True)
        else:
            print("could not download the LoFTR outdoor weights from any source")
            return 1
    print(f"weights file: {target} ({target.stat().st_size} bytes)")

    # Load through kornia exactly like the pipeline does, so a bad file fails here, not on the first scan.
    import kornia.feature as KF

    KF.LoFTR(pretrained="outdoor")
    print("LoFTR weights ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
