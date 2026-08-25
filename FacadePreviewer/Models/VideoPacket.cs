namespace FacadePreviewer.Models;

/// <summary>Managed copy of one VideoTsPacket DDS sample — Data is copied out
/// of native memory during the callback (the native pointer is only valid
/// for that call), so this is safe to queue/hold onto afterward. Raw MPEG-TS
/// bytes -- demuxing/H.264 decode into an actual displayable frame is not
/// implemented yet (see project CLAUDE.local.md's "남은 작업").</summary>
public sealed record VideoPacket(
    string StreamId,
    uint FrameId,
    ulong SequenceId,
    double TimestampSec,
    byte[] Data);
