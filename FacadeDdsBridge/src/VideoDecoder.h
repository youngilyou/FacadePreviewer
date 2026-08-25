// TS-demux + H.264 decode for one VideoTsPacket stream. Turns the raw MPEG-TS bytes
// DdsFrameSubscriber's VideoPacketListener hands back (see that file's own header comment --
// "not yet demuxed/decoded") into actual BGR24 frames, ready for OpenCvSharp/ORB-AKAZE
// stitching on the C# side.
//
// libmpeg (thirdparty/libmpeg, ts_demuxer_create/input) does the TS -> Annex-B H.264 demux;
// libavcodec (thirdparty via tools/Get-FfmpegDevModule.ps1) does the actual H.264 decode;
// libswscale converts the decoded YUV frame to BGR24 (the layout OpenCV/OpenCvSharp expects).
//
// Decode runs synchronously inside Feed() -- called directly from VideoPacketListener::
// on_data_available on Fast-DDS's own listener thread, same as this project's other DDS
// callbacks so far (no extra worker thread). Fine for one BEST_EFFORT preview-resolution
// H.264 stream at a time (this project's own design targets 2fps-equivalent lightweight
// preview, see previewer/CLAUDE.local.md) -- if this ever needs to be decoupled from the DDS
// listener thread, follow the same enqueue/dequeue pattern already used in DDS-Router's
// RtmpVideoBridge (see that project's main.cpp), not a new ad-hoc design.
#pragma once

#include <cstdint>
#include <functional>

class VideoDecoder
{
public:
    // width/height in pixels, stride in bytes (may exceed width*3 if the decoder pads rows).
    // bgr_data points at BGR24 pixel data, valid only for the duration of the callback -- copy
    // out if the caller needs it to outlive the call (same contract as this project's other
    // native->managed callbacks). timestamp_sec is the VideoTsPacket DDS sample's own
    // timestamp_sec for the most recent bytes fed in before this frame completed decoding --
    // not a true per-frame H.264 PTS (the TS demux layer here doesn't surface one), but close
    // enough for nearest-timestamp correlation against ImageSensorFrame at this project's
    // "2fps is enough" granularity (see previewer/CLAUDE.local.md).
    using FrameCallback = std::function<void (uint32_t width, uint32_t height, uint32_t stride,
            const uint8_t* bgr_data, double timestamp_sec)>;

    VideoDecoder();
    ~VideoDecoder();

    VideoDecoder(const VideoDecoder&) = delete;
    VideoDecoder& operator=(const VideoDecoder&) = delete;

    void SetFrameCallback(
            FrameCallback cb);

    // Feed one VideoTsPacket sample's raw bytes (a bundle of whole 188-byte MPEG-TS packets,
    // per this project's wire-format convention -- see VideoTsPacket.idl) plus that sample's
    // own timestamp_sec field. May synchronously invoke the frame callback zero or more times
    // before returning.
    void Feed(
            const uint8_t* ts_data,
            size_t ts_bytes,
            double timestamp_sec);

    // Public (not the usual PIMPL-private) so VideoDecoder.cpp's file-scope
    // ts_demuxer_onpacket trampoline function -- which libmpeg's C callback signature
    // requires to be a plain free function, not a member -- can name this type too.
    struct Impl;

private:
    Impl* impl_;
};
