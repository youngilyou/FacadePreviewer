using System.Runtime.InteropServices;

namespace FacadePreviewer.Services;

public sealed record AnalysisDispatched(long ArchiveId, string AssignedWorkerId, long AssignedAtEpochMs);
public sealed record AnalysisDispatchFailed(long ArchiveId, string Reason);
public sealed record AnalysisJobAccepted(long ArchiveId, string WorkerId, bool StartedImmediately);
public sealed record AnalysisJobQueued(long ArchiveId, string WorkerId, uint QueuePosition);
public sealed record AnalysisJobStarted(long ArchiveId, string WorkerId);
public sealed record AnalysisStatusUpdate(long ArchiveId, string WorkerId, string Stage, string Progress, long UpdatedAtEpochMs);
public sealed record AnalysisErrorNotify(long ArchiveId, string WorkerId, string Stage, string ErrorMessage, long OccurredAtEpochMs);
public sealed record AnalysisResult(long ArchiveId, string WorkerId, bool Success, long CompletedAtEpochMs);

/// <summary>Managed wrapper around FacadeDdsBridge.dll's facade_analysis_msgs dispatcher client
/// (domain 30, see AnalysisCommandBridge.h) -- independent of DdsBridgeService/
/// RsyncTransferService/FacadeStorageStatusService (own native handle, own lifecycle, own
/// domain). See https://github.com/youngilyou/AnalysisLoadBalancer README for the full protocol
/// this implements the FacadePreviewer ("dispatcher") side of.
///
/// IMPORTANT: like the other services in this folder, every event here fires on
/// FacadeDdsBridge's background DDS listener thread, not the WPF UI thread -- subscribers must
/// marshal via Application.Current.Dispatcher themselves.</summary>
public sealed class AnalysisCommandService : IDisposable
{
    private readonly IntPtr _handle;
    private readonly DdsBridgeInterop.AnalysisDispatchedCallback _dispatchedCb;
    private readonly DdsBridgeInterop.AnalysisDispatchFailedCallback _dispatchFailedCb;
    private readonly DdsBridgeInterop.AnalysisJobAcceptedCallback _jobAcceptedCb;
    private readonly DdsBridgeInterop.AnalysisJobQueuedCallback _jobQueuedCb;
    private readonly DdsBridgeInterop.AnalysisJobStartedCallback _jobStartedCb;
    private readonly DdsBridgeInterop.AnalysisStatusUpdateCallback _statusUpdateCb;
    private readonly DdsBridgeInterop.AnalysisErrorNotifyCallback _errorNotifyCb;
    private readonly DdsBridgeInterop.AnalysisResultCallback _resultCb;
    private bool _disposed;

    public event Action<AnalysisDispatched>? Dispatched;
    public event Action<AnalysisDispatchFailed>? DispatchFailed;
    public event Action<AnalysisJobAccepted>? JobAccepted;
    public event Action<AnalysisJobQueued>? JobQueued;
    public event Action<AnalysisJobStarted>? JobStarted;
    public event Action<AnalysisStatusUpdate>? StatusUpdate;
    public event Action<AnalysisErrorNotify>? ErrorNotify;
    public event Action<AnalysisResult>? ResultReceived;

    public AnalysisCommandService()
    {
        _handle = DdsBridgeInterop.AnalysisCommand_Create();
        if (_handle == IntPtr.Zero)
            throw new InvalidOperationException("AnalysisCommand_Create returned null.");

        _dispatchedCb = OnDispatchedNative;
        _dispatchFailedCb = OnDispatchFailedNative;
        _jobAcceptedCb = OnJobAcceptedNative;
        _jobQueuedCb = OnJobQueuedNative;
        _jobStartedCb = OnJobStartedNative;
        _statusUpdateCb = OnStatusUpdateNative;
        _errorNotifyCb = OnErrorNotifyNative;
        _resultCb = OnResultNative;
        DdsBridgeInterop.AnalysisCommand_SetCallbacks(_handle, _dispatchedCb, _dispatchFailedCb, _jobAcceptedCb,
            _jobQueuedCb, _jobStartedCb, _statusUpdateCb, _errorNotifyCb, _resultCb, IntPtr.Zero);
    }

