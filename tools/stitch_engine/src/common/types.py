"""Core data types shared across pipeline stages.

Field layout mirrors CLAUDE.local.md #5 (image metadata schema), #9 (geometry
quality gate) and #11 (stitch quality report). Unknown values are ``None`` —
never fabricated (CLAUDE.local.md #5: "없는 값은 null, 임의 생성 금지").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class GpsInfo:
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_m: Optional[float] = None


@dataclass
class PoseInfo:
    yaw_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    roll_deg: Optional[float] = None


@dataclass
class CameraInfo:
    focal_length_mm: Optional[float] = None
    equivalent_focal_length_mm: Optional[float] = None
    sensor_width_mm: Optional[float] = None
    sensor_height_mm: Optional[float] = None
    calibrated_focal_length_px: Optional[float] = None
    calibrated_optical_center_x_px: Optional[float] = None
    calibrated_optical_center_y_px: Optional[float] = None
    calibrated: bool = False


@dataclass
class MissionInfo:
    flight_id: Optional[str] = None
    waypoint_id: Optional[str] = None
    facade_hint: Optional[str] = None


@dataclass
class ImageMetadata:
    image_id: str
    file_path: str
    timestamp_utc: Optional[str] = None
    drone_model: Optional[str] = None
    camera_model: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

    gps: GpsInfo = field(default_factory=GpsInfo)
    drone_pose: PoseInfo = field(default_factory=PoseInfo)
    gimbal_pose: PoseInfo = field(default_factory=PoseInfo)
    camera: CameraInfo = field(default_factory=CameraInfo)
    mission: MissionInfo = field(default_factory=MissionInfo)


class GeometryFailureCode(str, Enum):
    LOW_MATCH = "LOW_MATCH"
    LOW_INLIER = "LOW_INLIER"
    HIGH_REPROJECTION_ERROR = "HIGH_REPROJECTION_ERROR"
    DEGENERATE_HOMOGRAPHY = "DEGENERATE_HOMOGRAPHY"
    # LoFTR matching itself never returned within the configured per-pair
    # timeout (see TimeoutLoFTRMatcher) -- e.g. an extreme repetitive-pattern
    # pair (#9's "반복 창문 패턴으로 오정합 증가") blowing up LoFTR's fine-matching
    # stage. Distinct from LOW_MATCH: the pair was never even scored.
    MATCH_TIMEOUT = "MATCH_TIMEOUT"
    # RANSAC found an internally-consistent inlier set (passes every check
    # above) but the recovered homography implies a vertical shift whose
    # SIGN disagrees with the two photos' own GPS altitude difference --
    # e.g. B was captured higher than A, but the homography says content
    # moved the wrong way. On a facade with repeating floors, an inlier set
    # that's internally consistent but actually describes matching against
    # the WRONG (visually identical) floor is exactly the failure mode this
    # catches, and it's independent of RANSAC's own inlier-ratio confidence
    # (2026-09-08/09 CLAUDE.local.md entry: found by empirically comparing
    # real matched homographies' implied shift against real GPS altitude on
    # a 168-image capture -- a same-real-altitude-gap pair can otherwise
    # imply wildly different (up to ~17x) apparent shift depending on which
    # specific image pair, all while passing every other gate).
    POSE_INCONSISTENT = "POSE_INCONSISTENT"
    # The homography's implied vertical shift, converted to "number of
    # floors" using THIS image's own measured facade repeat-period (see
    # src/geometry/facade_period.py), disagrees with the number of floors
    # implied by the two photos' real GPS altitude difference. Unlike
    # POSE_INCONSISTENT (sign only), this catches a wrong-floor-by-exactly-
    # one-or-more aliasing match even when the sign is right -- e.g. B is
    # correctly known to be one floor above A, but the homography places B's
    # content as if it were two floors up. Only applied when the per-image
    # period estimate's own confidence is high enough to trust (2026-09-09
    # CLAUDE.local.md entry).
    FLOOR_COUNT_MISMATCH = "FLOOR_COUNT_MISMATCH"


@dataclass
class PairCandidate:
    image_a: str
    image_b: str
    gps_distance_m: Optional[float] = None
    view_angle_delta_deg: Optional[float] = None
    reason: str = ""  # e.g. "temporal_neighbor", "gps_proximity"


@dataclass
class MatchResult:
    image_a: str
    image_b: str
    num_matches: int
    keypoints_a: "object" = None  # np.ndarray (N,2), original-resolution px
    keypoints_b: "object" = None  # np.ndarray (N,2), original-resolution px
    confidence: "object" = None  # np.ndarray (N,)


@dataclass
class GeometryResult:
    image_a: str
    image_b: str
    status: str  # "OK" or a GeometryFailureCode value
    homography: "object" = None  # np.ndarray (3,3), maps A -> B
    inlier_mask: "object" = None  # np.ndarray (N,) bool
    num_matches: int = 0
    num_inliers: int = 0
    inlier_ratio: float = 0.0
    median_reprojection_error_px: Optional[float] = None


@dataclass
class StitchQualityReport:
    facade_id: str
    image_count: int = 0
    matched_pair_count: int = 0
    failed_pair_count: int = 0
    mean_inlier_ratio: Optional[float] = None
    median_reprojection_error_px: Optional[float] = None
    coverage_ratio: Optional[float] = None
    disconnected_components: int = 1
    reference_image_id: Optional[str] = None
    unreachable_image_ids: list = field(default_factory=list)
    # Images that had ZERO passing geometry edge to anything (never even
    # became a node in the match graph) -- distinct from unreachable_image_ids
    # (which had edges, just not to the reference's component). image_count
    # above already excludes these (see runner.py's "never_matched" report),
    # so total catalog size for this facade is image_count + this.
    never_matched_count: int = 0
    global_drift_score_px: Optional[float] = None  # mean cycle-closure disagreement, see graph.py
    max_drift_score_px: Optional[float] = None
    cycle_edge_count: int = 0
    needs_colmap_fallback: bool = False
    colmap_fallback_reasons: list = field(default_factory=list)


@dataclass
class Crack:
    """CLAUDE.local.md #24/#27. mm fields stay None unless the facade mosaic
    carries real calibration (crack/measurement.py) — never fabricated."""

    crack_id: str
    building_id: str
    facade_id: str
    bbox_px: tuple  # (x0, y0, x1, y1), facade-global pixel coords
    polygon_px: "object"  # np.ndarray (N, 2)
    skeleton_px: "object"  # np.ndarray (N, 2)
    length_px: float
    max_width_px: float
    mean_width_px: float
    confidence: float
    observation_state: str  # "OBSERVED" — see crack/pipeline.py docstring
    length_mm: Optional[float] = None
    max_width_mm: Optional[float] = None
    source_tile_ids: list = field(default_factory=list)
    source_image_ids: list = field(default_factory=list)  # facade-level provenance, see crack/pipeline.py
