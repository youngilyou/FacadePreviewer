using System.Runtime.InteropServices;
using FacadePreviewer.Models;

namespace FacadePreviewer.Services;

/// <summary>Managed wrapper around FacadeDdsBridge.dll's C API. Owns exactly
/// one native DdsFrameSubscriber handle for this app's lifetime (no
/// multi-drone support planned, see project CLAUDE.local.md).
///
/// IMPORTANT: <see cref="SensorFrameReceived"/> and <see cref="VideoPacketReceived"/>
/// fire on the native DDS listener thread, NOT the WPF UI thread (unlike
/// CheckCrackViewer's DispatcherTimer-based services, which were already on
/// the UI thread). Subscribers that touch UI-bound state (ViewModels'
/// ObservableProperty setters included) MUST marshal to the UI thread
/// themselves, e.g. via Application.Current.Dispatcher.Invoke/BeginInvoke.</summary>
public sealed class DdsBridgeService : IDisposable
{
    private readonly IntPtr _handle;
    // Keep the delegates alive for the subscriber's lifetime -- the native
    // side only holds a raw function pointer to them, so if these get
    // garbage-collected while FacadeDdsBridge.dll can still call them, the
    // process crashes on the next incoming sample.
    private readonly DdsBridgeInterop.SensorFrameCallback _sensorCallback;
    private readonly DdsBridgeInterop.VideoPacketCallback _videoCallback;
    private readonly DdsBridgeInterop.DecodedFrameCallback _decodedFrameCallback;
    private bool _disposed;

    public event Action<SensorFrame>? SensorFrameReceived;
    public event Action<VideoPacket>? VideoPacketReceived;
    public event Action<DecodedVideoFrame>? DecodedFrameReceived;

    public DdsBridgeService()
    {
        _handle = DdsBridgeInterop.FacadeDds_Create();
        if (_handle == IntPtr.Zero)
            throw new InvalidOperationException("FacadeDds_Create returned null — native DLL failed to allocate a subscriber.");

        _sensorCallback = OnSensorFrameNative;
        _videoCallback = OnVideoPacketNative;
        _decodedFrameCallback = OnDecodedFrameNative;
        DdsBridgeInterop.FacadeDds_SetCallbacks(_handle, _sensorCallback, _videoCallback, _decodedFrameCallback, IntPtr.Zero);
    }

    // initialPeerHost/localInterfaceIp: null/empty means "not set" (falls back to this
    // process's FACADE_DDS_INITIAL_PEER/FACADE_DDS_INTERFACE_WHITELIST env vars, if any).
    // initialPeerPort <= 0 means "use the standard discovery-port formula".
    public void Start(int domainId, string sensorTopic, string videoTopic,
        string? initialPeerHost = null, int initialPeerPort = 0, string? localInterfaceIp = null)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        DdsBridgeInterop.FacadeDds_StartAsync(_handle, domainId, sensorTopic, videoTopic,
            initialPeerHost ?? "", initialPeerPort, localInterfaceIp ?? "");
    }

    public void Stop()
    {
        if (_disposed)
            return;
        DdsBridgeInterop.FacadeDds_Stop(_handle);
    }

    private void OnSensorFrameNative(IntPtr framePtr, IntPtr userData)
    {
        if (framePtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<DdsBridgeInterop.FacadeImageSensorFrame>(framePtr);
        var frame = new SensorFrame(
            StreamId: Marshal.PtrToStringAnsi(native.StreamId) ?? "",
            FrameId: native.FrameId,
            TimestampSec: native.TimestampSec,
            ImageEncoding: Marshal.PtrToStringAnsi(native.ImageEncoding) ?? "",
            ImageWidth: native.ImageWidth,
            ImageHeight: native.ImageHeight,
            HasGps: native.HasGps,
            GpsLatitudeDeg: native.GpsLatitudeDeg,
            GpsLongitudeDeg: native.GpsLongitudeDeg,
            GpsAltitudeM: native.GpsAltitudeM,
            HasCameraPose: native.HasCameraPose,
            CameraPositionMX: native.CameraPositionMX,
            CameraPositionMY: native.CameraPositionMY,
            CameraPositionMZ: native.CameraPositionMZ,
            CameraOrientationX: native.CameraOrientationX,
            CameraOrientationY: native.CameraOrientationY,
            CameraOrientationZ: native.CameraOrientationZ,
            CameraOrientationW: native.CameraOrientationW);
        SensorFrameReceived?.Invoke(frame);
    }

    private void OnVideoPacketNative(IntPtr packetPtr, IntPtr userData)
    {
        if (packetPtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<DdsBridgeInterop.FacadeVideoTsPacket>(packetPtr);

        // Copy out of native memory now -- native.Data is only valid for the
        // duration of this callback (see DdsFrameSubscriber.cpp's listener:
        // the sample object is reused on the next take_next_sample() call).
        var data = new byte[native.DataLength];
        if (native.DataLength > 0 && native.Data != IntPtr.Zero)
            Marshal.Copy(native.Data, data, 0, (int)native.DataLength);

        var packet = new VideoPacket(
            StreamId: Marshal.PtrToStringAnsi(native.StreamId) ?? "",
            FrameId: native.FrameId,
            SequenceId: native.SequenceId,
            TimestampSec: native.TimestampSec,
            Data: data);
        VideoPacketReceived?.Invoke(packet);
    }

    private void OnDecodedFrameNative(IntPtr framePtr, IntPtr userData)
    {
        if (framePtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<DdsBridgeInterop.FacadeDecodedFrame>(framePtr);

        // Copy out of native memory now -- native.BgrData is only valid for the duration of
        // this callback (VideoDecoder's frame buffer is reused on the next decoded frame).
        int byteCount = checked((int)native.Stride * (int)native.Height);
        var bgr = new byte[byteCount];
        if (byteCount > 0 && native.BgrData != IntPtr.Zero)
            Marshal.Copy(native.BgrData, bgr, 0, byteCount);

        var frame = new DecodedVideoFrame(
            StreamId: Marshal.PtrToStringAnsi(native.StreamId) ?? "",
            Width: native.Width,
            Height: native.Height,
            Stride: native.Stride,
            Bgr: bgr,
            TimestampSec: native.TimestampSec);
        DecodedFrameReceived?.Invoke(frame);
    }

    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        Stop();
        DdsBridgeInterop.FacadeDds_Destroy(_handle);
        GC.SuppressFinalize(this);
    }
}
