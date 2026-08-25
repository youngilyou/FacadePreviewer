namespace FacadePreviewer.Models;

/// <summary>Managed copy of one ImageSensorFrame DDS sample — safe to hold onto
/// after the native callback returns (unlike the raw interop struct, whose
/// pointers are only valid during the callback call).</summary>
public sealed record SensorFrame(
    string StreamId,
    uint FrameId,
    double TimestampSec,
    string ImageEncoding,
    uint ImageWidth,
    uint ImageHeight,
    bool HasGps,
    double GpsLatitudeDeg,
    double GpsLongitudeDeg,
    double GpsAltitudeM,
    bool HasCameraPose,
    double CameraPositionMX,
    double CameraPositionMY,
    double CameraPositionMZ,
    double CameraOrientationX,
    double CameraOrientationY,
    double CameraOrientationZ,
    double CameraOrientationW);
