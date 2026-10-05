# One-command build for FacadeDdsBridge. Requires
# tools\Get-FastDdsGenModule.ps1 to have been run first (see tools\README.md).
#
# The /p:VCToolsVersion override below is REQUIRED, not optional — without
# it the build fails with 9 unresolved external symbols
# (_Cnd_timedwait_for_unchecked, __std_search_1, etc.) because the
# Gen_IDL_DDS FastDDS SDK's prebuilt libs were compiled with MSVC toolset
# 14.44 (confirmed via `dumpbin /headers` on fastddsd-3.6.dll: "14.44
# linker version"), and CMake's own `-T version=14.44` generator-toolset
# flag silently failed to apply on this CMake/VS combination (the resulting
# .vcxproj never got a <VCToolsVersion> override, and the build kept
# defaulting to whichever v143 sub-version cl.exe resolves to first, e.g.
# 14.33 if that's also installed) -- passing the MSBuild property directly
# through cmake --build's `--` passthrough is what actually works.
#
# Builds BOTH Debug and Release by default: FacadePreviewer.csproj copies
# build\$(Configuration)\FacadeDdsBridge.dll, so a Release app build with
# only the Debug DLL present silently ships without it and the app dies on
# launch (hit 2026-10-05 on a fresh laptop). Pass -Config Debug|Release to
# build just one.
param(
    [string[]]$Config = @("Debug", "Release")
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# A fresh machine often has no standalone CMake on PATH; Visual Studio 2022 ships one.
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vsPath = if (Test-Path $vswhere) { & $vswhere -latest -property installationPath } else { $null }
    $vsCmake = if ($vsPath) { Join-Path $vsPath "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin" } else { $null }
    if ($vsCmake -and (Test-Path (Join-Path $vsCmake "cmake.exe"))) {
        Write-Host "cmake not on PATH -- using Visual Studio's: $vsCmake"
        $env:Path = "$vsCmake;$env:Path"
    } else {
        throw "cmake not found. Install it (winget install Kitware.CMake) or add Visual Studio's 'C++ CMake tools' component."
    }
}

cmake -S $ScriptDir -B (Join-Path $ScriptDir "build") -G "Visual Studio 17 2022" -A x64
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }
foreach ($c in $Config) {
    Write-Host "==> building $c"
    cmake --build (Join-Path $ScriptDir "build") --config $c -- /p:VCToolsVersion=14.44.35207
    if ($LASTEXITCODE -ne 0) { throw "cmake build ($c) failed" }
}
