@echo off
REM Double-click entry point that downloads/builds/installs every native dependency this
REM previewer needs under previewer/tools/, so a fresh checkout on another machine can
REM reproduce them without repeating the manual process documented in this folder's
REM README.md:
REM   1. ExtraModule + FastDDSGen  -> tools/Module/FastDDSGen/FastDDS  (Get-FastDdsGenModule.ps1)
REM   2. libffmpeg                 -> tools/libffmpeg/ (headers + .lib + .dll + ffmpeg.exe)
REM                                   (Get-FfmpegDevModule.ps1: H.264 decode for FacadeDdsBridge --
REM                                   this step used to be missing from this .bat entirely, so a
REM                                   fresh checkout built FacadeDdsBridge without it and failed
REM                                   with "libavcodec/avcodec.h: No such file or directory")
REM   3. cygwin_rsync              -> tools/cygwin_rsync/bin/rsync.exe + ssh.exe + runtime DLLs
REM                                   (Get-CygwinRsync.ps1: rsync+ssh client pair for the facade
REM                                   image transfer feature -- see that script's header for why
REM                                   both must be Cygwin-built (pairing Cygwin rsync with a native
REM                                   Win32 ssh.exe breaks the rsync protocol stream), why this is
REM                                   Cygwin-based rather than an MSVC build, and why it's kept
REM                                   fully isolated from the MSVC/Visual Studio build)
REM
REM COLMAP is no longer a native vendored build here -- stitch_engine/src/sfm/colmap_runner.py
REM now uses pycolmap (pip install -r tools/stitch_engine/requirements.txt) instead, matching
REM the main CheckCrack repo's own pipeline. See tools/stitch_engine/README.md.
REM
REM Requires: git, cmake, conda (on PATH), gh CLI (authenticated), Visual Studio 2022 with the
REM C++ workload. Safe to re-run -- each script skips steps that are already done.
REM
REM Usage: just double-click, or from a shell: tools\Setup-Tools.bat

setlocal

echo ============================================================
echo [1/3] ExtraModule + FastDDSGen
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Get-FastDdsGenModule.ps1"
if errorlevel 1 (
    echo.
    echo Get-FastDdsGenModule.ps1 failed -- see above. Stopping before libffmpeg.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo [2/3] libffmpeg ^(FFmpeg dev headers/libs/DLLs, for H.264 decode^)
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Get-FfmpegDevModule.ps1"
if errorlevel 1 (
    echo.
    echo Get-FfmpegDevModule.ps1 failed -- see above. Stopping before cygwin_rsync.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo [3/3] cygwin_rsync ^(rsync.exe + ssh.exe + runtime DLLs, for facade image transfer^)
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Get-CygwinRsync.ps1"
if errorlevel 1 (
    echo.
    echo Get-CygwinRsync.ps1 failed -- see above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo OK -- all tools installed.
echo ============================================================
pause
