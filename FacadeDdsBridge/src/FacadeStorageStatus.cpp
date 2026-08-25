#include "FacadeStorageStatus.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>

#include <fastdds/dds/domain/DomainParticipant.hpp>
#include <fastdds/dds/domain/DomainParticipantFactory.hpp>
#include <fastdds/dds/publisher/DataWriter.hpp>
#include <fastdds/dds/publisher/Publisher.hpp>
#include <fastdds/dds/publisher/qos/DataWriterQos.hpp>
#include <fastdds/dds/subscriber/DataReader.hpp>
#include <fastdds/dds/subscriber/DataReaderListener.hpp>
#include <fastdds/dds/subscriber/SampleInfo.hpp>
#include <fastdds/dds/subscriber/Subscriber.hpp>
#include <fastdds/dds/subscriber/qos/DataReaderQos.hpp>
#include <fastdds/dds/topic/Topic.hpp>
#include <fastdds/dds/topic/TypeSupport.hpp>

#include "FacadeStoragePubSubTypes.hpp"
#include "DdsQosHelpers.h"

#include <chrono>
#include <cstdio>
#include <sstream>
#include <string>
#include <vector>

using namespace eprosima::fastdds::dds;

namespace {

class FeedbackListener : public DataReaderListener
{
public:
    FacadeStorageFeedbackCallback callback = nullptr;
    void* user_data = nullptr;

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (!info.valid_data || !callback)
                continue;
            FacadeStorageFeedbackData out{};
            out.company = sample_.company().c_str();
            out.building = sample_.building().c_str();
            out.images_zipped = sample_.images_zipped();
            out.images_total = sample_.images_total();
            out.status = sample_.status().c_str();
            out.updated_at_epoch_ms = sample_.updated_at_epoch_ms();
            callback(&out, user_data);
        }
    }

private:
    facade_storage_msgs::msg::FacadeStorageFeedback sample_;
};

class ResultListener : public DataReaderListener
{
public:
    FacadeStorageResultCallback callback = nullptr;
    void* user_data = nullptr;

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (!info.valid_data || !callback)
                continue;
            FacadeStorageResultData out{};
            out.company = sample_.company().c_str();
            out.building = sample_.building().c_str();
            out.success = sample_.success();
            out.cancelled = sample_.cancelled();
            out.archive_id = sample_.archive_id();
            out.zip_path = sample_.zip_path().c_str();
            out.size_bytes = sample_.size_bytes();
            out.image_count = sample_.image_count();
            out.error_message = sample_.error_message().c_str();
            out.completed_at_epoch_ms = sample_.completed_at_epoch_ms();
            callback(&out, user_data);
        }
    }

private:
    facade_storage_msgs::msg::FacadeStorageResult sample_;
};

DWORD WINAPI TeardownParticipantThread(LPVOID param)
{
    DomainParticipant* participant = (DomainParticipant*)param;
    participant->delete_contained_entities();
    DomainParticipantFactory::get_instance()->delete_participant(participant);
    return 0;
}

} // namespace

struct FacadeStorageStatus::Impl
{
    DomainParticipant* participant = nullptr;
    Publisher* publisher = nullptr;
    Subscriber* subscriber = nullptr;

    Topic* feedback_topic = nullptr;
    Topic* result_topic = nullptr;
    Topic* cancel_topic = nullptr;
    Topic* requirements_topic = nullptr;
    DataReader* feedback_reader = nullptr;
    DataReader* result_reader = nullptr;
    DataWriter* cancel_writer = nullptr;
    DataWriter* requirements_writer = nullptr;

    TypeSupport feedback_type{new facade_storage_msgs::msg::FacadeStorageFeedbackPubSubType()};
    TypeSupport result_type{new facade_storage_msgs::msg::FacadeStorageResultPubSubType()};
    TypeSupport cancel_type{new facade_storage_msgs::msg::FacadeStorageCancelRequestPubSubType()};
    TypeSupport requirements_type{new facade_storage_msgs::msg::FacadeStorageRequirementsPubSubType()};

    FeedbackListener feedback_listener;
    ResultListener result_listener;
};

FacadeStorageStatus::FacadeStorageStatus() : impl_(new Impl()) {}

FacadeStorageStatus::~FacadeStorageStatus()
{
    Stop();
    delete impl_;
}

void FacadeStorageStatus::SetCallbacks(FacadeStorageFeedbackCallback feedback_cb, FacadeStorageResultCallback result_cb, void* user_data)
{
    impl_->feedback_listener.callback = feedback_cb;
    impl_->feedback_listener.user_data = user_data;
    impl_->result_listener.callback = result_cb;
    impl_->result_listener.user_data = user_data;
}

