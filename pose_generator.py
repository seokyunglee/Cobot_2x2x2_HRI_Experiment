from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

# 손목에서 드릴 끝/TCP까지의 보정 길이는 사용자별 입력을 받지 않고 0.21 m로 고정한다.
DRILL_TCP_OFFSET_M = 0.21

# The target shoulder angle is the camera's torso-shoulder-elbow angle.  The
# forearm/drill retains a measured baseline headward fold and flexes further
# as that target decreases.
ELBOW_STRAIGHT_SHOULDER_DEG = 130.0
ELBOW_FULL_FLEX_SHOULDER_DEG = 70.0
BASE_ELBOW_FLEXION_DEG = 7.5
MAX_ELBOW_FLEXION_DEG = 20.0
ELBOW_FLEXION_EXPONENT = 2.0

# 6차 측정 데이터 기준 바닥 높이 변환 상수다.
BASE_HEIGHT_M = 0.661

MeasuredPoseSample = tuple[
    float,
    tuple[float, float, float],
    tuple[float, float, float, float],
]

# 6차 측정에서 확보한 IK-feasible TCP pose lookup이다.
# 각 항목은 (바닥 기준 높이 H, link0 기준 position xyz, orientation quaternion xyzw) 형식이다.
MEASURED_POSES_6TH: tuple[MeasuredPoseSample, ...] = (
    (0.7152, (0.770, -0.077, 0.054), (0.330, 0.399, 0.723, 0.457)),  # 50 deg
    (0.7692, (0.735, -0.092, 0.108), (0.348, 0.393, 0.723, 0.449)),  # 60 deg
    (0.9022, (0.672, -0.078, 0.241), (0.348, 0.412, 0.720, 0.438)),  # 70 deg
    (1.0402, (0.729, -0.103, 0.379), (0.292, 0.464, 0.731, 0.406)),  # 80 deg
    (1.1252, (0.665, -0.097, 0.464), (0.330, 0.512, 0.713, 0.347)),  # 90 deg
    (1.2542, (0.598, -0.080, 0.593), (0.362, 0.491, 0.682, 0.404)),  # 100 deg
    (1.4822, (0.456, -0.128, 0.821), (0.423, 0.609, 0.613, 0.274)),  # 110 deg
    (1.5612, (0.431, -0.152, 0.900), (0.464, 0.638, 0.561, 0.254)),  # 120 deg
    (1.6692, (0.361, -0.084, 1.008), (0.499, 0.652, 0.490, 0.294)),  # 130 deg
)
MEASURED_POSES_5TH: tuple[MeasuredPoseSample, ...] = (
    (0.7122, (0.766, -0.103, 0.051), (0.490, 0.327, 0.793, 0.152)),
    (0.7942, (0.777, -0.090, 0.133), (0.455, 0.252, 0.839, 0.161)),
    (0.8882, (0.707, -0.100, 0.227), (0.502, 0.357, 0.770, 0.164)),
    (0.9442, (0.631, -0.002, 0.283), (0.390, 0.437, 0.707, 0.396)),
    (1.0182, (0.658, -0.058, 0.357), (0.386, 0.367, 0.696, 0.482)),
    (1.1062, (0.618, -0.159, 0.445), (0.351, 0.684, 0.518, 0.376)),
    (1.1722, (0.564, -0.110, 0.511), (0.294, 0.731, 0.403, 0.465)),
    (1.2692, (0.518, -0.103, 0.608), (0.184, 0.752, 0.342, 0.533)),
    (1.3642, (0.487, -0.048, 0.703), (0.434, 0.472, 0.632, 0.436)),
    (1.4682, (0.466, -0.142, 0.807), (0.246, 0.768, 0.359, 0.470)),
    (1.5242, (0.511, -0.148, 0.863), (0.266, 0.795, 0.377, 0.473)),
    (1.6412, (0.467, -0.143, 0.980), (0.196, 0.777, 0.320, 0.506)),
    (1.6802, (0.337, -0.094, 1.019), (0.126, 0.804, 0.305, 0.495)),
)

# 로봇 link0 기준 TCP z 사용 가능 범위다. 6차 측정 lookup 범위와 일치한다.
MIN_LINK0_Z_M = 0.054
MAX_LINK0_Z_M = 1.008


