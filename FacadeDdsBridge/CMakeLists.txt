cmake_minimum_required(VERSION 3.20)

# Must be set before project() to take effect. Without this policy,
# CMAKE_MSVC_RUNTIME_LIBRARY below is silently ignored and CMake falls back
# to injecting /MD(d) via CMAKE_CXX_FLAGS -- which normally matches anyway,
# but the prebuilt fastdds libs are consistently missing CRT-internal
# symbols (_Cnd_timedwait_for_unchecked, __std_search_1, etc.) at link time
# across three different prebuilt FastDDS sources tried, so being fully
# explicit here rules out a runtime-library-selection mismatch as the cause.
cmake_policy(SET CMP0091 NEW)

project(FacadeDdsBridge CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Always the Release CRT, even in a Debug config build -- NOT per-config
# ("MultiThreaded$<$<CONFIG:Debug>:Debug>DLL", which this line used to be). The vendored
# fastdds-3.6.dll/fastcdr-2.3.dll (previewer/tools/Module/FastDDSGen/FastDDS/bin) are
# Release-only builds (confirmed via dumpbin /dependents: they link MSVCP140.dll/
# VCRUNTIME140.dll, never a Debug-suffixed variant -- eProsima's own Debug build,
# fastddsd-3.6.dll, references CRT-debug-only symbols that don't resolve against a Release
# CRT, so it can't be substituted in either). If FacadeDdsBridge.dll itself links the Debug
# CRT (/MDd, MSVCP140D.dll/ucrtbased.dll) while calling into these Release-CRT DLLs, any
# C++ object (std::string, QoS structs, ...) that crosses the DLL boundary gets allocated in
# one CRT's heap and freed in the other's -- silent heap corruption, surfacing later and
# unpredictably as "Debug Assertion Failed ... __acrt_last_block == header" (confirmed this
# is exactly what was happening: FacadePreviewer.exe crashed like this within seconds of
# launch, before any user interaction, reproducing even on an unmodified checkout -- root
# cause traced to this line reverting to per-config CRT selection when the ORB-SLAM3 removal
# session dropped the force-Release-everywhere workaround this project had before). Same
# tradeoff already accepted and documented back then: our own code (DdsFrameSubscriber.cpp
# etc.) still gets real debug info (/Zi) and no optimization (/Od) in a Debug build, it just
# links the Release CRT underneath -- no source-level debugging into fastdds/fastcdr/ffmpeg
# themselves either way, since those are prebuilt.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")

# [YYIL] Without this, MSVC compiles Korean string literals in .cpp files that lack a UTF-8 BOM
# (e.g. RsyncTransfer.cpp's "SSH 인증 실패 (...) -- SSH 키 경로를 확인하세요.") using the build
# machine's active system codepage instead of UTF-8 -- a compile-time charset mismatch, not a
# P/Invoke marshaling issue, so no amount of fixing DdsBridgeInterop.cs's [MarshalAs] on the C#
# side can repair it (confirmed: the compiled string was never valid UTF-8 to begin with, so
# LPUTF8Str decoding it produces partial garbage -- some byte sequences happen to form valid
# UTF-8 by coincidence, the rest show as "?"). /utf-8 forces both source and execution charset to
# UTF-8 regardless of locale, matching how every string this project hands across the C++/C#
# boundary is otherwise assumed to be encoded.
if(MSVC)
  add_compile_options(/utf-8)
endif()

# FastDDS SDK from youngilyou/Gen_IDL_DDS's ExtraModule (per explicit user
# direction) -- installed locally via tools/Get-FastDdsGenModule.ps1, not
# committed to the repo (see tools/README.md). Override with
# -DFASTDDS_ROOT=... to point at a different SDK for local testing.
if(NOT DEFINED FASTDDS_ROOT)
  set(FASTDDS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../tools/Module/FastDDSGen/FastDDS")
endif()
# [YYIL] 2026-08-13: NOT using find_package(fastcdr)/find_package(fastdds) -- this vendored
# FASTDDS_ROOT ships raw bin/include/lib only, no CMake package config files, so
# find_package() (even with FASTDDS_ROOT on CMAKE_PREFIX_PATH) silently fell through to this
# dev machine's separate global install at C:/eProsima/fastdds3.6.1.0 instead, which is
# exactly the non-portable, machine-specific dependency this project's vendoring exists to
# avoid (previewer/CLAUDE.local.md: must run standalone on a field laptop). Reference the
# vendored files directly instead (same pattern used for FFmpeg below) -- Release variant
# specifically (fastdds-3.6.lib, not fastddsd-3.6.lib): fastddsd-3.6.lib (eProsima's own
# Debug build) references CRT-debug-only symbols (__imp__CrtDbgReport) that don't exist when
# linked against a Release CRT, and EPROSIMA_ALL_DYN_LINK below makes this whole project
# consume fastdds/fastcdr as DLLs either way, so there's no per-config Debug/Release variant
# selection to make -- always the Release DLL, in both configurations.

# FFmpeg shared/dev build (libavcodec/libavutil/libswscale headers + MSVC .lib + runtime
# .dll) from BtbN/FFmpeg-Builds -- installed locally via tools/Get-FfmpegDevModule.ps1, not
# committed (see tools/README.md). Same override convention as FASTDDS_ROOT above.
if(NOT DEFINED FFMPEG_DEV_ROOT)
  set(FFMPEG_DEV_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../tools/libffmpeg")
endif()

set(GENERATED_DIR "${CMAKE_CURRENT_SOURCE_DIR}/idl/generated/map2stitch_msgs/msg")
set(STORAGE_GENERATED_DIR "${CMAKE_CURRENT_SOURCE_DIR}/idl/generated/facade_storage_msgs/msg")
set(ANALYSIS_GENERATED_DIR "${CMAKE_CURRENT_SOURCE_DIR}/idl/generated/facade_analysis_msgs/msg")

add_subdirectory(thirdparty/libmpeg)

add_library(FacadeDdsBridge SHARED
    src/DdsFrameSubscriber.h
    src/DdsFrameSubscriber.cpp
    src/DdsQosHelpers.h
    src/DdsQosHelpers.cpp
    src/VideoDecoder.h
    src/VideoDecoder.cpp
    src/FacadeStorageStatus.h
    src/FacadeStorageStatus.cpp
    src/AnalysisCommandBridge.h
    src/AnalysisCommandBridge.cpp
    src/FacadeDdsBridge.h
    src/FacadeDdsBridge.cpp
    src/RsyncTransfer.h
    src/RsyncTransfer.cpp
    ${GENERATED_DIR}/ImageSensorFramePubSubTypes.cxx
    ${GENERATED_DIR}/ImageSensorFrameCdrAux.ipp
    ${GENERATED_DIR}/ImageSensorFrameTypeObjectSupport.cxx
    ${GENERATED_DIR}/VideoTsPacketPubSubTypes.cxx
    ${GENERATED_DIR}/VideoTsPacketCdrAux.ipp
    ${GENERATED_DIR}/VideoTsPacketTypeObjectSupport.cxx
    # facade_storage_msgs: operator-visibility layer (Feedback/Result/CancelRequest) for
    # CrackVisionArchiveManager's already-automatic archive trigger -- see
    # idl/facade_storage_msgs/msg/FacadeStorage.idl's header comment. Generated with the same
    # fastddsgen 4.0.6 jar and byte-for-byte identical to MngData backend_core's copy under
    # thirdparty/facade_storage_types/facade_storage_msgs/msg/.
    ${STORAGE_GENERATED_DIR}/FacadeStoragePubSubTypes.cxx
    ${STORAGE_GENERATED_DIR}/FacadeStorageCdrAux.ipp
    ${STORAGE_GENERATED_DIR}/FacadeStorageTypeObjectSupport.cxx
    # facade_analysis_msgs: crack-inspection remote analysis dispatch/status protocol -- see
    # idl/facade_analysis_msgs/msg/FacadeAnalysis.idl's header comment. Generated with fastddsgen
    # 4.0.6 and byte-for-byte identical to CheckCrackDdsBridge's copy (2026-08-27 session,
    # AnalysisLoadBalancer README).
    ${ANALYSIS_GENERATED_DIR}/FacadeAnalysisPubSubTypes.cxx
    ${ANALYSIS_GENERATED_DIR}/FacadeAnalysisCdrAux.ipp
    ${ANALYSIS_GENERATED_DIR}/FacadeAnalysisTypeObjectSupport.cxx
)
target_include_directories(FacadeDdsBridge PRIVATE src ${GENERATED_DIR} ${STORAGE_GENERATED_DIR} ${ANALYSIS_GENERATED_DIR}
    "${FFMPEG_DEV_ROOT}/include" "${FASTDDS_ROOT}/include"
)
# FastDDS/fastcdr headers carry MSVC autolink pragmas that request "libfastcdr-2.3.lib" (the
# alternate name vcpkg-style ports also ship alongside "fastcdr-2.3.lib" in this vendored
# FASTDDS_ROOT) by bare filename, which the linker can only resolve if its own directory is on
# the library search path -- the explicit full-path entries below don't add that search path
# by themselves.
target_link_directories(FacadeDdsBridge PRIVATE "${FASTDDS_ROOT}/lib")
target_link_libraries(FacadeDdsBridge PRIVATE mpeg_min
    "${FASTDDS_ROOT}/lib/fastdds-3.6.lib"
    "${FASTDDS_ROOT}/lib/fastcdr-2.3.lib"
    "${FASTDDS_ROOT}/lib/foonathan_memory-0.7.4.lib"
    "${FFMPEG_DEV_ROOT}/lib/avcodec.lib"
    "${FFMPEG_DEV_ROOT}/lib/avutil.lib"
    "${FFMPEG_DEV_ROOT}/lib/swscale.lib"
)
target_compile_definitions(FacadeDdsBridge PRIVATE FACADE_DDS_BRIDGE_EXPORTS
    COMPILEDWITHC11 _USE_MATH_DEFINES NOMINMAX _HAS_STD_BYTE=0
    # fastcdr/fastdds headers autolink-pragma the STATIC lib name (libfastcdr-2.3.lib/
    # libfastdds-3.6.lib) unless told otherwise -- without this, that pragma both fails to
    # resolve (no search path pointed at a static lib) and, once target_link_directories
    # above gave it one, duplicate-defines every symbol against the DLL import libs
    # (fastcdr-2.3.lib/fastdds-3.6.lib) explicitly linked above. This is the vendored SDK's
    # own documented opt-in for "I'm using the DLL, not the static lib" (see
    # fastcdr/fastcdr_dll.h / fastdds/fastdds_dll.hpp). Also means fastdds-3.6.dll's own
    # TCP-secure-transport SSL_* symbol needs are resolved internally by that DLL (eProsima's
    # own build), not something this project has to link OpenSSL for itself.
    EPROSIMA_ALL_DYN_LINK
)

# FFmpeg's runtime DLLs aren't found via PATH on a fresh checkout -- copy the ones this
# project actually links (avcodec/avutil/swscale, plus swresample which avcodec's own
# resampling code paths pull in even though this project never calls it directly) next to
# the built FacadeDdsBridge.dll/FacadeDdsBridgeSmokeTest.exe so `build.ps1`-only builds and
# SmokeTest runs work without any extra setup. FacadePreviewer.csproj has its own copy of
# this same DLL set for the WPF app's output dir (mirrors how it already copies the FastDDS
# runtime DLLs) -- see that file's <None> items.
file(GLOB FFMPEG_RUNTIME_DLLS "${FFMPEG_DEV_ROOT}/bin/avcodec-*.dll" "${FFMPEG_DEV_ROOT}/bin/avutil-*.dll"
    "${FFMPEG_DEV_ROOT}/bin/swscale-*.dll" "${FFMPEG_DEV_ROOT}/bin/swresample-*.dll")
foreach(dll ${FFMPEG_RUNTIME_DLLS})
    add_custom_command(TARGET FacadeDdsBridge POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${dll}" "$<TARGET_FILE_DIR:FacadeDdsBridge>"
    )
endforeach()

# Vendored FastDDS SDK's own runtime DLLs (Release variant, matching EPROSIMA_ALL_DYN_LINK
# above) -- FacadePreviewer.csproj has its own separate copy of these for the WPF app's output
# dir, but SmokeTest.exe (built here, run directly from build/$(Configuration)/) needs them
# too.
file(GLOB FASTDDS_RUNTIME_DLLS "${FASTDDS_ROOT}/bin/fastdds-*.dll" "${FASTDDS_ROOT}/bin/fastcdr-*.dll"
    "${FASTDDS_ROOT}/bin/foonathan_memory-*.dll")
# fastdds-*/fastcdr-* globs above already exclude their debug variants naturally (the debug
# filenames have an extra "d" before the dash: fastddsd-3.6.dll, fastcdrd-2.3.dll -- neither
# matches a "fastdds-*"/"fastcdr-*" glob). foonathan_memory's debug variant shares the same
# prefix though (foonathan_memory-0.7.4-dbg.dll), so it needs an explicit exclude.
list(FILTER FASTDDS_RUNTIME_DLLS EXCLUDE REGEX "-dbg\\.dll$")
foreach(dll ${FASTDDS_RUNTIME_DLLS})
    add_custom_command(TARGET FacadeDdsBridge POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${dll}" "$<TARGET_FILE_DIR:FacadeDdsBridge>"
    )
endforeach()

add_subdirectory(thirdparty/librtmp)

add_executable(FacadeDdsBridgeSmokeTest
    src/SmokeTest.cpp
    src/RtmpPublisher.h
    src/RtmpPublisher.cpp
    src/JpegFacadePublisher.h
    src/JpegFacadePublisher.cpp
    src/PublishUi.h
    src/PublishUi.cpp
)
target_include_directories(FacadeDdsBridgeSmokeTest PRIVATE src)
target_link_libraries(FacadeDdsBridgeSmokeTest PRIVATE FacadeDdsBridge rtmp_min)
if(WIN32)
    # ole32: CoCreateInstance/CoInitializeEx/CoUninitialize for PublishUi.cpp's IFileDialog
    # folder picker.
    target_link_libraries(FacadeDdsBridgeSmokeTest PRIVATE ws2_32 shell32 ole32)
endif()
