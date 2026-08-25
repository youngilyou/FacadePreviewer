# librtmp (vendored subset)

Cherry-picked source subset, used to give `FacadeDdsBridgeSmokeTest`'s `--rtmp-publish` mode
a real RTMP client (and, symmetrically, `RtmpVideoBridge` on the DDS-Router side an RTMP
server) without pulling in unrelated codec/container libraries this project doesn't need.
Same vendoring method as `DDS-Router/thirdparty/librtsp` (see that folder's own README for the
precedent this follows): clone the upstream repo into a scratch dir, cherry-pick only the files
actually needed (traced via the `#include` closure), discard the scratch clone, record
provenance here. Vendored fresh from GitHub, **not** copied from any local checkout (in
particular, not from `RobotMediaServer`'s own vendored copy of the same upstream project —
that copy belongs to a completely separate, independently-deployed system and this project
must stay self-contained per `previewer/CLAUDE.local.md`).

## Local patches

- `rtmp/source/rtmp-client-invoke-handler.h`, `rtmp_command_onstatus()`'s final `else` branch:
  changed `assert(0)` to a log-and-continue. Upstream aborts the whole process on any `onStatus`
  `code` string its hardcoded switch doesn't enumerate -- hit in practice against this project's
  own `RtmpVideoBridge` server, whose real `deleteStream` reply is `"NetStream.DeleteStream.Suceess"`
  (`rtmp-server.c`'s `rtmp_server_ondelete_stream`, upstream's own typo/quirk, not something this
  project controls) -- not in the client's known-codes list, so the publisher crashed at the end
  of every successful publish run right as it closed the stream. An unrecognized status code from
  a real server is data, not a programmer error; asserting on it is a correctness bug this patch
  fixes, not a project-specific policy override upstream would reject.

## Origin

- `rtmp/` — https://github.com/ireader/media-server (`librtmp`), MIT license
  (`LICENSE-media-server`). `rtmp/include` and `rtmp/source` are `librtmp/include` and
  `librtmp/source` from that repo, **except the one local patch above**, **excluding** `librtmp/aio/*` (Linux-only async
  I/O wrappers this project doesn't use — both the publish client and `RtmpVideoBridge` do
  their own blocking-socket read/write loop, matching how `RtspClientPublisher`/
  `RtspVideoBridge` already handle RTSP) and `librtmp/test/*` (upstream's own example programs,
  reference only, see their `rtmp-publish-test.cpp`/`rtmp-server-publish-test.cpp` for the
  usage pattern this project's own code follows).
- `libflv_min/amf0.{h,c}` — also from `ireader/media-server`, but from the sibling `libflv/`
  module (`libflv/include/amf0.h`, `libflv/source/amf0.c`). `librtmp` needs AMF0
  encode/decode for RTMP's `connect`/`publish`/`onStatus` command messages; this is the only
  file pair `libflv` contributes; **not** vendoring the rest of `libflv` (FLV tag mux/demux) —
  this project reads/writes FLV video-tag bodies directly against the `AVCPacketType`/NALU
  layout documented in `rtmp-server.h`'s `onvideo` comment, no FLV container parsing needed
  since `librtmp`'s `onvideo`/`push_video` already hand over exactly that tag body, not a
  file-level FLV stream.
- `sdk_min/` — https://github.com/ireader/sdk, MIT license (`LICENSE-sdk`). `librtmp` has a
  small, unavoidable compile-time dependency on this separate repo (RTMP handshake digest +
  URL parsing), traced via `librtmp`'s own `#include` closure:
  `sha.{h,c}` (HMAC-SHA256, RTMP complex handshake digest), `uri-parse.{h,c}` and
  `urlcodec.{h,c}` (parsing `rtmp://host/app/stream` URLs). Verified these three pull in
  nothing further (their own `#include`s only reference each other/standard headers).

Neither upstream repo's `.git/` was copied — cherry-picked via a shallow sparse-clone into a
scratch dir (not committed), selected files copied out, scratch dir discarded.

## What's NOT here

Full `libflv` (FLV container mux/demux — not needed, see above), `libmpeg`/`libmov`/`libmkv`/
`libhls`/`libdash`/`libsip`/`librtsp`/`librtp` (other unrelated media-server modules),
`librtmp/aio/*`, and the rest of `ireader/sdk` (threading/socket/aio abstractions used by other
media-server components, not by the plain blocking-socket code this project writes itself).

## Build

`CMakeLists.txt` in this folder builds everything above as a single static library target
`rtmp_min`. The consumer (`FacadeDdsBridgeSmokeTest`) links against it and includes
`librtmp/rtmp/include`, `librtmp/libflv_min`, and `librtmp/sdk_min`.
