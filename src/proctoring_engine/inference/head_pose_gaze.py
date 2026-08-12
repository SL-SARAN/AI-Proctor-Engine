"""Head pose / gaze inference module.

Wraps the MediaPipe Tasks API ``FaceLandmarker`` (478 iris-refined
landmarks, with ``output_face_blendshapes=True``) plus OpenCV
``solvePnP`` for head-pose estimation and a geometric iris-offset
heuristic for coarse "looking away" detection.

The module produces three derived signals per frame
(``docs/proctoring-engine-v1-spec.md`` §3.1, Stage 1):

1. **Head pose (yaw / pitch / roll)** via ``cv2.solvePnP`` against a
   canonical 3D face model built from six stable landmark points.
2. **Eye Aspect Ratio (EAR)** from the eye-contour landmarks, used
   to detect blinks so a closed-eye frame is never mis-classified as
   "looking away".
3. **Iris-position offset** relative to the eye-corner width, used as
   a coarse "looking off to the side" signal.

The per-frame ``off_screen`` classification is the end of this
module's responsibility.  The fusion engine (Stage 2) aggregates
consecutive ``off_screen=True`` frames into ``GazeAwayEvent`` objects
with the 800 ms minimum-duration filter and the escalation ladder.

**All thresholds are constructor arguments with documented defaults.**
They are *not* hardcoded: callers (the orchestration layer) pass values
from ``PolicyConfig`` or from deployment-environment calibration.

**Correction (2026-07-24):** the original design referenced
``mp.solutions.face_mesh``, which no longer exists in ``mediapipe``
0.10.31+.  The Tasks API ``FaceLandmarker`` is the direct replacement
— same 478 iris-refined landmarks, same blendshape output.

Input:  a :class:`~proctoring_engine.preprocessing.frames.DecodedFrame`
        in **RGB** channel order.
Output: a :class:`HeadPoseGazeResult`.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Final

import cv2
import numpy as np

from proctoring_engine.inference._types import (
    ConfidenceInterval,
    HeadPoseGazeResult,
)
from proctoring_engine.preprocessing.frames import DecodedFrame


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MP_FACE_LANDMARKER_BUNDLE_ENV: Final[str] = "MP_FACE_LANDMARKER_BUNDLE"
"""Environment variable pointing to the ``.task`` model bundle."""

_MODALITY: Final[str] = "gaze"

EVENT_ON_SCREEN: Final[str] = "on_screen"
EVENT_OFF_SCREEN: Final[str] = "off_screen"
EVENT_NO_LANDMARKS: Final[str] = "no_landmarks"

# --- Landmark indices (MediaPipe 478-point mesh, 0-indexed) ---

# Canonical 6-point face model for solvePnP.
#   nose tip, chin, left eye outer corner, right eye outer corner,
#   left mouth corner, right mouth corner.
_SOLVEPNP_LANDMARK_IDS: Final[tuple[int, ...]] = (1, 152, 33, 263, 61, 291)

# 3D model points (approximate, in mm relative to face centre).
# These are the widely-used reference coordinates for a generic face;
# the exact scale doesn't matter because solvePnP recovers angles, not
# absolute distances.
_FACE_3D_MODEL: Final[np.ndarray] = np.array([
    [0.0, 0.0, 0.0],         # nose tip
    [0.0, -330.0, -65.0],    # chin
    [-225.0, 170.0, -135.0], # left eye outer corner
    [225.0, 170.0, -135.0],  # right eye outer corner
    [-150.0, -150.0, -125.0],  # left mouth corner
    [150.0, -150.0, -125.0],  # right mouth corner
], dtype=np.float64)

# Left eye contour landmark indices (for EAR).
# Vertical: (160, 144), (158, 153).  Horizontal: (33, 133).
_LEFT_EYE_IDS: Final[tuple[int, ...]] = (33, 133, 160, 144, 158, 153)

# Right eye contour landmark indices (for EAR).
# Vertical: (387, 373), (385, 380).  Horizontal: (362, 263).
_RIGHT_EYE_IDS: Final[tuple[int, ...]] = (362, 263, 387, 373, 385, 380)

# Iris landmark indices (MediaPipe iris-refined mesh).
# Left iris: 468, 469, 470, 471.  Right iris: 473, 474, 475, 476.
_LEFT_IRIS_IDS: Final[tuple[int, ...]] = (468, 469, 470, 471)
_RIGHT_IRIS_IDS: Final[tuple[int, ...]] = (473, 474, 475, 476)


# ---------------------------------------------------------------------------
# Pure geometric helpers (unit-testable without a model)
# ---------------------------------------------------------------------------

def compute_ear(landmarks: np.ndarray, eye_ids: tuple[int, ...]) -> float:
    """Compute the Eye Aspect Ratio for one eye.

    EAR = (||p2-p6|| + ||p3-p5||) / (2 * ||p1-p4||)

    where p1 and p4 are the horizontal (outer, inner) eye corners,
    and p2/p6, p3/p5 are vertical pairs.  EAR ≈ 0.2–0.3 for an open
    eye and drops toward 0 during a blink.

    Parameters
    ----------
    landmarks:
        An (N, 3) array of normalised landmark coordinates.
    eye_ids:
        The six landmark indices in the order
        ``(outer_corner, inner_corner, upper1, lower1, upper2, lower2)``.

    Returns
    -------
    float
        The EAR value, ≥ 0.  Returns 0.0 on degenerate geometry
        (horizontal distance is zero).
    """

    p1 = landmarks[eye_ids[0]][:2]  # outer corner
    p4 = landmarks[eye_ids[1]][:2]  # inner corner
    p2 = landmarks[eye_ids[2]][:2]  # upper 1
    p6 = landmarks[eye_ids[3]][:2]  # lower 1
    p3 = landmarks[eye_ids[4]][:2]  # upper 2
    p5 = landmarks[eye_ids[5]][:2]  # lower 2

    vertical_1 = float(np.linalg.norm(p2 - p6))
    vertical_2 = float(np.linalg.norm(p3 - p5))
    horizontal = float(np.linalg.norm(p1 - p4))

    if horizontal == 0.0:
        return 0.0
    return (vertical_1 + vertical_2) / (2.0 * horizontal)


def compute_iris_offset(
    landmarks: np.ndarray,
    iris_ids: tuple[int, ...],
    eye_outer_id: int,
    eye_inner_id: int,
) -> float:
    """Compute the horizontal iris offset relative to eye width.

    The offset is the distance from the iris centroid to the
    midpoint of the eye corners, divided by the eye width.  A value
    near 0 means the iris is centred; a value > 0.3 typically means
    the person is looking to one side.

    Returns
    -------
    float
        Offset ratio, ≥ 0.  Returns 0.0 on degenerate geometry.
    """

    iris_pts = landmarks[list(iris_ids)][:, :2]
    iris_centroid = iris_pts.mean(axis=0)

    outer = landmarks[eye_outer_id][:2]
    inner = landmarks[eye_inner_id][:2]
    midpoint = (outer + inner) / 2.0
    eye_width = float(np.linalg.norm(outer - inner))

    if eye_width == 0.0:
        return 0.0

    return float(np.linalg.norm(iris_centroid - midpoint)) / eye_width


def compute_head_pose(
    landmarks: np.ndarray,
    image_width: int,
    image_height: int,
) -> tuple[float, float, float]:
    """Estimate yaw, pitch, roll (degrees) via ``cv2.solvePnP``.

    Parameters
    ----------
    landmarks:
        An (N, 3) array of normalised landmark coordinates from
        MediaPipe.  Only the six canonical points are used.
    image_width, image_height:
        Frame dimensions in pixels (needed for the camera matrix).

    Returns
    -------
    tuple[float, float, float]
        ``(yaw, pitch, roll)`` in degrees.  Yaw > 0 means looking
        to the camera's right (the subject's left).
    """

    # Extract the 6 canonical 2D points in pixel coordinates.
    image_points = np.array(
        [
            [landmarks[idx][0] * image_width, landmarks[idx][1] * image_height]
            for idx in _SOLVEPNP_LANDMARK_IDS
        ],
        dtype=np.float64,
    )

    # Approximate camera matrix (no lens distortion).
    focal_length = float(image_width)
    centre = (image_width / 2.0, image_height / 2.0)
    camera_matrix = np.array(
        [
            [focal_length, 0.0, centre[0]],
            [0.0, focal_length, centre[1]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)

    success, rotation_vec, _ = cv2.solvePnP(
        _FACE_3D_MODEL,
        image_points,
        camera_matrix,
        dist_coeffs,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not success:
        return (0.0, 0.0, 0.0)

    rotation_mat, _ = cv2.Rodrigues(rotation_vec)

    # Decompose rotation matrix into Euler angles.
    sy = math.sqrt(rotation_mat[0, 0] ** 2 + rotation_mat[1, 0] ** 2)
    singular = sy < 1e-6
    if not singular:
        pitch = math.atan2(rotation_mat[2, 1], rotation_mat[2, 2])
        yaw = math.atan2(-rotation_mat[2, 0], sy)
        roll = math.atan2(rotation_mat[1, 0], rotation_mat[0, 0])
    else:
        pitch = math.atan2(-rotation_mat[1, 2], rotation_mat[1, 1])
        yaw = math.atan2(-rotation_mat[2, 0], sy)
        roll = 0.0

    return (
        math.degrees(yaw),
        math.degrees(pitch),
        math.degrees(roll),
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class FaceLandmarkerRunner:
    """Stateless per-frame head-pose / gaze classifier.

    Parameters
    ----------
    model_bundle_path:
        Path to the ``.task`` model bundle.  Falls back to the
        ``MP_FACE_LANDMARKER_BUNDLE`` environment variable.
    yaw_threshold_deg:
        Absolute yaw angle (degrees) beyond which the face is
        classified ``off_screen``.  Default 30° — a calibration
        starting point, not a final answer.
    high_yaw_bypass_deg:
        Absolute yaw angle (degrees) beyond which the face is
        classified ``off_screen`` regardless of the EAR blink filter.
        Default 45°. At extreme rotation, the eye-state question is
        moot and foreshortening artificially lowers EAR.
    iris_offset_threshold:
        Iris offset ratio beyond which the eyes are classified as
        looking to the side.  Default 0.35.
    ear_blink_threshold:
        EAR below which the eyes are considered closed (blink) and
        the frame is classified ``on_screen`` regardless of other
        signals.  Default 0.20.

    Raises
    ------
    EnvironmentError
        If no model path is available.
    FileNotFoundError
        If the resolved path does not exist.
    """

    def __init__(
        self,
        *,
        model_bundle_path: str | None = None,
        yaw_threshold_deg: float = 30.0,
        high_yaw_bypass_deg: float = 45.0,
        iris_offset_threshold: float = 0.35,
        ear_blink_threshold: float = 0.20,
    ) -> None:
        if model_bundle_path is None:
            model_bundle_path = os.environ.get(MP_FACE_LANDMARKER_BUNDLE_ENV)
        if model_bundle_path is None:
            raise EnvironmentError(
                f"The {MP_FACE_LANDMARKER_BUNDLE_ENV} environment variable "
                f"is not set and no model_bundle_path was provided."
            )
        if not os.path.isfile(model_bundle_path):
            raise FileNotFoundError(
                f"MediaPipe FaceLandmarker model bundle not found at "
                f"'{model_bundle_path}'."
            )

        self._yaw_threshold = yaw_threshold_deg
        self._high_yaw_bypass = high_yaw_bypass_deg
        self._iris_threshold = iris_offset_threshold
        self._ear_threshold = ear_blink_threshold

        import mediapipe as mp

        base_options = mp.tasks.BaseOptions(
            model_asset_path=model_bundle_path,
        )
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=base_options,
            output_face_blendshapes=True,
            num_faces=1,
        )
        self._landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)

    def run(self, frame: DecodedFrame) -> HeadPoseGazeResult:
        """Classify a single frame for head pose / gaze direction.

        Parameters
        ----------
        frame:
            A :class:`DecodedFrame` in **RGB** channel order.

        Returns
        -------
        HeadPoseGazeResult
            ``event_type`` is ``"on_screen"`` or ``"off_screen"``.
        """

        import mediapipe as mp

        mp_image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=frame.array,
        )
        result = self._landmarker.detect(mp_image)

        if not result.face_landmarks:
            # No face detected. Treat as a distinct no_landmarks state,
            # NOT off_screen=True. The face-presence module independently
            # raises `no_face` for this frame.  Classifying this as
            # off_screen would double-count the absence toward the gaze
            # escalation ladder on top of the face-presence absence.
            return HeadPoseGazeResult(
                modality=_MODALITY,
                event_type=EVENT_NO_LANDMARKS,
                confidence=ConfidenceInterval(lower=1.0, score=1.0, upper=1.0),
                raw_value={"reason": "no_landmarks"},
                off_screen=None,
            )

        # MediaPipe returns landmark coordinates normalised to [0,1].
        face_lms = result.face_landmarks[0]
        landmarks = np.array(
            [[lm.x, lm.y, lm.z] for lm in face_lms],
            dtype=np.float64,
        )

        # 1. Head pose.
        yaw, pitch, roll = compute_head_pose(
            landmarks, frame.width, frame.height
        )

        # 2. EAR (averaged over both eyes).
        ear_left = compute_ear(landmarks, _LEFT_EYE_IDS)
        ear_right = compute_ear(landmarks, _RIGHT_EYE_IDS)
        ear = (ear_left + ear_right) / 2.0

        # 3. Iris offset (averaged over both eyes).
        offset_left = compute_iris_offset(
            landmarks, _LEFT_IRIS_IDS,
            eye_outer_id=_LEFT_EYE_IDS[0],
            eye_inner_id=_LEFT_EYE_IDS[1],
        )
        offset_right = compute_iris_offset(
            landmarks, _RIGHT_IRIS_IDS,
            eye_outer_id=_RIGHT_EYE_IDS[0],
            eye_inner_id=_RIGHT_EYE_IDS[1],
        )
        iris_offset = (offset_left + offset_right) / 2.0

        # Classification.
        eyes_open = ear >= self._ear_threshold
        yaw_off = abs(yaw) > self._yaw_threshold
        iris_off = iris_offset > self._iris_threshold
        high_yaw_bypass = abs(yaw) > self._high_yaw_bypass

        # The blink filter (eyes_open) prevents a closed-eye frame from
        # mis-classifying as looking away. But at extreme yaw, natural
        # foreshortening of the projected eye contour lowers EAR even
        # with eyes genuinely open. The high-yaw bypass overrides the
        # blink filter when the head is rotated so far that the
        # eye-state question is moot.
        if high_yaw_bypass:
            off_screen = True
        else:
            off_screen = eyes_open and (yaw_off or iris_off)

        event_type = EVENT_OFF_SCREEN if off_screen else EVENT_ON_SCREEN

        # Confidence: a rough heuristic — the further off-axis the
        # signal, the higher the confidence in the classification.
        # For on-screen, confidence is 1 minus the normalised
        # deviation from centre.
        yaw_ratio = min(abs(yaw) / max(self._yaw_threshold, 1.0), 1.0)
        if off_screen:
            conf_score = max(yaw_ratio, min(iris_offset / max(self._iris_threshold, 0.01), 1.0))
        else:
            conf_score = 1.0 - yaw_ratio

        conf_score = max(0.0, min(1.0, conf_score))
        confidence = ConfidenceInterval(
            lower=conf_score, score=conf_score, upper=conf_score,
        )

        return HeadPoseGazeResult(
            modality=_MODALITY,
            event_type=event_type,
            confidence=confidence,
            raw_value={
                "yaw": yaw,
                "pitch": pitch,
                "roll": roll,
                "ear": ear,
                "iris_offset": iris_offset,
                "yaw_threshold": self._yaw_threshold,
                "iris_threshold": self._iris_threshold,
                "ear_threshold": self._ear_threshold,
                # Landmarks as a list of (x, y, z) tuples in [0,1]
                # normalised image coordinates.  Required by the
                # identity-match dispatcher (it needs the bbox to
                # crop the face before passing it to the dlib
                # embedder); included for all frames so the
                # dispatcher's cropping logic is uniform.
                "landmarks": [
                    (float(lm[0]), float(lm[1]), float(lm[2]))
                    for lm in landmarks
                ],
            },
            off_screen=off_screen,
            yaw=yaw,
            pitch=pitch,
            roll=roll,
            ear=ear,
            iris_offset=iris_offset,
        )

    def close(self) -> None:
        """Release the underlying MediaPipe landmarker resources."""
        self._landmarker.close()

    def __enter__(self) -> FaceLandmarkerRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