def expected_elbow_flexion_deg(target_shoulder_angle_deg: float) -> float:
    """Return modeled headward elbow flexion for a target upper-arm angle."""
    angle_deg = float(target_shoulder_angle_deg)
    ratio = (
        (ELBOW_STRAIGHT_SHOULDER_DEG - angle_deg)
        / (ELBOW_STRAIGHT_SHOULDER_DEG - ELBOW_FULL_FLEX_SHOULDER_DEG)
    )
    ratio = max(0.0, min(1.0, ratio))
    return BASE_ELBOW_FLEXION_DEG + (
        MAX_ELBOW_FLEXION_DEG - BASE_ELBOW_FLEXION_DEG
    ) * (ratio ** ELBOW_FLEXION_EXPONENT)


def expected_elbow_angle_deg(target_shoulder_angle_deg: float) -> float:
    """Return the model elbow inner angle corresponding to its flexion model."""
    return 180.0 - expected_elbow_flexion_deg(target_shoulder_angle_deg)


@dataclass(frozen=True)
class HumanArmProfile:
    """수동 입력으로 받은 작업자 신체 치수를 저장한다."""

    user_height_m: float
    shoulder_height_m: float
    upper_arm_m: float
    forearm_m: float
    drill_tcp_offset_m: float = DRILL_TCP_OFFSET_M

    @property
    def total_arm_length_m(self) -> float:
        # 이 모델에서 전체 팔 길이 L은 상완 + 하완 + 손목~드릴/TCP 보정 길이다.
        return self.upper_arm_m + self.forearm_m + self.drill_tcp_offset_m

    @property
    def user_height_cm(self) -> float:
        return self.user_height_m * 100.0

    @property
    def shoulder_height_cm(self) -> float:
        return self.shoulder_height_m * 100.0

    @property
    def upper_arm_cm(self) -> float:
        return self.upper_arm_m * 100.0

    @property
    def forearm_cm(self) -> float:
        return self.forearm_m * 100.0

    @property
    def drill_tcp_offset_cm(self) -> float:
        return self.drill_tcp_offset_m * 100.0


@dataclass(frozen=True)
class TcpPoseResult:
    """생성된 link0 기준 TCP pose 결과."""

    requested_floor_height_m: float
    target_floor_height_m: float
    frame_id: str
    x_m: float
    y_m: float
    z_m: float
    qx: float
    qy: float
    qz: float
    qw: float
    shoulder_height_m: float
    total_arm_length_m: float
    was_height_clamped: bool

    def to_pose_dict(self) -> dict[str, Any]:
        """로봇 수신부가 읽을 TCP pose dict를 만든다."""
        return {
            "frame_id": self.frame_id,
            "position": {
                "x": self.x_m,
                "y": self.y_m,
                "z": self.z_m,
            },
            "orientation": {
                "x": self.qx,
                "y": self.qy,
                "z": self.qz,
                "w": self.qw,
            },
        }

    def to_pass_goal_dict(self, msg: str = "") -> dict[str, Any]:
        """HTTP sender에 넘길 JSON-ready dict를 만든다."""
        return {
            "msg": msg,
            "target_type": "tcp_pose",
            "target_floor_z_m": self.target_floor_height_m,
            "target_floor_z_mm": self.target_floor_height_m * 1000.0,
            "target_pose": self.to_pose_dict(),
            "model_info": {
                "model": "human_aware_ik_feasible_pose_generator_5th",
                "requested_floor_z_m": self.requested_floor_height_m,
                "was_height_clamped": self.was_height_clamped,
                "shoulder_height_m": self.shoulder_height_m,
                "total_arm_length_m": self.total_arm_length_m,
                "drill_tcp_offset_m": DRILL_TCP_OFFSET_M,
            },
        }

    def to_json_string(self, msg: str = "") -> str:
        """저장이나 디버그 출력이 필요할 때 JSON 문자열로 변환한다."""
        return json.dumps(self.to_pass_goal_dict(msg=msg), ensure_ascii=False)


