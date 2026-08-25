# Downloads youngilyou/Gen_IDL_DDS's ExtraModule (the packaged FastDDS SDK +
# fastddsgen Java runtime) and installs it locally, so this previewer's
# native DLL builds against a known, versioned FastDDS SDK instead of
# whichever ad-hoc local install happens to link on a given machine.
#
# Requires: gh CLI, already authenticated (`gh auth status`).
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\Get-FastDdsGenModule.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StagingDir = Join-Path $ScriptDir "ExtraModule"
$Repo = "youngilyou/Gen_IDL_DDS"

New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null

Write-Host "Listing $Repo/ExtraModule ..."
$names = gh api "repos/$Repo/contents/ExtraModule" --jq '.[].name' | Where-Object { $_ -ne "" }

foreach ($name in $names) {
    # download_url embeds a short-lived token -- fetched fresh right before
    # each download, not once upfront for the whole batch. Batching them
    # upfront was tried first and failed: by the time the loop reached the
    # 5th ~50MB file, the token from the initial listing call had expired
    # (404 Not Found), since sequentially downloading several 50MB files
    # takes longer than the token's lifetime.
    $entry = gh api "repos/$Repo/contents/ExtraModule/$name" | ConvertFrom-Json
    $destPath = Join-Path $StagingDir $entry.name

    if ((Test-Path $destPath) -and (Get-Item $destPath).Length -eq $entry.size) {
        Write-Host "  skip (already downloaded, size matches): $($entry.name)"
        continue
    }
    Write-Host "  downloading: $($entry.name) ($($entry.size) bytes)"
    # Binary-safe download via Invoke-WebRequest -OutFile. An earlier
    # `gh api ... > $destPath` approach corrupted the large split zip parts --
    # PowerShell's `>` redirect is text-mode and re-encodes stdout, which
    # silently mangles binary bytes (confirmed: Expand-Archive later failed
    # with "End of Central Directory record could not be found").
    Invoke-WebRequest -Uri $entry.download_url -OutFile $destPath -UseBasicParsing

    $actualSize = (Get-Item $destPath).Length
    if ($actualSize -ne $entry.size) {
        throw "Downloaded $($entry.name) but size mismatch: expected $($entry.size), got $actualSize"
    }
}

Write-Host "Running the repo's own install script (reassembles split zip parts, extracts, installs)..."
& (Join-Path $StagingDir "Install-ExtraModules.ps1")

$FastDdsRoot = Join-Path $ScriptDir "Module\FastDDSGen\FastDDS"
if (Test-Path $FastDdsRoot) {
    Write-Host "OK — FastDDS SDK installed at: $FastDdsRoot"
} else {
    throw "Install-ExtraModules.ps1 ran but $FastDdsRoot was not created — check its output above."
}
