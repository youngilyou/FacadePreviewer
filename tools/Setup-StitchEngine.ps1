# Installs the Python side of FacadePreviewer's scan button (tools/stitch_engine) into the Python the
# app will run, then checks it. FacadePreviewer launches plain `python` from PATH, so by default this
# installs into that same interpreter; pass -Python to target another one.
#
#   1. torch: the CUDA build when an NVIDIA GPU is present (nvidia-smi on PATH). A bare
#      `pip install torch` on Windows gets the CPU-only build, and LoFTR matching on CPU takes hours.
#   2. tools/stitch_engine/requirements.txt (pycolmap 4.3.0, kornia, opencv, pyproj, ...).
#   3. LoFTR "outdoor" weights, downloaded now into the torch hub cache so the first scan on a
#      field laptop without internet doesn't fail.
#   4. Import/version check of everything the engine uses.
#
# Safe to re-run. Usage:
#   powershell -ExecutionPolicy Bypass -File tools\Setup-StitchEngine.ps1 [-Python <path>] [-Cpu]

param(
    [string]$Python = "python",
    # Force the CPU-only torch build even when an NVIDIA GPU is present.
    [switch]$Cpu,
    # PyTorch wheel index for the CUDA build (cu126 is what the dev machine was verified with).
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu126"
)

$ErrorActionPreference = "Stop"
$engineDir = Join-Path $PSScriptRoot "stitch_engine"
$requirements = Join-Path $engineDir "requirements.txt"

function Invoke-Checked([string]$what, [scriptblock]$cmd) {
    Write-Host "==> $what"
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

$pyVersion = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $pyVersion) {
    throw "Python not found ('$Python'). Install Python 3.10+ (or Miniconda) and make sure it is on PATH -- FacadePreviewer runs 'python' from PATH."
}
if ([version]$pyVersion -lt [version]"3.10") { throw "Python $pyVersion is too old -- 3.10 or newer is required." }
Write-Host "Python $pyVersion ($(& $Python -c 'import sys; print(sys.executable)'))"

$hasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($hasNvidia -and -not $Cpu) {
    Invoke-Checked "torch (CUDA build, $TorchIndexUrl)" { & $Python -m pip install --upgrade torch --index-url $TorchIndexUrl }
} else {
    if (-not $hasNvidia) { Write-Warning "No NVIDIA GPU found (nvidia-smi missing): installing CPU-only torch. LoFTR matching will be very slow." }
    Invoke-Checked "torch (CPU build)" { & $Python -m pip install --upgrade torch }
}

Invoke-Checked "requirements.txt" { & $Python -m pip install -r $requirements }

Invoke-Checked "LoFTR outdoor weights (pre-download)" {
    & $Python -c "import kornia.feature as KF; KF.LoFTR(pretrained='outdoor'); print('LoFTR weights ready')"
}

$check = @'
import importlib, sys
mods = ["torch", "kornia", "cv2", "numpy", "pandas", "yaml", "scipy", "networkx", "PIL", "pycolmap", "pyproj"]
for m in mods:
    mod = importlib.import_module(m)
    print(f"  {m:10s} {getattr(mod, '__version__', '')}")
import pycolmap, torch
if pycolmap.__version__ != "4.3.0":
    sys.exit(f"pycolmap {pycolmap.__version__} installed, 4.3.0 required")
print("  CUDA available:", torch.cuda.is_available())
'@
Invoke-Checked "import check" { & $Python -c $check }

Write-Host ""
Write-Host "stitch_engine is ready. FacadePreviewer's scan will use: $(& $Python -c 'import sys; print(sys.executable)')"
