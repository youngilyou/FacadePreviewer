using System.Runtime.InteropServices;

namespace FacadePreviewer.Services;

/// <summary>Managed wrapper around FacadeDdsBridge.dll's rsync-over-ssh transfer C API (see
/// RsyncTransfer.h). Independent of <see cref="DdsBridgeService"/> -- own native handle, own
/// lifecycle, no shared state.
///
/// IMPORTANT: like DdsBridgeService, <see cref="ProgressReceived"/> and
/// <see cref="TransferCompleted"/> fire on FacadeDdsBridge's background reader thread, not the
/// WPF UI thread. Subscribers touching UI-bound state must marshal via
/// Application.Current.Dispatcher themselves.</summary>
public sealed class RsyncTransferService : IDisposable
{
    private readonly IntPtr _handle;
    private readonly DdsBridgeInterop.RsyncProgressCallback _progressCallback;
    private readonly DdsBridgeInterop.RsyncCompleteCallback _completeCallback;
    private bool _disposed;

    public event Action<ulong /*bytesTransferred*/, int /*percent*/, double /*rateMbps*/>? ProgressReceived;
    public event Action<int /*exitCode*/, string /*errorMessage*/>? TransferCompleted;

    public RsyncTransferService()
    {
        _handle = DdsBridgeInterop.FacadeRsync_Create();
        if (_handle == IntPtr.Zero)
            throw new InvalidOperationException("FacadeRsync_Create returned null.");

        _progressCallback = OnProgressNative;
        _completeCallback = OnCompleteNative;
    }

    /// <param name="rsyncExePath">previewer/tools/cygwin_rsync/bin/rsync.exe (see
    /// tools/Get-CygwinRsync.ps1).</param>
    /// <param name="localSourceDir">Local folder laid out as
    /// &lt;company&gt;/&lt;building&gt;/&lt;direction&gt;/&lt;session_id&gt;/*.jpg -- caller is
    /// responsible for that layout (see MainViewModel's session folder helpers), this service
    /// only pushes whatever is already there.</param>
    /// <param name="sshKeyPath">Empty string to use ssh's own default key discovery.</param>
    /// <param name="sshPassword">2026-08-27: only consulted when sshKeyPath is empty (key always
    /// wins) -- non-interactive password auth via sshpass, see RsyncTransfer.h's Start() doc
    /// comment. Pass empty string for the original key-only/default-discovery behavior.</param>
    /// <param name="remoteDestRoot">DDS-Router host's FacadeImageBridge watch root (absolute
    /// Linux path, e.g. "/srv/facade_images").</param>
    /// <param name="resume">false re-runs plain rsync (already-complete files are still skipped
    /// by rsync's own quick-check; only a file left mid-copy by an interrupted prior attempt is
    /// fully retransmitted). true adds --partial --append-verify so that file resumes from where
    /// it left off instead -- see RsyncTransfer.h's Start() doc comment. Exposed as an explicit
    /// operator choice ("처음부터 전송" vs "이어서 전송") after a transfer failure.</param>
    public bool Start(string rsyncExePath, string localSourceDir, string sshUser, string sshHost, int sshPort,
        string sshKeyPath, string sshPassword, string remoteDestRoot, bool resume = false)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return DdsBridgeInterop.FacadeRsync_Start(_handle, rsyncExePath, localSourceDir, sshUser, sshHost, sshPort,
            sshKeyPath, sshPassword, remoteDestRoot, resume, _progressCallback, _completeCallback, IntPtr.Zero);
    }

    public void Cancel()
    {
        if (_disposed)
            return;
        DdsBridgeInterop.FacadeRsync_Cancel(_handle);
    }

    private void OnProgressNative(ulong bytesTransferred, int percent, double rateMbps, IntPtr userData)
        => ProgressReceived?.Invoke(bytesTransferred, percent, rateMbps);

    private void OnCompleteNative(int exitCode, string errorMessage, IntPtr userData)
        => TransferCompleted?.Invoke(exitCode, errorMessage ?? "");

    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        Cancel();
        DdsBridgeInterop.FacadeRsync_Destroy(_handle);
        GC.SuppressFinalize(this);
    }
}
