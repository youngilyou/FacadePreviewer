// Native Fast-DDS client for facade_analysis_msgs, FacadePreviewer's "dispatcher" role -- see
// idl/facade_analysis_msgs/msg/FacadeAnalysis.idl's header comment and
// https://github.com/youngilyou/AnalysisLoadBalancer README for the full protocol.
//
// Domain 30 (FacadePreviewerDomainParticipant, see
// DDS-Router/config/ddsrouter/crack_inspection_analysis.yaml) -- a SEPARATE participant from
// FacadeStorageStatus's domain-0 one (facade_image_msgs/facade_storage_msgs). The two domains
// don't need routing between each other; this class and FacadeStorageStatus never talk to each
// other, only FacadePreviewer's own application code (TransferSettingsWindow) bridges them by
// reading a FacadeStorageResult's archive_id and passing it into SendDispatchRequest here.
//
// This class:
// - publishes AnalysisDispatchRequest (to AnalysisLoadBalancer) and AnalysisRetryRequest/
//   AnalysisStopRequest (directly to whichever CheckCrackViewer worker is handling that
//   archive_id -- routed by archive_id, not worker_id, see the IDL's own comment on this),
// - subscribes AnalysisDispatched/AnalysisDispatchFailed (from the balancer) and
//   AnalysisJobAccepted/AnalysisJobQueued/AnalysisJobStarted/AnalysisStatusUpdate/
//   AnalysisErrorNotify/AnalysisResult (directly from the assigned worker).
#pragma once

#include <cstdint>

extern "C" {

struct AnalysisDispatchedData { int64_t archive_id; const char* assigned_worker_id; int64_t assigned_at_epoch_ms; };
struct AnalysisDispatchFailedData { int64_t archive_id; const char* reason; };
struct AnalysisJobAcceptedData { int64_t archive_id; const char* worker_id; bool started_immediately; };
struct AnalysisJobQueuedData { int64_t archive_id; const char* worker_id; uint32_t queue_position; };
struct AnalysisJobStartedData { int64_t archive_id; const char* worker_id; };
struct AnalysisStatusUpdateData { int64_t archive_id; const char* worker_id; const char* stage; const char* progress; int64_t updated_at_epoch_ms; };
struct AnalysisErrorNotifyData { int64_t archive_id; const char* worker_id; const char* stage; const char* error_message; int64_t occurred_at_epoch_ms; };
struct AnalysisResultData { int64_t archive_id; const char* worker_id; bool success; int64_t completed_at_epoch_ms; };

using AnalysisDispatchedCallback = void(*)(const AnalysisDispatchedData*, void*);
using AnalysisDispatchFailedCallback = void(*)(const AnalysisDispatchFailedData*, void*);
using AnalysisJobAcceptedCallback = void(*)(const AnalysisJobAcceptedData*, void*);
using AnalysisJobQueuedCallback = void(*)(const AnalysisJobQueuedData*, void*);
using AnalysisJobStartedCallback = void(*)(const AnalysisJobStartedData*, void*);
using AnalysisStatusUpdateCallback = void(*)(const AnalysisStatusUpdateData*, void*);
using AnalysisErrorNotifyCallback = void(*)(const AnalysisErrorNotifyData*, void*);
using AnalysisResultCallback = void(*)(const AnalysisResultData*, void*);

} // extern "C"

class AnalysisCommandBridge
{
public:
    AnalysisCommandBridge();
    ~AnalysisCommandBridge();

    AnalysisCommandBridge(const AnalysisCommandBridge&) = delete;
    AnalysisCommandBridge& operator=(const AnalysisCommandBridge&) = delete;

    void SetCallbacks(AnalysisDispatchedCallback dispatchedCb, AnalysisDispatchFailedCallback dispatchFailedCb,
            AnalysisJobAcceptedCallback jobAcceptedCb, AnalysisJobQueuedCallback jobQueuedCb,
            AnalysisJobStartedCallback jobStartedCb, AnalysisStatusUpdateCallback statusUpdateCb,
            AnalysisErrorNotifyCallback errorNotifyCb, AnalysisResultCallback resultCb, void* userData);

    // topic_prefix: pass nullptr/"" for the default "rt/facade_analysis/" (must match
    // CheckCrackViewer's AnalysisBridge -- same convention, see that class's own Start doc).
    bool Start(int domainId, const char* topicPrefix = nullptr, const char* initialPeerHost = nullptr,
            int initialPeerPort = 0, const char* localInterfaceIp = nullptr);
    void Stop();

    // directionsCsv: comma-joined direction list, best-effort/informational only -- may be ""
    // (FacadeStorageResult doesn't carry a direction list; CheckCrackViewer determines the real
    // per-facade direction split itself by scanning the extracted archive's subfolders, see
    // AnalysisBridge's own AssignmentReceived doc comment, so an empty/approximate value here
    // doesn't break registration, only the "원격 분석 작업" window's display column).
    bool SendDispatchRequest(int64_t archiveId, const char* company, const char* building,
            const char* directionsCsv, uint32_t imageCount, const char* zipRemotePath, uint64_t sizeBytes);
    bool SendRetryRequest(int64_t archiveId);
    bool SendStopRequest(int64_t archiveId);

private:
    struct Impl;
    Impl* impl_;
};
