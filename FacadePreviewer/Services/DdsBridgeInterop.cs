using System.Runtime.InteropServices;

namespace FacadePreviewer.Services;

/// <summary>Raw P/Invoke surface for FacadeDdsBridge.dll (see
/// FacadeDdsBridge/src/FacadeDdsBridge.h and DdsFrameSubscriber.h — struct
/// field order/types must match those C structs exactly). Nothing here does
/// marshaling to managed types beyond what P/Invoke does automatically; see
/// DdsBridgeService for the layer that's actually safe to call from WPF.</summary>
internal static class DdsBridgeInterop
{
    private const string DllName = "FacadeDdsBridge.dll";

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct FacadeImageSensorFrame
    {
        public IntPtr StreamId; // const char*
        public uint FrameId;
        public double TimestampSec;

        public IntPtr ImageEncoding; // const char*
        public uint ImageWidth;
        public uint ImageHeight;

        [MarshalAs(UnmanagedType.I1)] public bool HasGps;
        public double GpsLatitudeDeg;
        public double GpsLongitudeDeg;
        public double GpsAltitudeM;

        [MarshalAs(UnmanagedType.I1)] public bool HasCameraPose;
        public double CameraPositionMX;
        public double CameraPositionMY;
        public double CameraPositionMZ;
        public double CameraOrientationX;
        public double CameraOrientationY;
        public double CameraOrientationZ;
        public double CameraOrientationW;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct FacadeVideoTsPacket
    {
        public IntPtr StreamId; // const char*
        public uint FrameId;
        public ulong SequenceId;
        public double TimestampSec;
        public IntPtr Data; // const uint8_t*
        public uint DataLength;
    }

    // Mirrors FacadeDecodedFrame (DdsFrameSubscriber.h) -- one H.264-decoded, BGR24 frame
    // (TS demux via libmpeg + decode via libavcodec, see VideoDecoder.h/.cpp). BgrData/Stride
    // valid only for the duration of the callback, same contract as FacadeVideoTsPacket::Data.
    [StructLayout(LayoutKind.Sequential)]
    public struct FacadeDecodedFrame
    {
        public IntPtr StreamId; // const char*
        public uint Width;
        public uint Height;
        public uint Stride; // bytes per row, may exceed Width*3
        public IntPtr BgrData; // const uint8_t*
        public double TimestampSec; // see VideoDecoder.h's FrameCallback doc comment -- not a true PTS
    }

    // Matches FacadeSensorFrameCallback / FacadeVideoPacketCallback / FacadeDecodedFrameCallback
    // in the native header exactly: void(*)(const T* sample, void* user_data). The sample
    // pointer is taken as IntPtr (not the struct directly) so the marshaling
    // happens explicitly and predictably on the C# side (DdsBridgeService),
    // rather than relying on the CLR to marshal a struct-by-pointer callback
    // parameter automatically -- that path has historically been a source of
    // ABI mismatches for exactly the bool/char* fields these structs have.
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void SensorFrameCallback(IntPtr framePtr, IntPtr userData);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void VideoPacketCallback(IntPtr packetPtr, IntPtr userData);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void DecodedFrameCallback(IntPtr framePtr, IntPtr userData);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern IntPtr FacadeDds_Create();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeDds_Destroy(IntPtr handle);

    // Native signature (FacadeDdsBridge.h) is sensor_cb, video_cb, decoded_frame_cb, user_data
    // -- 4 params after handle. Must be kept in lockstep with the native header's param count
    // (see git history: a previous version of this declaration once drifted from the native
    // signature and misaligned every argument).
    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeDds_SetCallbacks(IntPtr handle, SensorFrameCallback sensorCb, VideoPacketCallback videoCb,
        DecodedFrameCallback decodedFrameCb, IntPtr userData);

    // initialPeerHost/localInterfaceIp: pass "" for "not set" (native side treats empty same as
    // null -- falls back to FACADE_DDS_INITIAL_PEER/FACADE_DDS_INTERFACE_WHITELIST env vars).
    // initialPeerPort: pass 0 for "use the standard discovery-port formula".
    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeDds_StartAsync(IntPtr handle, int domainId,
        [MarshalAs(UnmanagedType.LPStr)] string sensorTopic, [MarshalAs(UnmanagedType.LPStr)] string videoTopic,
        [MarshalAs(UnmanagedType.LPStr)] string initialPeerHost, int initialPeerPort,
        [MarshalAs(UnmanagedType.LPStr)] string localInterfaceIp);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeDds_Stop(IntPtr handle);

    // Facade high-resolution image transfer (rsync-over-ssh) -- independent handle/lifecycle
    // from the DDS functions above, see RsyncTransfer.h.
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void RsyncProgressCallback(ulong bytesTransferred, int percent, double rateMbps, IntPtr userData);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    // LPUTF8Str, not LPStr (== ANSI/system codepage) -- RsyncTransfer.cpp builds Korean error text
    // ("SSH 인증 실패 (...) -- SSH 키 경로를 확인하세요.") as UTF-8, same reasoning as
    // FacadeRsync_Start's remoteDestRoot/FacadeStorageStatus_SendRequirements's company/building
    // above -- this one callback param was missed when those were fixed.
    public delegate void RsyncCompleteCallback(int exitCode, [MarshalAs(UnmanagedType.LPUTF8Str)] string errorMessage, IntPtr userData);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern IntPtr FacadeRsync_Create();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeRsync_Destroy(IntPtr handle);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool FacadeRsync_Start(IntPtr handle,
        [MarshalAs(UnmanagedType.LPWStr)] string rsyncExePath,
        [MarshalAs(UnmanagedType.LPWStr)] string localSourceDir,
        [MarshalAs(UnmanagedType.LPStr)] string sshUser,
        [MarshalAs(UnmanagedType.LPStr)] string sshHost,
        int sshPort,
        [MarshalAs(UnmanagedType.LPWStr)] string sshKeyPath,
        // LPUTF8Str, not LPStr (== ANSI/system codepage) -- remoteDestRoot embeds the
        // operator-chosen company/building text (e.g. Korean), and RsyncTransfer.cpp's
        // ToCygdrivePath/remote_spec construction already assumes UTF-8 bytes on the native side
        // (see its Utf8ToWide() call). Marshaling as ANSI here silently re-encoded that text into
        // the Windows system codepage instead, which Cygwin then misread as UTF-8 -- confirmed
        // via a real transfer where a Korean building name landed on the remote host as garbage
        // bytes, causing facade_image_sessions.building to no longer match what
        // FacadePreviewer separately registered via POST api/crackvision/building-requirements
        // (that call serializes to JSON/UTF-8 correctly, so it kept the original text) --
        // breaking check_and_enqueue_if_complete's match and silently stalling the archive
        // forever.
        [MarshalAs(UnmanagedType.LPUTF8Str)] string remoteDestRoot,
        [MarshalAs(UnmanagedType.I1)] bool resume,
        RsyncProgressCallback progressCb, RsyncCompleteCallback completeCb, IntPtr userData);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeRsync_Cancel(IntPtr handle);

    // CrackVisionArchiveManager operator-visibility status (Feedback/Result/CancelRequest) --
    // independent handle/lifecycle from both the DDS video functions and the rsync functions
    // above, see FacadeStorageStatus.h.
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct FacadeStorageFeedbackData
    {
        public IntPtr Company; // const char*
        public IntPtr Building; // const char*
        public uint ImagesZipped;
        public uint ImagesTotal;
        public IntPtr Status; // const char*
        public long UpdatedAtEpochMs;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct FacadeStorageResultData
    {
        public IntPtr Company; // const char*
        public IntPtr Building; // const char*
        [MarshalAs(UnmanagedType.I1)] public bool Success;
        [MarshalAs(UnmanagedType.I1)] public bool Cancelled;
        public long ArchiveId;
        public IntPtr ZipPath; // const char*
        public ulong SizeBytes;
        public uint ImageCount;
        public IntPtr ErrorMessage; // const char*
        public long CompletedAtEpochMs;
    }

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void StorageFeedbackCallback(IntPtr feedbackPtr, IntPtr userData);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void StorageResultCallback(IntPtr resultPtr, IntPtr userData);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern IntPtr FacadeStorageStatus_Create();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeStorageStatus_Destroy(IntPtr handle);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeStorageStatus_SetCallbacks(IntPtr handle, StorageFeedbackCallback feedbackCb,
        StorageResultCallback resultCb, IntPtr userData);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool FacadeStorageStatus_Start(IntPtr handle, int domainId,
        [MarshalAs(UnmanagedType.LPStr)] string feedbackTopic, [MarshalAs(UnmanagedType.LPStr)] string resultTopic,
        [MarshalAs(UnmanagedType.LPStr)] string cancelTopic, [MarshalAs(UnmanagedType.LPStr)] string requirementsTopic,
        [MarshalAs(UnmanagedType.LPStr)] string initialPeerHost,
        int initialPeerPort, [MarshalAs(UnmanagedType.LPStr)] string localInterfaceIp);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void FacadeStorageStatus_Stop(IntPtr handle);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool FacadeStorageStatus_SendCancelRequest(IntPtr handle,
        // LPUTF8Str, not LPStr -- same reasoning as FacadeRsync_Start's remoteDestRoot above:
        // company/building can be non-ASCII (Korean), and the server side compares these bytes
        // against what it recorded from FacadeImageMeta (UTF-8), so this must match.
        [MarshalAs(UnmanagedType.LPUTF8Str)] string company, [MarshalAs(UnmanagedType.LPUTF8Str)] string building);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool FacadeStorageStatus_SendRequirements(IntPtr handle,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string company, [MarshalAs(UnmanagedType.LPUTF8Str)] string building,
        [MarshalAs(UnmanagedType.LPStr)] string requiredDirectionsCsv,
        [MarshalAs(UnmanagedType.LPStr)] string requiredCountsCsv);
}
