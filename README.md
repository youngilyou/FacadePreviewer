# FacadePreviewer

Field-capture tool for the DJI facade crack-inspection project: while an
operator flies a facade, this app decodes the live DDS video stream and
saves fixed-size JPEG snapshots into a per-session folder. Once the flight
is done, one button runs the offline Kornia+COLMAP stitching pipeline
against those photos. This is a separate, independent tool from the
`CheckCrackV2` repo's offline pipeline (that one is for precision analysis;
this one is for field coverage/capture) — see `CLAUDE.local.md` in this
folder for the full design history and every decision that led here.

## Getting this project

This project runs standalone on a laptop out in the field — it doesn't need
the `CheckCrack`/`CheckCrackV2` repo (the Python crack-analysis pipeline, the
C# results viewer, datasets, etc.), and none of its own heavy build output
(FastDDS SDK, ffmpeg, cygwin rsync) is committed to git in the first place
(see `.gitignore`).

```powershell
git clone https://github.com/youngilyou/FacadePreviewer.git
cd FacadePreviewer
```

## Install (native dependencies)

**Prerequisites**: `git`, `cmake`, the `gh` CLI (run `gh auth login` once if
`gh auth status` says you're not logged in), and Visual Studio 2022 with the
C++ workload. `conda` is not required up front — see below.

```powershell
cd tools
.\Setup-Tools.bat
```

Double-click works too. This runs three steps in order, each skipping work
that's already done on a re-run:

1. **FastDDSGen** — the FastDDS SDK + fastddsgen Java runtime
   (`tools\Module\FastDDSGen`).
2. **libffmpeg** — FFmpeg dev headers/`.lib`/`.dll` + `ffmpeg.exe`
   (`tools\libffmpeg`), for H.264 decode.
3. **cygwin_rsync** — `rsync.exe` + a Cygwin-built `ssh.exe` (pairing Cygwin
   rsync with Windows' native ssh.exe breaks the rsync protocol stream, so
   both must come from Cygwin) + runtime DLLs, for the high-resolution
   facade-image transfer feature.

See `tools\README.md` for what each script does and why, in more detail.

COLMAP is no longer a native vendored build here — `pip install -r
tools\stitch_engine\requirements.txt` now pulls in `pycolmap` instead, same
as the main CheckCrack repo's own pipeline (see "Usage" below, step 4).

## Build

Two independent build steps, in this order:

**1. FacadeDdsBridge (native C++ DLL — DDS subscribe, H.264 decode, ORB
stitching helpers)**:

```powershell
cd FacadeDdsBridge
.\build.ps1                # Debug (default)
.\build.ps1 -Config Release
```

Wraps `cmake -S . -B build -G "Visual Studio 17 2022" -A x64` +
`cmake --build build --config <Config>` with a required
`/p:VCToolsVersion=14.44.35207` MSBuild override — don't drop that flag if
you ever build by hand instead (see the comment at the top of `build.ps1`
for why it's required, not optional). If you'd rather build from the
Visual Studio IDE after this, run `Setup-VisualStudio.ps1` once first (also
in this folder) so the generated `.sln` picks up the same toolset override.

**2. FacadePreviewer (C# WPF app)**:

```powershell
dotnet build FacadePreviewer.sln -c Debug
```

Or open `FacadePreviewer.sln` in Visual Studio and build there. This
project's `.csproj` copies `FacadeDdsBridge.dll` (built in step 1) and its
runtime DLLs (FastDDS/fastcdr, FFmpeg) next to the app automatically — build
FacadeDdsBridge first, or these copies will be stale/missing.

## Run

```powershell
cd FacadePreviewer\bin\Debug\net9.0-windows
.\FacadePreviewer.exe
```

(Or press F5 in Visual Studio.) Usage flow:

1. Enter the DDS-Router host/port (and, if needed, a local network
   interface to bind — see the input fields at the top of the window) and
   click **캡처 시작**. The app subscribes over DDS, decodes the incoming
   H.264 video, and saves a 640×640 JPEG snapshot roughly every 0.5s into a
   new `<측정장소>_<yyyyMMdd_HHmmss>/` folder under whatever capture root
   path you've set.
2. Fly the facade. There's no live mosaic preview by design (see
   `CLAUDE.local.md`'s 2026-08-13 redesign notes for why real-time stitching
   was dropped) — just a capture-count readout.
3. Click **캡처 중지** once the flight is done.
4. Click **스캔 시작(스티칭→COLMAP)** to run the offline
   `tools\stitch_engine\stitch_folder.py` pipeline against the captured
   folder — progress streams into the on-screen log box.
5. **초기화** resets state to prepare for the next facade.

Requires Python on `PATH` with `tools\stitch_engine\requirements.txt`
installed for step 4 to work (not yet automated by `Setup-Tools.bat` — see
that folder's own notes).

## High-resolution photo transfer (rsync-over-SSH)

Separately from the 640×640 preview capture above, the **고해상도 전송...**
button (main window) sends the *original* full-resolution DJI photos to the
storage server over `rsync`+SSH, for offline crack analysis. This needs an
SSH private key authorized on that server.

**Getting a key**: ask an administrator to issue one from the DDS Monitor
dashboard's **Certificates (인증서)** page → **SSH 키 발급** section, download
the resulting zip (`id_ed25519` + `id_ed25519.pub` + `README.txt`), and hand
you the file directly (download-only, same as the DTLS cert bundle above —
there is no email-delivery option). The matching public key is registered on
the transfer server automatically at issuance time — no extra setup needed
there.

**Using it**:

1. Save the private key (`id_ed25519` from the zip) somewhere private, e.g.
   `C:\Users\<you>\.ssh\<name>`.
2. In the previewer's transfer settings window, set **SSH 키 (선택)** to that
   file's path.
3. Transfer as usual. If this field is left blank, `ssh` falls back to
   password auth, which the server doesn't accept — the transfer fails
   immediately with an auth error (rsync reports it as a generic
   `error in rsync protocol data stream (code 12)`, but the real cause is a
   missing/wrong SSH key, not a network problem).

**If a key is lost or a laptop is decommissioned**: ask an administrator to
delete it from the same DDS Monitor page (removes it from the server's
`authorized_keys` immediately) and issue a new one.
