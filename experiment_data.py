from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from typing import Any


SHOULDER_ANGLE_BIN_DEG = 2.0


@dataclass
class PostureSample:
    """자세 측정 파일이 한 프레임에서 계산한 현재 자세값."""

    shoulder_angle_deg: float | None
    elbow_angle_deg: float | None
    rula_proxy: float | None
    visibility_ok: bool
    side: str


@dataclass
class CycleResult:
    """한 작업 cycle이 끝났을 때 metrics가 계산한 자세/위험 결과."""

    task_time_s: float
    rest_time_s: float
    working_time_s: float
    rest_ratio: float
    max_continuous_rest_time_s: float
    safe_time_s: float
    risky_time_s: float
    risky_ratio: float
    is_risky_cycle: bool
    visibility_ok_time_s: float
    visibility_ok_ratio: float
    representative_shoulder_angle_deg: float
    working_representative_shoulder_angle_deg: float
    avg_shoulder_angle_deg: float
    working_avg_shoulder_angle_deg: float
    avg_elbow_angle_deg: float
    working_avg_elbow_angle_deg: float
    avg_rula_proxy: float
    max_rula_proxy: float
    rula_high_time_s: float
    rula_high_ratio: float
    shoulder_samples: list[tuple[float, float]]


@dataclass
class TrialRecord:
    """raw CSV 한 줄에 저장할 trial 단위 기록."""

    timestamp: str
    condition_name: str
    trial_num: int
    lead_type: str
    control_type: str
    measured_side: str
    risk_shoulder_threshold_deg: float
    rest_shoulder_threshold_deg: float
    rest_abandonment_threshold_s: float
    safe_shoulder_min_deg: float
    safe_shoulder_max_deg: float
    user_height_cm: float
    shoulder_height_cm: float
    upper_arm_cm: float
    forearm_cm: float
    drill_tcp_offset_cm: float
    cycle: CycleResult
    representative_shoulder_angle_change_deg: float | None
    working_representative_shoulder_angle_change_deg: float | None
    avg_rula_proxy_change: float | None
    rest_time_change_s: float | None
    safe_time_change_s: float | None
    risky_time_change_s: float | None
    task_time_change_s: float | None
    working_time_change_s: float | None
    target_shoulder_angle_deg: float | None
    final_target_shoulder_angle_deg: float
    model_elbow_angle_deg: float
    model_elbow_h_m: float
    working_elbow_h_m: float | None
    angle_adjustment_deg: float | None
    target_angle_source: str
    response_action: str
    response_source: str
    llm_confidence: float
    decision_reason: str
    llm_fallback: bool
    prev_z_mm: float
    final_z_mm: float
    adjustment_z_mm: float
    user_voice: str
    final_z_m: float
    pose_height_clamped: bool
    robot_command_sent: bool
    is_approved: bool
    llm_latency_s: float
    is_invalid: bool
    cumulative_task_time_s: float
    cumulative_rest_time_s: float
    cumulative_safe_time_s: float
    cumulative_working_time_s: float
    cumulative_rest_ratio: float
    cumulative_risky_time_s: float
    cumulative_adjustment_magnitude_mm: float
    cumulative_completed_trials: int
    cumulative_failed_trials: int
    trial_result: str
    is_abandoned: bool
    stop_reason: str
    pose_x_m: float
    pose_y_m: float
    pose_z_m: float
    pose_qx: float
    pose_qy: float
    pose_qz: float
    pose_qw: float


@dataclass
class SummaryRecord:
    """summary CSV 한 줄에 저장할 실험 실행 단위 요약."""

    condition_name: str
    measured_side: str
    risk_shoulder_threshold_deg: float
    rest_shoulder_threshold_deg: float
    rest_abandonment_threshold_s: float
    safe_shoulder_min_deg: float
    safe_shoulder_max_deg: float
    user_height_cm: float
    shoulder_height_cm: float
    upper_arm_cm: float
    forearm_cm: float
    drill_tcp_offset_cm: float
    avg_representative_shoulder_angle_deg: float
    avg_shoulder_angle_deg: float
    working_avg_shoulder_angle_deg: float
    avg_rula_proxy: float
    risky_time_s: float
    risky_cycle_count: int
    risky_cycle_ratio_total: float
    total_task_time_s: float
    total_rest_time_s: float
    total_safe_time_s: float
    total_working_time_s: float
    total_rest_ratio: float
    total_rula_high_time_s: float
    max_continuous_rest_time_s: float
    long_rest_abandonment_count: int
    manual_stop_count: int
    completed_transfers: int
    failed_trials: int
    max_trials: int
    experiment_result: str
    experiment_end_reason: str
    experiment_duration_s: float
    avg_cycle_task_time_s: float
    avg_rest_time_per_cycle_s: float
    avg_safe_time_per_cycle_s: float
    avg_risky_time_per_cycle_s: float
    avg_rest_time_change_s: float
    avg_safe_time_change_s: float
    avg_risky_time_change_s: float
    throughput_transfers_per_min: float
    throughput_per_working_min: float
    system_interventions: int
    adjust_count: int
    total_adjustment_magnitude_mm: float
    avg_adj_mm: float
    correction_cmds: int
    invalid_cmds: int
    worker_approve_count: int
    worker_reject_count: int
    llm_call_count: int
    llm_fallback_count: int
    avg_llm_latency_s: float


