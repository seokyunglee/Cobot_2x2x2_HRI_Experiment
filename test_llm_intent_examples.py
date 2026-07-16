from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pose_generator import HumanArmProfile, HumanAwareTcpPoseGenerator
from voice_intent_interface import (
    ACTION_ADJUST,
    ACTION_COMPLETE,
    ACTION_KEEP,
    ACTION_NONE,
    ContinuousSpeechRecognizer,
    LlmIntentInterpreter,
    QueuedTtsSpeaker,
)


ROOT_DIR = Path(__file__).resolve().parent
RESULT_DIR = ROOT_DIR / "results" / "llm_response_json"
PROMPT_PATH = ROOT_DIR / "prompts" / "intent_interpreter_system.md"

OPENAI_API_KEY = ""
LLAMA_BASE_URL = "https://api.groq.com/openai/v1"

INITIAL_SHOULDER_ANGLE_DEG = 130.0
PILOT_FUNCTIONAL_MIN_SHOULDER_DEG = 60.0
PILOT_FUNCTIONAL_MAX_SHOULDER_DEG = 80.0
LLM_DEFAULT_SAFE_TARGET_DEG = 70.0
RULE_Z_STEP_M = 0.05

CONDITIONS = {
    1: {"intervention": "Intervention", "lead": "System", "control": "LLM", "name": "Cond1_Sys_LLM"},
    2: {"intervention": "Intervention", "lead": "System", "control": "Rule", "name": "Cond2_Sys_Rule"},
    3: {"intervention": "Intervention", "lead": "Worker", "control": "LLM", "name": "Cond3_Worker_LLM"},
    4: {"intervention": "Intervention", "lead": "Worker", "control": "Rule", "name": "Cond4_Worker_Rule"},
    5: {"intervention": "Non-Intervention", "lead": "System", "control": "None", "name": "Cond5_Control_NoInterv"},
}

tts_speaker = QueuedTtsSpeaker()
llm_latencies: list[float] = []


@dataclass
class FakeCycleResult:
    is_risky_cycle: bool
    representative_shoulder_angle_deg: float


def configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def speak(text: str) -> None:
    print(f"[TTS] {text}")
    tts_speaker.speak(text)


def speak_and_wait(text: str, timeout_sec: float | None = None) -> None:
    speak(text)
    tts_speaker.wait_until_done(timeout_sec=timeout_sec)


def ask_float(prompt: str, default: float) -> float:
    raw = input(f"{prompt} [기본: {default}]: ").strip()
    return float(raw) if raw else default


def ask_yes_no(prompt: str, default: bool) -> bool:
    default_label = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{default_label}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "1", "true", "t", "ㅇ", "예", "네"}


def safe_filename_part(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value.strip())
    return value.strip("._") or "case"


def unique_json_path(condition_name: str, label: str) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"interactive_{safe_filename_part(condition_name)}_{safe_filename_part(label)}.json"
    path = RESULT_DIR / filename
    suffix = 2
    while path.exists():
        filename = f"interactive_{safe_filename_part(condition_name)}_{safe_filename_part(label)}_{suffix:02d}.json"
        path = RESULT_DIR / filename
        suffix += 1
    return path


