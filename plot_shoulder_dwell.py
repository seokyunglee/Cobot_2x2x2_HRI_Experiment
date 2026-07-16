"""어깨각 체류시간 CSV를 trial별 PNG 그래프로 저장한다."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def read_dwell_csv(path: Path | str) -> dict[int, tuple[str, list[tuple[float, float, float]]]]:
    path = Path(path)
    trials: dict[int, tuple[str, list[tuple[float, float, float]]]] = {}
    rows: defaultdict[int, list[tuple[float, float, float]]] = defaultdict(list)
    results: dict[int, str] = {}

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"Trial_Num", "Trial_Result", "Angle_Bin_Start_deg", "Angle_Bin_End_deg", "Dwell_Time_s"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("어깨각 체류시간 CSV 헤더가 올바르지 않습니다.")
        for row in reader:
            trial_num = int(row["Trial_Num"])
            rows[trial_num].append((
                float(row["Angle_Bin_Start_deg"]),
                float(row["Angle_Bin_End_deg"]),
                float(row["Dwell_Time_s"]),
            ))
            results[trial_num] = row["Trial_Result"]

    for trial_num, values in rows.items():
        trials[trial_num] = (results[trial_num], sorted(values))
    return dict(sorted(trials.items()))


def save_dwell_plot(csv_path: Path | str, output_path: Path | str) -> None:
    csv_path = Path(csv_path)
    output_path = Path(output_path)
    trials = read_dwell_csv(csv_path)
    if not trials:
        raise ValueError("그래프로 만들 체류시간 데이터가 없습니다.")

    figure, axes = plt.subplots(len(trials), 1, figsize=(10, max(3, 2.8 * len(trials))), squeeze=False)
    for axis, (trial_num, (result, values)) in zip(axes.flat, trials.items()):
        starts = [value[0] for value in values]
        widths = [value[1] - value[0] for value in values]
        durations = [value[2] for value in values]
        color = "tab:blue" if result == "success" else "tab:red"
        axis.bar(starts, durations, width=widths, align="edge", color=color, edgecolor="black")
        axis.set_title(f"Trial {trial_num} ({result})")
        axis.set_ylabel("Dwell time (s)")
        axis.set_xlim(0, 150)
        axis.set_xticks(range(0, 151, 5))
        axis.grid(axis="y", alpha=0.3)

    axes.flat[-1].set_xlabel("Shoulder angle (deg)")
    figure.suptitle("Shoulder-angle dwell time by trial")
    figure.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description="어깨각 체류시간 CSV를 PNG 그래프로 저장합니다.")
    parser.add_argument("csv_path", type=Path, help="shoulder_angle_dwell_per_trial.csv 파일 경로")
    parser.add_argument("--output", type=Path, help="저장할 PNG 경로")
    args = parser.parse_args()

    output_path = args.output or args.csv_path.with_name(f"{args.csv_path.stem}_plot.png")
    save_dwell_plot(args.csv_path, output_path)
    print(f"[SHOULDER DWELL PLOT] saved: {output_path}")


if __name__ == "__main__":
    main()
