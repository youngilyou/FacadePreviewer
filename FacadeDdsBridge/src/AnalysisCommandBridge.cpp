#include "AnalysisCommandBridge.h"

#define WIN32_LEAN_AND_MEAN
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

#include "FacadeAnalysisPubSubTypes.hpp"
#include "DdsQosHelpers.h"

#include <chrono>
#include <cstdio>
#include <sstream>
#include <string>
#include <vector>

using namespace eprosima::fastdds::dds;
using namespace facade_analysis_msgs::msg;

namespace {

int64_t NowEpochMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string JoinCsv(const std::vector<std::string>& values)
{
    std::string out;
    for (size_t i = 0; i < values.size(); ++i)
    {
        if (i > 0) out += ',';
        out += values[i];
    }
    return out;
}

std::vector<std::string> SplitCsv(const char* csv)
{
    std::vector<std::string> out;
    if (!csv) return out;
    std::stringstream ss(csv);
    std::string item;
    while (std::getline(ss, item, ','))
        if (!item.empty()) out.push_back(item);
    return out;
}

// Generic "read every sample, decode to a C struct, invoke a C callback" listener -- each
// facade_analysis_msgs type this bridge reads gets one instantiation. Avoids 7 near-identical
// hand-written DataReaderListener subclasses (see AnalysisBridge.cpp on the CheckCrackDdsBridge
// side, which only had 3 and wrote them out by hand -- 7 was enough to warrant this template).
template <typename SampleT, typename DataT, typename CallbackT, typename DecodeFn>
class GenericListener : public DataReaderListener
{
public:
    CallbackT callback = nullptr;
    void* user_data = nullptr;
    DecodeFn decode;

    explicit GenericListener(DecodeFn fn) : decode(fn) {}

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (!info.valid_data || !callback)
                continue;
            DataT out{};
            decode(sample_, out);
            callback(&out, user_data);
        }
    }

private:
    SampleT sample_;
};

DWORD WINAPI TeardownParticipantThread(LPVOID param)
{
    DomainParticipant* participant = (DomainParticipant*)param;
    participant->delete_contained_entities();
    DomainParticipantFactory::get_instance()->delete_participant(participant);
    return 0;
}

} // namespace

struct AnalysisCommandBridge::Impl
{
    DomainParticipant* participant = nullptr;
    Publisher* publisher = nullptr;
    Subscriber* subscriber = nullptr;

    Topic* dispatch_topic = nullptr;
    Topic* dispatched_topic = nullptr;
    Topic* dispatch_failed_topic = nullptr;
    Topic* accepted_topic = nullptr;
    Topic* queued_topic = nullptr;
    Topic* started_topic = nullptr;
    Topic* status_topic = nullptr;
    Topic* error_topic = nullptr;
    Topic* retry_topic = nullptr;
    Topic* stop_topic = nullptr;
    Topic* result_topic = nullptr;

    DataWriter* dispatch_writer = nullptr;
    DataWriter* retry_writer = nullptr;
    DataWriter* stop_writer = nullptr;

    DataReader* dispatched_reader = nullptr;
    DataReader* dispatch_failed_reader = nullptr;
    DataReader* accepted_reader = nullptr;
    DataReader* queued_reader = nullptr;
    DataReader* started_reader = nullptr;
    DataReader* status_reader = nullptr;
    DataReader* error_reader = nullptr;
    DataReader* result_reader = nullptr;

    TypeSupport dispatch_type{new AnalysisDispatchRequestPubSubType()};
    TypeSupport dispatched_type{new AnalysisDispatchedPubSubType()};
    TypeSupport dispatch_failed_type{new AnalysisDispatchFailedPubSubType()};
    TypeSupport accepted_type{new AnalysisJobAcceptedPubSubType()};
    TypeSupport queued_type{new AnalysisJobQueuedPubSubType()};
    TypeSupport started_type{new AnalysisJobStartedPubSubType()};
    TypeSupport status_type{new AnalysisStatusUpdatePubSubType()};
    TypeSupport error_type{new AnalysisErrorNotifyPubSubType()};
    TypeSupport retry_type{new AnalysisRetryRequestPubSubType()};
    TypeSupport stop_type{new AnalysisStopRequestPubSubType()};
    TypeSupport result_type{new AnalysisResultPubSubType()};

