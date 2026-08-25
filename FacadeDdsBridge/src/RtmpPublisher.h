#pragma once

#include <atomic>
#include <string>

// Feasibility-check tool: reads an FLV file (produced by tools/publish_facade_rtmp.py via
// ffmpeg) tag-by-tag and pushes it to an RTMP server (RtmpVideoBridge) over the vendored
// librtmp client, verifying the RTMP-publish half of the DJI-Pilot2-direct-RTMP data path
// end to end. Not a general-purpose RTMP publisher -- single connect, single pass through the
// file, then disconnect (see previewer/CLAUDE.local.md "RTMP 직접 수신" for the full picture).
//
// url must be "rtmp://host[:port]/app/stream" (e.g. "rtmp://192.168.1.10:1935/live/front").
// Returns true if the whole file was pushed and the connection closed cleanly.
//
// `cancel`, if non-null, is polled once per FLV tag (PublishUi.cpp's "정지" button sets it) --
// same early-exit path as an actual send failure (connection closed cleanly, false returned).
// Default nullptr preserves every existing call site's behavior unchanged.
bool RunRtmpPublish(const std::string& url, const std::string& flv_path, std::atomic<bool>* cancel = nullptr);