    /// <param name="topicPrefix">Pass "" for the default "rt/facade_analysis/" (must match
    /// CheckCrackViewer/AnalysisLoadBalancer).</param>
    /// <param name="initialPeerHost">Pass "" to fall back to FACADE_DDS_INITIAL_PEER env var --
    /// reuses the same env var as DdsBridgeService/FacadeStorageStatusService since this is the
    /// same physical DDS-Router host, just a different domain (30, not 0).</param>
    public bool Start(int domainId, string topicPrefix = "", string initialPeerHost = "", int initialPeerPort = 0,
        string localInterfaceIp = "")
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.AnalysisCommand_Start(_handle, domainId, topicPrefix, initialPeerHost, initialPeerPort,
            localInterfaceIp);
    }

    /// <param name="directionsCsv">Best-effort/informational only -- see AnalysisCommandBridge.h's
    /// own comment on SendDispatchRequest.</param>
    public bool SendDispatchRequest(long archiveId, string company, string building, string directionsCsv,
        uint imageCount, string zipRemotePath, ulong sizeBytes)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.AnalysisCommand_SendDispatchRequest(_handle, archiveId, company, building,
            directionsCsv, imageCount, zipRemotePath, sizeBytes);
    }

    public bool SendRetryRequest(long archiveId)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.AnalysisCommand_SendRetryRequest(_handle, archiveId);
    }

    public bool SendStopRequest(long archiveId)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.AnalysisCommand_SendStopRequest(_handle, archiveId);
    }

    // PtrToStringUTF8 throughout -- worker_id/stage/progress/error_message can carry non-ASCII
    // text (e.g. Korean error messages from the pipeline), same reasoning as
    // FacadeStorageStatusService's own native callbacks.
    private void OnDispatchedNative(IntPtr ptr, IntPtr userData)
    {
        if (Dispatched == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisDispatchedData>(ptr);
        Dispatched.Invoke(new AnalysisDispatched(n.ArchiveId, Marshal.PtrToStringUTF8(n.AssignedWorkerId) ?? "", n.AssignedAtEpochMs));
    }

    private void OnDispatchFailedNative(IntPtr ptr, IntPtr userData)
    {
        if (DispatchFailed == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisDispatchFailedData>(ptr);
        DispatchFailed.Invoke(new AnalysisDispatchFailed(n.ArchiveId, Marshal.PtrToStringUTF8(n.Reason) ?? ""));
    }

    private void OnJobAcceptedNative(IntPtr ptr, IntPtr userData)
    {
        if (JobAccepted == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisJobAcceptedData>(ptr);
        JobAccepted.Invoke(new AnalysisJobAccepted(n.ArchiveId, Marshal.PtrToStringUTF8(n.WorkerId) ?? "", n.StartedImmediately));
    }

    private void OnJobQueuedNative(IntPtr ptr, IntPtr userData)
    {
        if (JobQueued == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisJobQueuedData>(ptr);
        JobQueued.Invoke(new AnalysisJobQueued(n.ArchiveId, Marshal.PtrToStringUTF8(n.WorkerId) ?? "", n.QueuePosition));
    }

    private void OnJobStartedNative(IntPtr ptr, IntPtr userData)
    {
        if (JobStarted == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisJobStartedData>(ptr);
        JobStarted.Invoke(new AnalysisJobStarted(n.ArchiveId, Marshal.PtrToStringUTF8(n.WorkerId) ?? ""));
    }

    private void OnStatusUpdateNative(IntPtr ptr, IntPtr userData)
    {
        if (StatusUpdate == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisStatusUpdateData>(ptr);
        StatusUpdate.Invoke(new AnalysisStatusUpdate(n.ArchiveId, Marshal.PtrToStringUTF8(n.WorkerId) ?? "",
            Marshal.PtrToStringUTF8(n.Stage) ?? "", Marshal.PtrToStringUTF8(n.Progress) ?? "", n.UpdatedAtEpochMs));
    }

    private void OnErrorNotifyNative(IntPtr ptr, IntPtr userData)
    {
        if (ErrorNotify == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisErrorNotifyData>(ptr);
        ErrorNotify.Invoke(new AnalysisErrorNotify(n.ArchiveId, Marshal.PtrToStringUTF8(n.WorkerId) ?? "",
            Marshal.PtrToStringUTF8(n.Stage) ?? "", Marshal.PtrToStringUTF8(n.ErrorMessage) ?? "", n.OccurredAtEpochMs));
    }

    private void OnResultNative(IntPtr ptr, IntPtr userData)
    {
        if (ResultReceived == null || ptr == IntPtr.Zero) return;
        var n = Marshal.PtrToStructure<DdsBridgeInterop.AnalysisResultData>(ptr);
        ResultReceived.Invoke(new AnalysisResult(n.ArchiveId, Marshal.PtrToStringUTF8(n.WorkerId) ?? "", n.Success, n.CompletedAtEpochMs));
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        DdsBridgeInterop.AnalysisCommand_Stop(_handle);
        DdsBridgeInterop.AnalysisCommand_Destroy(_handle);
        GC.SuppressFinalize(this);
    }
}
