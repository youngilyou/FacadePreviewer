# Installs the Python side of FacadePreviewer's scan button (tools/stitch_engine) into the Python the
# app will run, then checks it. FacadePreviewer launches plain `python` from PATH, so by default this
# installs into that same interpreter; pass -Python to target another one.
#
#   0. If `python` isn't on PATH, installs Python 3.12 with winget (added to the user PATH).
#   1. torch: the CUDA build when an NVIDIA GPU is present (nvidia-smi on PATH). A bare
#      `pip install torch` on Windows gets the CPU-only build, and LoFTR matching on CPU takes hours.
#   2. tools/stitch_engine/requirements.txt (pycolmap 4.2.0, kornia, opencv, pyproj, ...).
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

# Returns "3.12" etc., or $null when $exe isn't a working Python. try/catch because on a PC without
# Python, `python` is the Microsoft Store alias stub: it writes to stderr, and Windows PowerShell 5.1
# turns that into a terminating NativeCommandError under ErrorActionPreference=Stop even with 2>$null.
function Get-PythonVersion([string]$exe) {
    try { $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
    return "$v".Trim()
}

$pyVersion = Get-PythonVersion $Python
if (-not $pyVersion -and $Python -eq "python") {
    # No Python on PATH (fresh field laptop): install Python 3.12 with winget. The override makes the
    # python.org installer add itself to the user PATH (its default is not to), ahead of the
    # WindowsApps Store alias, so FacadePreviewer's plain `python` resolves to it.
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python not found and winget is unavailable. Install Python 3.10+ from https://www.python.org/downloads/ (check 'Add python.exe to PATH') and re-run."
    }
    Write-Host "==> Python not found: installing Python 3.12 (winget install --id Python.Python.3.12 -e)"
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements `
        --override "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1"
    # Don't trust winget's exit code alone (it is non-zero when the package is already installed);
    # what matters is whether python works afterwards.

    # This process still has the old PATH; reload it so the new install is visible here.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $pyVersion = Get-PythonVersion $Python
    if (-not $pyVersion) {
        $installed = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
        if (Test-Path $installed) {
            throw "Python 3.12 is installed at $installed but 'python' still doesn't resolve to it. Turn off the 'python.exe' / 'python3.exe' entries in Settings > Apps > Advanced app settings > App execution aliases, open a NEW PowerShell window, and re-run."
        }
        throw "winget did not install Python 3.12 (see output above). Install Python 3.10+ from https://www.python.org/downloads/ (check 'Add python.exe to PATH') and re-run."
    }
}
if (-not $pyVersion) {
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

Invoke-Checked "import check" { & $Python (Join-Path $engineDir "check_env.py") }

Write-Host ""
Write-Host "stitch_engine is ready. FacadePreviewer's scan will use: $(& $Python -c 'import sys; print(sys.executable)')"
