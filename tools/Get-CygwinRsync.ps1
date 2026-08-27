# Downloads a Cygwin-built rsync.exe + a Cygwin-built ssh.exe (+ both of their runtime DLLs)
# from the official Cygwin mirror and installs them locally, so FacadeDdsBridge's rsync DLL
# wrapper (previewer's high-resolution facade image transfer feature) has a working rsync+ssh
# client pair on a fresh Windows checkout.
#
# Why Cygwin and not a native MSVC build: rsync's source (upstream, including
# https://github.com/RsyncProject/rsync) depends on fork(), Unix sockets, and other POSIX APIs
# with no clean Win32 equivalent, plus an autoconf `configure` script -- none of which MSVC/CMake
# can build directly without a POSIX compatibility layer. Every "rsync for Windows" distribution
# that has ever existed (cwRsync, DeltaCopy, ...) is Cygwin-based for exactly this reason; this
# script fetches the same kind of build directly from Cygwin's own official package mirror
# instead of a third-party repackaging.
#
# Isolation from the MSVC/Visual Studio build (explicit project requirement -- mixing Cygwin and
# Visual Studio headers/libs in the same build has caused real problems on this project before):
# this installs ONLY prebuilt rsync.exe/ssh.exe + their Cygwin runtime DLLs, used purely as an
# external process invoked via CreateProcess (see FacadeDdsBridge's rsync wrapper) -- never added
# to any MSVC project's include/lib paths, same arm's-length pattern already used for ffmpeg.exe
# elsewhere in this project (tools/libffmpeg's bin/ffmpeg.exe, shelled out to by
# JpegFacadePublisher.cpp).
#
# [YYIL] 2026-08-21: SSH transport (`rsync -e ssh`) used to shell out to Windows' own built-in
# OpenSSH client (C:\Windows\System32\OpenSSH\ssh.exe) instead of vendoring one, on the
# assumption that any working ssh client would do. A real end-to-end transfer test proved that
# assumption wrong: pairing Cygwin's rsync.exe with a NATIVE (non-Cygwin) ssh.exe child process
# breaks the rsync binary protocol stream immediately every time ("connection unexpectedly
# closed (0 bytes received so far)", rsync error code 12) -- confirmed independent of Korean
# vs. ASCII paths, compression on/off, and dry-run, so it is a pipe/exec interop issue between
# Cygwin's and native Win32's differing stdio-handle semantics for a child process, not a
# configuration mistake. This is exactly the reason every historical Windows rsync distribution
# (cwRsync, DeltaCopy, ...) bundles its own Cygwin-native ssh client rather than relying on
# whatever ssh happens to be installed on the host -- vendoring one here now for the same reason.
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\Get-CygwinRsync.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $ScriptDir "cygwin_rsync"
$BinDir = Join-Path $InstallDir "bin"
$DownloadDir = Join-Path $InstallDir "_download"

