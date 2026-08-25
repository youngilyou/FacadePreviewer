#define FACADE_DDS_BRIDGE_EXPORTS
#include "FacadeDdsBridge.h"

extern "C" {

void* FacadeDds_Create()
{
    return new DdsFrameSubscriber();
}

void FacadeDds_Destroy(void* handle)
{
    delete static_cast<DdsFrameSubscriber*>(handle);
}

void FacadeDds_SetCallbacks(void* handle, FacadeSensorFrameCallback sensor_cb, FacadeVideoPacketCallback video_cb,
        FacadeDecodedFrameCallback decoded_frame_cb, void* user_data)
{
    static_cast<DdsFrameSubscriber*>(handle)->SetCallbacks(sensor_cb, video_cb, decoded_frame_cb, user_data);
}

void FacadeDds_StartAsync(void* handle, int domain_id, const char* sensor_topic, const char* video_topic,
        const char* initial_peer_host, int initial_peer_port, const char* local_interface_ip)
{
    static_cast<DdsFrameSubscriber*>(handle)->StartAsync(domain_id, sensor_topic, video_topic,
            initial_peer_host, initial_peer_port, local_interface_ip);
}

void FacadeDds_Stop(void* handle)
{
    static_cast<DdsFrameSubscriber*>(handle)->Stop();
}

void* FacadeRsync_Create()
{
    return new RsyncTransfer();
}

void FacadeRsync_Destroy(void* handle)
{
    delete static_cast<RsyncTransfer*>(handle);
}

bool FacadeRsync_Start(void* handle, const wchar_t* rsync_exe_path, const wchar_t* local_source_dir,
        const char* ssh_user, const char* ssh_host, int ssh_port, const wchar_t* ssh_key_path,
        const char* remote_dest_root, bool resume, FacadeRsyncProgressCallback progress_cb,
        FacadeRsyncCompleteCallback complete_cb, void* user_data)
{
    return static_cast<RsyncTransfer*>(handle)->Start(
            rsync_exe_path ? rsync_exe_path : L"",
            local_source_dir ? local_source_dir : L"",
            ssh_user ? ssh_user : "",
            ssh_host ? ssh_host : "",
            ssh_port,
            ssh_key_path ? ssh_key_path : L"",
            remote_dest_root ? remote_dest_root : "",
            resume,
            progress_cb, complete_cb, user_data);
}

void FacadeRsync_Cancel(void* handle)
{
    static_cast<RsyncTransfer*>(handle)->Cancel();
}

void* FacadeStorageStatus_Create()
{
    return new FacadeStorageStatus();
}

void FacadeStorageStatus_Destroy(void* handle)
{
    delete static_cast<FacadeStorageStatus*>(handle);
}

void FacadeStorageStatus_SetCallbacks(void* handle, FacadeStorageFeedbackCallback feedback_cb,
        FacadeStorageResultCallback result_cb, void* user_data)
{
    static_cast<FacadeStorageStatus*>(handle)->SetCallbacks(feedback_cb, result_cb, user_data);
}

bool FacadeStorageStatus_Start(void* handle, int domain_id, const char* feedback_topic, const char* result_topic,
        const char* cancel_topic, const char* requirements_topic, const char* initial_peer_host,
        int initial_peer_port, const char* local_interface_ip)
{
    return static_cast<FacadeStorageStatus*>(handle)->Start(domain_id, feedback_topic, result_topic, cancel_topic,
            requirements_topic, initial_peer_host, initial_peer_port, local_interface_ip);
}

void FacadeStorageStatus_Stop(void* handle)
{
    static_cast<FacadeStorageStatus*>(handle)->Stop();
}

bool FacadeStorageStatus_SendCancelRequest(void* handle, const char* company, const char* building)
{
    return static_cast<FacadeStorageStatus*>(handle)->SendCancelRequest(company, building);
}

bool FacadeStorageStatus_SendRequirements(void* handle, const char* company, const char* building,
        const char* required_directions_csv, const char* required_counts_csv)
{
    return static_cast<FacadeStorageStatus*>(handle)->SendRequirements(company, building, required_directions_csv,
            required_counts_csv);
}

}
