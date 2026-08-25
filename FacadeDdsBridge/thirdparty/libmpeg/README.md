# libmpeg (vendored subset)

Cherry-picked (whole-library, see below) from `ireader/media-server`, MIT license
(`LICENSE-media-server`). Used for the MPEG-TS **demux** side: `ts_demuxer_create()`/
`ts_demuxer_input()` (`include/mpeg-ts.h`) turn a `VideoTsPacket` sample's raw 188-byte TS
packets back into H.264 Annex-B access units, which then feed `libavcodec` (see
`../FfmpegDev`/`tools/Get-FfmpegDevModule.ps1`) for actual decode.

Same vendoring method as `../librtmp` (see that folder's README for the fuller precedent):
clone the upstream repo into a scratch dir, cherry-pick the needed subset, discard the scratch
clone, record provenance here. Vendored fresh from GitHub — **not** copied from
`Z:\DDS_Platform\DDS-Router\thirdparty\libmpeg`, even though it's the exact same upstream
source and file set (diffed identical against a fresh clone before vendoring here) — this
project never shares code/build with DDS_Platform, same rule already applied to `librtmp`.

## Origin

`libmpeg/include` and `libmpeg/source` from `https://github.com/ireader/media-server`,
unmodified, **whole library** (not hand-trimmed to only the demux files) — this mirrors
DDS-Router's own `libmpeg` vendoring choice: it's a small, internally cross-referencing
library (the PS/mux/demux modules share internal headers like `mpeg-ts-internal.h`), and
splitting it into a "demux-only" subset would need re-verifying that split on every upstream
change for no real benefit (previewer only *calls* the demux entry points, but the mux code
being present too costs nothing — it's not linked into anything unless referenced).

Neither upstream repo's `.git/` was copied — cherry-picked via a shallow clone into a scratch
dir (not committed), selected folders copied out, scratch dir discarded.

## Build

`CMakeLists.txt` in this folder builds everything as a single static library target
`mpeg_min`, same shape as `../librtmp`'s `rtmp_min`. The consumer (`FacadeDdsBridge`) links
against it and includes `libmpeg/include`.
