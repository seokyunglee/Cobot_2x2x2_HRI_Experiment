"""분석 완료된 어깨각 sample/segment CSV에서 trial별 지표표를 만든다.

실행:
    python summarize_shoulder_metrics.py

선택한 *_analysis_samples.csv와 같은 폴더에 있는 짝 파일
*_analysis_segments.csv를 자동으로 읽는다. 결과는 프로젝트의
derived_analysis 폴더에 *_trial_metrics.csv로 저장한다.

이 파일은 라벨을 새로 만들지 않는다. analyze_shoulder_timeseries.py가 만든
Work / Rest / Transition / Speech_Wait 라벨을 그대로 집계하는 용도다.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from statistics import median, pstdev


OUTPUT_DIR = Path(__file__).resolve().parent / "derived_analysis"


def as_float(value: object, default: float = 0.0) -> float:
    """빈 값도 안전하게 수치로 바꾼다."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def percentile(values: list[float], fraction: float) -> float | None:
    """외부 패키지 없이 선형 보간 분위수를 구한다."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def angle_metrics(prefix: str, values: list[float]) -> dict[str, float | int | str]:
    """각도 자세군의 중심, 범위, 변동성을 같은 형식으로 만든다."""
    if not values:
        return {
            f"{prefix}_Sample_Count": 0,
            f"{prefix}_Mean_Angle_deg": "",
            f"{prefix}_Median_Angle_deg": "",
            f"{prefix}_P10_Angle_deg": "",
            f"{prefix}_P90_Angle_deg": "",
            f"{prefix}_P90_P10_Range_deg": "",
            f"{prefix}_Angle_SD_deg": "",
        }
    p10 = percentile(values, 0.10)
    p90 = percentile(values, 0.90)
    return {
        f"{prefix}_Sample_Count": len(values),
        f"{prefix}_Mean_Angle_deg": sum(values) / len(values),
        f"{prefix}_Median_Angle_deg": median(values),
        f"{prefix}_P10_Angle_deg": p10,
        f"{prefix}_P90_Angle_deg": p90,
        f"{prefix}_P90_P10_Range_deg": p90 - p10 if p10 is not None and p90 is not None else "",
        f"{prefix}_Angle_SD_deg": pstdev(values) if len(values) > 1 else 0.0,
    }


def segment_durations(segments: list[dict[str, str]], label: str, direction: str = "") -> list[float]:
    """최종 segment 중 지정 라벨(및 필요하면 방향)의 지속시간 목록을 반환한다."""
    result: list[float] = []
    for segment in segments:
        if segment.get("Label") != label:
            continue
        if direction and segment.get("Transition_Direction") != direction:
            continue
        result.append(as_float(segment.get("Duration_s")))
    return result


def duration_summary(prefix: str, durations: list[float]) -> dict[str, float | int | str]:
    if not durations:
        return {
            f"{prefix}_Count": 0,
            f"{prefix}_Total_Time_s": 0.0,
            f"{prefix}_Mean_Duration_s": "",
            f"{prefix}_Longest_Duration_s": "",
        }
    return {
        f"{prefix}_Count": len(durations),
        f"{prefix}_Total_Time_s": sum(durations),
        f"{prefix}_Mean_Duration_s": sum(durations) / len(durations),
        f"{prefix}_Longest_Duration_s": max(durations),
    }


def summarize_trial(
    samples: list[dict[str, str]],
    segments: list[dict[str, str]],
    condition: str,
) -> dict[str, object]:
    """한 trial의 공식 cycle, 자세각, bout 및 전환 지표를 계산한다."""
    samples = sorted(samples, key=lambda row: as_float(row.get("Elapsed_Time_s")))
    segments = sorted(segments, key=lambda row: as_float(row.get("Start_Time_s")))
    dt_values = [as_float(row.get("Dt_s")) for row in samples]
    labels = [row.get("Label", "") for row in samples]
    smoothed_angles = [as_float(row.get("Smoothed_Shoulder_Angle_deg")) for row in samples]
    work_indices = [index for index, label in enumerate(labels) if label == "Work"]
    if work_indices:
        first_work, last_work = work_indices[0], work_indices[-1]
        behavioral_task_time = sum(dt_values[first_work : last_work + 1])
    else:
        behavioral_task_time = 0.0

    work_angles = [angle for angle, label in zip(smoothed_angles, labels) if label == "Work"]
    rest_angles = [angle for angle, label in zip(smoothed_angles, labels) if label == "Rest"]
    speech_wait_angles = [angle for angle, label in zip(smoothed_angles, labels) if label == "Speech_Wait"]
    work_targets = [
        as_float(row.get("Target_Shoulder_Angle_deg"))
        for row, label in zip(samples, labels)
        if label == "Work" and row.get("Target_Shoulder_Angle_deg", "").strip()
    ]
    work_target_errors = [
        angle - as_float(row.get("Target_Shoulder_Angle_deg"))
        for row, angle, label in zip(samples, smoothed_angles, labels)
        if label == "Work" and row.get("Target_Shoulder_Angle_deg", "").strip()
    ]

    # 각속도와 각도 이동량은 raw 노이즈 대신 smoothed angle의 연속 Work 프레임만
    # 사용한다. Work와 Rest/Transition 경계를 가로지르는 변화량은 포함하지 않는다.
    work_abs_velocities: list[float] = []
    work_total_travel = 0.0
    for index in range(1, len(samples)):
        if labels[index - 1] != "Work" or labels[index] != "Work":
            continue
        dt = dt_values[index]
        if dt <= 0:
            continue
        travel = abs(smoothed_angles[index] - smoothed_angles[index - 1])
        work_total_travel += travel
        work_abs_velocities.append(travel / dt)

    work_bouts = segment_durations(segments, "Work")
    rest_bouts = segment_durations(segments, "Rest")
    down_transitions = segment_durations(segments, "Transition", "Down")
    up_transitions = segment_durations(segments, "Transition", "Up")
    work_time = sum(work_bouts)
    rest_time = sum(rest_bouts)
    transition_down_time = sum(down_transitions)
    transition_up_time = sum(up_transitions)
    transition_time = transition_down_time + transition_up_time
    speech_wait_time = sum(segment_durations(segments, "Speech_Wait"))
    target_angle = median(work_targets) if work_targets else ""
    work_median = median(work_angles) if work_angles else ""
    work_p10 = percentile(work_angles, 0.10)
    work_p90 = percentile(work_angles, 0.90)
    rest_p10 = percentile(rest_angles, 0.10)
    rest_p90 = percentile(rest_angles, 0.90)

    result: dict[str, object] = {
        "Condition": condition,
        "Trial": samples[0].get("Trial_Num", ""),
        "Official_Cycle_Time_s": sum(dt_values),
        "Behavioral_Task_Time_s": behavioral_task_time,
        "Speech_Wait_Time_s": speech_wait_time,
        # 모든 최종 라벨 시간을 더했을 때 Official cycle과 일치하는지 보는 검산값.
        "Time_Accounting_Check_s": sum(dt_values) - (work_time + rest_time + transition_time + speech_wait_time),
        "Work_Time_s": work_time,
        "Work_Bout_Count": len(work_bouts),
        "Longest_Work_Bout_s": max(work_bouts) if work_bouts else "",
        "Mean_Work_Bout_s": sum(work_bouts) / len(work_bouts) if work_bouts else "",
        "Rest_Time_s": rest_time,
        "Rest_Bout_Count": len(rest_bouts),
        "Longest_Rest_Bout_s": max(rest_bouts) if rest_bouts else "",
        "Mean_Rest_Bout_s": sum(rest_bouts) / len(rest_bouts) if rest_bouts else "",
        "Rest_Ratio": rest_time / behavioral_task_time if behavioral_task_time > 0 else "",
        "Transition_Down_Time_s": transition_down_time,
        "Transition_Up_Time_s": transition_up_time,
        "Transition_Time_s": transition_time,
        "Work_Angle_Median_deg": work_median,
        "Work_Angle_P10_deg": work_p10 if work_p10 is not None else "",
        "Work_Angle_P90_deg": work_p90 if work_p90 is not None else "",
        "Work_Angle_P90_P10_Range_deg": work_p90 - work_p10 if work_p10 is not None and work_p90 is not None else "",
        "Work_Angle_SD_deg": pstdev(work_angles) if len(work_angles) > 1 else (0.0 if work_angles else ""),
        "Work_Mean_Abs_Angular_Velocity_deg_s": sum(work_abs_velocities) / len(work_abs_velocities) if work_abs_velocities else "",
        "Work_Total_Angular_Travel_deg": work_total_travel if work_angles else "",
        "Target_Angle_deg": target_angle,
        "Work_Target_Signed_Error_deg": work_median - target_angle if work_median != "" and target_angle != "" else "",
        "Work_Target_MAE_deg": sum(abs(error) for error in work_target_errors) / len(work_target_errors) if work_target_errors else "",
        "Rest_Angle_Median_deg": median(rest_angles) if rest_angles else "",
        "Rest_Angle_P10_deg": rest_p10 if rest_p10 is not None else "",
        "Rest_Angle_P90_deg": rest_p90 if rest_p90 is not None else "",
        "Speech_Wait_Angle_Median_deg": median(speech_wait_angles) if speech_wait_angles else "",
    }
    return result


METRIC_FIELDS = [
    "Condition", "Trial", "Official_Cycle_Time_s", "Behavioral_Task_Time_s", "Speech_Wait_Time_s",
    "Time_Accounting_Check_s", "Work_Time_s", "Work_Bout_Count", "Longest_Work_Bout_s", "Mean_Work_Bout_s",
    "Rest_Time_s", "Rest_Bout_Count", "Longest_Rest_Bout_s", "Mean_Rest_Bout_s", "Rest_Ratio",
    "Transition_Down_Time_s", "Transition_Up_Time_s", "Transition_Time_s",
    "Work_Angle_Median_deg", "Work_Angle_P10_deg", "Work_Angle_P90_deg", "Work_Angle_P90_P10_Range_deg",
    "Work_Angle_SD_deg", "Work_Mean_Abs_Angular_Velocity_deg_s", "Work_Total_Angular_Travel_deg",
    "Target_Angle_deg", "Work_Target_Signed_Error_deg", "Work_Target_MAE_deg",
    "Rest_Angle_Median_deg", "Rest_Angle_P10_deg", "Rest_Angle_P90_deg", "Speech_Wait_Angle_Median_deg",
]


def format_value(value: object) -> object:
    return round(value, 3) if isinstance(value, float) else value


def write_metrics(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=METRIC_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: format_value(row.get(field, "")) for field in METRIC_FIELDS})


def paired_segment_path(sample_path: Path) -> Path:
    name = sample_path.name
    if not name.endswith("_analysis_samples.csv"):
        raise ValueError("파일명은 _analysis_samples.csv로 끝나야 합니다.")
    return sample_path.with_name(name.removesuffix("_analysis_samples.csv") + "_analysis_segments.csv")


def source_condition(sample_path: Path) -> str:
    """예: 004_Cond2_Sys_Rule_analysis_samples.csv -> Cond2_Sys_Rule."""
    stem = sample_path.name.removesuffix("_analysis_samples.csv")
    match = re.fullmatch(r"(?P<participant>[^_]+)_(?P<condition>Cond\d+_.+)", stem)
    if not match:
        return stem
    return match.group("condition")


def source_participant_id(sample_path: Path) -> str:
    """파일명 첫 토큰의 참가자 ID를 반환한다. 예: 004_Cond1_* -> 004."""
    stem = sample_path.name.removesuffix("_analysis_samples.csv")
    match = re.fullmatch(r"(?P<participant>[^_]+)_Cond\d+_.+", stem)
    return match.group("participant") if match else ""


def main() -> None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected_paths = filedialog.askopenfilenames(
        title="지표를 계산할 analysis_samples CSV 파일 선택",
        filetypes=[("analysis samples CSV", "*_analysis_samples.csv"), ("CSV 파일", "*.csv")],
    )
    root.destroy()
    if not selected_paths:
        print("선택한 파일이 없어 지표 파일을 만들지 않습니다.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    combined_metrics: list[dict[str, object]] = []
    participant_ids: set[str] = set()
    for selected in map(Path, selected_paths):
        try:
            segments_path = paired_segment_path(selected)
        except ValueError as error:
            print(f"건너뜀: {selected.name} ({error})")
            continue
        if not segments_path.exists():
            print(f"건너뜀: 짝 segment 파일이 없습니다: {segments_path}")
            continue

        with selected.open(newline="", encoding="utf-8-sig") as file:
            samples = list(csv.DictReader(file))
        with segments_path.open(newline="", encoding="utf-8-sig") as file:
            segments = list(csv.DictReader(file))

        condition = source_condition(selected)

        samples_by_trial: dict[str, list[dict[str, str]]] = defaultdict(list)
        segments_by_trial: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in samples:
            samples_by_trial[row.get("Trial_Num", "")].append(row)
        for row in segments:
            segments_by_trial[row.get("Trial_Num", "")].append(row)

        metrics = [
            summarize_trial(
                trial_samples,
                segments_by_trial.get(trial_num, []),
                condition,
            )
            for trial_num, trial_samples in samples_by_trial.items()
            if trial_samples
        ]
        combined_metrics.extend(metrics)
        participant_id = source_participant_id(selected)
        if participant_id:
            participant_ids.add(participant_id)

    # 선택한 모든 condition을 Condition 열로 구분해 하나의 trial 지표 파일에 저장한다.
    if combined_metrics:
        combined_metrics.sort(key=lambda row: (str(row["Condition"]), as_float(row["Trial"])))
        if len(participant_ids) == 1:
            participant_id = next(iter(participant_ids))
            if len(selected_paths) == 5:
                combined_name = f"{participant_id}_all_conditions_trial_metrics.csv"
            else:
                combined_name = f"{participant_id}_selected_conditions_trial_metrics.csv"
        else:
            combined_name = "selected_conditions_trial_metrics.csv"
        combined_path = OUTPUT_DIR / combined_name
        write_metrics(combined_path, combined_metrics)
        print(f"생성 완료: {combined_path}")


if __name__ == "__main__":
    main()