class HumanAwareTcpPoseGenerator:
    """
    사람 기준 목표 높이 H를 link0 기준 TCP pose로 바꾸는 생성기.

    모델은 두 단계다.
    1. 사람 모델: 어깨각, 어깨높이, 팔 길이로 바닥 기준 목표 높이 H를 계산한다.
    2. 로봇 모델: 6차 측정 IK-feasible pose lookup에서 H 기준 TCP pose를 보간한다.
    """

    def __init__(
        self,
        base_height_m: float = BASE_HEIGHT_M,
        measured_pose_samples: tuple[MeasuredPoseSample, ...] = MEASURED_POSES_5TH,
    ) -> None:
        self.base_height_m = base_height_m
        self.measured_pose_samples = tuple(sorted(measured_pose_samples, key=lambda sample: sample[0]))
        if len(self.measured_pose_samples) < 2:
            raise ValueError("pose 보간에는 최소 2개 이상의 측정 샘플이 필요합니다.")
        self.min_link0_z_m = self.measured_pose_samples[0][1][2]
        self.max_link0_z_m = self.measured_pose_samples[-1][1][2]

    @property
    def min_floor_height_m(self) -> float:
        return self.measured_pose_samples[0][0]

    @property
    def max_floor_height_m(self) -> float:
        return self.measured_pose_samples[-1][0]

    def floor_height_from_shoulder_angle(
        self,
        profile: HumanArmProfile,
        target_shoulder_angle_deg: float,
    ) -> float:
        """
        목표 어깨각을 바닥 기준 목표 높이 H로 변환한다.

        상완과 하완+드릴을 2-link로 계산하며, 하완+드릴은 목표 상완각이
        낮아질수록 머리 방향으로 점차 굽힌다. 반환 H는 이후 TCP pose 생성의
        입력 높이다.
        """
        return self.floor_height_from_shoulder_angle_with_elbow_angle(
            profile=profile,
            target_shoulder_angle_deg=target_shoulder_angle_deg,
            elbow_angle_deg=expected_elbow_angle_deg(target_shoulder_angle_deg),
        )

    def floor_height_from_shoulder_angle_with_elbow_angle(
        self,
        profile: HumanArmProfile,
        target_shoulder_angle_deg: float,
        elbow_angle_deg: float,
    ) -> float:
        """Convert a shoulder angle and elbow inner angle to floor-reference H."""
        # H = H_s - L_upper*cos(theta) - L_forearm_drill*cos(theta + elbow_flexion)
        shoulder_angle_rad = math.radians(float(target_shoulder_angle_deg))
        elbow_flexion_rad = math.radians(180.0 - float(elbow_angle_deg))
        forearm_and_drill_m = profile.forearm_m + profile.drill_tcp_offset_m
        vertical_reach_m = (
            profile.upper_arm_m * math.cos(shoulder_angle_rad)
            + forearm_and_drill_m * math.cos(shoulder_angle_rad + elbow_flexion_rad)
        )
        return profile.shoulder_height_m - vertical_reach_m

    def shoulder_angle_from_floor_height(
        self,
        profile: HumanArmProfile,
        floor_height_m: float,
    ) -> float:
        """Convert a floor-reference work height to the model shoulder angle."""
        target_height_m = float(floor_height_m)
        low_deg = 0.0
        high_deg = 180.0
        low_height_m = self.floor_height_from_shoulder_angle(profile, low_deg)
        high_height_m = self.floor_height_from_shoulder_angle(profile, high_deg)

        if target_height_m <= low_height_m:
            return low_deg
        if target_height_m >= high_height_m:
            return high_deg

        for _ in range(48):
            mid_deg = (low_deg + high_deg) / 2.0
            mid_height_m = self.floor_height_from_shoulder_angle(profile, mid_deg)
            if mid_height_m < target_height_m:
                low_deg = mid_deg
            else:
                high_deg = mid_deg

        return (low_deg + high_deg) / 2.0

    def generate_pose_from_shoulder_angle(
        self,
        target_shoulder_angle_deg: float,
        profile: HumanArmProfile,
        frame_id: str = "link0",
        clamp_to_robot_range: bool = True,
    ) -> TcpPoseResult:
        """목표 어깨각에서 H를 계산한 뒤 TCP pose까지 생성한다."""
        target_floor_height_m = self.floor_height_from_shoulder_angle(
            profile=profile,
            target_shoulder_angle_deg=target_shoulder_angle_deg,
        )
        return self.generate_pose_from_floor_height(
            target_floor_height_m=target_floor_height_m,
            profile=profile,
            frame_id=frame_id,
            clamp_to_robot_range=clamp_to_robot_range,
        )

    def generate_pose_from_floor_height(
        self,
        target_floor_height_m: float,
        profile: HumanArmProfile,
        frame_id: str = "link0",
        clamp_to_robot_range: bool = True,
    ) -> TcpPoseResult:
        """바닥 기준 목표 높이 H를 link0 기준 TCP pose로 변환한다."""
        requested_height_m = float(target_floor_height_m)
        height_m = (
            self._clamp_floor_height(requested_height_m)
            if clamp_to_robot_range
            else requested_height_m
        )
        was_height_clamped = abs(height_m - requested_height_m) > 1e-9

        total_arm_length_m = profile.total_arm_length_m
        shoulder_height_m = profile.shoulder_height_m
        x_m, y_m, z_m, qx, qy, qz, qw = self.interpolate_measured_pose(height_m)

        return TcpPoseResult(
            requested_floor_height_m=requested_height_m,
            target_floor_height_m=height_m,
            frame_id=frame_id,
            x_m=x_m,
            y_m=y_m,
            z_m=z_m,
            qx=qx,
            qy=qy,
            qz=qz,
            qw=qw,
            shoulder_height_m=shoulder_height_m,
            total_arm_length_m=total_arm_length_m,
            was_height_clamped=was_height_clamped,
        )

    def build_pass_goal_dict_from_shoulder_angle(
        self,
        target_shoulder_angle_deg: float,
        profile: HumanArmProfile,
        msg: str = "",
        frame_id: str = "link0",
    ) -> dict[str, Any]:
        """
        목표 어깨각에서 바로 HTTP 전송용 dict를 만든다.

        메인 통합 파일에서는 기본적으로 이 함수를 쓰면 된다.
        LLM/rule이 어깨 목표각을 정하면, 이 함수가 H 계산과 TCP pose 생성을 이어서 처리한다.
        """
        pose = self.generate_pose_from_shoulder_angle(
            target_shoulder_angle_deg=target_shoulder_angle_deg,
            profile=profile,
            frame_id=frame_id,
        )
        return pose.to_pass_goal_dict(msg=msg)

    def build_pass_goal_dict_from_floor_height(
        self,
        target_floor_height_m: float,
        profile: HumanArmProfile,
        msg: str = "",
        frame_id: str = "link0",
    ) -> dict[str, Any]:
        """
        바닥 기준 목표 높이 H에서 HTTP 전송용 dict를 만든다.

        예비용 함수다. 초기 위치처럼 목표 높이 H가 이미 정해져 있을 때만 사용한다.
        """
        pose = self.generate_pose_from_floor_height(
            target_floor_height_m=target_floor_height_m,
            profile=profile,
            frame_id=frame_id,
        )
        return pose.to_pass_goal_dict(msg=msg)

    def build_pass_goal_dict_from_floor_height_mm(
        self,
        target_floor_height_mm: float,
        profile: HumanArmProfile,
        msg: str = "",
        frame_id: str = "link0",
    ) -> dict[str, Any]:
        """
        현재 main의 mm 단위 높이와 연결하기 위한 예비용 편의 함수.

        최종 통합에서 main의 높이 단위를 m로 정리하면 없어져도 된다.
        """
        return self.build_pass_goal_dict_from_floor_height(
            target_floor_height_m=target_floor_height_mm / 1000.0,
            profile=profile,
            msg=msg,
            frame_id=frame_id,
        )

    def interpolate_quaternion(self, target_floor_height_m: float) -> tuple[float, float, float, float]:
        """목표 높이 H에 가장 가까운 두 6차 측정 quaternion을 slerp로 보간한다."""
        lower, upper, alpha = self._find_measured_pose_interval(target_floor_height_m)
        return _slerp(lower[2], upper[2], alpha)

    def interpolate_measured_pose(
        self,
        target_floor_height_m: float,
    ) -> tuple[float, float, float, float, float, float, float]:
        """목표 높이 H에서 6차 측정 position과 orientation을 보간한다."""
        height_m = float(target_floor_height_m)
        lower, upper, alpha = self._find_measured_pose_interval(height_m)

        x_m = _lerp(lower[1][0], upper[1][0], alpha)
        y_m = _lerp(lower[1][1], upper[1][1], alpha)
        z_m = height_m - self.base_height_m
        qx, qy, qz, qw = _slerp(lower[2], upper[2], alpha)
        return x_m, y_m, z_m, qx, qy, qz, qw

    def _find_measured_pose_interval(
        self,
        target_floor_height_m: float,
    ) -> tuple[MeasuredPoseSample, MeasuredPoseSample, float]:
        height_m = float(target_floor_height_m)
        samples = self.measured_pose_samples

        if height_m <= samples[0][0]:
            return samples[0], samples[0], 0.0
        if height_m >= samples[-1][0]:
            return samples[-1], samples[-1], 0.0

        for lower, upper in zip(samples, samples[1:]):
            if lower[0] <= height_m <= upper[0]:
                interval = upper[0] - lower[0]
                alpha = 0.0 if interval == 0 else (height_m - lower[0]) / interval
                return lower, upper, alpha

        return samples[-1], samples[-1], 0.0

    def _clamp_floor_height(self, target_floor_height_m: float) -> float:
        return max(self.min_floor_height_m, min(self.max_floor_height_m, target_floor_height_m))


