#pragma once

#include <atomic>
#include <string>

// Feasibility-check tool (C++ port of the old tools/publish_facade_rtmp.py, deleted 2026-08-12
// per user request -- "FacadeDdsBridgeSmokeTest 이곳에서 c++로 코딩 하세요"): scans a facade
// folder's JPEGs, shells out to the vendored ffmpeg to encode them into an FLV (H.264), then
// pushes that FLV via RunRtmpPublish (RtmpPublisher.h) -- same JPEG -> ffmpeg -> RTMP chain,
// same reason (previewer/CLAUDE.local.md "RTMP 직접 수신" step 4), just no Python involved.
//
// facade_dir is a wide string (not std::string) specifically so a Korean-named folder
// (facades\앞 etc.) survives intact -- SmokeTest.cpp's main() is narrow-argv (ANSI codepage),
// which is lossy for non-ASCII paths, so the caller re-reads this one argument via
// CommandLineToArgvW instead of trusting argv[] for it. rtmp_url is plain ASCII already (a
// wire-format RTMP URL), no such risk.
//
// `cancel`, if non-null, is polled before/during the ffmpeg encode and RTMP push
// (PublishUi.cpp's "정지" button sets it) -- default nullptr preserves every existing call
// site's behavior unchanged.
bool PublishFacadeJpegsViaRtmp(const std::wstring& facade_dir, const std::string& rtmp_url,
                                double fps, bool keep_flv, std::atomic<bool>* cancel = nullptr);
