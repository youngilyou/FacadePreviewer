# tools/

## Setup-Tools.bat (run this first, on a fresh checkout)

Double-click entry point that runs `Get-FastDdsGenModule.ps1`,
`Get-FfmpegDevModule.ps1`, and `Get-CygwinRsync.ps1` in order — so a fresh
checkout on another machine can reproduce every native dependency this
project needs without repeating the manual acquisition process by hand.
Safe to re-run; each underlying script skips work that's already done.

```
tools\Setup-Tools.bat
```

Requires: `git`, `cmake`, `conda` (on `PATH`), the `gh` CLI (authenticated),
and Visual Studio 2022 with the C++ workload and the MSVC 14.44.35207
toolset (see `previewer/CLAUDE.local.md`'s "빌드 이식성" section for why that
exact sub-version matters).

## COLMAP

No longer a native vendored build here (the former `tools/colmap_deps/`
native COLMAP 3.13.0 C++ source+build, and the `Get-ColmapDeps.ps1` script
that produced it, were both removed). `stitch_engine/src/sfm/colmap_runner.py`
now uses `pycolmap` instead — installed via
`pip install -r tools/stitch_engine/requirements.txt`, same as the main
CheckCrack repo's own pipeline. See `tools/stitch_engine/README.md`.

## Get-CygwinRsync.ps1

