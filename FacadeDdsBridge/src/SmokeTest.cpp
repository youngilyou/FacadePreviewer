// Verifies FastDDS actually links/initializes on this machine and the
// generated map2stitch_msgs types compile against it -- the highest-risk
// unknown for this new project (separate build from every other DDS_Platform
// component, even though it targets the same FastDDS install).
//
// Also doubles as the RTMP-publish half of the "RTMP direct ingest" feasibility check (see
// previewer/CLAUDE.local.md "RTMP 직접 수신"): `--rtmp-publish <rtmp_url> <flv_file>` pushes
// an FLV file to an RtmpVideoBridge instance instead of running the DDS-subscribe smoke test
// below -- see RtmpPublisher.h for why this lives in the same exe rather than a new one.
// `--publish-facade <facade_dir> <rtmp_url> [fps] [--keep-flv]` adds the JPEG-sequence ->
// ffmpeg -> --rtmp-publish orchestration that used to be tools/publish_facade_rtmp.py (deleted
// 2026-08-12, ported to C++ here per explicit request) -- see JpegFacadePublisher.h.
// `--publish-ui` opens a minimal GUI (folder picker + 발행/정지/리셋) around the same
// --publish-facade orchestration -- see PublishUi.h.
#include "FacadeDdsBridge.h"
#include "RtmpPublisher.h"
#include "JpegFacadePublisher.h"
#include "PublishUi.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <shellapi.h>

#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <chrono>
#include <vector>

static std::atomic<int> g_sensor_count{0};
static std::atomic<int> g_video_count{0};
static std::atomic<int> g_decoded_frame_count{0};

static void OnSensorFrame(const FacadeImageSensorFrame* frame, void* /*user_data*/)
{
    g_sensor_count++;
    printf("[smoke] sensor frame: stream=%s frame_id=%u pos=(%.2f,%.2f,%.2f)\n",
           frame->stream_id, frame->frame_id,
           frame->camera_position_m_x, frame->camera_position_m_y, frame->camera_position_m_z);
}

static void OnVideoPacket(const FacadeVideoTsPacket* packet, void* /*user_data*/)
{
    g_video_count++;
    printf("[smoke] video packet: stream=%s chunk_id=%u bytes=%u\n",
           packet->stream_id, packet->chunk_id, packet->data_length);
}

// [YYIL] 2026-08-13 debug-only: dumps the Nth decoded frame to a .ppm (trivial, no extra
// deps -- P6 header + RGB bytes) so it can be visually inspected independent of the C#/
// OpenCvSharp side, to isolate whether corruption seen in FacadePreviewer's UI originates in
// native decode (this frame data itself) or somewhere in the managed marshaling/stitch path.
// Not meant to stay long-term -- remove once the corruption reported in chat is root-caused.
static void DumpFrameAsPpm(const FacadeDecodedFrame* frame, const char* path)
{
    FILE* f = nullptr;
    fopen_s(&f, path, "wb");
    if (!f) return;
    fprintf(f, "P6\n%u %u\n255\n", frame->width, frame->height);
    std::vector<unsigned char> row(frame->width * 3);
    for (uint32_t y = 0; y < frame->height; ++y)
    {
        const unsigned char* src = frame->bgr_data + (size_t)y * frame->stride;
        for (uint32_t x = 0; x < frame->width; ++x)
        {
            row[x * 3 + 0] = src[x * 3 + 2]; // R (swap from BGR)
            row[x * 3 + 1] = src[x * 3 + 1]; // G
            row[x * 3 + 2] = src[x * 3 + 0]; // B
        }
        fwrite(row.data(), 1, row.size(), f);
    }
    fclose(f);
    printf("[smoke] dumped frame to %s\n", path);
}

static void OnDecodedFrame(const FacadeDecodedFrame* frame, void* /*user_data*/)
{
    int n = ++g_decoded_frame_count;
    if (n == 1 || n % 25 == 0)
    {
        printf("[smoke] decoded frame #%d: stream=%s %ux%u stride=%u timestamp_sec=%.3f\n",
               n, frame->stream_id, frame->width, frame->height, frame->stride, frame->timestamp_sec);
    }
    const char* dump_path = std::getenv("FACADE_DUMP_FIRST_FRAME_PPM");
    if (n == 1 && dump_path && *dump_path)
    {
        DumpFrameAsPpm(frame, dump_path);
    }
}

