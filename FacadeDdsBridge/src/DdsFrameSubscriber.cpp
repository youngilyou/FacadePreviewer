#include "DdsFrameSubscriber.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>

#include <fastdds/dds/domain/DomainParticipant.hpp>
#include <fastdds/dds/domain/DomainParticipantFactory.hpp>
#include <fastdds/dds/domain/qos/DomainParticipantQos.hpp>
#include <fastdds/dds/log/Log.hpp>
#include <fastdds/dds/subscriber/DataReader.hpp>
#include <fastdds/dds/subscriber/DataReaderListener.hpp>
#include <fastdds/dds/subscriber/SampleInfo.hpp>
#include <fastdds/dds/subscriber/Subscriber.hpp>
#include <fastdds/dds/subscriber/qos/DataReaderQos.hpp>
#include <fastdds/dds/topic/Topic.hpp>
#include <fastdds/dds/topic/TypeSupport.hpp>

#include "ImageSensorFramePubSubTypes.hpp"
#include "VideoTsPacketPubSubTypes.hpp"
#include "VideoDecoder.h"
#include "DdsQosHelpers.h"

#include <cstdio>
#include <cstdlib>
#include <memory>
#include <sstream>
#include <string>

using namespace eprosima::fastdds::dds;

namespace {

// MakeUdpOnlyQos moved to DdsQosHelpers.h/.cpp (shared with FacadeStorageStatus.cpp's
// independent DDS participant) -- see that header for the full discovery-fix history.

class SensorFrameListener : public DataReaderListener
{
public:
    FacadeSensorFrameCallback callback = nullptr;
    void* user_data = nullptr;

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (info.instance_state != ALIVE_INSTANCE_STATE || !info.valid_data)
                continue;
            if (!callback)
                continue;

            FacadeImageSensorFrame out{};
            out.stream_id = sample_.stream_id().c_str();
            out.frame_id = sample_.frame_id();
            out.timestamp_sec = sample_.timestamp_sec();
            out.image_encoding = sample_.image_encoding().c_str();
            out.image_width = sample_.image_width();
            out.image_height = sample_.image_height();

            out.has_gps = sample_.has_gps();
            out.gps_latitude_deg = sample_.gps().latitude_deg();
            out.gps_longitude_deg = sample_.gps().longitude_deg();
            out.gps_altitude_m = sample_.gps().altitude_m();

            out.has_camera_pose = sample_.has_camera_pose();
            out.camera_position_m_x = sample_.camera_position_m().x();
            out.camera_position_m_y = sample_.camera_position_m().y();
            out.camera_position_m_z = sample_.camera_position_m().z();
            out.camera_orientation_x = sample_.camera_orientation().x();
            out.camera_orientation_y = sample_.camera_orientation().y();
            out.camera_orientation_z = sample_.camera_orientation().z();
            out.camera_orientation_w = sample_.camera_orientation().w();

            callback(&out, user_data);
        }
    }

    void on_subscription_matched(DataReader*, const SubscriptionMatchedStatus& info) override
    {
        if (info.current_count_change == 1)
            printf("DdsFrameSubscriber: ImageSensorFrame matched a publisher\n");
        else if (info.current_count_change == -1)
            printf("DdsFrameSubscriber: ImageSensorFrame publisher unmatched\n");
    }

private:
    map2stitch_msgs::msg::ImageSensorFrame sample_;
};

class VideoPacketListener : public DataReaderListener
{
public:
    FacadeVideoPacketCallback callback = nullptr;
    FacadeDecodedFrameCallback decoded_frame_callback = nullptr;
    void* user_data = nullptr;

    VideoPacketListener()
    {
        // Captures `this` by pointer, not by value -- VideoDecoder's callback contract
        // requires the callback to stay valid for the decoder's whole lifetime, which is
        // exactly this listener's own lifetime (decoder_ is a member, destroyed with us).
        decoder_.SetFrameCallback(
                [this](uint32_t width, uint32_t height, uint32_t stride, const uint8_t* bgr_data,
                        double timestamp_sec)
                {
                    if (decoded_frame_callback)
                    {
                        FacadeDecodedFrame frame{};
                        frame.stream_id = last_stream_id_.c_str();
                        frame.width = width;
                        frame.height = height;
                        frame.stride = stride;
                        frame.bgr_data = bgr_data;
                        frame.timestamp_sec = timestamp_sec;
                        decoded_frame_callback(&frame, user_data);
                    }
                });
    }

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (info.instance_state != ALIVE_INSTANCE_STATE || !info.valid_data)
                continue;

