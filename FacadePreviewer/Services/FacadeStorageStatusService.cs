using System.Linq;
using System.Runtime.InteropServices;

namespace FacadePreviewer.Services;

/// <summary>One reported progress update for an in-progress archive build (see
/// facade_storage_msgs::msg::FacadeStorageFeedback).</summary>
public sealed record FacadeStorageFeedback(string Company, string Building, uint ImagesZipped, uint ImagesTotal, string Status);

/// <summary>Final outcome of an archive build job (see facade_storage_msgs::msg::FacadeStorageResult).
/// CompletedAtEpochMs matters more than it looks: the Result topic is RELIABLE + TRANSIENT_LOCAL
/// (see FacadeStorageStatus.cpp's own comment on why -- a fast archive can otherwise finish and
/// publish before this dialog's reader has matched), which means a brand-new reader for the SAME
/// (company, building) as a PREVIOUS job can be replayed that OLD result at startup, before any
/// new one exists -- confirmed via a real repro where a "저장 완료" popup for a stale archive_id
/// appeared while a genuinely new transfer was still only 68% through. Callers must compare this
/// against when their OWN job started and discard anything older.</summary>
public sealed record FacadeStorageResult(string Company, string Building, bool Success, bool Cancelled,
    long ArchiveId, string ZipPath, ulong SizeBytes, uint ImageCount, string ErrorMessage, long CompletedAtEpochMs);

/// <summary>Managed wrapper around FacadeDdsBridge.dll's CrackVisionArchiveManager
/// operator-visibility status client (see FacadeStorageStatus.h). Independent of <see
/// cref="DdsBridgeService"/> and <see cref="RsyncTransferService"/> -- own native handle, own
/// lifecycle.
///
/// IMPORTANT: like the other services in this folder, <see cref="FeedbackReceived"/> and <see
/// cref="ResultReceived"/> fire on FacadeDdsBridge's background DDS listener thread, not the WPF
/// UI thread -- subscribers must marshal via Application.Current.Dispatcher themselves.
///
/// This client receives Feedback/Result for every (company, building) currently being archived
/// server-side, not just the one this dialog cares about -- callers filter by company/building
/// themselves (see TransferSettingsWindow's usage).</summary>
public sealed class FacadeStorageStatusService : IDisposable
{
    private readonly IntPtr _handle;
    private readonly DdsBridgeInterop.StorageFeedbackCallback _feedbackCallback;
    private readonly DdsBridgeInterop.StorageResultCallback _resultCallback;
    private bool _disposed;

    public event Action<FacadeStorageFeedback>? FeedbackReceived;
    public event Action<FacadeStorageResult>? ResultReceived;

    public FacadeStorageStatusService()
    {
        _handle = DdsBridgeInterop.FacadeStorageStatus_Create();
        if (_handle == IntPtr.Zero)
            throw new InvalidOperationException("FacadeStorageStatus_Create returned null.");

        _feedbackCallback = OnFeedbackNative;
        _resultCallback = OnResultNative;
        DdsBridgeInterop.FacadeStorageStatus_SetCallbacks(_handle, _feedbackCallback, _resultCallback, IntPtr.Zero);
    }

