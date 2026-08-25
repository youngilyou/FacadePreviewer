namespace FacadePreviewer.Models;

/// <summary>Managed copy of one H.264-decoded video frame (TS demux via libmpeg + decode via
/// libavcodec inside FacadeDdsBridge.dll, see FacadeDdsBridge/src/VideoDecoder.h). Bgr is
/// copied out of native memory during the callback (the native pointer is only valid for that
/// call) -- BGR24, row-major, Stride bytes per row (may exceed Width*3).</summary>
public sealed record DecodedVideoFrame(
    string StreamId,
    uint Width,
    uint Height,
    uint Stride,
    byte[] Bgr,
    double TimestampSec);