    // decode-function-carrying listeners -- constructed with a lambda that copies fields out of
    // the IDL-generated sample type into the plain C struct the caller's callback expects.
    GenericListener<AnalysisDispatched, AnalysisDispatchedData, AnalysisDispatchedCallback, void(*)(const AnalysisDispatched&, AnalysisDispatchedData&)>
        dispatched_listener{[](const AnalysisDispatched& s, AnalysisDispatchedData& out) {
            out.archive_id = s.archive_id();
            out.assigned_worker_id = s.assigned_worker_id().c_str();
            out.assigned_at_epoch_ms = s.assigned_at_epoch_ms();
        }};
    GenericListener<AnalysisDispatchFailed, AnalysisDispatchFailedData, AnalysisDispatchFailedCallback, void(*)(const AnalysisDispatchFailed&, AnalysisDispatchFailedData&)>
        dispatch_failed_listener{[](const AnalysisDispatchFailed& s, AnalysisDispatchFailedData& out) {
            out.archive_id = s.archive_id();
            out.reason = s.reason().c_str();
        }};
    GenericListener<AnalysisJobAccepted, AnalysisJobAcceptedData, AnalysisJobAcceptedCallback, void(*)(const AnalysisJobAccepted&, AnalysisJobAcceptedData&)>
        accepted_listener{[](const AnalysisJobAccepted& s, AnalysisJobAcceptedData& out) {
            out.archive_id = s.archive_id();
            out.worker_id = s.worker_id().c_str();
            out.started_immediately = s.started_immediately();
        }};
    GenericListener<AnalysisJobQueued, AnalysisJobQueuedData, AnalysisJobQueuedCallback, void(*)(const AnalysisJobQueued&, AnalysisJobQueuedData&)>
        queued_listener{[](const AnalysisJobQueued& s, AnalysisJobQueuedData& out) {
            out.archive_id = s.archive_id();
            out.worker_id = s.worker_id().c_str();
            out.queue_position = s.queue_position();
        }};
    GenericListener<AnalysisJobStarted, AnalysisJobStartedData, AnalysisJobStartedCallback, void(*)(const AnalysisJobStarted&, AnalysisJobStartedData&)>
        started_listener{[](const AnalysisJobStarted& s, AnalysisJobStartedData& out) {
            out.archive_id = s.archive_id();
            out.worker_id = s.worker_id().c_str();
        }};
    GenericListener<AnalysisStatusUpdate, AnalysisStatusUpdateData, AnalysisStatusUpdateCallback, void(*)(const AnalysisStatusUpdate&, AnalysisStatusUpdateData&)>
        status_listener{[](const AnalysisStatusUpdate& s, AnalysisStatusUpdateData& out) {
            out.archive_id = s.archive_id();
            out.worker_id = s.worker_id().c_str();
            out.stage = s.stage().c_str();
            out.progress = s.progress().c_str();
            out.updated_at_epoch_ms = s.updated_at_epoch_ms();
        }};
    GenericListener<AnalysisErrorNotify, AnalysisErrorNotifyData, AnalysisErrorNotifyCallback, void(*)(const AnalysisErrorNotify&, AnalysisErrorNotifyData&)>
        error_listener{[](const AnalysisErrorNotify& s, AnalysisErrorNotifyData& out) {
            out.archive_id = s.archive_id();
            out.worker_id = s.worker_id().c_str();
            out.stage = s.stage().c_str();
            out.error_message = s.error_message().c_str();
            out.occurred_at_epoch_ms = s.occurred_at_epoch_ms();
        }};
    GenericListener<AnalysisResult, AnalysisResultData, AnalysisResultCallback, void(*)(const AnalysisResult&, AnalysisResultData&)>
        result_listener{[](const AnalysisResult& s, AnalysisResultData& out) {
            out.archive_id = s.archive_id();
            out.worker_id = s.worker_id().c_str();
            out.success = s.success();
            out.completed_at_epoch_ms = s.completed_at_epoch_ms();
        }};
};