int main(int argc, char** argv)
{
    if (argc > 1 && 0 == std::strcmp(argv[1], "--publish-ui"))
    {
        return RunPublishUi() ? 0 : 1;
    }

    if (argc > 1 && 0 == std::strcmp(argv[1], "--rtmp-publish"))
    {
        if (argc < 4)
        {
            fprintf(stderr, "usage: %s --rtmp-publish <rtmp://host[:port]/app/stream> <flv_file>\n", argv[0]);
            return 2;
        }
        bool ok = RunRtmpPublish(argv[2], argv[3]);
        return ok ? 0 : 1;
    }

    if (argc > 1 && 0 == std::strcmp(argv[1], "--publish-facade"))
    {
        if (argc < 4)
        {
            fprintf(stderr, "usage: %s --publish-facade <facade_dir> <rtmp_url> [fps] [--keep-flv]\n", argv[0]);
            return 2;
        }

        double fps = 2.0;
        bool keep_flv = false;
        for (int i = 4; i < argc; ++i)
        {
            if (0 == std::strcmp(argv[i], "--keep-flv"))
            {
                keep_flv = true;
            }
            else
            {
                fps = std::atof(argv[i]);
            }
        }

        // argv[] is ANSI-codepage narrow, lossy for a non-ASCII (e.g. Korean) facade_dir --
        // re-read the real command line as wide chars for just that one argument (index 2,
        // matching "--publish-facade"=1, facade_dir=2 in both argv and this wide parse, since
        // CommandLineToArgvW splits on the same whitespace/quoting rules the CRT's own argv
        // setup already used to produce argv[]).
        std::wstring facade_dir_w;
        int wargc = 0;
        LPWSTR* wargv = CommandLineToArgvW(GetCommandLineW(), &wargc);
        if (wargv)
        {
            if (wargc > 2)
            {
                facade_dir_w = wargv[2];
            }
            LocalFree(wargv);
        }
        if (facade_dir_w.empty())
        {
            fprintf(stderr, "failed to read facade_dir argument\n");
            return 1;
        }

        bool ok = PublishFacadeJpegsViaRtmp(facade_dir_w, argv[3], fps, keep_flv);
        return ok ? 0 : 1;
    }

    int domain_id = argc > 1 ? atoi(argv[1]) : 0;
    const char* sensor_topic = "rt/map2stitch/facade_previewer/image_sensor_frame";
    const char* video_topic = "rt/map2stitch/facade_previewer/video_ts";
    int wait_seconds = argc > 2 ? atoi(argv[2]) : 5;

    printf("[smoke] creating subscriber, domain=%d sensor_topic=%s video_topic=%s\n",
           domain_id, sensor_topic, video_topic);

    void* handle = FacadeDds_Create();
    if (!handle)
    {
        printf("[smoke] FAILED: FacadeDds_Create returned null\n");
        return 1;
    }

    FacadeDds_SetCallbacks(handle, OnSensorFrame, OnVideoPacket, OnDecodedFrame, nullptr);

    FacadeDds_StartAsync(handle, domain_id, sensor_topic, video_topic, nullptr, 0, nullptr);

    printf("[smoke] waiting %ds for participant/topic/reader creation (and any live traffic)...\n", wait_seconds);
    std::this_thread::sleep_for(std::chrono::seconds(wait_seconds));

    printf("[smoke] sensor_count=%d video_count=%d decoded_frame_count=%d\n",
           g_sensor_count.load(), g_video_count.load(), g_decoded_frame_count.load());
    printf("[smoke] stopping...\n");
    FacadeDds_Stop(handle);
    FacadeDds_Destroy(handle);
    printf("[smoke] done — if no fprintf 'failed to create ...' lines appeared above, "
           "DDS participant/topic/reader creation succeeded (FastDDS link + transport OK). "
           "Sample counts above are only >0 if a real publisher happened to be on the wire.\n");
    return 0;
}