            const std::vector<uint8_t>& data = sample_.data();
            FacadeVideoTsPacket out{};
            out.stream_id = sample_.stream_id().c_str();
            out.chunk_id = sample_.chunk_id();
            out.sequence_id = sample_.sequence_id();
            out.timestamp_sec = sample_.timestamp_sec();
            out.data = data.empty() ? nullptr : data.data();
            out.data_length = static_cast<uint32_t>(data.size());

            if (callback)
                callback(&out, user_data);

            if (!data.empty())
            {
                // decoder_'s frame callback (above) reads last_stream_id_ synchronously,
                // inside decoder_.Feed()'s call stack -- set it right before, not after.
                last_stream_id_ = sample_.stream_id();
                decoder_.Feed(data.data(), data.size(), sample_.timestamp_sec());
            }
        }
    }

    void on_subscription_matched(DataReader*, const SubscriptionMatchedStatus& info) override
    {
        if (info.current_count_change == 1)
            printf("DdsFrameSubscriber: VideoTsPacket matched a publisher\n");
        else if (info.current_count_change == -1)
            printf("DdsFrameSubscriber: VideoTsPacket publisher unmatched\n");
    }

private:
    VideoDecoder decoder_;
    std::string last_stream_id_;
    map2stitch_msgs::msg::VideoTsPacket sample_;
};

} // namespace

struct DdsFrameSubscriber::Impl
{
    DomainParticipant* participant = nullptr;
    Subscriber* subscriber = nullptr;

    Topic* sensor_topic = nullptr;
    DataReader* sensor_reader = nullptr;
    TypeSupport sensor_type{new map2stitch_msgs::msg::ImageSensorFramePubSubType()};
    SensorFrameListener sensor_listener;

    Topic* video_topic = nullptr;
    DataReader* video_reader = nullptr;
    TypeSupport video_type{new map2stitch_msgs::msg::VideoTsPacketPubSubType()};
    VideoPacketListener video_listener;

    HANDLE start_thread = NULL;
};

DdsFrameSubscriber::DdsFrameSubscriber() : impl_(new Impl()) {}

DdsFrameSubscriber::~DdsFrameSubscriber()
{
    Stop();
    delete impl_;
}

void DdsFrameSubscriber::SetCallbacks(FacadeSensorFrameCallback sensor_cb, FacadeVideoPacketCallback video_cb,
        FacadeDecodedFrameCallback decoded_frame_cb, void* user_data)
{
    impl_->sensor_listener.callback = sensor_cb;
    impl_->sensor_listener.user_data = user_data;
    impl_->video_listener.callback = video_cb;
    impl_->video_listener.decoded_frame_callback = decoded_frame_cb;
    impl_->video_listener.user_data = user_data;
}

namespace {

DWORD WINAPI TeardownParticipantThread(LPVOID param)
{
    DomainParticipant* participant = (DomainParticipant*)param;
    participant->delete_contained_entities();
    DomainParticipantFactory::get_instance()->delete_participant(participant);
    return 0;
}

struct StartArgs
{
    DdsFrameSubscriber* self;
    int domain_id;
    std::string sensor_topic;
    std::string video_topic;
    std::string initial_peer_host;
    int initial_peer_port;
    std::string local_interface_ip;
};

DWORD WINAPI StartThread(LPVOID param)
{
    StartArgs* args = (StartArgs*)param;
    args->self->Start(args->domain_id, args->sensor_topic.c_str(), args->video_topic.c_str(),
            args->initial_peer_host.empty() ? nullptr : args->initial_peer_host.c_str(),
            args->initial_peer_port,
            args->local_interface_ip.empty() ? nullptr : args->local_interface_ip.c_str());
    delete args;
    return 0;
}

} // namespace

void DdsFrameSubscriber::StartAsync(int domain_id, const char* sensor_topic, const char* video_topic,
        const char* initial_peer_host, int initial_peer_port, const char* local_interface_ip)
{
    StartArgs* args = new StartArgs{
        this, domain_id, sensor_topic, video_topic,
        initial_peer_host ? initial_peer_host : "",
        initial_peer_port,
        local_interface_ip ? local_interface_ip : ""
    };
    impl_->start_thread = CreateThread(NULL, 0, StartThread, args, 0, NULL);
}