Downloads a Cygwin-built `rsync.exe` **and a Cygwin-built `ssh.exe`**, plus
both binaries' runtime DLLs, directly from Cygwin's official package
mirror, for the facade high-resolution image transfer feature
(FacadePreviewer → rsync-over-ssh → DDS-Router's `FacadeImageBridge`).

**Why Cygwin, not a native MSVC build**: rsync's upstream source (including
[RsyncProject/rsync](https://github.com/RsyncProject/rsync)) depends on
`fork()`, Unix sockets, and other POSIX APIs with no Win32 equivalent, plus
an autoconf `configure` script — none of which MSVC/CMake can build
directly without a POSIX layer. Every Windows rsync distribution that has
ever existed (cwRsync, DeltaCopy, ...) is Cygwin-based for this reason;
this script fetches the same kind of build straight from Cygwin's mirror
instead of a third-party repackaging.

**Isolation from the MSVC/Visual Studio build** (explicit project
requirement — mixing Cygwin and Visual Studio headers/libs in the same
build has caused real problems on this project before): this installs only
the prebuilt binaries, used purely as an external process invoked via
`CreateProcess` (see `FacadeDdsBridge`'s rsync wrapper) — never added to
any MSVC project's include/lib paths. Same arm's-length pattern already
used for `ffmpeg.exe` elsewhere in this project.

**SSH transport (`rsync -e ssh`) uses this same vendored Cygwin `ssh.exe`,
not Windows' built-in OpenSSH** — this changed 2026-08-21 after a real
end-to-end transfer test proved the original built-in-OpenSSH design wrong:
pairing Cygwin's `rsync.exe` with a *native* (non-Cygwin) `ssh.exe` child
process breaks the rsync binary protocol stream immediately, every time
(`connection unexpectedly closed (0 bytes received so far)`, rsync error
code 12) — confirmed independent of path encoding, compression, or
dry-run, so it's a pipe/exec interop mismatch between Cygwin's and native
Win32's stdio-handle semantics, not a configuration mistake. This is
exactly why cwRsync/DeltaCopy/etc. all bundle their own Cygwin ssh instead
of relying on whatever ssh happens to be on the host — same fix here.
`ssh.exe`'s own dependency closure pulls in a full Kerberos/GSSAPI stack
(`cyggssapi_krb5-2.dll` → `cygkrb5-3.dll`/`cygk5crypto-3.dll`/
`cygkrb5support-0.dll`/`cygcom_err-2.dll`/`cygintl-8.dll`) plus
`cyggcc_s-seh-1.dll` even though this project only ever uses plain
public-key auth — found by `dumpbin /dependents` on each binary in turn
until every PE import resolved (Windows resolves all of them eagerly at
load time regardless of which code paths actually run, so all of it has to
be physically present or `ssh.exe` won't even start:
`STATUS_DLL_NOT_FOUND`).

```powershell
powershell -ExecutionPolicy Bypass -File tools\Get-CygwinRsync.ps1
```

Re-running is safe (skips if `rsync.exe`/`cygwin1.dll`/`ssh.exe` all
already exist). Package versions are pinned (found via the mirror's
`x86_64/setup.ini` package database) rather than "latest", so a fresh
checkout reproduces the exact binaries tested when this script was
written. Output: `tools/cygwin_rsync/bin/{rsync,ssh}.exe` + their DLLs,
each verified to actually run (`rsync --version` / `ssh -V`) before the
script reports success.

## Get-FastDdsGenModule.ps1

Downloads and installs the FastDDS SDK from
[youngilyou/Gen_IDL_DDS](https://github.com/youngilyou/Gen_IDL_DDS)'s
`ExtraModule` (a packaged FastDDS SDK + fastddsgen Java runtime, distributed
as split zip parts since GitHub blocks single files over 100MB).

```powershell
powershell -ExecutionPolicy Bypass -File tools\Get-FastDdsGenModule.ps1
```

Requires the `gh` CLI, already authenticated (`gh auth status`). Downloads
into `tools/ExtraModule/` (staging, not committed — see `.gitignore`), then
runs the repo's own `Install-ExtraModules.ps1` to reassemble the split zip,
extract it, and install to `tools/Module/FastDDSGen/{FastDDS,Java}`.

`FacadeDdsBridge/CMakeLists.txt` points at `tools/Module/FastDDSGen/FastDDS`
by default — run this script once before building that project.

Re-running is safe: already-downloaded files are skipped (size-verified).

## Get-FfmpegDevModule.ps1 (the one ffmpeg install this project uses)

Downloads and installs a **shared/dev** FFmpeg build (`libavcodec`/`libavutil`/
`libswscale` headers + MSVC-linkable `.lib` import libraries + runtime `.dll`s
+ a working `bin\ffmpeg.exe`) to `tools/libffmpeg/`, from
[youngilyou/libffmpeg](https://github.com/youngilyou/libffmpeg)'s
`ffmpeg-n7.1-win64-lgpl-shared` release (a single zip asset, `gh release
download`).

**Why not download straight from BtbN/FFmpeg-Builds** (where this build
originally came from): this script used to query BtbN's *latest* release
for the exact filename `ffmpeg-n7.1-latest-win64-lgpl-shared-7.1.zip`, and
that broke on a real second machine two ways at once — a `gh api --jq`
filter that embedded the filename as a literal quoted string got mangled by
PowerShell's argument passing to the native `gh` process, and separately,
BtbN had since moved its "latest" release past n7.1 to n8.1/n9.0, so the
pinned filename no longer existed in *any* "latest" release by
construction. Re-hosting the already-tested build as a fixed release under
this project's own control removes both failure modes for good — no jq
filter to mis-quote, and no "latest" to drift out from under a pinned name.

Serves two roles at once:

- **Linking**: `FacadeDdsBridge`'s H.264 decode path (`VideoTsPacket` -> TS
  demux via `../FacadeDdsBridge/thirdparty/libmpeg` -> `avcodec_send_packet`/
  `avcodec_receive_frame` -> BGR frame for OpenCvSharp/ORB stitching) links
  against `tools/libffmpeg/lib/*.lib` and copies `tools/libffmpeg/bin/*.dll`
  next to the built binaries.
- **Encoding**: `FacadeDdsBridgeSmokeTest.exe --publish-facade`'s
  `JpegFacadePublisher` (JPEG -> FLV encoding for the RTMP-direct-ingest
  feasibility check, see `CLAUDE.local.md` "RTMP 직접 수신") shells out to
  `tools/libffmpeg/bin/ffmpeg.exe` directly (found relative to the calling
  exe's own path, no PATH setup needed) using `-c:v libopenh264`, **not**
  `libx264` — this LGPL-shared build has libx264 compiled out on purpose
  (GPL), and OpenH264's output is standard-compliant Annex-B H.264 that
  decodes identically on the receiving end.

```powershell
powershell -ExecutionPolicy Bypass -File tools\Get-FfmpegDevModule.ps1
```

Requires the `gh` CLI, already authenticated. `FacadeDdsBridge/CMakeLists.txt`
points at `tools/libffmpeg` by default — run this script once before
building. Re-running is safe/idempotent (skips if `lib/avcodec.lib` already
exists).

**History**: until 2026-08-13 this project vendored a second, separate ffmpeg
install (`tools/ffmpeg/`, ~650MB, a full **static** CLI-only GPL build with
libx264, originally pulled in via `winget install Gyan.FFmpeg` to support
`JpegFacadePublisher` before this shared/dev build existed at all). Removed
once `tools/libffmpeg/bin/ffmpeg.exe` (which this script already installs)
took over that job too — see `JpegFacadePublisher.cpp`'s own comments for the
full story, including a real corruption bug this consolidation pass also
uncovered and fixed (unrelated to the ffmpeg-install merge itself: publishing
at the source JPEGs' full DJI resolution overwhelmed this project's
deliberately-BEST_EFFORT DDS QoS and corrupted H.264 decode -- fixed by
capping the encode at 1080p, which was always this previewer's actual design
target, see project memory `project-dds-previewer-design`).
