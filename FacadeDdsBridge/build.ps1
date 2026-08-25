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
param(
    [string]$Config = "Debug"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

cmake -S $ScriptDir -B (Join-Path $ScriptDir "build") -G "Visual Studio 17 2022" -A x64
cmake --build (Join-Path $ScriptDir "build") --config $Config -- /p:VCToolsVersion=14.44.35207