AnalysisCommandBridge::AnalysisCommandBridge() : impl_(new Impl()) {}

AnalysisCommandBridge::~AnalysisCommandBridge()
{
    Stop();
    delete impl_;
}

void AnalysisCommandBridge::SetCallbacks(AnalysisDispatchedCallback dispatchedCb, AnalysisDispatchFailedCallback dispatchFailedCb,
        AnalysisJobAcceptedCallback jobAcceptedCb, AnalysisJobQueuedCallback jobQueuedCb,
        AnalysisJobStartedCallback jobStartedCb, AnalysisStatusUpdateCallback statusUpdateCb,
        AnalysisErrorNotifyCallback errorNotifyCb, AnalysisResultCallback resultCb, void* userData)
{
    impl_->dispatched_listener.callback = dispatchedCb; impl_->dispatched_listener.user_data = userData;
    impl_->dispatch_failed_listener.callback = dispatchFailedCb; impl_->dispatch_failed_listener.user_data = userData;
    impl_->accepted_listener.callback = jobAcceptedCb; impl_->accepted_listener.user_data = userData;
    impl_->queued_listener.callback = jobQueuedCb; impl_->queued_listener.user_data = userData;
    impl_->started_listener.callback = jobStartedCb; impl_->started_listener.user_data = userData;
    impl_->status_listener.callback = statusUpdateCb; impl_->status_listener.user_data = userData;
    impl_->error_listener.callback = errorNotifyCb; impl_->error_listener.user_data = userData;
    impl_->result_listener.callback = resultCb; impl_->result_listener.user_data = userData;
}