class ExperimentMetrics:
    """cycle 중 자세값을 누적하고, 실험 전체 summary 값을 계산한다."""

    def __init__(
        self,
        risk_shoulder_deg: float,
        rest_shoulder_deg: float,
        rest_abandonment_threshold_s: float,
        rula_high_score_threshold: float,
        safe_shoulder_min_deg: float,
        safe_shoulder_max_deg: float,
    ) -> None:
        self.risk_shoulder_deg = risk_shoulder_deg
        self.rest_shoulder_deg = rest_shoulder_deg
        self.rest_abandonment_threshold_s = rest_abandonment_threshold_s
        self.rula_high_score_threshold = rula_high_score_threshold
        self.safe_shoulder_min_deg = safe_shoulder_min_deg
        self.safe_shoulder_max_deg = safe_shoulder_max_deg
        if self.safe_shoulder_min_deg > self.safe_shoulder_max_deg:
            raise ValueError("safe_shoulder_min_deg must not exceed safe_shoulder_max_deg.")

        self.completed_transfers = 0
        self.risky_posture_time_s = 0.0
        self.risky_cycle_count = 0
        self.system_intervention_count = 0
        self.robot_adjustment_count = 0
        self.total_adjustment_magnitude_mm = 0.0
        self.correction_commands_count = 0
        self.invalid_cmds = 0
        self.worker_approve_count = 0
        self.worker_reject_count = 0
        self.llm_call_count = 0
        self.llm_fallback_count = 0
        self.total_task_time_s = 0.0
        self.total_rest_time_s = 0.0
        self.total_safe_time_s = 0.0
        self.total_working_time_s = 0.0
        self.total_rula_high_time_s = 0.0
        self.max_continuous_rest_time_s = 0.0
        self.long_rest_abandonment_count = 0
        self.manual_stop_count = 0
        self.failed_trials = 0
        self.experiment_result = "failure"
        self.experiment_end_reason = "incomplete"

        self.llm_latencies: list[float] = []
        self.cycle_durations: list[float] = []
        self.cycle_representative_shoulder_angles: list[tuple[float, float]] = []
        self.cycle_avg_shoulder_angles: list[tuple[float, float]] = []
        self.cycle_working_avg_shoulder_angles: list[tuple[float, float]] = []
        self.cycle_avg_rula_scores: list[tuple[float, float]] = []
        self.completed_rest_times: list[float] = []
        self.completed_safe_times: list[float] = []
        self.completed_risky_times: list[float] = []
        self.rest_time_changes: list[float] = []
        self.safe_time_changes: list[float] = []
        self.risky_time_changes: list[float] = []
        self.last_completed_cycle: CycleResult | None = None

        self.start_cycle()

    def start_cycle(self) -> None:
        """새 AT_TASK cycle의 자세 누적값을 초기화한다."""
        self.cycle_task_time_s = 0.0
        self.cycle_rest_time_s = 0.0
        self.cycle_continuous_rest_time_s = 0.0
        self.cycle_max_continuous_rest_time_s = 0.0
        self.cycle_safe_time_s = 0.0
        self.cycle_risky_time_s = 0.0
        self.cycle_shoulder_samples: list[tuple[float, float]] = []
        self.cycle_working_shoulder_samples: list[tuple[float, float]] = []
        self.cycle_shoulder_weighted_sum = 0.0
        self.cycle_elbow_weighted_sum = 0.0
        self.cycle_working_elbow_weighted_sum = 0.0
        self.cycle_working_visibility_ok_time_s = 0.0
        self.cycle_rula_weighted_sum = 0.0
        self.cycle_max_rula = 1.0
        self.cycle_rula_high_time_s = 0.0
        self.cycle_visibility_ok_time_s = 0.0

    def add_posture_sample(self, posture: PostureSample, dt: float) -> bool:
        """AT_TASK 자세를 누적하고 연속 휴식 포기 여부를 반환한다."""
        if dt <= 0:
            return False

        self.cycle_task_time_s += dt

        if not posture.visibility_ok:
            self.cycle_continuous_rest_time_s = 0.0
            return False
        if posture.shoulder_angle_deg is None or posture.elbow_angle_deg is None or posture.rula_proxy is None:
            self.cycle_continuous_rest_time_s = 0.0
            return False

        shoulder_angle_deg = float(posture.shoulder_angle_deg)
        if shoulder_angle_deg <= self.rest_shoulder_deg:
            self.cycle_rest_time_s += dt
            self.cycle_continuous_rest_time_s += dt
            self.cycle_max_continuous_rest_time_s = max(
                self.cycle_max_continuous_rest_time_s,
                self.cycle_continuous_rest_time_s,
            )
        else:
            self.cycle_continuous_rest_time_s = 0.0

        self.cycle_visibility_ok_time_s += dt
        self.cycle_shoulder_samples.append((shoulder_angle_deg, dt))
        if shoulder_angle_deg > self.rest_shoulder_deg:
            self.cycle_working_shoulder_samples.append((shoulder_angle_deg, dt))
            self.cycle_working_elbow_weighted_sum += posture.elbow_angle_deg * dt
            self.cycle_working_visibility_ok_time_s += dt
        self.cycle_shoulder_weighted_sum += shoulder_angle_deg * dt
        self.cycle_elbow_weighted_sum += posture.elbow_angle_deg * dt
        self.cycle_rula_weighted_sum += posture.rula_proxy * dt
        self.cycle_max_rula = max(self.cycle_max_rula, float(posture.rula_proxy))

        if posture.rula_proxy >= self.rula_high_score_threshold:
            self.cycle_rula_high_time_s += dt
        if self.safe_shoulder_min_deg <= shoulder_angle_deg <= self.safe_shoulder_max_deg:
            self.cycle_safe_time_s += dt
        if shoulder_angle_deg >= self.risk_shoulder_deg:
            self.cycle_risky_time_s += dt

        return self.cycle_continuous_rest_time_s >= self.rest_abandonment_threshold_s

    def finish_cycle(self) -> CycleResult:
        """현재 cycle 누적값을 평균/비율로 정리한다."""
        task_time = self.cycle_task_time_s
        rest_time = max(0.0, min(self.cycle_rest_time_s, task_time))
        working_time = task_time - rest_time
        visible_time = self.cycle_visibility_ok_time_s
        risky_ratio = self.cycle_risky_time_s / visible_time if visible_time > 0 else 0.0
        visibility_ratio = self.cycle_visibility_ok_time_s / task_time if task_time > 0 else 0.0
        rula_high_ratio = self.cycle_rula_high_time_s / visible_time if visible_time > 0 else 0.0
        representative_shoulder_angle = _mode_angle_by_time(self.cycle_shoulder_samples)
        working_representative_shoulder_angle = _mode_angle_by_time(self.cycle_working_shoulder_samples)

        return CycleResult(
            task_time_s=task_time,
            rest_time_s=rest_time,
            working_time_s=working_time,
            rest_ratio=rest_time / task_time if task_time > 0 else 0.0,
            max_continuous_rest_time_s=self.cycle_max_continuous_rest_time_s,
            safe_time_s=self.cycle_safe_time_s,
            risky_time_s=self.cycle_risky_time_s,
            risky_ratio=max(0.0, min(1.0, risky_ratio)),
            is_risky_cycle=working_representative_shoulder_angle > self.risk_shoulder_deg,
            visibility_ok_time_s=self.cycle_visibility_ok_time_s,
            visibility_ok_ratio=max(0.0, min(1.0, visibility_ratio)),
            representative_shoulder_angle_deg=representative_shoulder_angle,
            working_representative_shoulder_angle_deg=working_representative_shoulder_angle,
            avg_shoulder_angle_deg=self.cycle_shoulder_weighted_sum / visible_time if visible_time > 0 else 0.0,
            working_avg_shoulder_angle_deg=(
                sum(angle * dt for angle, dt in self.cycle_working_shoulder_samples)
                / self.cycle_working_visibility_ok_time_s
                if self.cycle_working_visibility_ok_time_s > 0
                else 0.0
            ),
            avg_elbow_angle_deg=self.cycle_elbow_weighted_sum / visible_time if visible_time > 0 else 80.0,
            working_avg_elbow_angle_deg=(
                self.cycle_working_elbow_weighted_sum / self.cycle_working_visibility_ok_time_s
                if self.cycle_working_visibility_ok_time_s > 0
                else 0.0
            ),
            avg_rula_proxy=self.cycle_rula_weighted_sum / visible_time if visible_time > 0 else 1.0,
            max_rula_proxy=self.cycle_max_rula,
            rula_high_time_s=self.cycle_rula_high_time_s,
            rula_high_ratio=max(0.0, min(1.0, rula_high_ratio)),
            shoulder_samples=list(self.cycle_shoulder_samples),
        )

    def record_completed_trial(
        self,
        cycle: CycleResult,
        adjustment_z_mm: float,
        is_invalid: bool,
        is_correction: bool,
    ) -> dict[str, float | None]:
        """trial 확정 후 summary용 누적 지표를 갱신한다."""
        previous_cycle = self.last_completed_cycle
        self.completed_transfers += 1
        self._record_cycle_time(cycle)
        self.cycle_durations.append(cycle.task_time_s)
        self.completed_rest_times.append(cycle.rest_time_s)
        self.completed_safe_times.append(cycle.safe_time_s)
        self.completed_risky_times.append(cycle.risky_time_s)
        if abs(adjustment_z_mm) > 10.0:
            self.robot_adjustment_count += 1
        self.total_adjustment_magnitude_mm += abs(adjustment_z_mm)
        if is_invalid:
            self.invalid_cmds += 1
        if is_correction:
            self.correction_commands_count += 1
        self.last_completed_cycle = cycle

        if previous_cycle is None:
            return {}
        comparison = {
            "representative_shoulder_angle_change_deg": (
                cycle.representative_shoulder_angle_deg - previous_cycle.representative_shoulder_angle_deg
            ),
            "working_representative_shoulder_angle_change_deg": (
                cycle.working_representative_shoulder_angle_deg
                - previous_cycle.working_representative_shoulder_angle_deg
            ),
            "avg_rula_proxy_change": cycle.avg_rula_proxy - previous_cycle.avg_rula_proxy,
            "rest_time_change_s": cycle.rest_time_s - previous_cycle.rest_time_s,
            "safe_time_change_s": cycle.safe_time_s - previous_cycle.safe_time_s,
            "risky_time_change_s": cycle.risky_time_s - previous_cycle.risky_time_s,
            "task_time_change_s": cycle.task_time_s - previous_cycle.task_time_s,
            "working_time_change_s": cycle.working_time_s - previous_cycle.working_time_s,
        }
        self.rest_time_changes.append(float(comparison["rest_time_change_s"]))
        self.safe_time_changes.append(float(comparison["safe_time_change_s"]))
        self.risky_time_changes.append(float(comparison["risky_time_change_s"]))
        return comparison

    def record_failed_trial(self, cycle: CycleResult) -> None:
        """중단된 cycle을 summary에 누적한다."""
        self._record_cycle_time(cycle)
        self.failed_trials += 1

    def _record_cycle_time(self, cycle: CycleResult) -> None:
        self.total_task_time_s += cycle.task_time_s
        self.total_rest_time_s += cycle.rest_time_s
        self.total_safe_time_s += cycle.safe_time_s
        self.total_working_time_s += cycle.working_time_s
        self.total_rula_high_time_s += cycle.rula_high_time_s
        self.risky_posture_time_s += cycle.risky_time_s
        visible_time = cycle.visibility_ok_time_s
        self.cycle_representative_shoulder_angles.append((cycle.representative_shoulder_angle_deg, visible_time))
        self.cycle_avg_shoulder_angles.append((cycle.avg_shoulder_angle_deg, visible_time))
        working_visible_time = max(0.0, visible_time - cycle.rest_time_s)
        self.cycle_working_avg_shoulder_angles.append(
            (cycle.working_avg_shoulder_angle_deg, working_visible_time)
        )
        self.cycle_avg_rula_scores.append((cycle.avg_rula_proxy, visible_time))
        if cycle.is_risky_cycle:
            self.risky_cycle_count += 1
        self.max_continuous_rest_time_s = max(
            self.max_continuous_rest_time_s,
            cycle.max_continuous_rest_time_s,
        )

    def record_llm_call(self, latency_s: float) -> None:
        self.llm_call_count += 1
        if latency_s > 0:
            self.llm_latencies.append(latency_s)

    def record_llm_fallback(self) -> None:
        self.llm_fallback_count += 1

    def record_worker_response(self, action: str) -> None:
        if action in ("approve", "adjust"):
            self.worker_approve_count += 1
        elif action in ("reject", "keep"):
            self.worker_reject_count += 1

    def record_system_intervention(self) -> None:
        self.system_intervention_count += 1

    def record_invalid_command(self) -> None:
        self.invalid_cmds += 1

    # 종료 사유를 summary용 성공·실패 결과로 저장한다.
    def finish_experiment(self, reason: str) -> None:
        self.experiment_result = "success" if reason == "max_trials_completed" else "failure"
        self.experiment_end_reason = reason
        if reason == "long_rest_abandonment":
            self.long_rest_abandonment_count += 1
        elif reason == "manual_stop":
            self.manual_stop_count += 1

    def build_summary(
        self,
        condition_name: str,
        measured_side: str,
        experiment_duration_s: float,
        user_height_cm: float,
        shoulder_height_cm: float,
        upper_arm_cm: float,
        forearm_cm: float,
        drill_tcp_offset_cm: float,
        max_trials: int,
    ) -> SummaryRecord:
        """실험 종료 시 summary CSV에 쓸 값을 만든다."""
        completed = self.completed_transfers
        avg_cycle_time = sum(self.cycle_durations) / len(self.cycle_durations) if self.cycle_durations else 0.0
        avg_rest_time_per_cycle = (
            sum(self.completed_rest_times) / len(self.completed_rest_times)
            if self.completed_rest_times
            else 0.0
        )
        avg_safe_time_per_cycle = (
            sum(self.completed_safe_times) / len(self.completed_safe_times)
            if self.completed_safe_times
            else 0.0
        )
        avg_risky_time_per_cycle = (
            sum(self.completed_risky_times) / len(self.completed_risky_times)
            if self.completed_risky_times
            else 0.0
        )
        throughput_per_min = completed * 60.0 / experiment_duration_s if experiment_duration_s > 0 else 0.0
        throughput_per_working_min = (
            completed * 60.0 / self.total_working_time_s
            if self.total_working_time_s > 0
            else 0.0
        )
        avg_adj_mm = self.total_adjustment_magnitude_mm / completed if completed > 0 else 0.0
        total_trials = completed + self.failed_trials
        risky_cycle_ratio_total = self.risky_cycle_count / total_trials if total_trials > 0 else 0.0
        avg_llm_latency = sum(self.llm_latencies) / len(self.llm_latencies) if self.llm_latencies else 0.0
        avg_representative_shoulder = _weighted_average(self.cycle_representative_shoulder_angles)
        avg_shoulder = _weighted_average(self.cycle_avg_shoulder_angles)
        working_avg_shoulder = _weighted_average(self.cycle_working_avg_shoulder_angles)
        avg_rula = _weighted_average(self.cycle_avg_rula_scores)

        return SummaryRecord(
            condition_name=condition_name,
            measured_side=measured_side,
            risk_shoulder_threshold_deg=self.risk_shoulder_deg,
            rest_shoulder_threshold_deg=self.rest_shoulder_deg,
            rest_abandonment_threshold_s=self.rest_abandonment_threshold_s,
            safe_shoulder_min_deg=self.safe_shoulder_min_deg,
            safe_shoulder_max_deg=self.safe_shoulder_max_deg,
            user_height_cm=user_height_cm,
            shoulder_height_cm=shoulder_height_cm,
            upper_arm_cm=upper_arm_cm,
            forearm_cm=forearm_cm,
            drill_tcp_offset_cm=drill_tcp_offset_cm,
            avg_representative_shoulder_angle_deg=avg_representative_shoulder,
            avg_shoulder_angle_deg=avg_shoulder,
            working_avg_shoulder_angle_deg=working_avg_shoulder,
            avg_rula_proxy=avg_rula,
            risky_time_s=self.risky_posture_time_s,
            risky_cycle_count=self.risky_cycle_count,
            risky_cycle_ratio_total=risky_cycle_ratio_total,
            total_task_time_s=self.total_task_time_s,
            total_rest_time_s=self.total_rest_time_s,
            total_safe_time_s=self.total_safe_time_s,
            total_working_time_s=self.total_working_time_s,
            total_rest_ratio=(
                self.total_rest_time_s / self.total_task_time_s
                if self.total_task_time_s > 0
                else 0.0
            ),
            total_rula_high_time_s=self.total_rula_high_time_s,
            max_continuous_rest_time_s=self.max_continuous_rest_time_s,
            long_rest_abandonment_count=self.long_rest_abandonment_count,
            manual_stop_count=self.manual_stop_count,
            completed_transfers=completed,
            failed_trials=self.failed_trials,
            max_trials=max_trials,
            experiment_result=self.experiment_result,
            experiment_end_reason=self.experiment_end_reason,
            experiment_duration_s=experiment_duration_s,
            avg_cycle_task_time_s=avg_cycle_time,
            avg_rest_time_per_cycle_s=avg_rest_time_per_cycle,
            avg_safe_time_per_cycle_s=avg_safe_time_per_cycle,
            avg_risky_time_per_cycle_s=avg_risky_time_per_cycle,
            avg_rest_time_change_s=(
                sum(self.rest_time_changes) / len(self.rest_time_changes)
                if self.rest_time_changes
                else 0.0
            ),
            avg_safe_time_change_s=(
                sum(self.safe_time_changes) / len(self.safe_time_changes)
                if self.safe_time_changes
                else 0.0
            ),
            avg_risky_time_change_s=(
                sum(self.risky_time_changes) / len(self.risky_time_changes)
                if self.risky_time_changes
                else 0.0
            ),
            throughput_transfers_per_min=throughput_per_min,
            throughput_per_working_min=throughput_per_working_min,
            system_interventions=self.system_intervention_count,
            adjust_count=self.robot_adjustment_count,
            total_adjustment_magnitude_mm=self.total_adjustment_magnitude_mm,
            avg_adj_mm=avg_adj_mm,
            correction_cmds=self.correction_commands_count,
            invalid_cmds=self.invalid_cmds,
            worker_approve_count=self.worker_approve_count,
            worker_reject_count=self.worker_reject_count,
            llm_call_count=self.llm_call_count,
            llm_fallback_count=self.llm_fallback_count,
            avg_llm_latency_s=avg_llm_latency,
        )


