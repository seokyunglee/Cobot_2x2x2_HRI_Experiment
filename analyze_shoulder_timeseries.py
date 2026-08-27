"""어깨각 시계열을 검토 가능한 분석 단계 파일로 변환한다.

원본 시계열은 수정하지 않는다. 중앙 이동 중앙값으로 완만하게 보정한
신호는 급격한 변화를 찾는 데만 사용하며, 결과 파일에는 원본과 보정값을
모두 남겨 checkpoint를 나중에 검토할 수 있게 한다.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import median, pstdev


SMOOTH_HALF_WINDOW_S = 0.20
CHANGE_WINDOW_S = 0.50
CHANGE_THRESHOLD_DEG = 18.0
EVENT_JOIN_GAP_S = 0.35


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def fmt(value: float) -> str:
    return "" if not math.isfinite(value) else f"{value:.4f}"


def nearest_before(times: list[float], index: int, target: float) -> int:
    """index 이전 범위에서 target 시점 이하인 마지막 샘플 위치를 반환한다."""
    cursor = index
    while cursor > 0 and times[cursor] > target:
        cursor -= 1
    return cursor


def rolling_median(times: list[float], values: list[float]) -> list[float]:
    smoothed: list[float] = []
    left = 0
    right = 0
    for i, timestamp in enumerate(times):
        while left < len(times) and times[left] < timestamp - SMOOTH_HALF_WINDOW_S:
            left += 1
        while right < len(times) and times[right] <= timestamp + SMOOTH_HALF_WINDOW_S:
            right += 1
        smoothed.append(median(values[left:right]))
    return smoothed


def detect_events(times: list[float], smoothed: list[float]) -> tuple[list[float], list[tuple[int, int, str]]]:
    deltas: list[float] = []
    candidates: list[tuple[int, str]] = []
    for i, timestamp in enumerate(times):
        previous = nearest_before(times, i, timestamp - CHANGE_WINDOW_S)
        delta = smoothed[i] - smoothed[previous]
        deltas.append(delta)
        if i > 0 and abs(delta) >= CHANGE_THRESHOLD_DEG:
            candidates.append((i, "Up" if delta > 0 else "Down"))

    # 한 번의 지속적인 전환은 임계값을 넘는 인접 샘플을 여러 개 만든다.
    # 이를 하나의 움직임 후보로 합치되, 아직 최종 phase 라벨은 부여하지 않는다.
    events: list[tuple[int, int, str]] = []
    for index, direction in candidates:
        if events and direction == events[-1][2] and times[index] - times[events[-1][1]] <= EVENT_JOIN_GAP_S:
            events[-1] = (events[-1][0], index, direction)
        else:
            events.append((index, index, direction))

    # 후보 시작은 처음 임계값을 통과한 프레임으로 둔다. 이전처럼 0.5초 앞의
    # 프레임까지 확장하지 않으므로, 마지막 하강의 Speech_Wait도 일찍 시작하지 않는다.
    return deltas, events


def two_cluster_labels(plateau_values: list[float]) -> list[str]:
    """plateau 대표각을 결정론적인 1차원 두 군으로 나눈다."""
    if not plateau_values:
        return []
    low, high = min(plateau_values), max(plateau_values)
    if high - low < 8.0:
        return ["Unclassified"] * len(plateau_values)
    for _ in range(20):
        groups = [[], []]
        for value in plateau_values:
            groups[0 if abs(value - low) <= abs(value - high) else 1].append(value)
        new_low = mean(groups[0]) if groups[0] else low
        new_high = mean(groups[1]) if groups[1] else high
        if abs(new_low - low) < 0.001 and abs(new_high - high) < 0.001:
            break
        low, high = new_low, new_high
    midpoint = (low + high) / 2.0
    return ["Low_Plateau" if value <= midpoint else "High_Plateau" for value in plateau_values]


def partition_trial(rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    times = [float(row["Elapsed_Time_s"]) for row in rows]
    raw_angles = [float(row["Shoulder_Angle_deg"]) for row in rows]
    smooth = rolling_median(times, raw_angles)
    deltas, events = detect_events(times, smooth)

    # plateau와 움직임 후보가 번갈아 나타나는 전체 시퀀스를 만든다.
    segments: list[dict[str, object]] = []
    cursor = 0
    for start, end, direction in events:
        if cursor < start:
            segments.append({"kind": "plateau", "start": cursor, "end": start - 1})
        segments.append({"kind": f"Transition_{direction}", "start": start, "end": end})
        cursor = end + 1
    if cursor < len(rows):
        segments.append({"kind": "plateau", "start": cursor, "end": len(rows) - 1})

    # 기본 상태는 Work다. Work를 좁은 각도 범위로 정의하지 않으므로,
    # 70 -> 100도 변화도 Work 안에 남을 수 있다. Rest는 저각도 자세군과
    # 일치하고 Work -> low -> Work 복귀가 완성된 경우에만 부여한다.
    for segment in segments:
        segment["label"] = "Work"
        segment["transition_direction"] = ""

    plateau_indices = [index for index, segment in enumerate(segments) if segment["kind"] == "plateau"]
    plateau_center = {
        index: median(smooth[segments[index]["start"] : segments[index]["end"] + 1])
        for index in plateau_indices
    }

    # Plateau의 중앙값을 trial 안에서 Low / High 두 자세군으로 나눈다. Up / Down은
    # 별도의 움직임 후보이므로 이 군분류에는 넣지 않는다.
    ordered_plateau_indices = list(plateau_indices)
    ordered_centers = [plateau_center[index] for index in ordered_plateau_indices]
    ordered_cluster_labels = two_cluster_labels(ordered_centers)
    posture_cluster = dict(zip(ordered_plateau_indices, ordered_cluster_labels))
    high_plateau_indices = [
        index for index in ordered_plateau_indices
        if posture_cluster[index] == "High_Plateau"
    ]
    low_plateau_indices = [
        index for index in ordered_plateau_indices
        if posture_cluster[index] == "Low_Plateau"
    ]

    # Low plateau 하나의 바로 앞/뒤를 보지 않는다. 연속 High plateau 두 개 사이를
    # 통째로 검사해서, 그 사이 Low가 있으면 하나의 Rest episode로 묶는다. 따라서
    # Low -> Down -> Low처럼 휴식 중에 생긴 흔들림은 Rest를 쪼개지 않는다.
    for left_high, right_high in zip(high_plateau_indices, high_plateau_indices[1:]):
        lows_between = [
            index for index in low_plateau_indices
            if left_high < index < right_high
        ]
        if not lows_between:
            continue

        first_low, last_low = lows_between[0], lows_between[-1]
        down_before_low = [
            index for index in range(left_high + 1, first_low)
            if segments[index]["kind"] == "Transition_Down"
        ]
        up_after_low = [
            index for index in range(last_low + 1, right_high)
            if segments[index]["kind"] == "Transition_Up"
        ]
        if not down_before_low or not up_after_low:
            continue

        # 움직임 후보가 임계값을 넘은 짧은 부분만 Transition으로 남기지 않는다.
        # High plateau가 끝난 직후부터 첫 Low plateau 직전까지를 전체 Down 전환,
        # 마지막 Low plateau 직후부터 다음 High plateau 직전까지를 전체 Up 전환으로
        # 둔다. 따라서 느린 올림/내림도 Work가 아니라 Transition에 포함된다.
        transition_down_start = left_high + 1
        transition_down_end = first_low - 1
        rest_start = first_low
        rest_end = last_low
        transition_up_start = last_low + 1
        transition_up_end = right_high - 1

        for index in range(rest_start, rest_end + 1):
            segments[index]["label"] = "Rest"
            segments[index]["transition_direction"] = ""
        for index in range(transition_down_start, transition_down_end + 1):
            segments[index]["label"] = "Transition"
            segments[index]["transition_direction"] = "Down"
        for index in range(transition_up_start, transition_up_end + 1):
            segments[index]["label"] = "Transition"
            segments[index]["transition_direction"] = "Up"

    # 마지막 High 이후 Low 자세군으로 내려간 뒤 High로 복귀하지 않으면 terminal
    # tail이다. 실제 Down 후보의 첫 프레임부터 끝까지 Speech_Wait로 둔다.
    if high_plateau_indices:
        last_high = high_plateau_indices[-1]
        lows_after_last_high = [index for index in low_plateau_indices if index > last_high]
        if lows_after_last_high:
            first_low = lows_after_last_high[0]
            down_before_low = [
                index for index in range(last_high + 1, first_low)
                if segments[index]["kind"] == "Transition_Down"
            ]
            if down_before_low:
                speech_wait_start = down_before_low[0]
                for index in range(speech_wait_start, len(segments)):
                    segments[index]["label"] = "Speech_Wait"
                    segments[index]["transition_direction"] = ""

    # 출력은 원시 움직임 후보 조각이나 구현용 segment ID가 아니라, 최종 라벨을
    # 연속 구간별로 묶은 결과다.
    merged_segments: list[dict[str, object]] = []
    for segment in segments:
        if (
            merged_segments
            and merged_segments[-1]["label"] == segment["label"]
            and merged_segments[-1]["transition_direction"] == segment["transition_direction"]
        ):
            merged_segments[-1]["end"] = segment["end"]
        else:
            merged_segments.append(segment)
    segments = merged_segments

    # checkpoint는 최종 라벨 경계에서만 남긴다. Work 내부에서 버려진 움직임
    # 후보는 더 이상 잘못된 checkpoint를 남기지 않는다.
    checkpoint_at: dict[int, tuple[str, str]] = {}
    for segment in segments:
        start, end = segment["start"], segment["end"]
        if segment["label"] == "Transition":
            direction = segment["transition_direction"]
            checkpoint_at[start] = ("Transition_Start", direction)
            checkpoint_at[end] = ("Transition_End", direction)
        elif segment["label"] == "Speech_Wait":
            checkpoint_at[start] = ("Speech_Wait_Start", "Down")

    sample_output: list[dict[str, object]] = []
    for segment in segments:
        for i in range(segment["start"], segment["end"] + 1):
            checkpoint, direction = checkpoint_at.get(i, ("", ""))
            sample_output.append({
                **rows[i],
                "Smoothed_Shoulder_Angle_deg": smooth[i],
                "Delta_0p5s_deg": deltas[i],
                "Checkpoint": checkpoint,
                "Checkpoint_Direction": direction,
                "Label": segment["label"],
                "Transition_Direction": segment["transition_direction"],
                "Is_Speech_Wait": segment["label"] == "Speech_Wait",
            })

    segment_output: list[dict[str, object]] = []
    for segment in segments:
        start, end = segment["start"], segment["end"]
        raw_slice = raw_angles[start : end + 1]
        smooth_slice = smooth[start : end + 1]
        start_checkpoint = checkpoint_at.get(start, ("", ""))[0]
        end_checkpoint = checkpoint_at.get(end, ("", ""))[0]
        segment_output.append({
            "Trial_Num": rows[0]["Trial_Num"],
            "Trial_Result": rows[0]["Trial_Result"],
            "Label": segment["label"],
            "Transition_Direction": segment["transition_direction"],
            "Is_Speech_Wait": segment["label"] == "Speech_Wait",
            "Start_Time_s": times[start],
            "End_Time_s": times[end],
            # 실험 logger와 같은 프레임별 dt 누적값을 사용한다. timestamp 차이에는
            # 작은 샘플링 공백이 포함될 수 있어, 이를 쓰면 segment 합계와 공식
            # cycle 시간이 어긋날 수 있다.
            "Duration_s": sum(float(row["Dt_s"]) for row in rows[start : end + 1]),
            "Mean_Angle_deg": mean(raw_slice),
            "Angle_SD_deg": pstdev(raw_slice) if len(raw_slice) > 1 else 0.0,
            "Min_Angle_deg": min(raw_slice),
            "Max_Angle_deg": max(raw_slice),
            "Smoothed_Mean_Angle_deg": mean(smooth_slice),
            "Smoothed_SD_deg": pstdev(smooth_slice) if len(smooth_slice) > 1 else 0.0,
            "Checkpoint_At_Start": start_checkpoint,
            "Checkpoint_At_End": end_checkpoint,
        })
    return sample_output, segment_output


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: fmt(row[field]) if isinstance(row.get(field), float) else row.get(field, "") for field in fields})


def main() -> None:
    # 명령줄에서 경로를 입력할 필요 없이 Windows 파일 선택창을 연다. 여러 raw
    # 파일을 한 번에 고를 수 있으며, 파일마다 두 개의 결과 CSV가 생성된다.
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected_paths = filedialog.askopenfilenames(
        title="어깨각 raw time-series CSV 파일 선택",
        filetypes=[("CSV 파일", "*.csv"), ("모든 파일", "*.*")],
    )
    root.destroy()
    if not selected_paths:
        print("선택한 파일이 없어 분석 파일을 만들지 않았습니다.")
        return

    output_dir = Path(__file__).resolve().parent / "derived_analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_fields = [
        "Trial_Num", "Trial_Result", "Elapsed_Time_s", "Dt_s", "Visibility_OK", "Shoulder_Angle_deg", "Target_Shoulder_Angle_deg",
        "Smoothed_Shoulder_Angle_deg", "Delta_0p5s_deg", "Checkpoint", "Checkpoint_Direction", "Label", "Transition_Direction", "Is_Speech_Wait",
    ]
    segment_fields = [
        "Trial_Num", "Trial_Result", "Label", "Transition_Direction", "Is_Speech_Wait", "Start_Time_s", "End_Time_s", "Duration_s",
        "Mean_Angle_deg", "Angle_SD_deg", "Min_Angle_deg", "Max_Angle_deg", "Smoothed_Mean_Angle_deg", "Smoothed_SD_deg",
        "Checkpoint_At_Start", "Checkpoint_At_End",
    ]
    for selected_path in map(Path, selected_paths):
        with selected_path.open(newline="", encoding="utf-8-sig") as file:
            source_rows = list(csv.DictReader(file))
        by_trial: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in source_rows:
            if row["Visibility_OK"].strip().lower() in {"false", "0"} or not row["Shoulder_Angle_deg"].strip():
                continue
            by_trial[row["Trial_Num"]].append(row)

        samples: list[dict[str, object]] = []
        segments: list[dict[str, object]] = []
        for trial_rows in by_trial.values():
            trial_samples, trial_segments = partition_trial(trial_rows)
            samples.extend(trial_samples)
            segments.extend(trial_segments)

        stem = selected_path.name.removesuffix("_shoulder_angle_timeseries_per_trial.csv")
        samples_path = output_dir / f"{stem}_analysis_samples.csv"
        segments_path = output_dir / f"{stem}_analysis_segments.csv"
        write_csv(samples_path, samples, sample_fields)
        write_csv(segments_path, segments, segment_fields)
        print(f"생성 완료: {samples_path}")
        print(f"생성 완료: {segments_path}")


if __name__ == "__main__":
    main()