def prompt_human_arm_profile() -> HumanArmProfile:
    """
    기존 main_integrated.py의 신체 치수 입력 로직을 옮겨오기 위한 함수.

    손목~드릴/TCP 보정 길이는 사용자에게 묻지 않고 0.21 m로 고정한다.
    """
    print("\n" + "=" * 60)
    print(" 실험자 신체 정보 입력 (Enter를 누르면 기본값 적용)")
    print("=" * 60)

    user_height_cm = _prompt_float(" 1. 작업자 키(cm) [기본: 175.0]: ", 175.0)
    default_shoulder_height_cm = user_height_cm - 30.0
    shoulder_height_cm = _prompt_float(
        f" 2. 어깨 높이(cm) [기본: {default_shoulder_height_cm:.1f}]: ",
        default_shoulder_height_cm,
    )
    upper_arm_cm = _prompt_float(" 3. 상완 길이(어깨~팔꿈치, cm) [기본: 30.0]: ", 30.0)
    forearm_cm = _prompt_float(" 4. 하완 길이(팔꿈치~손목, cm) [기본: 25.0]: ", 25.0)

    profile = HumanArmProfile(
        user_height_m=user_height_cm / 100.0,
        shoulder_height_m=shoulder_height_cm / 100.0,
        upper_arm_m=upper_arm_cm / 100.0,
        forearm_m=forearm_cm / 100.0,
    )
    print(
        "\n [적용 완료] "
        f"키: {profile.user_height_cm:.1f}cm | "
        f"어깨 높이: {profile.shoulder_height_cm:.1f}cm | "
        f"상완: {profile.upper_arm_cm:.1f}cm | "
        f"하완: {profile.forearm_cm:.1f}cm | "
        f"손목~드릴/TCP: {profile.drill_tcp_offset_cm:.1f}cm 고정"
    )
    return profile


