"""Build an interim participant/condition analysis workbook from experiment CSV files.

Usage
-----
    python build_interim_analysis_workbook.py "C:\\Experiment_Data"

When no folder is supplied, a folder-selection dialog is shown.  The selected
folder is searched recursively for these six files per participant:

* ``<ID>_all_conditions_trial_metrics.csv``
* ``<ID>_Cond[1-5]_..._experiment_raw_data_per_trial.csv``

The output workbook contains one sheet per participant plus participant-level
condition summaries, overall condition trends, and validation messages.
It deliberately uses only the Python standard library, so no Excel package
needs to be installed before running it.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.utils import column_index_from_string, get_column_letter


CONDITIONS = [
    "Cond1_Sys_LLM",
    "Cond2_Sys_Rule",
    "Cond3_Worker_LLM",
    "Cond4_Worker_Rule",
    "Cond5_Control_NoInterv",
]

PARTICIPANT_COLUMNS = [
    "Participant_ID", "Condition", "Trial", "Trial_Result", "Is_Abandoned",
    "Stop_Reason", "Official_Cycle_Time_s",
    "Behavioral_Task_Time_s", "Work_Time_s", "Rest_Time_s", "Rest_Ratio",
    "Work_Angle_Median_deg", "Work_Target_Signed_Error_deg", "Work_Target_MAE_deg",
    "Work_Angle_SD_deg", "Work_Angle_P90_P10_Range_deg", "Target_Angle_deg",
    "Risky_Time_s", "Avg_RULA_Proxy", "RULA_High_Time_s", "Response_Action",
    "Adjustment_Z_mm", "LLM_Latency_s", "LLM_Fallback", "Include_Time_Posture",
    "Include_Target_Posture", "Condition_Fully_Completed",
    "Include_Complete_Condition_Trend",
]

SUMMARY_COLUMNS = [
    "Participant_ID", "Condition", "N_Attempted", "N_Success", "N_Fail",
    "Success_Rate", "Failure_Rate", "N_Analyzed_Trials_Excl_T1",
    "Mean_Work_Time_Excl_T1_s", "Mean_Rest_Time_Excl_T1_s",
    "Mean_Rest_Ratio_Excl_T1", "N_Targeted_Trials_Excl_T1",
    "Mean_Target_Signed_Error_deg", "Mean_Target_MAE_deg",
    "Mean_Work_Angle_SD_deg", "Mean_Work_Angle_P90_P10_Range_deg",
    "Condition_Fully_Completed", "Include_Complete_Condition_Trend",
]

TREND_COLUMNS = [
    "Condition", "N_Participants", "N_Condition_Success_Participants",
    "N_Condition_Failure_Participants", "Condition_Success_Rate", "Condition_Failure_Rate",
    "N_Attempted_Total", "N_Success_Total", "N_Fail_Total",
    "Success_Rate_Total", "Failure_Rate_Total", "N_Successful_Trial_Time_Conditions",
    "N_Successful_Trial_Targeted_Conditions", "N_Complete_Targeted_Participants",
    "Mean_Successful_Trial_Work_Time_Excl_T1_s", "SD_Successful_Trial_Work_Time_Excl_T1_s",
    "Mean_Successful_Trial_Rest_Time_Excl_T1_s", "SD_Successful_Trial_Rest_Time_Excl_T1_s",
    "Mean_Successful_Trial_Rest_Ratio_Excl_T1",
    "Mean_Successful_Trial_Work_Angle_Median_deg", "SD_Successful_Trial_Work_Angle_Median_deg",
    "Mean_Successful_Trial_Work_Target_Signed_Error_deg", "SD_Successful_Trial_Work_Target_Signed_Error_deg",
    "Mean_Successful_Trial_Target_MAE_deg", "SD_Successful_Trial_Target_MAE_deg",
    "Mean_Successful_Trial_Work_Angle_SD_deg", "SD_Successful_Trial_Work_Angle_SD_deg",
    "Mean_Successful_Trial_Work_Angle_P90_P10_Range_deg", "SD_Successful_Trial_Work_Angle_P90_P10_Range_deg",
    "Mean_Complete_Condition_Work_Time_Excl_T1_s", "SD_Complete_Condition_Work_Time_Excl_T1_s",
    "Mean_Complete_Condition_Rest_Time_Excl_T1_s", "SD_Complete_Condition_Rest_Time_Excl_T1_s",
    "Mean_Complete_Condition_Rest_Ratio_Excl_T1",
    "Mean_Complete_Condition_Work_Angle_Median_deg", "SD_Complete_Condition_Work_Angle_Median_deg",
    "Mean_Complete_Condition_Work_Target_Signed_Error_deg", "SD_Complete_Condition_Work_Target_Signed_Error_deg",
    "Mean_Complete_Condition_Target_MAE_deg", "SD_Complete_Condition_Target_MAE_deg",
    "Mean_Complete_Condition_Work_Angle_SD_deg", "SD_Complete_Condition_Work_Angle_SD_deg",
    "Mean_Complete_Condition_Work_Angle_P90_P10_Range_deg", "SD_Complete_Condition_Work_Angle_P90_P10_Range_deg",
]

METRIC_FILE_RE = re.compile(r"^(?P<participant>[^_]+)_all_conditions_trial_metrics\.csv$", re.I)
RAW_FILE_RE = re.compile(
    r"^(?P<participant>[^_]+)_(?P<condition>Cond[1-5]_.+)_experiment_raw_data_per_trial\.csv$",
    re.I,
)


@dataclass
class ParticipantFiles:
    metric_files: list[Path] = field(default_factory=list)
    raw_files: dict[str, list[Path]] = field(default_factory=lambda: defaultdict(list))


@dataclass
class ValidationMessage:
    participant_id: str
    level: str
    check: str
    detail: str


def excel_column(index: int) -> str:
    """Convert a 1-based column number to an Excel column reference."""
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def as_number(value: object) -> float | None:
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def text_or_blank(value: object) -> str:
    return "" if value is None else str(value).strip()


def safe_sheet_name(value: str, used_names: set[str]) -> str:
    clean = re.sub(r"[\\/*?:\[\]]", "_", value).strip() or "Participant"
    clean = clean[:31]
    candidate = clean
    suffix = 2
    while candidate.lower() in used_names:
        ending = f"_{suffix}"
        candidate = clean[: 31 - len(ending)] + ending
        suffix += 1
    used_names.add(candidate.lower())
    return candidate


def quote_sheet_name(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def choose_root() -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="Select the top-level experiment-data folder")
        root.destroy()
        return Path(selected) if selected else None
    except Exception as error:  # pragma: no cover - environment-specific GUI fallback
        print(f"Folder dialog could not be opened: {error}", file=sys.stderr)
        return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def discover_files(root: Path) -> dict[str, ParticipantFiles]:
    participants: dict[str, ParticipantFiles] = defaultdict(ParticipantFiles)
    for path in root.rglob("*.csv"):
        metric_match = METRIC_FILE_RE.fullmatch(path.name)
        if metric_match:
            participants[metric_match.group("participant")].metric_files.append(path)
            continue
        raw_match = RAW_FILE_RE.fullmatch(path.name)
        if raw_match:
            participants[raw_match.group("participant")].raw_files[raw_match.group("condition")].append(path)
    return dict(participants)


def select_unique_file(
    participant_id: str,
    kind: str,
    files: list[Path],
    messages: list[ValidationMessage],
) -> Path | None:
    if not files:
        messages.append(ValidationMessage(participant_id, "ERROR", kind, "File not found."))
        return None
    if len(files) > 1:
        paths = " | ".join(str(path) for path in sorted(files))
        messages.append(ValidationMessage(participant_id, "ERROR", kind, f"Duplicate files found: {paths}"))
        return None
    return files[0]


def collect_participant_rows(
    participant_id: str,
    files: ParticipantFiles,
    messages: list[ValidationMessage],
) -> list[dict[str, str]]:
    """Join the all-condition metrics with raw trial records for one participant."""
    metric_path = select_unique_file(participant_id, "All-condition metrics", files.metric_files, messages)
    raw_paths: dict[str, Path] = {}
    for condition in CONDITIONS:
        path = select_unique_file(
            participant_id,
            f"Raw file: {condition}",
            files.raw_files.get(condition, []),
            messages,
        )
        if path:
            raw_paths[condition] = path

    if metric_path is None or len(raw_paths) != len(CONDITIONS):
        messages.append(ValidationMessage(
            participant_id,
            "ERROR",
            "Participant processing",
            "Excluded from summary sheets because the required six files are not uniquely available.",
        ))
        return []

    try:
        metric_rows = read_csv_rows(metric_path)
    except (OSError, csv.Error) as error:
        messages.append(ValidationMessage(participant_id, "ERROR", "All-condition metrics", f"Could not read file: {error}"))
        return []

    raw_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for condition, raw_path in raw_paths.items():
        try:
            raw_rows = read_csv_rows(raw_path)
        except (OSError, csv.Error) as error:
            messages.append(ValidationMessage(participant_id, "ERROR", f"Raw file: {condition}", f"Could not read file: {error}"))
            return []
        for raw in raw_rows:
            key = (text_or_blank(raw.get("Condition")) or condition, text_or_blank(raw.get("Trial_Num")))
            if key in raw_by_key:
                messages.append(ValidationMessage(participant_id, "ERROR", "Raw trial key", f"Duplicate Condition + Trial_Num: {key[0]} / {key[1]}"))
            raw_by_key[key] = raw

    records: list[dict[str, str]] = []
    seen_metric_keys: set[tuple[str, str]] = set()
    for metric in metric_rows:
        condition = text_or_blank(metric.get("Condition"))
        trial = text_or_blank(metric.get("Trial"))
        key = (condition, trial)
        if key in seen_metric_keys:
            messages.append(ValidationMessage(participant_id, "ERROR", "Metrics trial key", f"Duplicate Condition + Trial: {condition} / {trial}"))
            continue
        seen_metric_keys.add(key)
        raw = raw_by_key.get(key)
        if raw is None:
            messages.append(ValidationMessage(participant_id, "ERROR", "Trial join", f"No raw record matched metrics row: {condition} / {trial}"))
            continue

        row = {
            "Participant_ID": participant_id,
            "Condition": condition,
            "Trial": trial,
            "Lead_Type": text_or_blank(raw.get("Lead_Type")),
            "Control_Type": text_or_blank(raw.get("Control_Type")),
            "Trial_Result": text_or_blank(raw.get("Trial_Result")),
            "Is_Abandoned": text_or_blank(raw.get("Is_Abandoned")),
            "Stop_Reason": text_or_blank(raw.get("Stop_Reason")),
            "Official_Cycle_Time_s": text_or_blank(metric.get("Official_Cycle_Time_s")),
            "Behavioral_Task_Time_s": text_or_blank(metric.get("Behavioral_Task_Time_s")),
            "Work_Time_s": text_or_blank(metric.get("Work_Time_s")),
            "Rest_Time_s": text_or_blank(metric.get("Rest_Time_s")),
            "Rest_Ratio": text_or_blank(metric.get("Rest_Ratio")),
            "Work_Angle_Median_deg": text_or_blank(metric.get("Work_Angle_Median_deg")),
            "Work_Target_Signed_Error_deg": text_or_blank(metric.get("Work_Target_Signed_Error_deg")),
            "Work_Target_MAE_deg": text_or_blank(metric.get("Work_Target_MAE_deg")),
            "Work_Angle_SD_deg": text_or_blank(metric.get("Work_Angle_SD_deg")),
            "Work_Angle_P90_P10_Range_deg": text_or_blank(metric.get("Work_Angle_P90_P10_Range_deg")),
            "Target_Angle_deg": text_or_blank(metric.get("Target_Angle_deg")),
            "Risky_Time_s": text_or_blank(raw.get("Risky_Time_s")),
            "Avg_RULA_Proxy": text_or_blank(raw.get("Avg_RULA_Proxy")),
            "RULA_High_Time_s": text_or_blank(raw.get("RULA_High_Time_s")),
            "Response_Action": text_or_blank(raw.get("Response_Action")),
            "Adjustment_Z_mm": text_or_blank(raw.get("Adjustment_Z_mm")),
            "LLM_Latency_s": text_or_blank(raw.get("LLM_Latency_s")),
            "LLM_Fallback": text_or_blank(raw.get("LLM_Fallback")),
        }
        records.append(row)

    # Failed or abandoned trials may have a raw record but no posture-analysis
    # files.  Keep those attempts so failure rates remain correct; leave the
    # posture metrics blank so they cannot enter time/posture averages.
    extra_raw_keys = set(raw_by_key) - seen_metric_keys
    for condition, trial in sorted(extra_raw_keys):
        raw = raw_by_key[(condition, trial)]
        records.append({
            "Participant_ID": participant_id,
            "Condition": condition,
            "Trial": trial,
            "Lead_Type": text_or_blank(raw.get("Lead_Type")),
            "Control_Type": text_or_blank(raw.get("Control_Type")),
            "Trial_Result": text_or_blank(raw.get("Trial_Result")),
            "Is_Abandoned": text_or_blank(raw.get("Is_Abandoned")),
            "Stop_Reason": text_or_blank(raw.get("Stop_Reason")),
            "Official_Cycle_Time_s": "",
            "Behavioral_Task_Time_s": "",
            "Work_Time_s": "",
            "Rest_Time_s": "",
            "Rest_Ratio": "",
            "Work_Angle_Median_deg": "",
            "Work_Target_Signed_Error_deg": "",
            "Work_Target_MAE_deg": "",
            "Work_Angle_SD_deg": "",
            "Work_Angle_P90_P10_Range_deg": "",
            "Target_Angle_deg": "",
            "Risky_Time_s": text_or_blank(raw.get("Risky_Time_s")),
            "Avg_RULA_Proxy": text_or_blank(raw.get("Avg_RULA_Proxy")),
            "RULA_High_Time_s": text_or_blank(raw.get("RULA_High_Time_s")),
            "Response_Action": text_or_blank(raw.get("Response_Action")),
            "Adjustment_Z_mm": text_or_blank(raw.get("Adjustment_Z_mm")),
            "LLM_Latency_s": text_or_blank(raw.get("LLM_Latency_s")),
            "LLM_Fallback": text_or_blank(raw.get("LLM_Fallback")),
        })
        messages.append(ValidationMessage(
            participant_id,
            "WARNING",
            "Posture-metric coverage",
            f"Raw trial is retained for outcome counts but has no posture metrics: {condition} / {trial}",
        ))

    expected = len(CONDITIONS) * 5
    if len(records) != expected:
        messages.append(ValidationMessage(
            participant_id,
            "WARNING",
            "Joined trial count",
            f"Joined {len(records)} rows; expected {expected} for five conditions with five trials each.",
        ))
    else:
        messages.append(ValidationMessage(participant_id, "OK", "Joined trial count", "25 metrics/raw trial records joined."))

    return sorted(records, key=lambda row: (CONDITIONS.index(row["Condition"]) if row["Condition"] in CONDITIONS else 99, as_number(row["Trial"]) or 0))


def xml_text(value: object) -> str:
    return str(value).replace("\x00", "")


class XlsxWriter:
    """A small dependency-free XLSX writer for the tables used by this script."""

    def __init__(self) -> None:
        self.sheets: list[dict[str, object]] = []

    def add_sheet(
        self,
        name: str,
        rows: list[list[object]],
        header_rows: int = 1,
        widths: list[float] | None = None,
        freeze_row: int | None = None,
        filter_range: str | None = None,
        merges: list[str] | None = None,
    ) -> None:
        self.sheets.append({
            "name": name,
            "rows": rows,
            "header_rows": header_rows,
            "widths": widths or [],
            "freeze_row": freeze_row,
            "filter_range": filter_range,
            "merges": merges or [],
        })

    @staticmethod
    def _cell_xml(column: int, row: int, value: object, style: int) -> str:
        ref = f"{excel_column(column)}{row}"
        if value is None or value == "":
            return f'<c r="{ref}" s="{style}"/>'
        if isinstance(value, str) and value.startswith("="):
            formula = xml_escape(value[1:])
            return f'<c r="{ref}" s="{style}"><f>{formula}</f></c>'
        if isinstance(value, bool):
            return f'<c r="{ref}" s="{style}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        escaped = xml_text(value)
        escaped = escaped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{escaped}</t></is></c>'

    def _worksheet_xml(self, sheet: dict[str, object]) -> str:
        rows: list[list[object]] = sheet["rows"]  # type: ignore[assignment]
        header_rows = int(sheet["header_rows"])
        max_col = max((len(row) for row in rows), default=1)
        max_row = max(len(rows), 1)
        xml: list[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            f'<dimension ref="A1:{excel_column(max_col)}{max_row}"/>',
            '<sheetViews><sheetView workbookViewId="0">',
        ]
        freeze_row = sheet["freeze_row"]
        if freeze_row:
            xml.append(f'<pane ySplit="{freeze_row}" topLeftCell="A{int(freeze_row) + 1}" activePane="bottomLeft" state="frozen"/>')
        xml.append('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>')

        widths: list[float] = sheet["widths"]  # type: ignore[assignment]
        if widths:
            xml.append('<cols>')
            for index, width in enumerate(widths, start=1):
                xml.append(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>')
            xml.append('</cols>')

        xml.append('<sheetData>')
        for row_index, row in enumerate(rows, start=1):
            row_style = 1 if row_index <= header_rows else 0
            cells = [self._cell_xml(column, row_index, value, row_style) for column, value in enumerate(row, start=1)]
            xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
        xml.append('</sheetData>')

        merges: list[str] = sheet["merges"]  # type: ignore[assignment]
        if merges:
            xml.append(f'<mergeCells count="{len(merges)}">')
            xml.extend(f'<mergeCell ref="{ref}"/>' for ref in merges)
            xml.append('</mergeCells>')
        filter_range = sheet["filter_range"]
        if filter_range:
            xml.append(f'<autoFilter ref="{filter_range}"/>')
        xml.append('</worksheet>')
        return "".join(xml)

    def write(self, path: Path) -> None:
        workbook = Workbook()
        workbook.remove(workbook.active)
        workbook.properties.creator = "Codex"
        workbook.properties.title = "HRI Interim Analysis Workbook"
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        for sheet in self.sheets:
            worksheet = workbook.create_sheet(str(sheet["name"]))
            rows: list[list[object]] = sheet["rows"]  # type: ignore[assignment]
            header_rows = int(sheet["header_rows"])
            for row_index, row in enumerate(rows, start=1):
                for column_index, value in enumerate(row, start=1):
                    cell = worksheet.cell(row=row_index, column=column_index, value=value)

            widths: list[float] = sheet["widths"]  # type: ignore[assignment]
            for column_index, width in enumerate(widths, start=1):
                worksheet.column_dimensions[get_column_letter(column_index)].width = width
            if sheet["freeze_row"]:
                worksheet.freeze_panes = f"A{int(sheet['freeze_row']) + 1}"
            if sheet["filter_range"]:
                worksheet.auto_filter.ref = str(sheet["filter_range"])
            for merge_range in sheet["merges"]:  # type: ignore[union-attr]
                worksheet.merge_cells(str(merge_range))

        workbook.save(path)


def participant_sheet_rows(records: list[dict[str, str]]) -> list[list[object]]:
    rows: list[list[object]] = [PARTICIPANT_COLUMNS]
    for excel_row, record in enumerate(records, start=2):
        values: list[object] = []
        for column in PARTICIPANT_COLUMNS:
            if column == "Include_Time_Posture":
                values.append(f'=IF(AND(C{excel_row}>1,D{excel_row}="success",I{excel_row}<>""),1,0)')
            elif column == "Include_Target_Posture":
                values.append(f'=IF(AND(Y{excel_row}=1,Q{excel_row}<>""),1,0)')
            elif column == "Condition_Fully_Completed":
                values.append(
                    f'=IF(AND(COUNTIF($B$2:$B$26,B{excel_row})=5,'
                    f'COUNTIFS($B$2:$B$26,B{excel_row},$D$2:$D$26,"success")=5,'
                    f'COUNTIFS($B$2:$B$26,B{excel_row},$D$2:$D$26,"fail")=0),"Yes","No")'
                )
            elif column == "Include_Complete_Condition_Trend":
                values.append(f'=IF(AA{excel_row}="Yes",1,0)')
            else:
                number = as_number(record.get(column, ""))
                values.append(number if number is not None else record.get(column, ""))
        rows.append(values)
    return rows


def build_summary_rows(sheet_map: dict[str, str]) -> list[list[object]]:
    rows: list[list[object]] = [SUMMARY_COLUMNS]
    for participant_id in sorted(sheet_map, key=lambda value: (as_number(value) is None, as_number(value) or 0, value)):
        sheet = quote_sheet_name(sheet_map[participant_id])
        for condition in CONDITIONS:
            # Each participant sheet uses the same standardized schema and has at most 25 trial rows.
            condition_ref = f'{sheet}!$B$2:$B$26'
            result_ref = f'{sheet}!$F$2:$F$26'
            time_flag_ref = f'{sheet}!$AA$2:$AA$26'
            target_flag_ref = f'{sheet}!$AB$2:$AB$26'
            rows.append([
                participant_id,
                condition,
                f'=COUNTIF({condition_ref},B{len(rows) + 1})',
                f'=COUNTIFS({condition_ref},B{len(rows) + 1},{result_ref},"success")',
                f'=COUNTIFS({condition_ref},B{len(rows) + 1},{result_ref},"fail")',
                f'=IF(C{len(rows) + 1}=0,"",D{len(rows) + 1}/C{len(rows) + 1})',
                f'=IF(C{len(rows) + 1}=0,"",E{len(rows) + 1}/C{len(rows) + 1})',
                f'=COUNTIFS({condition_ref},B{len(rows) + 1},{time_flag_ref},1)',
                f'=IFERROR(AVERAGEIFS({sheet}!$K$2:$K$26,{condition_ref},B{len(rows) + 1},{time_flag_ref},1),"")',
                f'=IFERROR(AVERAGEIFS({sheet}!$L$2:$L$26,{condition_ref},B{len(rows) + 1},{time_flag_ref},1),"")',
                f'=IFERROR(AVERAGEIFS({sheet}!$M$2:$M$26,{condition_ref},B{len(rows) + 1},{time_flag_ref},1),"")',
                f'=COUNTIFS({condition_ref},B{len(rows) + 1},{target_flag_ref},1)',
                f'=IFERROR(AVERAGEIFS({sheet}!$O$2:$O$26,{condition_ref},B{len(rows) + 1},{target_flag_ref},1),"")',
                f'=IFERROR(AVERAGEIFS({sheet}!$P$2:$P$26,{condition_ref},B{len(rows) + 1},{target_flag_ref},1),"")',
                f'=IFERROR(AVERAGEIFS({sheet}!$Q$2:$Q$26,{condition_ref},B{len(rows) + 1},{target_flag_ref},1),"")',
                f'=IFERROR(AVERAGEIFS({sheet}!$R$2:$R$26,{condition_ref},B{len(rows) + 1},{target_flag_ref},1),"")',
                f'=IF(AND(C{len(rows) + 1}=5,D{len(rows) + 1}=5,E{len(rows) + 1}=0),"Yes","No")',
                f'=IF(Q{len(rows) + 1}="Yes",1,0)',
            ])
    return rows


def build_detailed_trend_rows(sheet_map: dict[str, str]) -> list[list[object]]:
    """Build formulas that read participant sheets directly; no intermediate sheet."""
    ordered_sheets = [quote_sheet_name(sheet_map[participant_id]) for participant_id in sorted(
        sheet_map,
        key=lambda value: (as_number(value) is None, as_number(value) or 0, value),
    )]

    def sum_formula(terms: list[str]) -> str:
        return "=SUM(" + ",".join(terms) + ")"

    def average_formula(terms: list[str], denominator_cell: str) -> str:
        """Average participant-condition means without passing empty strings to AVERAGE.

        Excel treats a formula-produced empty string as an invalid direct AVERAGE
        argument.  Using a zero fallback and dividing by the independently
        calculated eligible-participant count avoids that issue.
        """
        return '=IFERROR(SUM(' + ",".join(terms) + f')/{denominator_cell},"")'

    def stdev_formula(terms: list[str], denominator_cell: str, mean_cell: str) -> str:
        squares = [f'({term})^2' for term in terms]
        return (
            '=IFERROR(SQRT((SUM(' + ",".join(squares) + f')-{denominator_cell}*{mean_cell}^2)'
            + f'/({denominator_cell}-1)),"")'
        )

    rows: list[list[object]] = [TREND_COLUMNS]
    for condition in CONDITIONS:
        row_num = len(rows) + 1
        condition_cell = f"$A{row_num}"
        per_sheet: list[dict[str, str]] = []
        for sheet in ordered_sheets:
            per_sheet.append({
                "condition": f"{sheet}!$B$2:$B$26",
                "result": f"{sheet}!$D$2:$D$26",
                "work": f"{sheet}!$I$2:$I$26",
                "rest": f"{sheet}!$J$2:$J$26",
                "rest_ratio": f"{sheet}!$K$2:$K$26",
                "work_angle_median": f"{sheet}!$L$2:$L$26",
                "signed_error": f"{sheet}!$M$2:$M$26",
                "target_mae": f"{sheet}!$N$2:$N$26",
                "work_sd": f"{sheet}!$O$2:$O$26",
                "work_angle_range": f"{sheet}!$P$2:$P$26",
                "include_time": f"{sheet}!$Y$2:$Y$26",
                "include_target": f"{sheet}!$Z$2:$Z$26",
                "include_complete": f"{sheet}!$AB$2:$AB$26",
            })

        participant_terms = [f"IF(COUNTIF({r['condition']},{condition_cell})>0,1,0)" for r in per_sheet]
        attempted_terms = [f"COUNTIF({r['condition']},{condition_cell})" for r in per_sheet]
        success_terms = [f"COUNTIFS({r['condition']},{condition_cell},{r['result']},\"success\")" for r in per_sheet]
        fail_terms = [f"COUNTIFS({r['condition']},{condition_cell},{r['result']},\"fail\")" for r in per_sheet]
        time_count_terms = [f"IF(COUNTIFS({r['condition']},{condition_cell},{r['include_time']},1)>0,1,0)" for r in per_sheet]
        complete_count_terms = [f"IF(COUNTIFS({r['condition']},{condition_cell},{r['include_complete']},1)>0,1,0)" for r in per_sheet]
        target_count_terms = [f"IF(COUNTIFS({r['condition']},{condition_cell},{r['include_target']},1)>0,1,0)" for r in per_sheet]
        complete_target_count_terms = [f"IF(COUNTIFS({r['condition']},{condition_cell},{r['include_complete']},1,{r['include_target']},1)>0,1,0)" for r in per_sheet]

        def participant_average(value_key: str, criteria: str) -> list[str]:
            terms: list[str] = []
            for r in per_sheet:
                if criteria == "successful":
                    terms.append(f'IFERROR(AVERAGEIFS({r[value_key]},{r["condition"]},{condition_cell},{r["include_time"]},1),0)')
                elif criteria == "targeted":
                    terms.append(f'IFERROR(AVERAGEIFS({r[value_key]},{r["condition"]},{condition_cell},{r["include_target"]},1),0)')
                elif criteria == "complete":
                    terms.append(f'IFERROR(AVERAGEIFS({r[value_key]},{r["condition"]},{condition_cell},{r["include_complete"]},1,{r["include_time"]},1),0)')
                else:  # complete_targeted
                    terms.append(f'IFERROR(AVERAGEIFS({r[value_key]},{r["condition"]},{condition_cell},{r["include_complete"]},1,{r["include_target"]},1),0)')
            return terms

        successful_work = participant_average("work", "successful")
        successful_rest = participant_average("rest", "successful")
        successful_rest_ratio = participant_average("rest_ratio", "successful")
        successful_work_angle_median = participant_average("work_angle_median", "successful")
        successful_signed_error = participant_average("signed_error", "targeted")
        successful_target_mae = participant_average("target_mae", "targeted")
        successful_work_sd = participant_average("work_sd", "successful")
        successful_work_angle_range = participant_average("work_angle_range", "successful")
        complete_work = participant_average("work", "complete")
        complete_rest = participant_average("rest", "complete")
        complete_rest_ratio = participant_average("rest_ratio", "complete")
        complete_work_angle_median = participant_average("work_angle_median", "complete")
        complete_signed_error = participant_average("signed_error", "complete_targeted")
        complete_target_mae = participant_average("target_mae", "complete_targeted")
        complete_work_sd = participant_average("work_sd", "complete")
        complete_work_angle_range = participant_average("work_angle_range", "complete")

        rows.append([
            condition,
            sum_formula(participant_terms),
            sum_formula(complete_count_terms),
            f'=B{row_num}-C{row_num}',
            f'=IF(B{row_num}=0,"",C{row_num}/B{row_num})',
            f'=IF(B{row_num}=0,"",D{row_num}/B{row_num})',
            sum_formula(attempted_terms),
            sum_formula(success_terms),
            sum_formula(fail_terms),
            f'=IF(G{row_num}=0,"",H{row_num}/G{row_num})',
            f'=IF(G{row_num}=0,"",I{row_num}/G{row_num})',
            sum_formula(time_count_terms),
            sum_formula(target_count_terms),
            sum_formula(complete_target_count_terms),
            average_formula(successful_work, f"L{row_num}"),
            stdev_formula(successful_work, f"L{row_num}", f"O{row_num}"),
            average_formula(successful_rest, f"L{row_num}"),
            stdev_formula(successful_rest, f"L{row_num}", f"Q{row_num}"),
            average_formula(successful_rest_ratio, f"L{row_num}"),
            average_formula(successful_work_angle_median, f"L{row_num}"),
            stdev_formula(successful_work_angle_median, f"L{row_num}", f"T{row_num}"),
            average_formula(successful_signed_error, f"M{row_num}"),
            stdev_formula(successful_signed_error, f"M{row_num}", f"V{row_num}"),
            average_formula(successful_target_mae, f"M{row_num}"),
            stdev_formula(successful_target_mae, f"M{row_num}", f"X{row_num}"),
            average_formula(successful_work_sd, f"L{row_num}"),
            stdev_formula(successful_work_sd, f"L{row_num}", f"Z{row_num}"),
            average_formula(successful_work_angle_range, f"L{row_num}"),
            stdev_formula(successful_work_angle_range, f"L{row_num}", f"AB{row_num}"),
            average_formula(complete_work, f"C{row_num}"),
            stdev_formula(complete_work, f"C{row_num}", f"AD{row_num}"),
            average_formula(complete_rest, f"C{row_num}"),
            stdev_formula(complete_rest, f"C{row_num}", f"AF{row_num}"),
            average_formula(complete_rest_ratio, f"C{row_num}"),
            average_formula(complete_work_angle_median, f"C{row_num}"),
            stdev_formula(complete_work_angle_median, f"C{row_num}", f"AI{row_num}"),
            average_formula(complete_signed_error, f"N{row_num}"),
            stdev_formula(complete_signed_error, f"N{row_num}", f"AK{row_num}"),
            average_formula(complete_target_mae, f"N{row_num}"),
            stdev_formula(complete_target_mae, f"N{row_num}", f"AM{row_num}"),
            average_formula(complete_work_sd, f"C{row_num}"),
            stdev_formula(complete_work_sd, f"C{row_num}", f"AO{row_num}"),
            average_formula(complete_work_angle_range, f"C{row_num}"),
            stdev_formula(complete_work_angle_range, f"C{row_num}", f"AQ{row_num}"),
        ])
    return rows


def build_trend_rows(sheet_map: dict[str, str]) -> list[list[object]]:
    """Rearrange the established detailed formulas into four vertical blocks."""
    detailed = build_detailed_trend_rows(sheet_map)

    def value_at(row: list[object], column: str) -> object:
        return row[column_index_from_string(column) - 1]

    def move_formula(
        value: object,
        source_row: int,
        target_row: int,
        column_map: dict[str, str],
    ) -> object:
        if not isinstance(value, str) or not value.startswith("="):
            return value
        moved = value
        for source_column in sorted(column_map, key=len, reverse=True):
            target_column = column_map[source_column]
            pattern = re.compile(
                rf"(?<![A-Z])(?P<absolute>\$?){source_column}{source_row}(?!\d)"
            )
            moved = pattern.sub(
                lambda match: f"{match.group('absolute')}{target_column}{target_row}",
                moved,
            )
        return moved

    def moved_values(
        detailed_row: list[object],
        source_row: int,
        target_row: int,
        source_columns: list[str],
        column_map: dict[str, str],
    ) -> list[object]:
        return [
            move_formula(value_at(detailed_row, column), source_row, target_row, column_map)
            for column in source_columns
        ]

    rows: list[list[object]] = []

    rows.append(["1. Condition Completion"])
    rows.append([
        "Condition", "N_Participants", "N_Condition_Success", "N_Condition_Failure",
        "Condition_Success_Rate", "Condition_Failure_Rate", "N_Attempted_Trials",
        "N_Success_Trials", "N_Fail_Trials", "Trial_Success_Rate", "Trial_Failure_Rate",
    ])
    count_map = {column: column for column in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]}
    for index, condition in enumerate(CONDITIONS, start=1):
        source_row = index + 1
        target_row = len(rows) + 1
        detailed_row = detailed[index]
        rows.append(moved_values(
            detailed_row, source_row, target_row,
            ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"],
            count_map,
        ))
    rows.append([])

    rows.append(["2. Successful Trial Time and Rest (Trial 2-5; failed trials excluded)"])
    rows.append([
        "Condition", "N_Analyzed_Participant_Conditions", "Mean_Work_Time_s", "SD_Work_Time_s",
        "Mean_Rest_Time_s", "SD_Rest_Time_s", "Mean_Rest_Ratio",
    ])
    successful_time_map = {"A": "A", "L": "B", "O": "C", "Q": "E"}
    for index, condition in enumerate(CONDITIONS, start=1):
        source_row = index + 1
        target_row = len(rows) + 1
        detailed_row = detailed[index]
        rows.append(moved_values(
            detailed_row, source_row, target_row,
            ["A", "L", "O", "P", "Q", "R", "S"],
            successful_time_map,
        ))
    rows.append([])

    rows.append(["3. Complete Condition Time and Rest (five successful trials required)"])
    rows.append([
        "Condition", "N_Complete_Participant_Conditions", "Mean_Work_Time_s", "SD_Work_Time_s",
        "Mean_Rest_Time_s", "SD_Rest_Time_s", "Mean_Rest_Ratio",
    ])
    complete_time_map = {"A": "A", "C": "B", "AD": "C", "AF": "E"}
    for index, condition in enumerate(CONDITIONS, start=1):
        source_row = index + 1
        target_row = len(rows) + 1
        detailed_row = detailed[index]
        rows.append(moved_values(
            detailed_row, source_row, target_row,
            ["A", "C", "AD", "AE", "AF", "AG", "AH"],
            complete_time_map,
        ))
    rows.append([])

    rows.append(["4. Posture Guidance and Stability"])
    rows.append([
        "Condition", "Analysis_Basis", "N_Time_Eligible", "N_Target_Eligible",
        "Mean_Actual_Angle_deg", "SD_Actual_Angle_Between_Participants_deg",
        "Mean_Signed_Error_deg", "SD_Signed_Error_Between_Participants_deg",
        "Mean_Target_MAE_deg", "SD_Target_MAE_Between_Participants_deg",
        "Mean_Within_Trial_Angle_SD_deg", "SD_Within_Trial_Angle_SD_Between_Participants_deg",
        "Mean_Within_Trial_Angle_Range_deg", "SD_Within_Trial_Angle_Range_Between_Participants_deg",
    ])
    successful_posture_map = {
        "A": "A", "L": "C", "M": "D", "T": "E", "V": "G",
        "X": "I", "Z": "K", "AB": "M",
    }
    complete_posture_map = {
        "A": "A", "C": "C", "N": "D", "AI": "E", "AK": "G",
        "AM": "I", "AO": "K", "AQ": "M",
    }
    for index, condition in enumerate(CONDITIONS, start=1):
        source_row = index + 1
        detailed_row = detailed[index]

        target_row = len(rows) + 1
        successful_values = moved_values(
            detailed_row, source_row, target_row,
            ["A", "L", "M", "T", "U", "V", "W", "X", "Y", "Z", "AA", "AB", "AC"],
            successful_posture_map,
        )
        rows.append([successful_values[0], "Successful_Trial", *successful_values[1:]])

        target_row = len(rows) + 1
        complete_values = moved_values(
            detailed_row, source_row, target_row,
            ["A", "C", "N", "AI", "AJ", "AK", "AL", "AM", "AN", "AO", "AP", "AQ", "AR"],
            complete_posture_map,
        )
        rows.append([complete_values[0], "Complete_Condition", *complete_values[1:]])

    return rows


def dictionary_rows() -> list[list[object]]:
    return [
        ["Sheet", "Purpose", "Rule"],
        ["Participant sheets", "One joined trial-level table per participant", "All-condition metrics are the primary source for work/rest/target metrics. Raw files provide outcome, risk, RULA, robot, and LLM fields."],
        ["Participant_Condition_Summary", "One row per Participant_ID × Condition", "Failure rates include all trials. Time/rest/posture means use successful trials 2–5 only. A condition is fully completed only when all five trials succeed."],
        ["Overall_Condition_Trends", "One row per condition", "Failure rates use all attempted trials. Successful-trial trends exclude only failed trials. Complete-condition trends exclude an entire participant-condition row unless all five trials succeeded."],
        ["Target-posture analysis", "Target guidance quality", "Only trials with Target_Angle_deg and Include_Time_Posture=1 are included."],
        ["Validation", "Input audit trail", "ERROR rows identify missing, duplicate, unreadable, or unmatched input files and trial records."],
    ]


def validation_rows(messages: Iterable[ValidationMessage]) -> list[list[object]]:
    rows: list[list[object]] = [["Participant_ID", "Level", "Check", "Detail"]]
    rows.extend([[message.participant_id, message.level, message.check, message.detail] for message in messages])
    return rows


def create_workbook(root: Path, output: Path) -> tuple[int, list[ValidationMessage]]:
    messages: list[ValidationMessage] = []
    discovered = discover_files(root)
    if not discovered:
        messages.append(ValidationMessage("", "ERROR", "File discovery", "No matching CSV files found in the selected folder tree."))

    participant_rows: dict[str, list[dict[str, str]]] = {}
    for participant_id in sorted(discovered, key=lambda value: (as_number(value) is None, as_number(value) or 0, value)):
        records = collect_participant_rows(participant_id, discovered[participant_id], messages)
        if records:
            participant_rows[participant_id] = records

    if not participant_rows:
        messages.append(ValidationMessage("", "ERROR", "Workbook", "No complete participant dataset was available for summary creation."))

    writer = XlsxWriter()
    used_sheet_names: set[str] = set()

    sheet_map: dict[str, str] = {}
    for participant_id, records in participant_rows.items():
        sheet_name = safe_sheet_name(f"P{participant_id}", used_sheet_names)
        sheet_map[participant_id] = sheet_name
        rows = participant_sheet_rows(records)
        writer.add_sheet(
            sheet_name,
            rows,
            widths=[14, 22, 8, 12, 12, 13, 13, 22] + [18] * (len(PARTICIPANT_COLUMNS) - 8),
            freeze_row=1,
            filter_range=f"A1:{excel_column(len(PARTICIPANT_COLUMNS))}{len(rows)}",
        )

    trend_rows = build_trend_rows(sheet_map)
    writer.add_sheet(
        "Overall_Condition_Trends",
        trend_rows,
        widths=[26, 28] + [22] * 12,
    )
    writer.add_sheet(
        "Validation",
        validation_rows(messages),
        widths=[16, 12, 28, 115],
        freeze_row=1,
        filter_range=f"A1:D{max(1, len(messages) + 1)}",
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    writer.write(output)
    return len(participant_rows), messages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, help="Top-level folder to search recursively")
    parser.add_argument("--output", "-o", type=Path, help="Output .xlsx path; defaults to the selected root folder")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root or choose_root()
    if root is None:
        print("No folder selected. No workbook was created.")
        return
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Folder does not exist: {root}")
    output = args.output or root / "HRI_Interim_Analysis_Workbook.xlsx"
    if output.suffix.lower() != ".xlsx":
        output = output.with_suffix(".xlsx")
    try:
        participants, messages = create_workbook(root, output)
    except PermissionError as error:
        if output.exists():
            message = f"Could not overwrite {output}. Close the workbook in Excel or any preview pane, then run again."
        else:
            message = f"Could not create {output}. Check that the output folder is writable."
        raise SystemExit(f"{message} ({error})") from error
    errors = sum(message.level == "ERROR" for message in messages)
    warnings = sum(message.level == "WARNING" for message in messages)
    print(f"Created workbook: {output}")
    print(f"Participants included: {participants}; validation errors: {errors}; warnings: {warnings}")


if __name__ == "__main__":
    main()
