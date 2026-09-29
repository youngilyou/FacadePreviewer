"""Checks that this Python has everything tools/stitch_engine needs (run by tools/Setup-StitchEngine.ps1).

Exits non-zero with a message on the first missing module or too-old pycolmap.
"""

from __future__ import annotations

import importlib
import sys

MODULES = ["torch", "kornia", "cv2", "numpy", "pandas", "yaml", "scipy", "networkx", "PIL", "pycolmap", "pyproj"]


def main() -> None:
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 -- report any import failure, not just ImportError
            sys.exit(f"missing or broken module {name}: {exc}")
        print(f"  {name:10s} {getattr(mod, '__version__', '')}")

    import pycolmap
    import torch

    major, minor = (int(x) for x in pycolmap.__version__.split(".")[:2])
    if (major, minor) < (4, 2):
        sys.exit(f"pycolmap {pycolmap.__version__} installed, 4.2 or newer required")
    print("  CUDA available:", torch.cuda.is_available())


if __name__ == "__main__":
    main()