def make_human_arm_profile(
    shoulder_height_cm: float,
    upper_arm_cm: float,
    forearm_cm: float,
    user_height_cm: float = 175.0,
) -> HumanArmProfile:
    """main에서 이미 입력받은 값으로 HumanArmProfile을 만들 때 쓴다."""
    return HumanArmProfile(
        user_height_m=user_height_cm / 100.0,
        shoulder_height_m=shoulder_height_cm / 100.0,
        upper_arm_m=upper_arm_cm / 100.0,
        forearm_m=forearm_cm / 100.0,
    )


def _prompt_float(prompt: str, default_value: float) -> float:
    try:
        raw_value = input(prompt)
        return float(raw_value) if raw_value.strip() else default_value
    except Exception:
        return default_value


def _lerp(start: float, end: float, alpha: float) -> float:
    return (1.0 - alpha) * start + alpha * end


def _normalize_quaternion(
    quaternion_xyzw: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    norm = math.sqrt(sum(value * value for value in quaternion_xyzw))
    if norm == 0:
        raise ValueError("0 길이 quaternion은 사용할 수 없습니다.")
    return tuple(value / norm for value in quaternion_xyzw)


def _slerp(
    q1_xyzw: tuple[float, float, float, float],
    q2_xyzw: tuple[float, float, float, float],
    alpha: float,
) -> tuple[float, float, float, float]:
    """두 quaternion 사이를 구면 선형 보간한다."""
    q1 = _normalize_quaternion(q1_xyzw)
    q2 = _normalize_quaternion(q2_xyzw)
    alpha = max(0.0, min(1.0, alpha))

    dot = sum(a * b for a, b in zip(q1, q2))
    if dot < 0.0:
        # 같은 회전을 더 짧은 경로로 보간하기 위한 처리다.
        q2 = tuple(-value for value in q2)
        dot = -dot

    if dot > 0.9995:
        # 두 quaternion이 거의 같으면 slerp 대신 일반 선형 보간을 쓴다.
        blended = tuple((1.0 - alpha) * a + alpha * b for a, b in zip(q1, q2))
        return _normalize_quaternion(blended)

    theta_0 = math.acos(max(-1.0, min(1.0, dot)))
    sin_theta_0 = math.sin(theta_0)
    theta = theta_0 * alpha
    sin_theta = math.sin(theta)

    scale_1 = math.cos(theta) - dot * sin_theta / sin_theta_0
    scale_2 = sin_theta / sin_theta_0
    return tuple(scale_1 * a + scale_2 * b for a, b in zip(q1, q2))
