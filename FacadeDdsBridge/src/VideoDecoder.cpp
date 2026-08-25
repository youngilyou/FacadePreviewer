#include "VideoDecoder.h"

#include <cstdio>
#include <cstring>
#include <vector>

#include "mpeg-proto.h" // PSI_STREAM_H264
#include "mpeg-ts.h"    // ts_demuxer_t

extern "C" {
#include <libavcodec/avcodec.h>
#include <libswscale/swscale.h>
}

struct VideoDecoder::Impl
{
    ts_demuxer_t* demuxer = nullptr;

    const AVCodec* codec = nullptr;
    AVCodecContext* codec_ctx = nullptr;
    AVPacket* pkt = nullptr;
    AVFrame* frame = nullptr;

    SwsContext* sws_ctx = nullptr;
    int sws_width = 0;
    int sws_height = 0;
    AVPixelFormat sws_src_fmt = AV_PIX_FMT_NONE;
    std::vector<uint8_t> bgr_buf;

    FrameCallback callback;
    double latest_timestamp_sec = 0.0;

    void DecodeAnnexB(
            const uint8_t* data,
            size_t bytes)
    {
        if (0 != av_new_packet(pkt, (int)bytes))
        {
            fprintf(stderr, "VideoDecoder: av_new_packet failed (%zu bytes)\n", bytes);
            return;
        }
        std::memcpy(pkt->data, data, bytes);

        int send_ret = avcodec_send_packet(codec_ctx, pkt);
        av_packet_unref(pkt);
        if (send_ret < 0)
        {
            // Expected/harmless early on: the very first access units before the decoder has
            // seen an SPS/PPS-bearing IDR won't decode -- not logged as an error to avoid
            // spamming the console every stream start.
            return;
        }

        for (;;)
        {
            int recv_ret = avcodec_receive_frame(codec_ctx, frame);
            if (recv_ret == AVERROR(EAGAIN) || recv_ret == AVERROR_EOF)
            {
                break;
            }
            if (recv_ret < 0)
            {
                fprintf(stderr, "VideoDecoder: avcodec_receive_frame failed, ret=%d\n", recv_ret);
                break;
            }
            EmitFrame();
            av_frame_unref(frame);
        }
    }

    void EmitFrame()
    {
        if (!callback)
        {
            return;
        }

        const int width = frame->width;
        const int height = frame->height;
        const AVPixelFormat src_fmt = (AVPixelFormat)frame->format;

        if (!sws_ctx || sws_width != width || sws_height != height || sws_src_fmt != src_fmt)
        {
            if (sws_ctx)
            {
                sws_freeContext(sws_ctx);
            }
            sws_ctx = sws_getContext(width, height, src_fmt, width, height, AV_PIX_FMT_BGR24,
                    SWS_BILINEAR, nullptr, nullptr, nullptr);
            sws_width = width;
            sws_height = height;
            sws_src_fmt = src_fmt;
            bgr_buf.assign((size_t)width * height * 3, 0);
        }
        if (!sws_ctx)
        {
            fprintf(stderr, "VideoDecoder: sws_getContext failed for %dx%d fmt=%d\n", width, height, (int)src_fmt);
            return;
        }

        uint8_t* dst_data[4] = { bgr_buf.data(), nullptr, nullptr, nullptr };
        int dst_linesize[4] = { width * 3, 0, 0, 0 };
        sws_scale(sws_ctx, frame->data, frame->linesize, 0, height, dst_data, dst_linesize);

        callback((uint32_t)width, (uint32_t)height, (uint32_t)dst_linesize[0], bgr_buf.data(),
                latest_timestamp_sec);
    }
};

namespace {

int OnTsPacket(
        void* param,
        int /*program*/,
        int /*stream*/,
        int codecid,
        int /*flags*/,
        int64_t /*pts*/,
        int64_t /*dts*/,
        const void* data,
        size_t bytes)
{
    if (codecid != PSI_STREAM_H264)
    {
        return 0; // this bridge/wire-format is H.264-only (see RtmpVideoBridge's own config
                  // validation), but guard anyway rather than assume.
    }
    VideoDecoder::Impl* impl = (VideoDecoder::Impl*)param;
    impl->DecodeAnnexB((const uint8_t*)data, bytes);
    return 0;
}

} // namespace

VideoDecoder::VideoDecoder() : impl_(new Impl())
{
    impl_->demuxer = ts_demuxer_create(OnTsPacket, impl_);

    impl_->codec = avcodec_find_decoder(AV_CODEC_ID_H264);
    if (impl_->codec)
    {
        impl_->codec_ctx = avcodec_alloc_context3(impl_->codec);
        if (impl_->codec_ctx && avcodec_open2(impl_->codec_ctx, impl_->codec, nullptr) < 0)
        {
            fprintf(stderr, "VideoDecoder: avcodec_open2 failed\n");
            avcodec_free_context(&impl_->codec_ctx);
        }
    }
    else
    {
        fprintf(stderr, "VideoDecoder: no H.264 decoder found (avcodec_find_decoder)\n");
    }

    impl_->pkt = av_packet_alloc();
    impl_->frame = av_frame_alloc();
}

VideoDecoder::~VideoDecoder()
{
    if (impl_->demuxer)
    {
        ts_demuxer_destroy(impl_->demuxer);
    }
    if (impl_->sws_ctx)
    {
        sws_freeContext(impl_->sws_ctx);
    }
    if (impl_->codec_ctx)
    {
        avcodec_free_context(&impl_->codec_ctx);
    }
    av_frame_free(&impl_->frame);
    av_packet_free(&impl_->pkt);
    delete impl_;
}

void VideoDecoder::SetFrameCallback(
        FrameCallback cb)
{
    impl_->callback = std::move(cb);
}

void VideoDecoder::Feed(
        const uint8_t* ts_data,
        size_t ts_bytes,
        double timestamp_sec)
{
    if (!impl_->demuxer || !impl_->codec_ctx)
    {
        return;
    }
    // Recorded before demuxing so any frame that completes decode from this call's packets
    // (via OnTsPacket -> DecodeAnnexB -> EmitFrame, all synchronous within ts_demuxer_input)
    // reports this sample's timestamp -- see FrameCallback's own doc comment for why this is
    // "close enough", not a true per-frame PTS.
    impl_->latest_timestamp_sec = timestamp_sec;
    // ts_demuxer_input() takes exactly one 188-byte TS packet per call (asserts on it,
    // mpeg-ts-dec.c) -- a VideoTsPacket sample is a bundle of kTsPacketsPerBundle of them
    // (RtmpVideoBridge's own wire-format convention, see that project's ts_write()), so split
    // the bundle into individual packets here rather than handing the whole buffer over at once.
    constexpr size_t kTsPacketSize = 188;
    for (size_t off = 0; off + kTsPacketSize <= ts_bytes; off += kTsPacketSize)
    {
        ts_demuxer_input(impl_->demuxer, ts_data + off, kTsPacketSize);
    }
}