class ExperimentDataLogger:
    """TrialRecord와 SummaryRecord를 results CSV 파일에 저장한다."""

    RAW_HEADER = [
        "Time", "Condition", "Trial_Num", "Lead_Type", "Control_Type", "Measured_Side",
        "Risk_Shoulder_Threshold_deg", "Rest_Shoulder_Threshold_deg", "Rest_Abandonment_Threshold_s",
        "Safe_Shoulder_Min_deg", "Safe_Shoulder_Max_deg",
        "User_Height_cm", "Shoulder_Height_cm", "Upper_Arm_cm", "Forearm_cm", "Drill_TCP_Offset_cm",
        "Task_Time_s", "Rest_Time_s", "Safe_Time_s", "Working_Time_s", "Rest_Ratio", "Max_Continuous_Rest_Time_s",
        "Risky_Time_s", "Risky_Ratio", "Is_Risky_Cycle",
        "Visibility_OK_Time_s", "Visibility_OK_Ratio",
        "Representative_Shoulder_Angle_deg", "Working_Representative_Shoulder_Angle_deg",
        "Avg_Shoulder_Angle_deg", "Working_Avg_Shoulder_Angle_deg",
        "Avg_Elbow_Angle_deg", "Working_Avg_Elbow_Angle_deg", "Model_Elbow_Angle_deg",
        "Avg_RULA_Proxy", "Max_RULA_Proxy", "RULA_High_Time_s", "RULA_High_Ratio",
        "Representative_Shoulder_Angle_Change_deg", "Working_Representative_Shoulder_Angle_Change_deg",
        "Avg_RULA_Proxy_Change", "Rest_Time_Change_s", "Safe_Time_Change_s", "Risky_Time_Change_s",
        "Task_Time_Change_s", "Working_Time_Change_s",
        "Target_Shoulder_Angle_deg", "Final_Target_Shoulder_Angle_deg", "Angle_Adjustment_deg", "Target_Angle_Source",
        "Response_Action", "Response_Source", "LLM_Confidence", "Decision_Reason", "LLM_Fallback",
        "Prev_Z_mm", "Final_Z_mm", "Adjustment_Z_mm", "User_Voice", "Final_Z_m",
        "Model_Elbow_H_m", "Working_Elbow_H_m",
        "Pose_Height_Clamped", "Robot_Command_Sent", "Is_Approved", "LLM_Latency_s", "Is_Invalid",
        "Cumulative_Task_Time_s", "Cumulative_Rest_Time_s", "Cumulative_Safe_Time_s", "Cumulative_Working_Time_s",
        "Cumulative_Rest_Ratio", "Cumulative_Risky_Time_s", "Cumulative_Adjustment_Magnitude_mm",
        "Cumulative_Completed_Trials", "Cumulative_Failed_Trials",
        "Trial_Result", "Is_Abandoned", "Stop_Reason",
        "Pose_X_m", "Pose_Y_m", "Pose_Z_m", "Pose_QX", "Pose_QY", "Pose_QZ", "Pose_QW",
    ]

    SUMMARY_HEADER = [
        "Condition", "Measured_Side",
        "Risk_Shoulder_Threshold_deg", "Rest_Shoulder_Threshold_deg", "Rest_Abandonment_Threshold_s",
        "Safe_Shoulder_Min_deg", "Safe_Shoulder_Max_deg",
        "User_Height_cm", "Shoulder_Height_cm", "Upper_Arm_cm", "Forearm_cm", "Drill_TCP_Offset_cm",
        "Avg_Representative_Shoulder_Angle_deg", "Avg_Shoulder_Angle_deg", "Working_Avg_Shoulder_Angle_deg", "Avg_RULA_Proxy",
        "Risky_Time_s", "Risky_Cycle_Count", "Risky_Cycle_Ratio_Total",
        "Total_Task_Time_s", "Total_Rest_Time_s", "Total_Safe_Time_s", "Total_Working_Time_s", "Total_Rest_Ratio",
        "Total_RULA_High_Time_s",
        "Max_Continuous_Rest_Time_s", "Long_Rest_Abandonment_Count", "Manual_Stop_Count",
        "Completed_Transfers", "Failed_Trials", "Max_Trials", "Experiment_Result", "Experiment_End_Reason",
        "Experiment_Duration_s", "Avg_Cycle_Task_Time_s",
        "Avg_Rest_Time_Per_Cycle_s", "Avg_Safe_Time_Per_Cycle_s", "Avg_Risky_Time_Per_Cycle_s",
        "Avg_Rest_Time_Change_s", "Avg_Safe_Time_Change_s", "Avg_Risky_Time_Change_s",
        "Throughput_Transfers_Per_Min", "Throughput_Per_Working_Min",
        "System_Interventions", "Adjust_Count", "Total_Adjustment_Magnitude_mm", "Avg_Adj_mm", "Correction_Cmds", "Invalid_Cmds",
        "Worker_Approve_Count", "Worker_Reject_Count",
        "LLM_Call_Count", "LLM_Fallback_Count", "Avg_LLM_Latency_s",
    ]

    def __init__(self, result_dir: str, participant_id: str, condition_name: str) -> None:
        os.makedirs(result_dir, exist_ok=True)
        self.participant_id = _safe_filename_part(participant_id)
        self.condition_name = _safe_filename_part(condition_name)
        self.filename_prefix = f"{self.participant_id}_{self.condition_name}"
        self.raw_path = os.path.join(
            result_dir,
            f"{self.filename_prefix}_experiment_raw_data_per_trial.csv",
        )
        self.summary_path = os.path.join(
            result_dir,
            f"{self.filename_prefix}_experiment_summary_matrix.csv",
        )
        self.shoulder_dwell_path = os.path.join(
            result_dir,
            f"{self.filename_prefix}_shoulder_angle_dwell_per_trial.csv",
        )
        self.pass_goal_dir = os.path.join(result_dir, "pass_goal_json")
        self.llm_response_dir = os.path.join(result_dir, "llm_response_json")
        os.makedirs(self.pass_goal_dir, exist_ok=True)
        os.makedirs(self.llm_response_dir, exist_ok=True)

    def write_trial(self, record: TrialRecord) -> None:
        self._append_row(self.raw_path, self.RAW_HEADER, self._trial_to_row(record))

    def write_summary(self, record: SummaryRecord) -> None:
        self._append_row(self.summary_path, self.SUMMARY_HEADER, self._summary_to_row(record))

    def write_shoulder_dwell(
        self,
        trial_num: int,
        trial_result: str,
        samples: list[tuple[float, float]],
    ) -> None:
        bins: dict[int, float] = {}
        for angle, dt in samples:
            bin_key = int(float(angle) // SHOULDER_ANGLE_BIN_DEG)
            bins[bin_key] = bins.get(bin_key, 0.0) + dt

        header = ["Trial_Num", "Trial_Result", "Angle_Bin_Start_deg", "Angle_Bin_End_deg", "Dwell_Time_s"]
        header_needed = not os.path.isfile(self.shoulder_dwell_path) or os.path.getsize(self.shoulder_dwell_path) == 0
        with open(self.shoulder_dwell_path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if header_needed:
                writer.writerow(header)
            for bin_key, duration in sorted(bins.items()):
                bin_start = bin_key * SHOULDER_ANGLE_BIN_DEG
                writer.writerow([
                    trial_num,
                    trial_result,
                    _round(bin_start, 1),
                    _round(bin_start + SHOULDER_ANGLE_BIN_DEG, 1),
                    _round(duration, 2),
                ])

    def write_pass_goal_json(self, payload: dict[str, Any], label: str) -> str:
        safe_label = _safe_filename_part(label)
        filename = f"{self.filename_prefix}_{safe_label}.json"
        path = os.path.join(self.pass_goal_dir, filename)
        suffix = 2
        while os.path.exists(path):
            filename = f"{self.filename_prefix}_{safe_label}_{suffix:02d}.json"
            path = os.path.join(self.pass_goal_dir, filename)
            suffix += 1

        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")

        return path

    def write_llm_response_json(self, payload: dict[str, Any], label: str) -> str:
        safe_label = _safe_filename_part(label)
        filename = f"{self.filename_prefix}_{safe_label}.json"
        path = os.path.join(self.llm_response_dir, filename)
        suffix = 2
        while os.path.exists(path):
            filename = f"{self.filename_prefix}_{safe_label}_{suffix:02d}.json"
            path = os.path.join(self.llm_response_dir, filename)
            suffix += 1

        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")

        return path

    def _append_row(self, filename: str, header: list[str], row: list[Any]) -> None:
        header_needed = not os.path.isfile(filename) or os.path.getsize(filename) == 0
        with open(filename, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if header_needed:
                writer.writerow(header)
            writer.writerow(row)

    def _trial_to_row(self, record: TrialRecord) -> list[Any]:
        cycle = record.cycle
        return [
            record.timestamp,
            record.condition_name,
            record.trial_num,
            record.lead_type,
            record.control_type,
            record.measured_side,
            record.risk_shoulder_threshold_deg,
            record.rest_shoulder_threshold_deg,
            record.rest_abandonment_threshold_s,
            record.safe_shoulder_min_deg,
            record.safe_shoulder_max_deg,
            _round(record.user_height_cm, 1),
            _round(record.shoulder_height_cm, 1),
            _round(record.upper_arm_cm, 1),
            _round(record.forearm_cm, 1),
            _round(record.drill_tcp_offset_cm, 1),
            _round(cycle.task_time_s, 2),
            _round(cycle.rest_time_s, 2),
            _round(cycle.safe_time_s, 2),
            _round(cycle.working_time_s, 2),
            _round(cycle.rest_ratio, 3),
            _round(cycle.max_continuous_rest_time_s, 2),
            _round(cycle.risky_time_s, 2),
            _round(cycle.risky_ratio, 3),
            cycle.is_risky_cycle,
            _round(cycle.visibility_ok_time_s, 2),
            _round(cycle.visibility_ok_ratio, 3),
            _round(cycle.representative_shoulder_angle_deg, 2),
            _round(cycle.working_representative_shoulder_angle_deg, 2),
            _round(cycle.avg_shoulder_angle_deg, 2),
            _round(cycle.working_avg_shoulder_angle_deg, 2),
            _round(cycle.avg_elbow_angle_deg, 2),
            _round(cycle.working_avg_elbow_angle_deg, 2),
            _round(record.model_elbow_angle_deg, 2),
            _round(cycle.avg_rula_proxy, 2),
            _round(cycle.max_rula_proxy, 2),
            _round(cycle.rula_high_time_s, 2),
            _round(cycle.rula_high_ratio, 3),
            _round(record.representative_shoulder_angle_change_deg, 2),
            _round(record.working_representative_shoulder_angle_change_deg, 2),
            _round(record.avg_rula_proxy_change, 2),
            _round(record.rest_time_change_s, 2),
            _round(record.safe_time_change_s, 2),
            _round(record.risky_time_change_s, 2),
            _round(record.task_time_change_s, 2),
            _round(record.working_time_change_s, 2),
            _round(record.target_shoulder_angle_deg, 2),
            _round(record.final_target_shoulder_angle_deg, 2),
            _round(record.angle_adjustment_deg, 2),
            record.target_angle_source,
            record.response_action,
            record.response_source,
            _round(record.llm_confidence, 3),
            record.decision_reason,
            record.llm_fallback,
            _round(record.prev_z_mm, 1),
            _round(record.final_z_mm, 1),
            _round(record.adjustment_z_mm, 1),
            record.user_voice,
            _round(record.final_z_m, 3),
            _round(record.model_elbow_h_m, 3),
            _round(record.working_elbow_h_m, 3),
            record.pose_height_clamped,
            record.robot_command_sent,
            record.is_approved,
            _round(record.llm_latency_s, 2),
            record.is_invalid,
            _round(record.cumulative_task_time_s, 2),
            _round(record.cumulative_rest_time_s, 2),
            _round(record.cumulative_safe_time_s, 2),
            _round(record.cumulative_working_time_s, 2),
            _round(record.cumulative_rest_ratio, 3),
            _round(record.cumulative_risky_time_s, 2),
            _round(record.cumulative_adjustment_magnitude_mm, 1),
            record.cumulative_completed_trials,
            record.cumulative_failed_trials,
            record.trial_result,
            record.is_abandoned,
            record.stop_reason,
            _round(record.pose_x_m, 4),
            _round(record.pose_y_m, 4),
            _round(record.pose_z_m, 4),
            _round(record.pose_qx, 5),
            _round(record.pose_qy, 5),
            _round(record.pose_qz, 5),
            _round(record.pose_qw, 5),
        ]

    def _summary_to_row(self, record: SummaryRecord) -> list[Any]:
        return [
            record.condition_name,
            record.measured_side,
            record.risk_shoulder_threshold_deg,
            record.rest_shoulder_threshold_deg,
            record.rest_abandonment_threshold_s,
            record.safe_shoulder_min_deg,
            record.safe_shoulder_max_deg,
            _round(record.user_height_cm, 1),
            _round(record.shoulder_height_cm, 1),
            _round(record.upper_arm_cm, 1),
            _round(record.forearm_cm, 1),
            _round(record.drill_tcp_offset_cm, 1),
            _round(record.avg_representative_shoulder_angle_deg, 2),
            _round(record.avg_shoulder_angle_deg, 2),
            _round(record.working_avg_shoulder_angle_deg, 2),
            _round(record.avg_rula_proxy, 2),
            _round(record.risky_time_s, 2),
            record.risky_cycle_count,
            _round(record.risky_cycle_ratio_total, 3),
            _round(record.total_task_time_s, 2),
            _round(record.total_rest_time_s, 2),
            _round(record.total_safe_time_s, 2),
            _round(record.total_working_time_s, 2),
            _round(record.total_rest_ratio, 3),
            _round(record.total_rula_high_time_s, 2),
            _round(record.max_continuous_rest_time_s, 2),
            record.long_rest_abandonment_count,
            record.manual_stop_count,
            record.completed_transfers,
            record.failed_trials,
            record.max_trials,
            record.experiment_result,
            record.experiment_end_reason,
            _round(record.experiment_duration_s, 2),
            _round(record.avg_cycle_task_time_s, 2),
            _round(record.avg_rest_time_per_cycle_s, 2),
            _round(record.avg_safe_time_per_cycle_s, 2),
            _round(record.avg_risky_time_per_cycle_s, 2),
            _round(record.avg_rest_time_change_s, 2),
            _round(record.avg_safe_time_change_s, 2),
            _round(record.avg_risky_time_change_s, 2),
            _round(record.throughput_transfers_per_min, 3),
            _round(record.throughput_per_working_min, 3),
            record.system_interventions,
            record.adjust_count,
            _round(record.total_adjustment_magnitude_mm, 1),
            _round(record.avg_adj_mm, 1),
            record.correction_cmds,
            record.invalid_cmds,
            record.worker_approve_count,
            record.worker_reject_count,
            record.llm_call_count,
            record.llm_fallback_count,
            _round(record.avg_llm_latency_s, 2),
        ]


def _mode_angle_by_time(samples: list[tuple[float, float]], bin_size_deg: float = SHOULDER_ANGLE_BIN_DEG, default: float = 0.0) -> float:
    """2도 단위로 묶어 가장 오래 머문 어깨각 구간의 대표값을 계산한다."""
    if not samples:
        return default

    bins: dict[int, list[float]] = {}
    for angle, dt in samples:
        bin_key = int(float(angle) // bin_size_deg)
        weighted_sum, duration = bins.setdefault(bin_key, [0.0, 0.0])
        bins[bin_key] = [weighted_sum + float(angle) * dt, duration + dt]

    weighted_sum, duration = max(bins.values(), key=lambda values: values[1])
    return weighted_sum / duration if duration > 0 else default


def _weighted_average(samples: list[tuple[float, float]], default: float = 0.0) -> float:
    duration = sum(dt for _, dt in samples)
    return sum(value * dt for value, dt in samples) / duration if duration > 0 else default


def _round(value: float | None, digits: int) -> float | str:
    if value is None:
        return ""
    return round(float(value), digits)


def _safe_filename_part(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value))
    safe = safe.strip("_")
    return safe or "pass_goal"