bool FacadeStorageStatus::Start(int domain_id, const char* feedback_topic, const char* result_topic, const char* cancel_topic,
        const char* requirements_topic, const char* initial_peer_host, int initial_peer_port, const char* local_interface_ip)
{
    Impl* impl = impl_;
    auto factory = DomainParticipantFactory::get_instance();

    impl->participant = factory->create_participant(domain_id,
            MakeUdpOnlyQos(domain_id, initial_peer_host, initial_peer_port, local_interface_ip, "FacadeStorageStatus"));
    if (!impl->participant)
    {
        fprintf(stderr, "FacadeStorageStatus: failed to create participant (domain %d)\n", domain_id);
        return false;
    }

    impl->feedback_type.register_type(impl->participant);
    impl->result_type.register_type(impl->participant);
    impl->cancel_type.register_type(impl->participant);
    impl->requirements_type.register_type(impl->participant);

    impl->publisher = impl->participant->create_publisher(PUBLISHER_QOS_DEFAULT);
    impl->subscriber = impl->participant->create_subscriber(SUBSCRIBER_QOS_DEFAULT);
    if (!impl->publisher || !impl->subscriber)
    {
        fprintf(stderr, "FacadeStorageStatus: failed to create publisher/subscriber\n");
        return false;
    }

    impl->feedback_topic = impl->participant->create_topic(feedback_topic, impl->feedback_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->result_topic = impl->participant->create_topic(result_topic, impl->result_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->cancel_topic = impl->participant->create_topic(cancel_topic, impl->cancel_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->requirements_topic = impl->participant->create_topic(requirements_topic, impl->requirements_type.get_type_name(), TOPIC_QOS_DEFAULT);
    if (!impl->feedback_topic || !impl->result_topic || !impl->cancel_topic || !impl->requirements_topic)
    {
        fprintf(stderr, "FacadeStorageStatus: failed to create topics\n");
        return false;
    }

    // BEST_EFFORT for feedback -- matches MngData's writer QoS (see crackvision_archive_manager.cpp),
    // losing one progress update is harmless.
    DataReaderQos feedback_rqos = DATAREADER_QOS_DEFAULT;
    feedback_rqos.reliability().kind = BEST_EFFORT_RELIABILITY_QOS;
    feedback_rqos.history().kind = KEEP_LAST_HISTORY_QOS;
    feedback_rqos.history().depth = 10;
    impl->feedback_reader = impl->subscriber->create_datareader(impl->feedback_topic, feedback_rqos, &impl->feedback_listener);

    // RELIABLE for result -- the operator's completion popup depends on this arriving.
    // TRANSIENT_LOCAL -- must match crackvision_archive_manager.cpp's result_writer durability
    // (see that file's comment): this reader is created fresh right when the operator clicks
    // "전송", and the matching writer on backend_core may finish an already-in-flight archive
    // job's Result before this reader has finished discovering/matching it.
    DataReaderQos result_rqos = DATAREADER_QOS_DEFAULT;
    result_rqos.reliability().kind = RELIABLE_RELIABILITY_QOS;
    result_rqos.durability().kind = TRANSIENT_LOCAL_DURABILITY_QOS;
    result_rqos.history().kind = KEEP_LAST_HISTORY_QOS;
    result_rqos.history().depth = 100;
    impl->result_reader = impl->subscriber->create_datareader(impl->result_topic, result_rqos, &impl->result_listener);

    if (!impl->feedback_reader || !impl->result_reader)
    {
        fprintf(stderr, "FacadeStorageStatus: failed to create feedback/result reader\n");
        return false;
    }

    // RELIABLE -- a lost cancel request would leave the operator stuck with no way to interrupt.
    // TRANSIENT_LOCAL to match crackvision_archive_manager.cpp's cancel_reader durability.
    DataWriterQos cancel_wqos = DATAWRITER_QOS_DEFAULT;
    cancel_wqos.reliability().kind = RELIABLE_RELIABILITY_QOS;
    cancel_wqos.durability().kind = TRANSIENT_LOCAL_DURABILITY_QOS;
    cancel_wqos.history().kind = KEEP_LAST_HISTORY_QOS;
    cancel_wqos.history().depth = 10;
    impl->cancel_writer = impl->publisher->create_datawriter(impl->cancel_topic, cancel_wqos);
    if (!impl->cancel_writer)
    {
        fprintf(stderr, "FacadeStorageStatus: failed to create cancel writer\n");
        return false;
    }

    // RELIABLE -- a lost requirements declaration would mean the archive silently never
    // triggers, with no error surfaced anywhere. TRANSIENT_LOCAL -- this is THE case that
    // actually broke in practice: this writer is created and written to essentially back-to-back
    // (right at "전송" click time), so backend_core's reader (already running, but not yet
    // discovered by this brand-new participant) very likely hasn't matched yet at the moment of
    // the write. Must match crackvision_archive_manager.cpp's requirements_reader durability.
    DataWriterQos requirements_wqos = DATAWRITER_QOS_DEFAULT;
    requirements_wqos.reliability().kind = RELIABLE_RELIABILITY_QOS;
    requirements_wqos.durability().kind = TRANSIENT_LOCAL_DURABILITY_QOS;
    requirements_wqos.history().kind = KEEP_LAST_HISTORY_QOS;
    requirements_wqos.history().depth = 10;
    impl->requirements_writer = impl->publisher->create_datawriter(impl->requirements_topic, requirements_wqos);
    if (!impl->requirements_writer)
    {
        fprintf(stderr, "FacadeStorageStatus: failed to create requirements writer\n");
        return false;
    }

    printf("FacadeStorageStatus: listening on domain %d (feedback='%s', result='%s', cancel='%s', requirements='%s')\n",
            domain_id, feedback_topic, result_topic, cancel_topic, requirements_topic);
    return true;
}

void FacadeStorageStatus::Stop()
{
    if (!impl_->participant)
        return;

    // Same bounded-wait teardown as DdsFrameSubscriber::Stop() -- delete_contained_entities()
    // has been observed to hang on this project waiting on unrelated participants; a timeout
    // here means a slow teardown never blocks the WPF UI thread indefinitely.
    HANDLE thread = CreateThread(NULL, 0, TeardownParticipantThread, impl_->participant, 0, NULL);
    if (thread)
    {
        const DWORD kTeardownBudgetMs = 2000;
        if (WaitForSingleObject(thread, kTeardownBudgetMs) == WAIT_TIMEOUT)
            fprintf(stderr, "FacadeStorageStatus: graceful teardown exceeded %ums, abandoning it\n", kTeardownBudgetMs);
        CloseHandle(thread);
    }
    impl_->participant = nullptr;
    impl_->publisher = nullptr;
    impl_->subscriber = nullptr;
    impl_->feedback_topic = nullptr;
    impl_->result_topic = nullptr;
    impl_->cancel_topic = nullptr;
    impl_->requirements_topic = nullptr;
    impl_->feedback_reader = nullptr;
    impl_->result_reader = nullptr;
    impl_->cancel_writer = nullptr;
    impl_->requirements_writer = nullptr;
}

bool FacadeStorageStatus::SendCancelRequest(const char* company, const char* building)
{
    if (!impl_->cancel_writer)
        return false;
    facade_storage_msgs::msg::FacadeStorageCancelRequest request;
    request.company(company ? company : "");
    request.building(building ? building : "");
    request.requested_at_epoch_ms(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
    // ReturnCode_t is a plain int32_t (RETCODE_OK == 0) -- an implicit bool conversion would
    // treat any nonzero *error* code as truthy, backwards from what's wanted here.
    return impl_->cancel_writer->write(&request) == RETCODE_OK;
}

bool FacadeStorageStatus::SendRequirements(const char* company, const char* building, const char* required_directions_csv,
        const char* required_counts_csv)
{
    if (!impl_->requirements_writer)
        return false;

    std::vector<std::string> directions;
    if (required_directions_csv)
    {
        std::stringstream ss(required_directions_csv);
        std::string direction;
        while (std::getline(ss, direction, ','))
        {
            if (!direction.empty())
                directions.push_back(direction);
        }
    }

    // Parsed independently of directions above, then only used if its length actually matches --
    // a mismatched or absent counts_csv degrades to "no counts declared" (empty vector) rather
    // than risking a misaligned direction<->count pairing, which the server would otherwise
    // silently mis-consume as index-by-index.
    std::vector<uint32_t> counts;
    if (required_counts_csv)
    {
        std::stringstream ss(required_counts_csv);
        std::string count_text;
        while (std::getline(ss, count_text, ','))
        {
            if (!count_text.empty())
                counts.push_back(static_cast<uint32_t>(std::strtoul(count_text.c_str(), nullptr, 10)));
        }
    }
    if (counts.size() != directions.size())
        counts.clear();

    facade_storage_msgs::msg::FacadeStorageRequirements request;
    request.company(company ? company : "");
    request.building(building ? building : "");
    request.required_directions(directions);
    request.required_counts(counts);
    request.requested_at_epoch_ms(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
    return impl_->requirements_writer->write(&request) == RETCODE_OK;
}