    /// <param name="initialPeerHost">Pass "" to fall back to FACADE_DDS_INITIAL_PEER env var.</param>
    /// <param name="localInterfaceIp">Pass "" to fall back to FACADE_DDS_INTERFACE_WHITELIST env var.</param>
    public bool Start(int domainId, string feedbackTopic, string resultTopic, string cancelTopic, string requirementsTopic,
        string finalizeTopic, string initialPeerHost = "", int initialPeerPort = 0, string localInterfaceIp = "")
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.FacadeStorageStatus_Start(_handle, domainId, feedbackTopic, resultTopic, cancelTopic,
            requirementsTopic, finalizeTopic, initialPeerHost, initialPeerPort, localInterfaceIp);
    }

    public bool SendCancelRequest(string company, string building)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.FacadeStorageStatus_SendCancelRequest(_handle, company, building);
    }

    /// <summary>Operator-confirmed "this is everything for this building" (Yes/No prompt after a
    /// single-direction transfer) -- tells the server to archive whatever has been received so
    /// far, bypassing SendRequirements' normal auto-complete-when-all-directions-arrive check.
    /// See CrackVisionArchiveManager::finalize_now's own comment for why that check alone isn't
    /// reliable across many separate sessions reusing the same (company, building).</summary>
    public bool SendFinalize(string company, string building)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.FacadeStorageStatus_SendFinalizeRequest(_handle, company, building);
    }

    /// <summary>Declares the expected direction set for (company, building) -- DDS-native
    /// replacement for the old HTTP POST api/crackvision/building-requirements call. Additive on
    /// the server side (see CrackVisionArchiveManager::set_building_requirements): repeated calls
    /// for the same building accumulate rather than overwrite, so sending one direction at a time
    /// across separate visits (this project's "1 Facade = 1 Flight" policy) still ends up with
    /// the correct full required set.</summary>
    // requiredCounts is the expected image count per direction, so the server can tell "this
    // direction has fully arrived" apart from "this direction has merely been seen at all" --
    // without it, a batch transfer that sends directions one at a time could trigger the archive
    // the instant the LAST direction's very first image lands (see FacadeStorage.idl's own
    // comment on required_counts for the real test that found this). Pass 0 for a direction
    // whose count is unknown -- that direction falls back to presence-only on the server.
    public bool SendRequirements(string company, string building, IEnumerable<(string Direction, int Count)> requirements)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        var list = requirements.ToList();
        var directionsCsv = string.Join(',', list.Select(r => r.Direction));
        var countsCsv = string.Join(',', list.Select(r => r.Count));
        return DdsBridgeInterop.FacadeStorageStatus_SendRequirements(_handle, company, building, directionsCsv, countsCsv);
    }

    // PtrToStringUTF8, not PtrToStringAnsi -- Company/Building can be non-ASCII (Korean); the
    // native side always sends UTF-8 bytes (see crackvision_archive_manager.cpp's DDS publish
    // calls, sourced from Postgres text columns), so decoding as the system ANSI codepage here
    // would silently corrupt them (same class of bug fixed in DdsBridgeInterop's LPUTF8Str
    // marshaling attributes above).
    private void OnFeedbackNative(IntPtr feedbackPtr, IntPtr userData)
    {
        if (FeedbackReceived == null || feedbackPtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<DdsBridgeInterop.FacadeStorageFeedbackData>(feedbackPtr);
        FeedbackReceived.Invoke(new FacadeStorageFeedback(
            Marshal.PtrToStringUTF8(native.Company) ?? "",
            Marshal.PtrToStringUTF8(native.Building) ?? "",
            native.ImagesZipped, native.ImagesTotal,
            Marshal.PtrToStringUTF8(native.Status) ?? ""));
    }

    private void OnResultNative(IntPtr resultPtr, IntPtr userData)
    {
        if (ResultReceived == null || resultPtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<DdsBridgeInterop.FacadeStorageResultData>(resultPtr);
        ResultReceived.Invoke(new FacadeStorageResult(
            Marshal.PtrToStringUTF8(native.Company) ?? "",
            Marshal.PtrToStringUTF8(native.Building) ?? "",
            native.Success, native.Cancelled, native.ArchiveId,
            Marshal.PtrToStringUTF8(native.ZipPath) ?? "",
            native.SizeBytes, native.ImageCount,
            Marshal.PtrToStringUTF8(native.ErrorMessage) ?? "",
            native.CompletedAtEpochMs));
    }

    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        DdsBridgeInterop.FacadeStorageStatus_Stop(_handle);
        DdsBridgeInterop.FacadeStorageStatus_Destroy(_handle);
        GC.SuppressFinalize(this);
    }
}
