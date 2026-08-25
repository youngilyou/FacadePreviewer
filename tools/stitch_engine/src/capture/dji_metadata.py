"""DJI still-image metadata extraction (CLAUDE.local.md #5).

DJI embeds flight/gimbal telemetry as XMP attributes on a
``<rdf:Description rdf:about="DJI Meta Data">`` element (namespace
``drone-dji``) inside the JPEG APP1 segment, alongside standard EXIF for
timestamp/resolution/focal length. No 3rd-party EXIF library is required:
the XMP packet is plain UTF-8 XML and PIL exposes standard EXIF.

Any field DJI/EXIF does not provide is left as ``None`` — never fabricated.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from PIL import ExifTags, Image

from src.common.types import CameraInfo, GpsInfo, ImageMetadata, MissionInfo, PoseInfo

_XMP_START = b"<x:xmpmeta"
_XMP_END = b"</x:xmpmeta>"


def _extract_xmp_attrs(raw: bytes) -> dict[str, str]:
    """Return DJI XMP attributes (local tag name -> string value), or {} if absent."""
    start = raw.find(_XMP_START)
    end = raw.find(_XMP_END)
    if start == -1 or end == -1:
        return {}
    xmp_str = raw[start : end + len(_XMP_END)].decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(xmp_str)
    except ET.ParseError:
        return {}

    attrs: dict[str, str] = {}
    for desc in root.iter():
        if desc.tag.endswith("}Description") or desc.tag == "Description":
            for key, value in desc.attrib.items():
                local_name = key.split("}")[-1] if "}" in key else key
                attrs[local_name] = value
    return attrs


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _exif_dict(image: Image.Image) -> dict:
    raw_exif = image.getexif()
    if not raw_exif:
        return {}
    named = {ExifTags.TAGS.get(k, k): v for k, v in raw_exif.items()}
    # Sub-IFDs (EXIF proper, GPS) are nested; merge them in.
    for ifd_id in (ExifTags.IFD.Exif, ExifTags.IFD.GPSInfo):
        try:
            sub = raw_exif.get_ifd(ifd_id)
        except (KeyError, AttributeError):
            continue
        if not sub:
            continue
        tag_map = ExifTags.GPSTAGS if ifd_id == ExifTags.IFD.GPSInfo else ExifTags.TAGS
        named.update({tag_map.get(k, k): v for k, v in sub.items()})
    return named


def _rational_to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _exif_timestamp(exif: dict) -> Optional[str]:
    raw = exif.get("DateTimeOriginal") or exif.get("DateTime")
    if not raw:
        return None
    # "YYYY:MM:DD HH:MM:SS" -> "YYYY-MM-DDTHH:MM:SS". This is the camera's
    # local clock, not a verified UTC time (DJI still images carry no TZ
    # offset), so we do not append a "Z"/offset suffix.
    match = re.match(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}:\d{2}:\d{2})$", raw)
    if not match:
        return raw
    y, m, d, hms = match.groups()
    return f"{y}-{m}-{d}T{hms}"


def _exif_gps(exif: dict) -> GpsInfo:
    def dms_to_deg(dms, ref) -> Optional[float]:
        if not dms:
            return None
        try:
            deg = float(dms[0]) + float(dms[1]) / 60.0 + float(dms[2]) / 3600.0
        except (TypeError, ValueError, IndexError):
            return None
        if ref in ("S", "W"):
            deg = -deg
        return deg

    lat = dms_to_deg(exif.get("GPSLatitude"), exif.get("GPSLatitudeRef"))
    lon = dms_to_deg(exif.get("GPSLongitude"), exif.get("GPSLongitudeRef"))
    alt = _rational_to_float(exif.get("GPSAltitude"))
    return GpsInfo(latitude=lat, longitude=lon, altitude_m=alt)


def parse_dji_image(file_path: str | Path) -> ImageMetadata:
    """Parse one DJI JPEG into ImageMetadata. Missing fields stay None."""
    file_path = Path(file_path)
    raw = file_path.read_bytes()
    xmp = _extract_xmp_attrs(raw)

    with Image.open(file_path) as image:
        width, height = image.size
        exif = _exif_dict(image)

    drone_model = xmp.get("Make") or exif.get("Make")
    camera_model = xmp.get("Model") or exif.get("Model")

    if "GpsLatitude" in xmp or "GpsLongtitude" in xmp:
        gps = GpsInfo(
            latitude=_to_float(xmp.get("GpsLatitude")),
            longitude=_to_float(xmp.get("GpsLongtitude")),
            altitude_m=_to_float(xmp.get("AbsoluteAltitude")),
        )
    else:
        gps = _exif_gps(exif)

    drone_pose = PoseInfo(
        yaw_deg=_to_float(xmp.get("FlightYawDegree")),
        pitch_deg=_to_float(xmp.get("FlightPitchDegree")),
        roll_deg=_to_float(xmp.get("FlightRollDegree")),
    )
    gimbal_pose = PoseInfo(
        yaw_deg=_to_float(xmp.get("GimbalYawDegree")),
        pitch_deg=_to_float(xmp.get("GimbalPitchDegree")),
        roll_deg=_to_float(xmp.get("GimbalRollDegree")),
    )

    # Sensor physical size is not embedded by DJI and is not guessed here —
    # without it, focal_length_mm alone cannot yield a trustworthy mm scale
    # (CLAUDE.local.md #26), so `calibrated` stays False regardless of the
    # pixel-space CalibratedFocalLength XMP carries.
    camera = CameraInfo(
        focal_length_mm=_rational_to_float(exif.get("FocalLength")),
        equivalent_focal_length_mm=_rational_to_float(exif.get("FocalLengthIn35mmFilm")),
        sensor_width_mm=None,
        sensor_height_mm=None,
        calibrated_focal_length_px=_to_float(xmp.get("CalibratedFocalLength")),
        calibrated_optical_center_x_px=_to_float(xmp.get("CalibratedOpticalCenterX")),
        calibrated_optical_center_y_px=_to_float(xmp.get("CalibratedOpticalCenterY")),
        calibrated=False,
    )

    mission = MissionInfo(flight_id=file_path.parent.name, waypoint_id=None, facade_hint=None)

    return ImageMetadata(
        image_id=file_path.stem,
        file_path=str(file_path.resolve()),
        timestamp_utc=_exif_timestamp(exif),
        drone_model=drone_model,
        camera_model=camera_model,
        width=width,
        height=height,
        gps=gps,
        drone_pose=drone_pose,
        gimbal_pose=gimbal_pose,
        camera=camera,
        mission=mission,
    )
