// Native Fast-DDS client for facade_storage_msgs (FacadeStorageFeedback/Result/CancelRequest) --
// see idl/facade_storage_msgs/msg/FacadeStorage.idl's header comment for what this trio is for.
// Independent participant from DdsFrameSubscriber (video/pose) and from RsyncTransfer (a plain
// CreateProcess wrapper, no DDS at all) -- own opaque handle, own lifecycle, matching this
// project's per-concern-own-participant convention.
#pragma once

#include <cstdint>

extern "C" {

// Mirrors facade_storage_msgs::msg::FacadeStorageFeedback. Strings valid only for the duration
// of the callback (same contract as FacadeImageSensorFrame etc.).
struct FacadeStorageFeedbackData
{
    const char* company;
    const char* building;
    uint32_t images_zipped;
    uint32_t images_total;
    const char* status;
    int64_t updated_at_epoch_ms;
};

// Mirrors facade_storage_msgs::msg::FacadeStorageResult.
struct FacadeStorageResultData
{
    const char* company;
    const char* building;
    bool success;
    bool cancelled;
    int64_t archive_id;
    const char* zip_path;
    uint64_t size_bytes;
    uint32_t image_count;
    const char* error_message;
    int64_t completed_at_epoch_ms;
};

using FacadeStorageFeedbackCallback = void(*)(const FacadeStorageFeedbackData* feedback, void* user_data);
using FacadeStorageResultCallback = void(*)(const FacadeStorageResultData* result, void* user_data);

} // extern "C"

class FacadeStorageStatus
{
public:
    FacadeStorageStatus();
    ~FacadeStorageStatus();

    FacadeStorageStatus(const FacadeStorageStatus&) = delete;
    FacadeStorageStatus& operator=(const FacadeStorageStatus&) = delete;

    void SetCallbacks(FacadeStorageFeedbackCallback feedback_cb, FacadeStorageResultCallback result_cb, void* user_data);

    // Same discovery-override convention as DdsFrameSubscriber::Start (see DdsQosHelpers.h) --
    // pass nullptr/""/<=0 for initial_peer_host/initial_peer_port/local_interface_ip to fall
    // back to FACADE_DDS_INITIAL_PEER/FACADE_DDS_INTERFACE_WHITELIST.
    bool Start(int domain_id, const char* feedback_topic, const char* result_topic, const char* cancel_topic,
            const char* requirements_topic, const char* finalize_topic, const char* initial_peer_host = nullptr,
            int initial_peer_port = 0, const char* local_interface_ip = nullptr);
    void Stop();

    // Publishes a FacadeStorageCancelRequest for (company, building). No-op (returns false) if
    // Start() hasn't been called or failed.
    bool SendCancelRequest(const char* company, const char* building);

    // Publishes a FacadeStorageFinalizeRequest for (company, building) -- the operator has
    // confirmed "yes, this is everything" in FacadePreviewer's Yes/No prompt. Tells the server to
    // archive whatever has been received so far, bypassing SendRequirements' normal
    // auto-complete-when-all-directions-arrive check (see FacadeStorage.idl's own comment on why
    // that check alone isn't reliable across many separate test/real sessions reusing the same
    // building). No-op (returns false) if Start() hasn't been called or failed.
    bool SendFinalizeRequest(const char* company, const char* building);

    // Publishes a FacadeStorageRequirements declaration for (company, building) -- DDS-native
    // replacement for the old HTTP POST api/crackvision/building-requirements call (see
    // FacadeStorage.idl's own comment on why). required_directions_csv is a comma-separated
    // direction list (e.g. "FRONT,BACK,ROOF") -- a plain delimited string rather than a marshaled
    // string array, since this project's direction vocabulary (FRONT/BACK/LEFT/RIGHT/ROOF/OTHER)
    // never contains commas and this keeps the C API a single flat parameter.
    //
    // required_counts_csv is a parallel comma-separated list of expected image counts, same order
    // and same count as required_directions_csv (e.g. "26,47,13" for "FRONT,BACK,ROOF") -- lets
    // the server tell "this direction has fully arrived" apart from "this direction has merely
    // been seen at all", see FacadeStorage.idl's own comment on required_counts for why this
    // matters. Pass "" (or a mismatched-length string) to declare no counts -- the server then
    // falls back to presence-only for every direction in this call, same as before this existed.
    // contract_id/customer_name (2026-08-28): pre-baked into the GenerateJson-produced
    // ApartmentAssignment file the operator loaded (see ApartmentAssignment.cs), not
    // operator-entered here. Pass "" for either when no contract is loaded/known -- backend
    // stores them as-is, no fabricated placeholder (see FacadeStorage.idl's own comment).
    bool SendRequirements(const char* company, const char* building, const char* required_directions_csv,
            const char* required_counts_csv = "", const char* contract_id = "", const char* customer_name = "");

private:
    struct Impl;
    Impl* impl_;
};