if ((Test-Path (Join-Path $BinDir "rsync.exe")) -and (Test-Path (Join-Path $BinDir "cygwin1.dll")) -and (Test-Path (Join-Path $BinDir "ssh.exe")) -and (Test-Path (Join-Path $BinDir "sshpass.exe"))) {
    Write-Host "OK -- already installed at $BinDir, skipping (delete that folder to force a re-download)."
    exit 0
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null

# Pinned to specific package versions (found via the mirror's x86_64/setup.ini package database)
# rather than "latest", so a fresh checkout reproduces the exact same binaries that were tested
# when this script was written. Bump these deliberately, not accidentally, if ever needed.
$Mirror = "https://cygwin.mirror.constant.com/x86_64/release"
$Packages = @(
    @{ Name = "rsync";      Url = "$Mirror/rsync/rsync-3.3.0-1.tar.xz";                             ExtractPath = "usr/bin/rsync.exe" }
    @{ Name = "cygwin";     Url = "$Mirror/cygwin/cygwin-3.6.10-1-x86_64.tar.xz";                    ExtractPath = "usr/bin/cygwin1.dll" }
    @{ Name = "libssl3";    Url = "$Mirror/openssl/libssl3/libssl3-3.5.7-1-x86_64.tar.zst";          ExtractPath = "usr/bin/cygcrypto-3.dll" }
    @{ Name = "libiconv2";  Url = "$Mirror/libiconv/libiconv2/libiconv2-1.19-2-x86_64.tar.xz";       ExtractPath = "usr/bin/cygiconv-2.dll" }
    @{ Name = "liblz4_1";   Url = "$Mirror/lz4/liblz4_1/liblz4_1-1.9.4-1.tar.xz";                    ExtractPath = "usr/bin/cyglz4-1.dll" }
    @{ Name = "libzstd1";   Url = "$Mirror/zstd/libzstd1/libzstd1-1.5.7-1.tar.zst";                  ExtractPath = "usr/bin/cygzstd-1.dll" }
    @{ Name = "libxxhash0"; Url = "$Mirror/xxhash/libxxhash0/libxxhash0-0.8.3-1.tar.xz";             ExtractPath = "usr/bin/cygxxhash-0.dll" }
    @{ Name = "zlib0";      Url = "$Mirror/zlib/zlib0/zlib0-1.3.2-1-x86_64.tar.zst";                 ExtractPath = "usr/bin/cygz.dll" }
    # Cygwin ssh.exe (see header comment on why this replaces native Windows OpenSSH) + its own
    # transitive DLL closure, found via `dumpbin /dependents` on each binary in turn until every
    # import resolved (cyggssapi_krb5-2.dll pulls in a Kerberos stack even though this project
    # only ever uses plain public-key auth -- PE imports are resolved eagerly at load time
    # regardless of which code paths actually run, so all of it must be physically present).
    @{ Name = "openssh";           Url = "$Mirror/openssh/openssh-10.4p1-1-x86_64.tar.xz";                          ExtractPath = "usr/bin/ssh.exe" }
    @{ Name = "libgcc1";           Url = "$Mirror/gcc/libgcc1/libgcc1-14.4.0-1-x86_64.tar.zst";                     ExtractPath = "usr/bin/cyggcc_s-seh-1.dll" }
    @{ Name = "libgssapi_krb5_2";  Url = "$Mirror/krb5/libgssapi_krb5_2/libgssapi_krb5_2-1.15.2-2.tar.xz";          ExtractPath = "usr/bin/cyggssapi_krb5-2.dll" }
    @{ Name = "libk5crypto3";      Url = "$Mirror/krb5/libk5crypto3/libk5crypto3-1.15.2-2.tar.xz";                  ExtractPath = "usr/bin/cygk5crypto-3.dll" }
    @{ Name = "libkrb5_3";         Url = "$Mirror/krb5/libkrb5_3/libkrb5_3-1.15.2-2.tar.xz";                        ExtractPath = "usr/bin/cygkrb5-3.dll" }
    @{ Name = "libkrb5support0";   Url = "$Mirror/krb5/libkrb5support0/libkrb5support0-1.15.2-2.tar.xz";            ExtractPath = "usr/bin/cygkrb5support-0.dll" }
    @{ Name = "libcom_err2";       Url = "$Mirror/e2fsprogs/libcom_err2/libcom_err2-1.44.5-1.tar.xz";                ExtractPath = "usr/bin/cygcom_err-2.dll" }
    @{ Name = "libintl8";          Url = "$Mirror/gettext/libintl8/libintl8-0.26-1-x86_64.tar.xz";                  ExtractPath = "usr/bin/cygintl-8.dll" }
    # sshpass: non-interactive password auth for ssh (2026-08-27, "SSH 키 없으면 Password로"
    # requirement -- see RsyncTransfer.cpp's ssh_password handling). Depends only on cygwin1.dll
    # (already vendored above, confirmed via this package's own .hint file: "requires: cygwin"),
    # no extra transitive DLLs.
    @{ Name = "sshpass";           Url = "$Mirror/sshpass/sshpass-1.10-1-x86_64.tar.xz";                            ExtractPath = "usr/bin/sshpass.exe" }
)

# Windows' own bundled tar.exe (bsdtar/libarchive) handles both .tar.xz and .tar.zst natively --
# confirmed on this project's dev machine (bsdtar 3.8.4, libarchive 3.8.4 with libzstd/1.5.7).
# No separate xz/zstd decoder needs installing.
$TarExe = "$env:SystemRoot\System32\tar.exe"
if (-not (Test-Path $TarExe)) {
    throw "Windows' bundled tar.exe not found at $TarExe -- this script relies on it to extract .tar.xz/.tar.zst without installing a separate decoder."
}

foreach ($pkg in $Packages) {
    $archivePath = Join-Path $DownloadDir ([System.IO.Path]::GetFileName($pkg.Url))
    Write-Host "[$($pkg.Name)] downloading $($pkg.Url) ..."
    Invoke-WebRequest -Uri $pkg.Url -OutFile $archivePath -UseBasicParsing

    Write-Host "[$($pkg.Name)] extracting $($pkg.ExtractPath) ..."
    & $TarExe -xf $archivePath -C $DownloadDir $pkg.ExtractPath
    if ($LASTEXITCODE -ne 0) {
        throw "$($pkg.Name): tar extraction failed (exit $LASTEXITCODE)"
    }

    $extractedFile = Join-Path $DownloadDir $pkg.ExtractPath
    $destFile = Join-Path $BinDir (Split-Path -Leaf $pkg.ExtractPath)
    Copy-Item -Path $extractedFile -Destination $destFile -Force
}

Remove-Item -Recurse -Force $DownloadDir -ErrorAction SilentlyContinue

# Verify both extracted binaries actually run (catches a missing/mismatched DLL immediately
# instead of surprising whoever's first to invoke it through the C++ wrapper).
$rsyncExe = Join-Path $BinDir "rsync.exe"
$versionOutput = & $rsyncExe --version 2>&1
if ($LASTEXITCODE -ne 0 -or -not ($versionOutput -match "rsync\s+version")) {
    throw "rsync.exe did not run correctly after install -- output: $versionOutput"
}

$sshExe = Join-Path $BinDir "ssh.exe"
# ssh -V writes its version string to stderr with no stdout at all -- under
# $ErrorActionPreference = "Stop" (set at the top of this script), a bare `2>&1` redirect on a
# native command wraps that stderr line in a terminating ErrorRecord even though ssh.exe's own
# exit code is 0 (see the general PowerShell-native-stderr gotcha this project has hit before).
# Flip to Continue for just this one call so a normal, successful `-V` doesn't abort the script.
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$sshVersionOutput = & $sshExe -V 2>&1 | Out-String
$ErrorActionPreference = $previousErrorActionPreference
if ($LASTEXITCODE -ne 0 -or -not ($sshVersionOutput -match "OpenSSH")) {
    throw "ssh.exe did not run correctly after install (missing DLL?) -- output: $sshVersionOutput"
}

$sshpassExe = Join-Path $BinDir "sshpass.exe"
# sshpass -V exits 1 by design (not 0) but still prints its version banner to stdout -- checking
# the banner text, not $LASTEXITCODE, matches sshpass's own actual documented behavior.
$sshpassVersionOutput = & $sshpassExe -V 2>&1 | Out-String
if (-not ($sshpassVersionOutput -match "sshpass")) {
    throw "sshpass.exe did not run correctly after install (missing DLL?) -- output: $sshpassVersionOutput"
}

Write-Host ""
Write-Host "OK -- rsync installed at: $rsyncExe"
Write-Host ($versionOutput | Select-Object -First 1)
Write-Host "OK -- ssh installed at: $sshExe"
Write-Host ($sshVersionOutput.Trim())
Write-Host "OK -- sshpass installed at: $sshpassExe"
Write-Host ($sshpassVersionOutput | Select-Object -First 1)
