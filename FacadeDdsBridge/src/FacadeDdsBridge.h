// Exported C API for FacadePreviewer's C# WPF app to P/Invoke. Wraps a
// single DdsFrameSubscriber instance -- one bridge handle per process is
// all this previewer needs (no multi-drone support planned, see project
// memory: project-dds-previewer-design).
#pragma once

#include "DdsFrameSubscriber.h"
#include "RsyncTransfer.h"
#include "FacadeStorageStatus.h"
#include "AnalysisCommandBridge.h"

extern "C" {

#ifdef FACADE_DDS_BRIDGE_EXPORTS
#define FACADE_API __declspec(dllexport)
#else
#define FACADE_API __declspec(dllimport)
#endif

// Opaque handle -- callers never touch DdsFrameSubscriber directly.
FACADE_API void* FacadeDds_Create();
FACADE_API void FacadeDds_Destroy(void* handle);

// decoded_frame_cb: BGR24 frames decoded from VideoTsPacket (TS demux + H.264 decode, see
// VideoDecoder.h) -- fires in addition to video_cb, not instead of it. Pass nullptr if the
// caller only wants the raw-bytes/counting callback.
FACADE_API void FacadeDds_SetCallbacks(void* handle, FacadeSensorFrameCallback sensor_cb, FacadeVideoPacketCallback video_cb,
        FacadeDecodedFrameCallback decoded_frame_cb, void* user_data);

// initial_peer_host/initial_peer_port/local_interface_ip: UI-sourced discovery overrides (see
// DdsFrameSubscriber::StartAsync) -- pass nullptr/""/0 for any of them to fall back to the
// FACADE_DDS_INITIAL_PEER/FACADE_DDS_INTERFACE_WHITELIST env vars.
FACADE_API void FacadeDds_StartAsync(void* handle, int domain_id, const char* sensor_topic, const char* video_topic,
        const char* initial_peer_host, int initial_peer_port, const char* local_interface_ip);
FACADE_API void FacadeDds_Stop(void* handle);

// Facade high-resolution image transfer (rsync-over-ssh -> DDS-Router's FacadeImageBridge).
// Independent of the DDS handle above -- own opaque handle, own lifecycle. See RsyncTransfer.h
// for the full contract (path conventions, callback threading).
FACADE_API void* FacadeRsync_Create();
FACADE_API void FacadeRsync_Destroy(void* handle);
FACADE_API bool FacadeRsync_Start(void* handle, const wchar_t* rsync_exe_path, const wchar_t* local_source_dir,
        const char* ssh_user, const char* ssh_host, int ssh_port, const wchar_t* ssh_key_path,
        const char* remote_dest_root, bool resume, FacadeRsyncProgressCallback progress_cb,
        FacadeRsyncCompleteCallback complete_cb, void* user_data);
FACADE_API void FacadeRsync_Cancel(void* handle);

// CrackVisionArchiveManager operator-visibility status (Feedback/Result/CancelRequest) --
// independent of the DDS handle above (own participant, own lifecycle), see
// FacadeStorageStatus.h for the full contract.
FACADE_API void* FacadeStorageStatus_Create();
FACADE_API void FacadeStorageStatus_Destroy(void* handle);
FACADE_API void FacadeStorageStatus_SetCallbacks(void* handle, FacadeStorageFeedbackCallback feedback_cb,
        FacadeStorageResultCallback result_cb, void* user_data);
FACADE_API bool FacadeStorageStatus_Start(void* handle, int domain_id, const char* feedback_topic,
        const char* result_topic, const char* cancel_topic, const char* requirements_topic,
        const char* finalize_topic, const char* initial_peer_host, int initial_peer_port,
        const char* local_interface_ip);
FACADE_API void FacadeStorageStatus_Stop(void* handle);
FACADE_API bool FacadeStorageStatus_SendCancelRequest(void* handle, const char* company, const char* building);
FACADE_API bool FacadeStorageStatus_SendRequirements(void* handle, const char* company, const char* building,
        const char* required_directions_csv, const char* required_counts_csv);
FACADE_API bool FacadeStorageStatus_SendFinalizeRequest(void* handle, const char* company, const char* building);

// facade_analysis_msgs crack-inspection remote analysis dispatch (domain 30, see
// AnalysisCommandBridge.h) -- independent of all handles above (own participant, own lifecycle).
FACADE_API void* AnalysisCommand_Create();
FACADE_API void AnalysisCommand_Destroy(void* handle);
FACADE_API void AnalysisCommand_SetCallbacks(void* handle, AnalysisDispatchedCallback dispatchedCb,
        AnalysisDispatchFailedCallback dispatchFailedCb, AnalysisJobAcceptedCallback jobAcceptedCb,
        AnalysisJobQueuedCallback jobQueuedCb, AnalysisJobStartedCallback jobStartedCb,
        AnalysisStatusUpdateCallback statusUpdateCb, AnalysisErrorNotifyCallback errorNotifyCb,
        AnalysisResultCallback resultCb, void* userData);
FACADE_API bool AnalysisCommand_Start(void* handle, int domainId, const char* topicPrefix,
        const char* initialPeerHost, int initialPeerPort, const char* localInterfaceIp);
FACADE_API void AnalysisCommand_Stop(void* handle);
FACADE_API bool AnalysisCommand_SendDispatchRequest(void* handle, int64_t archiveId, const char* company,
        const char* building, const char* directionsCsv, uint32_t imageCount, const char* zipRemotePath,
        uint64_t sizeBytes);
FACADE_API bool AnalysisCommand_SendRetryRequest(void* handle, int64_t archiveId);
FACADE_API bool AnalysisCommand_SendStopRequest(void* handle, int64_t archiveId);

}