void DdsFrameSubscriber::Stop()
{
    // Same bounded-wait teardown as DdsVideoSubscriber: RTPS entity disposal
    // can hang tens of seconds waiting on unrelated participants on the
    // network, which read as "the app never closes" -- give it a budget and
    // move on regardless, the process exiting reclaims everything anyway.
    if (impl_->start_thread)
    {
        const DWORD kStartJoinBudgetMs = 5000;
        if (WaitForSingleObject(impl_->start_thread, kStartJoinBudgetMs) == WAIT_TIMEOUT)
            fprintf(stderr, "DdsFrameSubscriber: Start() still running after %ums, abandoning it\n", kStartJoinBudgetMs);
        CloseHandle(impl_->start_thread);
        impl_->start_thread = NULL;
    }

    if (impl_->participant)
    {
        HANDLE thread = CreateThread(NULL, 0, TeardownParticipantThread, impl_->participant, 0, NULL);
        if (thread)
        {
            const DWORD kTeardownBudgetMs = 2000;
            if (WaitForSingleObject(thread, kTeardownBudgetMs) == WAIT_TIMEOUT)
                fprintf(stderr, "DdsFrameSubscriber: graceful teardown exceeded %ums, abandoning it\n", kTeardownBudgetMs);
            CloseHandle(thread);
        }
        impl_->participant = nullptr;
        impl_->subscriber = nullptr;
        impl_->sensor_topic = nullptr;
        impl_->sensor_reader = nullptr;
        impl_->video_topic = nullptr;
        impl_->video_reader = nullptr;
    }
}

bool DdsFrameSubscriber::Start(int domain_id, const char* sensor_topic, const char* video_topic,
        const char* initial_peer_host, int initial_peer_port, const char* local_interface_ip)
{
    Impl* impl = impl_;
    auto factory = DomainParticipantFactory::get_instance();

    // [YYIL] 2026-08-12 diagnostic: opt-in (FACADE_DDS_VERBOSE_LOG=1) Fast-DDS internal
    // logging -- tracking down why a matched VideoTsPacket writer/reader pair (real writer
    // on a remote Linux host, confirmed via on_subscription_matched) never actually delivers
    // any samples. No admin/sudo needed (unlike packet capture), so trying this first.
    if (std::getenv("FACADE_DDS_VERBOSE_LOG"))
    {
        eprosima::fastdds::dds::Log::SetVerbosity(eprosima::fastdds::dds::Log::Kind::Info);
        printf("DdsFrameSubscriber: Fast-DDS verbose logging enabled\n");
    }

    impl->participant = factory->create_participant(domain_id,
            MakeUdpOnlyQos(domain_id, initial_peer_host, initial_peer_port, local_interface_ip, "DdsFrameSubscriber"));
    if (!impl->participant)
    {
        fprintf(stderr, "DdsFrameSubscriber: failed to create participant (domain %d)\n", domain_id);
        return false;
    }

    impl->sensor_type.register_type(impl->participant);
    impl->video_type.register_type(impl->participant);

    impl->subscriber = impl->participant->create_subscriber(SUBSCRIBER_QOS_DEFAULT);
    if (!impl->subscriber)
    {
        fprintf(stderr, "DdsFrameSubscriber: failed to create subscriber\n");
        return false;
    }

    // BEST_EFFORT (DataReader default, matches the publisher side's convention
    // documented across this DDS_Platform ecosystem for image/video data) with
    // a deeper-than-default KEEP_LAST history so a burst of samples arriving
    // faster than on_data_available() drains them doesn't silently discard the
    // ones in between (same reasoning/depth as DdsVideoSubscriber).
    DataReaderQos reader_qos = DATAREADER_QOS_DEFAULT;
    reader_qos.history().kind = KEEP_LAST_HISTORY_QOS;
    reader_qos.history().depth = 20;

    impl->sensor_topic = impl->participant->create_topic(sensor_topic, impl->sensor_type.get_type_name(), TOPIC_QOS_DEFAULT);
    if (!impl->sensor_topic)
    {
        fprintf(stderr, "DdsFrameSubscriber: failed to create topic '%s'\n", sensor_topic);
        return false;
    }
    impl->sensor_reader = impl->subscriber->create_datareader(impl->sensor_topic, reader_qos, &impl->sensor_listener);
    if (!impl->sensor_reader)
    {
        fprintf(stderr, "DdsFrameSubscriber: failed to create datareader for '%s'\n", sensor_topic);
        return false;
    }

    impl->video_topic = impl->participant->create_topic(video_topic, impl->video_type.get_type_name(), TOPIC_QOS_DEFAULT);
    if (!impl->video_topic)
    {
        fprintf(stderr, "DdsFrameSubscriber: failed to create topic '%s'\n", video_topic);
        return false;
    }
    impl->video_reader = impl->subscriber->create_datareader(impl->video_topic, reader_qos, &impl->video_listener);
    if (!impl->video_reader)
    {
        fprintf(stderr, "DdsFrameSubscriber: failed to create datareader for '%s'\n", video_topic);
        return false;
    }

    printf("DdsFrameSubscriber: listening, domain=%d sensor_topic=%s video_topic=%s\n", domain_id, sensor_topic, video_topic);
    return true;
}
