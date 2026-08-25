cmake_minimum_required(VERSION 3.16)
project(rtmp_min C CXX)

# Vendored subset of ireader/media-server (librtmp + libflv's amf0.{h,c}) + ireader/sdk
# (sdk_min, cherry-picked) -- see README.md for exactly what's here and why.

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED YES)

file(GLOB RTMP_SOURCES     "${CMAKE_CURRENT_SOURCE_DIR}/rtmp/source/*.c")
file(GLOB LIBFLV_MIN_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/libflv_min/*.c")
file(GLOB SDK_MIN_SOURCES  "${CMAKE_CURRENT_SOURCE_DIR}/sdk_min/*.c")

add_library(rtmp_min STATIC
    ${RTMP_SOURCES}
    ${LIBFLV_MIN_SOURCES}
    ${SDK_MIN_SOURCES}
)

target_include_directories(rtmp_min PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/rtmp/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/rtmp/source"
    "${CMAKE_CURRENT_SOURCE_DIR}/libflv_min"
    "${CMAKE_CURRENT_SOURCE_DIR}/sdk_min"
)

if(UNIX)
    target_compile_definitions(rtmp_min PRIVATE _GNU_SOURCE)
endif()
if(MSVC)
    target_compile_definitions(rtmp_min PRIVATE _CRT_SECURE_NO_WARNINGS)
endif()
