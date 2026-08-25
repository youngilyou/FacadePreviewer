cmake_minimum_required(VERSION 3.16)
project(mpeg_min C)

# [YYIL] 2026-08-13: vendored subset of ireader/media-server's libmpeg
# (https://github.com/ireader/media-server/tree/master/libmpeg) -- the demux half
# (ts_demuxer_create/input, see mpeg-ts.h) this project needs to turn a received
# VideoTsPacket's raw 188-byte TS packets back into H.264 Annex-B access units before
# feeding them to libavcodec. Same upstream project, same file set as
# Z:\DDS_Platform\DDS-Router\thirdparty\libmpeg (that copy's own CMakeLists.txt explains why
# the whole library -- mux AND demux -- is vendored as one unit rather than a hand-trimmed
# subset: it's a small, tightly-coupled library, splitting it isn't worth the maintenance
# burden), but fetched fresh from GitHub independently, not copied from DDS-Router's tree
# (this project never shares code/build with DDS_Platform, see ../librtmp/README.md for the
# same reasoning already applied there).

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED YES)

file(GLOB MPEG_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/source/*.c")

add_library(mpeg_min STATIC ${MPEG_SOURCES})

target_include_directories(mpeg_min PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