bool AnalysisCommandBridge::Start(int domainId, const char* topicPrefix, const char* initialPeerHost,
        int initialPeerPort, const char* localInterfaceIp)
{
    Impl* impl = impl_;
    std::string prefix = (topicPrefix && *topicPrefix) ? topicPrefix : "rt/facade_analysis/";

    auto factory = DomainParticipantFactory::get_instance();
    impl->participant = factory->create_participant(domainId,
            MakeUdpOnlyQos(domainId, initialPeerHost, initialPeerPort, localInterfaceIp, "AnalysisCommandBridge"));
    if (!impl->participant)
    {
        fprintf(stderr, "AnalysisCommandBridge: failed to create participant (domain %d)\n", domainId);
        return false;
    }

    impl->dispatch_type.register_type(impl->participant);
    impl->dispatched_type.register_type(impl->participant);
    impl->dispatch_failed_type.register_type(impl->participant);
    impl->accepted_type.register_type(impl->participant);
    impl->queued_type.register_type(impl->participant);
    impl->started_type.register_type(impl->participant);
    impl->status_type.register_type(impl->participant);
    impl->error_type.register_type(impl->participant);
    impl->retry_type.register_type(impl->participant);
    impl->stop_type.register_type(impl->participant);
    impl->result_type.register_type(impl->participant);

    impl->publisher = impl->participant->create_publisher(PUBLISHER_QOS_DEFAULT);
    impl->subscriber = impl->participant->create_subscriber(SUBSCRIBER_QOS_DEFAULT);
    if (!impl->publisher || !impl->subscriber)
    {
        fprintf(stderr, "AnalysisCommandBridge: failed to create publisher/subscriber\n");
        return false;
    }

    impl->dispatch_topic = impl->participant->create_topic(prefix + "AnalysisDispatchRequest", impl->dispatch_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->dispatched_topic = impl->participant->create_topic(prefix + "AnalysisDispatched", impl->dispatched_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->dispatch_failed_topic = impl->participant->create_topic(prefix + "AnalysisDispatchFailed", impl->dispatch_failed_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->accepted_topic = impl->participant->create_topic(prefix + "AnalysisJobAccepted", impl->accepted_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->queued_topic = impl->participant->create_topic(prefix + "AnalysisJobQueued", impl->queued_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->started_topic = impl->participant->create_topic(prefix + "AnalysisJobStarted", impl->started_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->status_topic = impl->participant->create_topic(prefix + "AnalysisStatusUpdate", impl->status_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->error_topic = impl->participant->create_topic(prefix + "AnalysisErrorNotify", impl->error_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->retry_topic = impl->participant->create_topic(prefix + "AnalysisRetryRequest", impl->retry_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->stop_topic = impl->participant->create_topic(prefix + "AnalysisStopRequest", impl->stop_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->result_topic = impl->participant->create_topic(prefix + "AnalysisResult", impl->result_type.get_type_name(), TOPIC_QOS_DEFAULT);
    if (!impl->dispatch_topic || !impl->dispatched_topic || !impl->dispatch_failed_topic || !impl->accepted_topic ||
            !impl->queued_topic || !impl->started_topic || !impl->status_topic || !impl->error_topic ||
            !impl->retry_topic || !impl->stop_topic || !impl->result_topic)
    {
        fprintf(stderr, "AnalysisCommandBridge: failed to create topics\n");
        return false;
    }

    auto make_reliable_wqos = []() {
        DataWriterQos wqos = DATAWRITER_QOS_DEFAULT;
        wqos.reliability().kind = RELIABLE_RELIABILITY_QOS;
        wqos.durability().kind = TRANSIENT_LOCAL_DURABILITY_QOS;
        wqos.history().kind = KEEP_LAST_HISTORY_QOS;
        wqos.history().depth = 20;
        return wqos;
    };
    impl->dispatch_writer = impl->publisher->create_datawriter(impl->dispatch_topic, make_reliable_wqos());
    impl->retry_writer = impl->publisher->create_datawriter(impl->retry_topic, make_reliable_wqos());
    impl->stop_writer = impl->publisher->create_datawriter(impl->stop_topic, make_reliable_wqos());
    if (!impl->dispatch_writer || !impl->retry_writer || !impl->stop_writer)
    {
        fprintf(stderr, "AnalysisCommandBridge: failed to create one or more writers\n");
        return false;
    }

    auto make_reliable_rqos = []() {
        DataReaderQos rqos = DATAREADER_QOS_DEFAULT;
        rqos.reliability().kind = RELIABLE_RELIABILITY_QOS;
        rqos.durability().kind = TRANSIENT_LOCAL_DURABILITY_QOS;
        rqos.history().kind = KEEP_LAST_HISTORY_QOS;
        rqos.history().depth = 20;
        return rqos;
    };
    impl->dispatched_reader = impl->subscriber->create_datareader(impl->dispatched_topic, make_reliable_rqos(), &impl->dispatched_listener);
    impl->dispatch_failed_reader = impl->subscriber->create_datareader(impl->dispatch_failed_topic, make_reliable_rqos(), &impl->dispatch_failed_listener);
    impl->accepted_reader = impl->subscriber->create_datareader(impl->accepted_topic, make_reliable_rqos(), &impl->accepted_listener);
    impl->queued_reader = impl->subscriber->create_datareader(impl->queued_topic, make_reliable_rqos(), &impl->queued_listener);
    impl->started_reader = impl->subscriber->create_datareader(impl->started_topic, make_reliable_rqos(), &impl->started_listener);
    impl->error_reader = impl->subscriber->create_datareader(impl->error_topic, make_reliable_rqos(), &impl->error_listener);
    impl->result_reader = impl->subscriber->create_datareader(impl->result_topic, make_reliable_rqos(), &impl->result_listener);

    // BEST_EFFORT -- matches AnalysisBridge's status_writer QoS on the worker side.
    DataReaderQos status_rqos = DATAREADER_QOS_DEFAULT;
    status_rqos.reliability().kind = BEST_EFFORT_RELIABILITY_QOS;
    status_rqos.history().kind = KEEP_LAST_HISTORY_QOS;
    status_rqos.history().depth = 10;
    impl->status_reader = impl->subscriber->create_datareader(impl->status_topic, status_rqos, &impl->status_listener);

    if (!impl->dispatched_reader || !impl->dispatch_failed_reader || !impl->accepted_reader || !impl->queued_reader ||
            !impl->started_reader || !impl->status_reader || !impl->error_reader || !impl->result_reader)
    {
        fprintf(stderr, "AnalysisCommandBridge: failed to create one or more readers\n");
        return false;
    }

    printf("AnalysisCommandBridge: listening on domain %d (topic prefix '%s')\n", domainId, prefix.c_str());
    return true;
}

void AnalysisCommandBridge::Stop()
{
    if (!impl_->participant)
        return;

    HANDLE thread = CreateThread(NULL, 0, TeardownParticipantThread, impl_->participant, 0, NULL);
    if (thread)
    {
        const DWORD kTeardownBudgetMs = 2000;
        if (WaitForSingleObject(thread, kTeardownBudgetMs) == WAIT_TIMEOUT)
            fprintf(stderr, "AnalysisCommandBridge: graceful teardown exceeded %ums, abandoning it\n", kTeardownBudgetMs);
        CloseHandle(thread);
    }
    impl_->participant = nullptr;
    impl_->publisher = nullptr;
    impl_->subscriber = nullptr;
    impl_->dispatch_topic = impl_->dispatched_topic = impl_->dispatch_failed_topic = impl_->accepted_topic =
            impl_->queued_topic = impl_->started_topic = impl_->status_topic = impl_->error_topic =
            impl_->retry_topic = impl_->stop_topic = impl_->result_topic = nullptr;
    impl_->dispatch_writer = impl_->retry_writer = impl_->stop_writer = nullptr;
    impl_->dispatched_reader = impl_->dispatch_failed_reader = impl_->accepted_reader = impl_->queued_reader =
            impl_->started_reader = impl_->status_reader = impl_->error_reader = impl_->result_reader = nullptr;
}

bool AnalysisCommandBridge::SendDispatchRequest(int64_t archiveId, const char* company, const char* building,
        const char* directionsCsv, uint32_t imageCount, const char* zipRemotePath, uint64_t sizeBytes)
{
    if (!impl_->dispatch_writer)
        return false;
    AnalysisDispatchRequest msg;
    msg.archive_id(archiveId);
    msg.company(company ? company : "");
    msg.building(building ? building : "");
    msg.directions(SplitCsv(directionsCsv));
    msg.image_count(imageCount);
    msg.zip_remote_path(zipRemotePath ? zipRemotePath : "");
    msg.size_bytes(sizeBytes);
    msg.requested_at_epoch_ms(NowEpochMs());
    return impl_->dispatch_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisCommandBridge::SendRetryRequest(int64_t archiveId)
{
    if (!impl_->retry_writer)
        return false;
    AnalysisRetryRequest msg;
    msg.archive_id(archiveId);
    msg.requested_at_epoch_ms(NowEpochMs());
    return impl_->retry_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisCommandBridge::SendStopRequest(int64_t archiveId)
{
    if (!impl_->stop_writer)
        return false;
    AnalysisStopRequest msg;
    msg.archive_id(archiveId);
    msg.requested_at_epoch_ms(NowEpochMs());
    return impl_->stop_writer->write(&msg) == RETCODE_OK;
}
