// Native Fast-DDS subscriber for this previewer's two map2stitch_msgs
// topics (ImageSensorFrame metadata/pose, VideoTsPacket raw MPEG-TS bytes).
// Pattern follows D:\GCS\MpegTS\NDRONE_MULTI_VIEWER\DdsVideoSubscriber
// (confirmed working on this machine) but delivers samples via C callbacks
// instead of writing into that project's Mpegtsfifo — this project has no
// dependency on NDRONE_MULTI_VIEWER's code, only on the shared IDL contract.
//
// [YYIL] 2026-08-13: VideoTsPacket samples are now also fed through a VideoDecoder (TS demux
// via libmpeg + H.264 decode via libavcodec, see VideoDecoder.h) as they arrive -- decoded
// BGR24 frames are delivered via FacadeDecodedFrameCallback, in addition to (not instead of)
// the raw-bytes FacadeVideoPacketCallback which still fires for every sample (kept for the
// existing sample-count UI). Decode runs synchronously on the DDS listener thread, see
// VideoDecoder.h's own header comment for why that's fine for now.
#pragma once

#include <cstdint>

extern "C" {

// One ImageSensorFrame sample, flattened to a C-compatible struct (mirrors
// map2stitch_msgs::msg::ImageSensorFrame — see idl/map2stitch_msgs/msg/ImageSensorFrame.idl).
struct FacadeImageSensorFrame
{
    const char* stream_id;
    uint32_t frame_id;
    double timestamp_sec;

    const char* image_encoding;
    uint32_t image_width;
    uint32_t image_height;

    bool has_gps;
    double gps_latitude_deg;
    double gps_longitude_deg;
    double gps_altitude_m;

    bool has_camera_pose;
    double camera_position_m_x;
    double camera_position_m_y;
    double camera_position_m_z;
    double camera_orientation_x;
    double camera_orientation_y;
    double camera_orientation_z;
    double camera_orientation_w;
};

// One VideoTsPacket sample — raw bytes handed back as-is (no demux/decode yet).
struct FacadeVideoTsPacket
{
    const char* stream_id;
    uint32_t chunk_id;
    uint64_t sequence_id;
    double timestamp_sec;
    const uint8_t* data;
    uint32_t data_length;
};

// One decoded video frame, BGR24 (OpenCV/OpenCvSharp's default layout) -- see VideoDecoder.h.
// bgr_data/stride valid only for the duration of the callback, same contract as
// FacadeVideoTsPacket::data.
struct FacadeDecodedFrame
{
    const char* stream_id;
    uint32_t width;
    uint32_t height;
    uint32_t stride; // bytes per row, may exceed width*3
    const uint8_t* bgr_data;
    double timestamp_sec; // see VideoDecoder.h's FrameCallback doc comment -- not a true PTS
};

using FacadeSensorFrameCallback = void(*)(const FacadeImageSensorFrame* frame, void* user_data);
using FacadeVideoPacketCallback = void(*)(const FacadeVideoTsPacket* packet, void* user_data);
using FacadeDecodedFrameCallback = void(*)(const FacadeDecodedFrame* frame, void* user_data);

} // extern "C"

class DdsFrameSubscriber
{
public:
    DdsFrameSubscriber();
    ~DdsFrameSubscriber();

    DdsFrameSubscriber(const DdsFrameSubscriber&) = delete;
    DdsFrameSubscriber& operator=(const DdsFrameSubscriber&) = delete;

    void SetCallbacks(FacadeSensorFrameCallback sensor_cb, FacadeVideoPacketCallback video_cb,
            FacadeDecodedFrameCallback decoded_frame_cb, void* user_data);

    // domain_id: matches DDS-Router's ControlCenterDomainParticipant convention
    // (see Z:\DDS_Platform\CLAUDE.local.md) -- 0 for this side, drones publish on 10.
    //
    // initial_peer_host/initial_peer_port/local_interface_ip: UI-sourced discovery
    // overrides (FacadePreviewer's Host/Port/로컬 인터페이스 fields) -- same knobs as
    // the FACADE_DDS_INITIAL_PEER/FACADE_DDS_INTERFACE_WHITELIST env vars in
    // MakeUdpOnlyQos(), just explicit parameters instead of process environment so the
    // WPF app can set them per-run from its own UI. Pass nullptr/empty/<=0 for any of
    // these to fall back to the corresponding env var (keeps SmokeTest's CLI-only
    // usage working unchanged).
    bool Start(int domain_id, const char* sensor_topic, const char* video_topic,
            const char* initial_peer_host = nullptr, int initial_peer_port = 0,
            const char* local_interface_ip = nullptr);
    void StartAsync(int domain_id, const char* sensor_topic, const char* video_topic,
            const char* initial_peer_host = nullptr, int initial_peer_port = 0,
            const char* local_interface_ip = nullptr);
    void Stop();

private:
    struct Impl;
    Impl* impl_;
};
