# Downloads a shared/dev FFmpeg build (headers + import .lib + runtime .dll for
# libavcodec/libavutil/libswscale, MSVC-linkable, PLUS a working ffmpeg.exe) and installs it
# to tools\libffmpeg\, so FacadeDdsBridge's H.264 decode path builds against a known,
# versioned FFmpeg instead of whichever ad-hoc local install happens to link on a given
# machine. This is the ONE ffmpeg install this project uses -- previewer\tools\ffmpeg\ (a
# separate static CLI-only "full_build", GPL/libx264) was removed 2026-08-13 once this build's
# own bundled bin\ffmpeg.exe took over the JPEG->FLV test-encode job too (JpegFacadePublisher
# uses libopenh264 instead of libx264 for that now specifically so this LGPL-shared build,
# which has libx264 compiled out on purpose, still works for it -- see that file's comments).
#
# Source: youngilyou/libffmpeg (a small dedicated repo, one GitHub Release with the zip as
# its only asset) -- NOT BtbN/FFmpeg-Builds directly anymore. The original build still came
# from there (ffmpeg-n7.1-latest-win64-lgpl-shared-7.1.zip), but querying BtbN's "latest"
# release for that exact filename broke on a real second machine two different ways at once:
# (1) the `gh api --jq` filter embedded the asset name as a literal double-quoted string,
# and PowerShell's argument marshaling to the native `gh` process silently dropped the
# embedded quote characters, corrupting the jq filter syntax; (2) even with correct quoting,
# BtbN had since moved its "latest" release past n7.1 to n8.1/n9.0 -- the exact filename this
# script pinned to no longer exists in *any* "latest" release, by construction, the moment a
# newer FFmpeg version ships there. Re-hosting the already-tested build as a fixed release
# under this project's own control removes both problems: no jq filter to mis-quote (gh
# release download resolves the asset by name directly), and no "latest" to drift out from
# under a pinned filename (this repo's release tag never moves).
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\Get-FfmpegDevModule.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ModuleDir = Join-Path $ScriptDir "Module" # scratch/staging only, see below -- final install is NOT under here
$DestDir = Join-Path $ScriptDir "libffmpeg"
$Repo = "youngilyou/libffmpeg"
$ReleaseTag = "ffmpeg-n7.1-win64-lgpl-shared"
$AssetName = "libffmpeg-n7.1-win64-lgpl-shared.zip"

New-Item -ItemType Directory -Force -Path $ModuleDir | Out-Null

if (Test-Path (Join-Path $DestDir "lib\avcodec.lib")) {
    Write-Host "OK — already installed at: $DestDir"
    exit 0
}

$zipPath = Join-Path $ModuleDir $AssetName
Write-Host "Downloading $AssetName from $Repo release '$ReleaseTag' ..."
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
gh release download $ReleaseTag --repo $Repo --pattern $AssetName --dir $ModuleDir --clobber
if ($LASTEXITCODE -ne 0) {
    throw "gh release download failed (exit $LASTEXITCODE) -- check https://github.com/$Repo/releases/tag/$ReleaseTag exists and gh is authenticated (gh auth status)."
}
if (-not (Test-Path $zipPath)) {
    throw "gh release download reported success but $zipPath doesn't exist -- asset name mismatch?"
}

$ExtractDir = Join-Path $ModuleDir "ffmpeg-dev-extract"
if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
Write-Host "Extracting ..."
Expand-Archive -Path $zipPath -DestinationPath $ExtractDir -Force

$InnerDir = Get-ChildItem $ExtractDir -Directory | Select-Object -First 1
if (-not $InnerDir) {
    throw "Expected one top-level folder inside the zip, found none."
}

if (Test-Path $DestDir) { Remove-Item -Recurse -Force $DestDir }
Move-Item $InnerDir.FullName $DestDir

Remove-Item -Force $zipPath
Remove-Item -Recurse -Force $ExtractDir

if (Test-Path (Join-Path $DestDir "lib\avcodec.lib")) {
    Write-Host "OK — FFmpeg dev build installed at: $DestDir"
} else {
    throw "Extraction finished but $DestDir\lib\avcodec.lib was not found -- check the zip layout above."
}