def save_llm_response_json(
    intent_interpreter: LlmIntentInterpreter,
    condition_name: str,
    label: str,
    user_voice: str | None,
    intent_result: Any,
) -> None:
    path = unique_json_path(condition_name, label)
    payload = {
        "condition_name": condition_name,
        "label": label,
        "model": intent_interpreter.model,
        "latency_s": intent_interpreter.last_latency_s,
        "user_voice": user_voice,
        "request_payload": intent_interpreter.last_request_payload,
        "parsed_response": intent_interpreter.last_parsed_response,
        "intent_result": intent_result.to_dict() if intent_result is not None else None,
        "error": intent_interpreter.last_error,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if intent_interpreter.last_latency_s > 0:
        llm_latencies.append(intent_interpreter.last_latency_s)
    print(f"[LLM JSON saved] {path}")


def save_test_summary(
    *,
    condition_name: str,
    completed_trials: int,
    adjust_count: int,
    keep_count: int,
    current_target_floor_height_m: float,
    current_target_shoulder_angle_deg: float,
) -> None:
    avg_latency_s = sum(llm_latencies) / len(llm_latencies) if llm_latencies else 0.0
    path = unique_json_path(condition_name, "summary")
    payload = {
        "condition_name": condition_name,
        "completed_trials": completed_trials,
        "adjust_count": adjust_count,
        "keep_count": keep_count,
        "llm_call_count": len(llm_latencies),
        "avg_llm_latency_s": avg_latency_s,
        "final_target_floor_height_m": current_target_floor_height_m,
        "final_target_shoulder_angle_deg": current_target_shoulder_angle_deg,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[TEST SUMMARY saved] {path}")


def wait_for_worker_voice(speech_recognizer: ContinuousSpeechRecognizer, question_text: str) -> str:
    # main_integrated.py와 같은 방식: TTS 후 STT 결과가 들어올 때까지 계속 기다린다.
    speech_recognizer.get_and_clear()
    speak_and_wait(question_text)
    time.sleep(0.6)
    speech_recognizer.get_and_clear()
    while True:
        worker_voice = speech_recognizer.get_and_clear()
        if worker_voice:
            return worker_voice
        time.sleep(0.05)


def dummy_send_hold_finished() -> None:
    print("[DUMMY HTTP] SendHoldFinished()")


def dummy_send_pass_goal(pose_result, trial_number: int) -> None:
    payload = pose_result.to_pass_goal_dict(msg=f"dummy_trial_{trial_number:03d}")
    print("[DUMMY HTTP] SendPassGoal skipped. Payload preview:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def worker_llm_ack_text(intent) -> str:
    if intent.direction == "up":
        if intent.amount_ratio is not None and intent.amount_ratio <= 0.34:
            return "네, 조금 올리겠습니다."
        if intent.amount_ratio is not None and intent.amount_ratio >= 0.99:
            return "네, 강하게 올리겠습니다."
        return "네, 올리겠습니다."
    if intent.direction == "down":
        if intent.amount_ratio is not None and intent.amount_ratio <= 0.34:
            return "네, 조금 내리겠습니다."
        if intent.amount_ratio is not None and intent.amount_ratio >= 0.99:
            return "네, 강하게 내리겠습니다."
        return "네, 내리겠습니다."
    raise RuntimeError(f"Cond3_Worker_LLM requires direction up/down for safe adjustment, got {intent.direction!r}.")


def resolve_returning_pose_target(
    *,
    condition_name: str,
    cycle_result: FakeCycleResult,
    intent_interpreter: LlmIntentInterpreter,
    speech_recognizer: ContinuousSpeechRecognizer,
    pose_generator: HumanAwareTcpPoseGenerator,
    current_target_floor_height_m: float,
    current_target_shoulder_angle_deg: float,
    trial_count: int,
) -> dict[str, Any]:
    is_risky_cycle = bool(cycle_result.is_risky_cycle)
    worker_clarify_text = "잘 이해하지 못했습니다. 올려드릴까요 내려드릴까요 유지할까요?"

    def keep_target() -> dict[str, Any]:
        return {
            "action": "keep",
            "target_type": "none",
            "target_floor_height_m": None,
            "target_shoulder_angle_deg": None,
            "user_voice": None,
            "llm_confidence": None,
            "decision_reason": "",
        }

    def floor_target(target_floor_height_m: float, intent=None, user_voice: str | None = None) -> dict[str, Any]:
        clamped_floor_height_m = max(
            pose_generator.min_floor_height_m,
            min(pose_generator.max_floor_height_m, float(target_floor_height_m)),
        )
        return {
            "action": "adjust",
            "target_type": "floor_height",
            "target_floor_height_m": clamped_floor_height_m,
            "target_shoulder_angle_deg": None,
            "user_voice": user_voice,
            "llm_confidence": getattr(intent, "confidence", None),
            "decision_reason": getattr(intent, "reason", ""),
        }

    def shoulder_target(target_shoulder_angle_deg: float, intent=None, user_voice: str | None = None) -> dict[str, Any]:
        return {
            "action": "adjust",
            "target_type": "shoulder_angle",
            "target_floor_height_m": None,
            "target_shoulder_angle_deg": float(target_shoulder_angle_deg),
            "user_voice": user_voice,
            "llm_confidence": getattr(intent, "confidence", None),
            "decision_reason": getattr(intent, "reason", ""),
        }

    def call_adjustment_llm(worker_text: str | None, is_system_adjustment: bool, label: str):
        intent = intent_interpreter.interpret_adjustment(
            worker_text,
            cycle_result,
            PILOT_FUNCTIONAL_MIN_SHOULDER_DEG,
            LLM_DEFAULT_SAFE_TARGET_DEG,
            PILOT_FUNCTIONAL_MAX_SHOULDER_DEG,
            current_target_shoulder_angle_deg=current_target_shoulder_angle_deg,
            is_first_completed_cycle=(trial_count == 0),
            is_system_adjustment=is_system_adjustment,
        )
        save_llm_response_json(intent_interpreter, condition_name, label, worker_text, intent)
        if getattr(intent_interpreter, "last_error", None):
            raise RuntimeError(f"Adjustment LLM failed: {intent_interpreter.last_error}")
        return intent

    def ask_worker_until_valid_intent(question_text: str):
        next_question_text = question_text
        while True:
            worker_voice = wait_for_worker_voice(speech_recognizer, next_question_text)
            next_question_text = worker_clarify_text
            intent = call_adjustment_llm(
                worker_text=worker_voice,
                is_system_adjustment=False,
                label=f"trial_{trial_count + 1:03d}_worker_adjustment",
            )
            if intent is None:
                raise RuntimeError("Adjustment LLM returned no intent.")
            if intent.action in {"clarify", ACTION_NONE, ACTION_COMPLETE}:
                continue
            if intent.action == ACTION_KEEP:
                return intent, worker_voice
            if intent.action != ACTION_ADJUST:
                raise RuntimeError(f"Unexpected worker intent action: {intent.action!r}")
            if (
                intent.direction == "up"
                and current_target_shoulder_angle_deg >= PILOT_FUNCTIONAL_MAX_SHOULDER_DEG - 0.05
            ):
                speak_and_wait(
                    "현재 안전 범위에서 가장 높은 각도입니다. "
                    "더 올릴 수 없습니다. 내려드릴까요 유지할까요?"
                )
                continue
            if (
                intent.direction == "down"
                and current_target_shoulder_angle_deg <= PILOT_FUNCTIONAL_MIN_SHOULDER_DEG + 0.05
            ):
                speak_and_wait(
                    "현재 안전 범위에서 가장 낮은 각도입니다. "
                    "더 내릴 수 없습니다. 올려드릴까요 유지할까요?"
                )
                continue
            if intent.direction == "up" and current_target_floor_height_m >= pose_generator.max_floor_height_m:
                speak_and_wait(
                    "현재 로봇이 전달할 수 있는 최고 높이입니다. "
                    "더 높여서 전달할 수 없습니다. 다시 말씀해 주세요."
                )
                continue
            if intent.direction == "down" and current_target_floor_height_m <= pose_generator.min_floor_height_m:
                speak_and_wait(
                    "현재 로봇이 전달할 수 있는 최저 높이입니다. "
                    "더 낮춰서 전달할 수 없습니다. 다시 말씀해 주세요."
                )
                continue
            return intent, worker_voice

    match condition_name:
        case "Cond1_Sys_LLM":
            if not is_risky_cycle:
                speak_and_wait("안전자세가 감지되어 유지합니다.")
                return keep_target()

            intent = call_adjustment_llm(
                worker_text="",
                is_system_adjustment=True,
                label=f"trial_{trial_count + 1:03d}_system_adjustment",
            )
            if intent.action == ACTION_ADJUST and intent.target_shoulder_angle_deg is not None:
                speak_and_wait("불편자세가 감지되어 조정합니다.")
                return shoulder_target(intent.target_shoulder_angle_deg, intent=intent)
            raise RuntimeError(
                "Cond1_Sys_LLM risky cycle requires LLM action=adjust "
                "with target_shoulder_angle_deg."
            )

        case "Cond2_Sys_Rule":
            if not is_risky_cycle:
                speak_and_wait("안전자세가 감지되어 유지합니다.")
                return keep_target()

            speak_and_wait("불편자세가 감지되어 조정합니다.")
            if current_target_floor_height_m <= pose_generator.min_floor_height_m:
                raise RuntimeError("Cond2_Sys_Rule cannot lower because current target is already at min height.")
            return floor_target(current_target_floor_height_m - RULE_Z_STEP_M)

        case "Cond3_Worker_LLM":
            question_text = (
                "불편자세를 감지했습니다. 작업 높이를 변경해드릴까요?"
                if is_risky_cycle
                else "안전자세가 감지되었으나 작업높이를 변경해드릴까요?"
            )
            intent, worker_voice = ask_worker_until_valid_intent(question_text)
            if intent.action == ACTION_KEEP:
                speak_and_wait("네, 유지하겠습니다.")
                target_info = keep_target()
                target_info["user_voice"] = worker_voice
                target_info["llm_confidence"] = intent.confidence
                target_info["decision_reason"] = intent.reason
                return target_info
            if intent.target_shoulder_angle_deg is None:
                raise RuntimeError("Cond3_Worker_LLM requires target_shoulder_angle_deg for adjust intent.")
            if is_risky_cycle:
                speak_and_wait("네, 안전 각도로 조정하겠습니다.")
            else:
                speak_and_wait(worker_llm_ack_text(intent))
            return shoulder_target(intent.target_shoulder_angle_deg, intent=intent, user_voice=worker_voice)

        case "Cond4_Worker_Rule":
            question_text = (
                "자세 부담이 감지되었습니다. 작업높이를 변경할까요?"
                if is_risky_cycle
                else "자세 부담이 감지되지 않았습니다. 작업높이를 변경할까요?"
            )
            intent, worker_voice = ask_worker_until_valid_intent(question_text)
            if intent.action == ACTION_KEEP:
                speak_and_wait("네, 유지하겠습니다.")
                target_info = keep_target()
                target_info["user_voice"] = worker_voice
                target_info["llm_confidence"] = intent.confidence
                target_info["decision_reason"] = intent.reason
                return target_info
            if intent.direction == "up":
                speak_and_wait("네, 룰 조건이라 고정 수치인 5센치만 올라갑니다.")
                return floor_target(
                    current_target_floor_height_m + RULE_Z_STEP_M,
                    intent=intent,
                    user_voice=worker_voice,
                )
            if intent.direction == "down":
                speak_and_wait("네, 룰 조건이라 고정 수치인 5센치만 내려갑니다.")
                return floor_target(
                    current_target_floor_height_m - RULE_Z_STEP_M,
                    intent=intent,
                    user_voice=worker_voice,
                )
            raise RuntimeError(f"Cond4_Worker_Rule requires direction up/down, got {intent.direction!r}.")

        case "Cond5_Control_NoInterv":
            return keep_target()

        case _:
            raise RuntimeError(f"Unknown condition: {condition_name}")


def run_task_completion_phase(
    *,
    intent_interpreter: LlmIntentInterpreter,
    speech_recognizer: ContinuousSpeechRecognizer,
    condition_name: str,
    trial_number: int,
) -> None:
    speech_recognizer.get_and_clear()
    speak_and_wait("의자를 작업 위치로 옮겨주세요.")
    time.sleep(1)
    speech_recognizer.get_and_clear()
    speak("블록의 네 개 볼트에 있는 너트를 드릴로 빼주세요.")

    while True:
        worker_voice = wait_for_worker_voice(speech_recognizer, "작업이 끝나면 완료했다고 말씀해 주세요.")
        intent = intent_interpreter.interpret_task_completion(worker_voice)
        save_llm_response_json(
            intent_interpreter,
            condition_name,
            f"trial_{trial_number:03d}_task_completion",
            worker_voice,
            intent,
        )
        if getattr(intent_interpreter, "last_error", None):
            raise RuntimeError(f"Task completion LLM failed: {intent_interpreter.last_error}")
        if intent.action == ACTION_COMPLETE:
            print(f"[TASK COMPLETE] reason={intent.reason}")
            speak_and_wait("작업 완료를 확인했습니다.")
            dummy_send_hold_finished()
            return
        speak_and_wait("작업 완료로 이해하지 못했습니다. 작업이 끝났으면 완료했다고 말씀해 주세요.")


def execute_target(
    *,
    target_info: dict[str, Any],
    pose_generator: HumanAwareTcpPoseGenerator,
    human_profile: HumanArmProfile,
    current_target_floor_height_m: float,
    trial_number: int,
) -> tuple[float, float]:
    action = target_info.get("action")
    target_type = target_info.get("target_type")
    print("[TARGET INFO]")
    print(json.dumps(target_info, ensure_ascii=False, indent=2))

    if action == "adjust" and target_type == "shoulder_angle":
        if target_info.get("target_shoulder_angle_deg") is None:
            raise RuntimeError("target_shoulder_angle_deg is required.")
        pose_result = pose_generator.generate_pose_from_shoulder_angle(
            target_info["target_shoulder_angle_deg"],
            human_profile,
        )
        dummy_send_pass_goal(pose_result, trial_number)
    elif action == "adjust" and target_type == "floor_height":
        if target_info.get("target_floor_height_m") is None:
            raise RuntimeError("target_floor_height_m is required.")
        pose_result = pose_generator.generate_pose_from_floor_height(
            target_info["target_floor_height_m"],
            human_profile,
        )
        dummy_send_pass_goal(pose_result, trial_number)
    elif action == "keep" and target_type == "none":
        pose_result = pose_generator.generate_pose_from_floor_height(
            current_target_floor_height_m,
            human_profile,
        )
        print("[DUMMY HTTP] keep target. SendPassGoal skipped.")
    else:
        raise RuntimeError(f"Invalid target_info: action={action!r}, target_type={target_type!r}")

    next_floor_height_m = pose_result.target_floor_height_m
    next_shoulder_angle_deg = pose_generator.shoulder_angle_from_floor_height(
        profile=human_profile,
        floor_height_m=next_floor_height_m,
    )
    print(
        "[POSE RESULT] "
        f"requested_h={pose_result.requested_floor_height_m:.3f}m | "
        f"final_h={pose_result.target_floor_height_m:.3f}m | "
        f"clamped={pose_result.was_height_clamped} | "
        f"next_target_shoulder={next_shoulder_angle_deg:.1f}deg"
    )
    return next_floor_height_m, next_shoulder_angle_deg


def main() -> None:
    configure_stdout()
    print("\n" + "=" * 60)
    print("LLM/TTS/STT 더미 통합 테스트")
    print("=" * 60)

    user_height_cm = ask_float("1. 작업자 키(cm)", 155.0)
    user_shoulder_height_cm = ask_float("2. 어깨 높이(cm)", user_height_cm - 30.0)
    upper_arm_cm = ask_float("3. 상완 길이(cm)", 30.0)
    forearm_cm = ask_float("4. 하완 길이(cm)", 25.0)

    human_profile = HumanArmProfile(
        user_height_m=user_height_cm / 100.0,
        shoulder_height_m=user_shoulder_height_cm / 100.0,
        upper_arm_m=upper_arm_cm / 100.0,
        forearm_m=forearm_cm / 100.0,
    )
    pose_generator = HumanAwareTcpPoseGenerator()

    print("\n" + "=" * 60)
    for key_num, condition in CONDITIONS.items():
        print(f" [{key_num}] {condition['name']}")
    print("=" * 60)
    try:
        choice = int(input("테스트 조건 번호를 입력하세요 (1~5): "))
    except Exception:
        choice = 1
    condition = CONDITIONS.get(choice, CONDITIONS[1])
    condition_name = condition["name"]
    print(f"[CONDITION] {condition_name}")

    is_risky_cycle = ask_yes_no("더미 cycle을 위험 사이클로 둘까요?", condition_name in {"Cond1_Sys_LLM", "Cond2_Sys_Rule"})
    representative_default = 125.0 if is_risky_cycle else 70.0
    representative_shoulder_angle_deg = ask_float("대표 어깨각(deg)", representative_default)

    trial_count = 0
    print(f"[INITIAL GOAL READY] 목표 어깨각 {INITIAL_SHOULDER_ANGLE_DEG:.1f}도로 초기 위치를 계산합니다.")
    current_pose = pose_generator.generate_pose_from_shoulder_angle(
        INITIAL_SHOULDER_ANGLE_DEG,
        human_profile,
    )
    current_target_floor_height_m = current_pose.target_floor_height_m
    current_target_shoulder_angle_deg = pose_generator.shoulder_angle_from_floor_height(
        profile=human_profile,
        floor_height_m=current_target_floor_height_m,
    )
    print(
        "[CURRENT TARGET] "
        f"floor_h={current_target_floor_height_m:.3f}m | "
        f"shoulder_angle={current_target_shoulder_angle_deg:.1f}deg | "
        f"clamped={current_pose.was_height_clamped}"
    )

    intent_interpreter = (
        LlmIntentInterpreter(api_key=OPENAI_API_KEY, base_url=LLAMA_BASE_URL)
        if OPENAI_API_KEY
        else None
    )
    if intent_interpreter is None:
        raise RuntimeError("OPENAI_API_KEY is empty. Voice intent requires LLM.")
    print(f"[LLM] model={intent_interpreter.model} prompt={PROMPT_PATH}")

    speech_recognizer = ContinuousSpeechRecognizer(on_text=lambda text: print(f"[STT] '{text}'"))
    speech_recognizer.start()

    completed_trials = 0
    adjust_count = 0
    keep_count = 0

    try:
        while True:
            cycle_result = FakeCycleResult(
                is_risky_cycle=is_risky_cycle,
                representative_shoulder_angle_deg=representative_shoulder_angle_deg,
            )
            trial_number = trial_count + 1
            print("\n" + "-" * 60)
            print(
                f"[DUMMY TRIAL {trial_number:03d}] "
                f"risky={cycle_result.is_risky_cycle} | "
                f"representative_shoulder={cycle_result.representative_shoulder_angle_deg:.1f}deg | "
                f"current_target={current_target_shoulder_angle_deg:.1f}deg/"
                f"{current_target_floor_height_m:.3f}m"
            )

            run_task_completion_phase(
                intent_interpreter=intent_interpreter,
                speech_recognizer=speech_recognizer,
                condition_name=condition_name,
                trial_number=trial_number,
            )
            print("[DUMMY ROBOT STATE] RETURNING")
            target_info = resolve_returning_pose_target(
                condition_name=condition_name,
                cycle_result=cycle_result,
                intent_interpreter=intent_interpreter,
                speech_recognizer=speech_recognizer,
                pose_generator=pose_generator,
                current_target_floor_height_m=current_target_floor_height_m,
                current_target_shoulder_angle_deg=current_target_shoulder_angle_deg,
                trial_count=trial_count,
            )
            current_target_floor_height_m, current_target_shoulder_angle_deg = execute_target(
                target_info=target_info,
                pose_generator=pose_generator,
                human_profile=human_profile,
                current_target_floor_height_m=current_target_floor_height_m,
                trial_number=trial_number,
            )

            completed_trials += 1
            trial_count = trial_number
            if target_info.get("action") == "adjust":
                adjust_count += 1
            elif target_info.get("action") == "keep":
                keep_count += 1

            print(
                f"[TEST METRICS] completed_trials={completed_trials} | "
                f"adjust_count={adjust_count} | keep_count={keep_count} | "
                f"llm_call_count={len(llm_latencies)}"
            )
            next_raw = input("다음 더미 cycle을 계속하려면 Enter, 종료하려면 q: ").strip().lower()
            if next_raw in {"q", "quit", "exit"}:
                break

            is_risky_cycle = ask_yes_no("다음 더미 cycle을 위험 사이클로 둘까요?", is_risky_cycle)
            representative_default = 125.0 if is_risky_cycle else current_target_shoulder_angle_deg
            representative_shoulder_angle_deg = ask_float("다음 대표 어깨각(deg)", representative_default)
    finally:
        save_test_summary(
            condition_name=condition_name,
            completed_trials=completed_trials,
            adjust_count=adjust_count,
            keep_count=keep_count,
            current_target_floor_height_m=current_target_floor_height_m,
            current_target_shoulder_angle_deg=current_target_shoulder_angle_deg,
        )
        speech_recognizer.stop()
        tts_speaker.stop()


if __name__ == "__main__":
    main()
