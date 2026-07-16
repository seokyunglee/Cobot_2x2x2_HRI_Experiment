from __future__ import annotations

import atexit
import os
import time

import cv2

from experiment_data import ExperimentDataLogger, ExperimentMetrics, TrialRecord

from hri_http_sender import GetRobotState, SendHoldFinished, SendPassGoal, SetReviewPending
from pose_generator import (
    DRILL_TCP_OFFSET_M,
    HumanArmProfile,
    HumanAwareTcpPoseGenerator,
)
from posture_estimator import MEASURED_SIDE, PostureEstimator
from voice_intent_interface import (
    ACTION_COMPLETE,
    ContinuousSpeechRecognizer,
    LlmIntentInterpreter,
    QueuedTtsSpeaker,
    manual_task_completion_from_key,
    manual_task_completion_from_text,
)


OPENAI_API_KEY = ""
LLAMA_BASE_URL = "https://api.groq.com/openai/v1"

INITIAL_SHOULDER_ANGLE_DEG = 130.0
MAX_EXPERIMENT_TIME_SEC = 360.0
PILOT_FUNCTIONAL_MIN_SHOULDER_DEG = 60.0
PILOT_FUNCTIONAL_MAX_SHOULDER_DEG = 80.0
LLM_DEFAULT_SAFE_TARGET_DEG = 70.0
RULE_Z_STEP_M = 0.075
ROBOT_STATE_POLL_SEC = 0.2
RISK_SHOULDER_DEG = 110.0
RISKY_CYCLE_RATIO_THRESHOLD = 0.60
RULA_HIGH_SCORE_THRESHOLD = 3.0

CAMERA_FRAME_WIDTH = 1920
CAMERA_FRAME_HEIGHT = 1080
DISPLAY_WINDOW_NAME = "HRI Ergonomic Bolt Fastening Task"
DISPLAY_WINDOW_WIDTH = 1920
DISPLAY_WINDOW_HEIGHT = 1080

RESULT_DIR = os.path.join(os.path.dirname(__file__), "results")

CONDITIONS = {
    1: {"intervention": "Intervention", "lead": "System", "control": "LLM", "name": "Cond1_Sys_LLM"},
    2: {"intervention": "Intervention", "lead": "System", "control": "Rule", "name": "Cond2_Sys_Rule"},
    3: {"intervention": "Intervention", "lead": "Worker", "control": "LLM", "name": "Cond3_Worker_LLM"},
    4: {"intervention": "Intervention", "lead": "Worker", "control": "Rule", "name": "Cond4_Worker_Rule"},
    5: {"intervention": "Non-Intervention", "lead": "System", "control": "None", "name": "Cond5_Control_NoInterv"},
}

tts_speaker = QueuedTtsSpeaker()

def speak(text: str) -> None:
    print(f"[TTS] {text}")
    tts_speaker.speak(text)


def speak_and_wait(text: str, timeout_sec: float | None = None) -> None:
    speak(text)
    tts_speaker.wait_until_done(timeout_sec=timeout_sec)


def main() -> None:
    print("\n" + "=" * 60)
    print("작업자 신체 정보 입력")
    print("=" * 60)

    try:
        raw = input(" 1. 작업자 키(cm) [기본: 175.0]: ")
        user_height_cm = float(raw) if raw.strip() else 155.0
    except Exception:
        user_height_cm = 155.0

    try:
        default_shoulder = user_height_cm - 30.0
        raw = input(f" 2. 어깨 높이(cm) [기본: {default_shoulder:.1f}]: ")
        user_shoulder_height_cm = float(raw) if raw.strip() else default_shoulder
    except Exception:
        user_shoulder_height_cm = user_height_cm - 30.0

    try:
        raw = input(" 3. 상완 길이(cm) [기본: 30.0]: ")
        l1_cm = float(raw) if raw.strip() else 30.0
    except Exception:
        l1_cm = 30.0

    try:
        raw = input(" 4. 하완 길이(cm) [기본: 25.0]: ")
        l2_cm = float(raw) if raw.strip() else 25.0
    except Exception:
        l2_cm = 25.0

    print(
        f"\n[신체 정보] 키={user_height_cm:.1f}cm | 어깨={user_shoulder_height_cm:.1f}cm | "
        f"상완={l1_cm:.1f}cm | 하완={l2_cm:.1f}cm"
    )

    human_profile = HumanArmProfile(
        user_height_m=user_height_cm / 100.0,
        shoulder_height_m=user_shoulder_height_cm / 100.0,
        upper_arm_m=l1_cm / 100.0,
        forearm_m=l2_cm / 100.0,
    )
    pose_generator = HumanAwareTcpPoseGenerator()
    h_at_130_deg_m = pose_generator.floor_height_from_shoulder_angle(human_profile, 130.0)
    h_at_60_deg_m = pose_generator.floor_height_from_shoulder_angle(human_profile, 60.0)
    min_pose_h_m = pose_generator.min_floor_height_m
    max_pose_h_m = pose_generator.max_floor_height_m
    robot_min_reachable_shoulder_angle_deg = pose_generator.shoulder_angle_from_floor_height(
        profile=human_profile,
        floor_height_m=min_pose_h_m,
    )
    robot_max_reachable_shoulder_angle_deg = pose_generator.shoulder_angle_from_floor_height(
        profile=human_profile,
        floor_height_m=max_pose_h_m,
    )

    print(
        f"[어깨각 기준 목표 H] 130도={h_at_130_deg_m:.3f}m ({h_at_130_deg_m * 100.0:.1f}cm) | "
        f"60도={h_at_60_deg_m:.3f}m ({h_at_60_deg_m * 100.0:.1f}cm)"
    )
    print(
        f"[Pose H 범위] {min_pose_h_m:.3f}m~{max_pose_h_m:.3f}m | "
        f"130도: {f'하한 clamp -> {min_pose_h_m:.3f}m' if h_at_130_deg_m < min_pose_h_m else f'상한 clamp -> {max_pose_h_m:.3f}m' if h_at_130_deg_m > max_pose_h_m else '범위 내'} | "
        f"60도: {f'하한 clamp -> {min_pose_h_m:.3f}m' if h_at_60_deg_m < min_pose_h_m else f'상한 clamp -> {max_pose_h_m:.3f}m' if h_at_60_deg_m > max_pose_h_m else '범위 내'}"
    )
    print(
        f"[사용 가능 어깨각 범위] robot={robot_min_reachable_shoulder_angle_deg:.1f}~"
        f"{robot_max_reachable_shoulder_angle_deg:.1f}deg | "
        f"comfort={PILOT_FUNCTIONAL_MIN_SHOULDER_DEG:.1f}~"
        f"{PILOT_FUNCTIONAL_MAX_SHOULDER_DEG:.1f}deg | "
        f"default={LLM_DEFAULT_SAFE_TARGET_DEG:.1f}deg"
    )

    print("\n" + "=" * 60)
    for key_num, condition in CONDITIONS.items():
        print(f" [{key_num}] {condition['name']}")
    print("=" * 60)
    try:
        choice = int(input("실험 조건 번호를 입력하세요 (1~5): "))
    except Exception:
        choice = 1
    current_condition = CONDITIONS.get(choice, CONDITIONS[1])
    print(f"[CONDITION] {current_condition['name']}")

    intent_interpreter = (
        LlmIntentInterpreter(api_key=OPENAI_API_KEY, base_url=LLAMA_BASE_URL)
        if OPENAI_API_KEY
        else None
    )
    if intent_interpreter is None:
        print("[LLM DISABLED] OPENAI_API_KEY is empty. Voice intent requires manual keys.")

    speech_recognizer = ContinuousSpeechRecognizer(
        on_text=lambda text: print(f"[STT] '{text}'")
    )
    speech_recognizer.start()

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[CAMERA ERROR] 카메라를 열 수 없어 실험을 시작하지 않습니다.")
        cap.release()
        speech_recognizer.stop()
        tts_speaker.stop()
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_FRAME_HEIGHT)
    cv2.namedWindow(DISPLAY_WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(DISPLAY_WINDOW_NAME, DISPLAY_WINDOW_WIDTH, DISPLAY_WINDOW_HEIGHT)

    posture_estimator = PostureEstimator()
    metrics = ExperimentMetrics(
        risk_shoulder_deg=RISK_SHOULDER_DEG,
        risky_cycle_ratio_threshold=RISKY_CYCLE_RATIO_THRESHOLD,
        rula_high_score_threshold=RULA_HIGH_SCORE_THRESHOLD,
    )
    data_logger = ExperimentDataLogger(RESULT_DIR)
    print(f"[DATA RAW CSV] {data_logger.raw_path}")
    print(f"[DATA SUMMARY CSV] {data_logger.summary_path}")
    
    review_cycle_result = None
    last_task_sample_time = 0.0
    waiting_for_chair_confirmation = False
    rest_s = 0.0
    rest_start = None
    current_target_floor_height_m = 0.0
    current_target_shoulder_angle_deg = INITIAL_SHOULDER_ANGLE_DEG
    trial_count = 0

    # Save pass-goal payloads for trial traceability.
    def save_pass_goal_json(payload: dict, label: str) -> None:
        try:
            json_path = data_logger.write_pass_goal_json(payload, label)
            print(f"[PASS_GOAL JSON saved] {json_path}")
        except Exception as exc:
            print(f"[PASS_GOAL JSON save failed] {exc}")

    # LLM 요청 payload와 파싱된 응답을 별도 JSON으로 남긴다.
    def save_llm_response_json(label: str) -> None:
        if intent_interpreter is None:
            return
        payload = {
            "trial_num": trial_count + 1,
            "label": label,
            "model": intent_interpreter.model,
            "prompt": (
                intent_interpreter.last_prompt_path.name
                if intent_interpreter.last_prompt_path is not None
                else None
            ),
            "latency_s": intent_interpreter.last_latency_s,
            "usage": intent_interpreter.last_usage,
            "request_payload": intent_interpreter.last_request_payload,
            "parsed_response": intent_interpreter.last_parsed_response,
            "error": intent_interpreter.last_error,
        }
        try:
            json_path = data_logger.write_llm_response_json(payload, label, current_condition["name"])
            print(f"[LLM JSON saved] {json_path}")
        except Exception as exc:
            print(f"[LLM JSON save failed] {exc}")

 
    # pose_generator가 만든 목표 pose를 로봇에 보내고 높이 변화량을 반환한다.
    def send_pose_goal(
        pose_result,
        trial_number: int,
        previous_target_floor_height_m: float,
    ) -> tuple[float, float, bool]:
        next_target_floor_height_m = pose_result.target_floor_height_m
        adjustment_m = next_target_floor_height_m - previous_target_floor_height_m
        if abs(adjustment_m) <= 1e-9:
            print("[PASS_GOAL skipped] Target height unchanged.")
            return next_target_floor_height_m, adjustment_m, False

        payload = pose_result.to_pass_goal_dict(msg=f"Trial {trial_number} Setup")
        save_pass_goal_json(payload, f"trial_{trial_number:03d}_pass_goal")
        robot_command_sent = SendPassGoal(payload)
        if not robot_command_sent:
            print("[PASS_GOAL failed] Next target was not sent to robot.")
        return next_target_floor_height_m, adjustment_m, robot_command_sent

    # resolve_returning_pose_target()의 결과와 실제 전송 pose를 raw trial로 기록한다.
    def write_target_trial_record(
        target_info: dict,
        cycle,
        pose_result,
        trial_number: int,
        previous_target_floor_height_m: float,
        next_target_floor_height_m: float,
        adjustment_m: float,
        robot_command_sent: bool,
    ) -> None:
        action = str(target_info.get("action", "keep"))
        target_type = str(target_info.get("target_type", "none"))
        response_source = str(target_info.get("response_source", target_type))
        llm_confidence = float(target_info.get("confidence") or 0.0)
        decision_reason = str(target_info.get("reason", ""))
        llm_fallback = bool(target_info.get("llm_fallback", False))
        llm_latency_s = float(target_info.get("llm_latency_s") or 0.0)
        user_voice = str(target_info.get("user_voice", ""))
        is_invalid = bool(target_info.get("is_invalid", action == "clarify"))
        should_adjust = action == "adjust"

        if llm_fallback:
            metrics.record_llm_fallback()
        if (
            should_adjust
            and current_condition["lead"] == "System"
            and cycle.is_risky_cycle
        ):
            metrics.record_system_intervention()

        metrics.record_completed_trial(
            cycle=cycle,
            adjustment_z_mm=adjustment_m * 1000.0,
            is_invalid=is_invalid,
            is_correction=should_adjust,
        )
        if current_condition["lead"] == "Worker":
            metrics.record_worker_response(action)

        if not should_adjust:
            target_shoulder_angle_deg = None
        elif target_type == "floor_height":
            target_shoulder_angle_deg = pose_generator.shoulder_angle_from_floor_height(
                profile=human_profile,
                floor_height_m=pose_result.target_floor_height_m,
            )
        else:
            target_shoulder_angle_deg = target_info.get("target_shoulder_angle_deg")
        angle_adjustment_deg = (
            float(target_shoulder_angle_deg) - cycle.representative_shoulder_angle_deg
            if target_shoulder_angle_deg is not None
            else None
        )

        trial_record = TrialRecord(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            condition_name=current_condition["name"],
            trial_num=trial_number,
            lead_type=current_condition["lead"],
            control_type=current_condition["control"],
            measured_side=MEASURED_SIDE,
            risk_shoulder_threshold_deg=RISK_SHOULDER_DEG,
            risky_cycle_ratio_threshold=RISKY_CYCLE_RATIO_THRESHOLD,
            user_height_cm=user_height_cm,
            shoulder_height_cm=user_shoulder_height_cm,
            upper_arm_cm=l1_cm,
            forearm_cm=l2_cm,
            drill_tcp_offset_cm=DRILL_TCP_OFFSET_M * 100.0,
            cycle=cycle,
            target_shoulder_angle_deg=target_shoulder_angle_deg,
            angle_adjustment_deg=angle_adjustment_deg,
            target_angle_source=target_type,
            response_action=action,
            response_source=response_source,
            llm_confidence=llm_confidence,
            decision_reason=decision_reason,
            llm_fallback=llm_fallback,
            prev_z_mm=previous_target_floor_height_m * 1000.0,
            final_z_mm=next_target_floor_height_m * 1000.0,
            adjustment_z_mm=adjustment_m * 1000.0,
            user_voice=user_voice,
            final_z_m=pose_result.target_floor_height_m,
            pose_height_clamped=pose_result.was_height_clamped,
            robot_command_sent=robot_command_sent,
            is_approved=should_adjust,
            llm_latency_s=llm_latency_s,
            is_invalid=is_invalid,
            pose_x_m=pose_result.x_m,
            pose_y_m=pose_result.y_m,
            pose_z_m=pose_result.z_m,
            pose_qx=pose_result.qx,
            pose_qy=pose_result.qy,
            pose_qz=pose_result.qz,
            pose_qw=pose_result.qw,
        )
        data_logger.write_trial(trial_record)

    def resolve_returning_pose_target(cycle_result) -> dict:
        # RETURNING에서 cycle_result와 current_condition을 보고 pose_generator 입력을 만든다.
        # 컨디션별 실험 로직은 유지한다.
        # - System+LLM, Worker+LLM: LLM이 만든 target_shoulder_angle_deg를 사용한다.
        # - System+Rule, Worker+Rule: target_shoulder_angle_deg를 쓰지 않고 floor height를 5cm step으로 바꾼다.
        # - Control: 위험 여부와 상관없이 유지한다.
        #
        # 위험 판단은 무조건 cycle_result.is_risky_cycle 하나만 쓴다.
        # 대표 어깨각도나 RULA proxy로 여기서 다시 위험 여부를 만들지 않는다.
        is_risky_cycle = bool(cycle_result.is_risky_cycle)
        condition_name = current_condition["name"]
        worker_clarify_text = "잘 이해하지 못했습니다. 올려드릴까요 내려드릴까요 유지할까요?"

        def keep_target() -> dict:
            return {
                "action": "keep",
                "target_type": "none",
                "target_floor_height_m": None,
                "target_shoulder_angle_deg": None,
            }

        def floor_target(target_floor_height_m: float) -> dict:
            clamped_floor_height_m = max(
                pose_generator.min_floor_height_m,
                min(pose_generator.max_floor_height_m, float(target_floor_height_m)),
            )
            return {
                "action": "adjust",
                "target_type": "floor_height",
                "target_floor_height_m": clamped_floor_height_m,
                "target_shoulder_angle_deg": None,
            }

        def shoulder_target(target_shoulder_angle_deg: float) -> dict:
            return {
                "action": "adjust",
                "target_type": "shoulder_angle",
                "target_floor_height_m": None,
                "target_shoulder_angle_deg": float(target_shoulder_angle_deg),
            }

        def with_intent_metadata(target_info: dict, intent) -> dict:
            return {
                **target_info,
                "confidence": intent.confidence,
                "reason": intent.reason,
                "user_voice": intent.user_voice,
                "llm_latency_s": intent.llm_latency_s,
                "llm_fallback": intent.llm_fallback,
            }

        def finish_adjustment_llm_call(label: str, error_context: str):
            metrics.record_llm_call(intent_interpreter.last_latency_s)
            save_llm_response_json(f"trial_{trial_count + 1:03d}_{label}")
            if getattr(intent_interpreter, "last_error", None):
                metrics.mark_early_stop()
                raise RuntimeError(f"{error_context} failed: {intent_interpreter.last_error}")

        def call_system_adjustment_llm():
            if intent_interpreter is None:
                raise RuntimeError("System adjustment LLM is required but intent_interpreter is not initialized.")
            intent = intent_interpreter.interpret_system_adjustment(
                cycle_result,
                PILOT_FUNCTIONAL_MIN_SHOULDER_DEG,
                LLM_DEFAULT_SAFE_TARGET_DEG,
                PILOT_FUNCTIONAL_MAX_SHOULDER_DEG,
            )
            finish_adjustment_llm_call("system_adjustment", "System adjustment LLM")
            return intent

        def call_worker_adjustment_llm(worker_text: str):
            if intent_interpreter is None:
                raise RuntimeError("Worker adjustment LLM is required but intent_interpreter is not initialized.")
            intent = intent_interpreter.interpret_worker_adjustment(
                worker_text,
                cycle_result,
                PILOT_FUNCTIONAL_MIN_SHOULDER_DEG,
                LLM_DEFAULT_SAFE_TARGET_DEG,
                PILOT_FUNCTIONAL_MAX_SHOULDER_DEG,
                current_target_shoulder_angle_deg=current_target_shoulder_angle_deg,
                is_first_completed_cycle=(trial_count == 0),
            )
            finish_adjustment_llm_call("worker_adjustment", "Worker adjustment LLM")
            return intent

        def wait_for_worker_voice(question_text: str) -> str:
            # 작업자 주도 조건에서는 시간 제한 없이 작업자 답변을 기다린다.
            speech_recognizer.get_and_clear()
            speak_and_wait(question_text)
            time.sleep(0.6)
            speech_recognizer.get_and_clear()
            while True:
                worker_voice = speech_recognizer.get_and_clear()
                if worker_voice:
                    return worker_voice
                time.sleep(0.05)

        def ask_worker_until_valid_intent(question_text: str):
            # 작업자 발화 -> LLM 의도해석을 반복해서 keep/adjust intent만 반환한다.
            # 애매한 응답, 작업완료 응답, LLM 실패, 이미 한계인 방향 요청은 재질문한다.
            next_question_text = question_text
            while True:
                worker_voice = wait_for_worker_voice(next_question_text)
                next_question_text = worker_clarify_text
                intent = call_worker_adjustment_llm(worker_voice)
                if intent is None:
                    raise RuntimeError("Adjustment LLM returned no intent.")
                if intent.action in {"clarify", "none", "complete"}:
                    continue
                if intent.action == "keep":
                    return intent
                if intent.action != "adjust":
                    raise RuntimeError(f"Unexpected worker intent action: {intent.action!r}")

                if (
                    condition_name == "Cond3_Worker_LLM"
                    and intent.direction == "up"
                    and current_target_shoulder_angle_deg >= PILOT_FUNCTIONAL_MAX_SHOULDER_DEG - 0.05
                ):
                    next_question_text = (
                        "현재 안전 범위에서 가장 높은 각도입니다. "
                        "내려드릴까요 유지할까요?"
                    )
                    continue

                if (
                    condition_name == "Cond3_Worker_LLM"
                    and intent.direction == "down"
                    and current_target_shoulder_angle_deg <= PILOT_FUNCTIONAL_MIN_SHOULDER_DEG + 0.05
                ):
                    next_question_text = (
                        "현재 안전 범위에서 가장 낮은 각도입니다. "
                        "올려드릴까요 유지할까요?"
                    )
                    continue

                if intent.direction == "up" and current_target_floor_height_m >= max_pose_h_m:
                    next_question_text = (
                        "현재 로봇이 전달할 수 있는 최고 높이입니다. "
                        "더 높여서 전달할 수 없습니다. 다시 말씀해 주세요."
                    )
                    continue
                if intent.direction == "down" and current_target_floor_height_m <= min_pose_h_m:
                    next_question_text = (
                        "현재 로봇이 전달할 수 있는 최저 높이입니다. "
                        "더 낮춰서 전달할 수 없습니다. 다시 말씀해 주세요."
                    )
                    continue
                return intent

        match condition_name:
            case "Cond1_Sys_LLM":
                # 시스템 주도 + LLM:
                # 위험하지 않으면 유지한다.
                # 위험하면 빈 utterance로 조정 LLM을 호출하고 target_shoulder_angle_deg만 사용한다.
                if not is_risky_cycle:
                    speak_and_wait("안전자세가 감지되어 유지합니다.")
                    return keep_target()

                intent = call_system_adjustment_llm()
                if (
                    intent is not None
                    and intent.action == "adjust"
                    and intent.target_shoulder_angle_deg is not None
                ):
                    speak_and_wait("불편자세가 감지되어 조정합니다.")
                    return with_intent_metadata(
                        shoulder_target(intent.target_shoulder_angle_deg),
                        intent,
                    )
                raise RuntimeError(
                    "Cond1_Sys_LLM risky cycle requires LLM action=adjust "
                    "with target_shoulder_angle_deg."
                )

            case "Cond2_Sys_Rule":
                # 시스템 주도 + Rule:
                # 위험하지 않으면 유지한다.
                # 위험하면 현재 floor height에서 5cm 내린다.
                if not is_risky_cycle:
                    speak_and_wait("안전자세가 감지되어 유지합니다.")
                    return keep_target()

                speak_and_wait("불편자세가 감지되어 조정합니다.")
                if current_target_floor_height_m <= min_pose_h_m:
                    raise RuntimeError("Cond2_Sys_Rule cannot lower because current target is already at min height.")
                return floor_target(current_target_floor_height_m - RULE_Z_STEP_M)

            case "Cond3_Worker_LLM":
                # 작업자 주도 + LLM:
                # 위험/비위험에 따라 질문 문구를 다르게 말한다.
                # 작업자 답변은 조정 LLM으로 해석하고, target_shoulder_angle_deg만 실행에 사용한다.
                # 답변이 비었거나 애매하면 같은 자리에서 다시 질문한다.
                question_text = (
                    "불편자세를 감지했습니다. 작업 높이를 변경해드릴까요?"
                    if is_risky_cycle
                    else "안전자세가 감지되었으나 작업높이를 변경해드릴까요?"
                )
                intent = ask_worker_until_valid_intent(question_text)
                if intent.action == "keep":
                    speak_and_wait("네, 유지하겠습니다.")
                    return with_intent_metadata(keep_target(), intent)
                if intent.target_shoulder_angle_deg is None:
                    raise RuntimeError("Cond3_Worker_LLM requires target_shoulder_angle_deg for adjust intent.")
                if is_risky_cycle:
                    speak_and_wait("네, 안전 각도로 조정하겠습니다.")
                elif intent.direction == "up":
                    if intent.amount_ratio is not None and intent.amount_ratio <= 0.34:
                        speak_and_wait("네, 조금 올리겠습니다.")
                    elif intent.amount_ratio is not None and intent.amount_ratio >= 0.99:
                        speak_and_wait("네, 강하게 올리겠습니다.")
                    else:
                        speak_and_wait("네, 올리겠습니다.")
                elif intent.direction == "down":
                    if intent.amount_ratio is not None and intent.amount_ratio <= 0.34:
                        speak_and_wait("네, 조금 내리겠습니다.")
                    elif intent.amount_ratio is not None and intent.amount_ratio >= 0.99:
                        speak_and_wait("네, 강하게 내리겠습니다.")
                    else:
                        speak_and_wait("네, 내리겠습니다.")
                else:
                    raise RuntimeError(f"Cond3_Worker_LLM requires direction up/down or risky adjustment, got {intent.direction!r}.")
                return with_intent_metadata(
                    shoulder_target(intent.target_shoulder_angle_deg),
                    intent,
                )

            case "Cond4_Worker_Rule":
                # 작업자 주도 + Rule:
                # 작업자 답변은 조정 LLM으로 해석하되 target_shoulder_angle_deg는 무조건 무시한다.
                # 실행에는 direction만 사용한다. up이면 +5cm, down이면 -5cm다.
                # 답변이 비었거나 방향이 애매하면 같은 자리에서 다시 질문한다.
                question_text = (
                    "자세 부담이 감지되었습니다. 작업높이를 변경할까요?"
                    if is_risky_cycle
                    else "자세 부담이 감지되지 않았습니다. 작업높이를 변경할까요?"
                )
                intent = ask_worker_until_valid_intent(question_text)
                if intent.action == "keep":
                    speak_and_wait("네, 유지하겠습니다.")
                    return with_intent_metadata(keep_target(), intent)
                if intent.direction == "up":
                    speak_and_wait("네, 룰 조건이라 고정 수치인 5센치만 올라갑니다.")
                    return with_intent_metadata(
                        floor_target(current_target_floor_height_m + RULE_Z_STEP_M),
                        intent,
                    )
                if intent.direction == "down":
                    speak_and_wait("네, 룰 조건이라 고정 수치인 5센치만 내려갑니다.")
                    return with_intent_metadata(
                        floor_target(current_target_floor_height_m - RULE_Z_STEP_M),
                        intent,
                    )
                raise RuntimeError(
                    f"Cond4_Worker_Rule requires direction up/down, got {intent.direction!r}."
                )

            case "Cond5_Control_NoInterv":
                # 비개입 조건:
                # 위험 사이클이어도 실험 조건상 아무 조정도 하지 않는다.
                return keep_target()

            case _:
                # 알 수 없는 조건:
                # 잘못된 condition이면 조용히 유지하지 않고 바로 드러나게 한다.
                raise RuntimeError(f"Unknown condition: {condition_name}")

    print(f"[INITIAL GOAL READY] 목표 어깨각 {INITIAL_SHOULDER_ANGLE_DEG:.1f}도로 초기 위치를 계산합니다.")
    initial_pose = pose_generator.generate_pose_from_shoulder_angle(INITIAL_SHOULDER_ANGLE_DEG, human_profile)
    current_target_floor_height_m = initial_pose.target_floor_height_m
    current_target_shoulder_angle_deg = INITIAL_SHOULDER_ANGLE_DEG

    print("[START READY] 카메라 화면을 확인하고 S를 누르면 실험을 시작합니다. Q/ESC는 종료입니다.")
    started = False
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            print("[CAMERA ERROR] 시작 대기 중 프레임을 읽지 못해 실험을 시작하지 않습니다.")
            break

        posture_sample = posture_estimator.process_frame(frame)
        posture_angle = posture_sample.shoulder_angle_deg if posture_sample.shoulder_angle_deg is not None else 0.0

        cv2.putText(frame, "Press S to start experiment", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(frame, "Q/ESC: Stop", (20, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 200), 2)
        cv2.putText(frame, f"Posture Angle: {posture_angle:.1f} deg", (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        cv2.imshow(DISPLAY_WINDOW_NAME, frame)

        start_key = cv2.waitKey(10) & 0xFF
        if start_key in (ord("s"), ord("S")):
            started = True
            break
        if start_key in (27, ord("q"), ord("Q")):
            posture_estimator.close()
            cap.release()
            cv2.destroyAllWindows()
            speech_recognizer.stop()
            tts_speaker.stop()
            return

    if not started:
        posture_estimator.close()
        cap.release()
        cv2.destroyAllWindows()
        speech_recognizer.stop()
        tts_speaker.stop()
        return

    initial_payload = initial_pose.to_pass_goal_dict(msg="Initial Trial Setup")
    save_pass_goal_json(initial_payload, "initial_pass_goal")
    speak_and_wait("실험을 시작하겠습니다. 바른 자세로 로봇을 바라보고 앉아주세요.")
    SendPassGoal(initial_payload)
    SetReviewPending(False)

    experiment_start_time = 0.0
    finalized = False

    def finalize_experiment(say_goodbye: bool = False) -> None:
        nonlocal finalized
        if finalized:
            return
        finalized = True

        speech_recognizer.stop()
        posture_estimator.close()
        cap.release()
        cv2.destroyAllWindows()

        actual_experiment_duration = time.time() - experiment_start_time if experiment_start_time else 0.0
        print("\n" + "=" * 60)
        print(f"[SUMMARY] {current_condition['name']} complete ({actual_experiment_duration:.1f}s)")
        print("=" * 60)

        summary_record = metrics.build_summary(
            condition_name=current_condition["name"],
            measured_side=MEASURED_SIDE,
            experiment_duration_s=actual_experiment_duration,
            user_height_cm=user_height_cm,
            shoulder_height_cm=user_shoulder_height_cm,
            upper_arm_cm=l1_cm,
            forearm_cm=l2_cm,
            drill_tcp_offset_cm=DRILL_TCP_OFFSET_M * 100.0,
        )
        data_logger.write_summary(summary_record)

        if say_goodbye:
            speak_and_wait("수고하셨습니다. 실험이 종료되었습니다.")
        tts_speaker.stop()

    atexit.register(finalize_experiment)

    experiment_start_time = time.time()
    key = -1
    robot_state = "UNKNOWN"
    previous_robot_state = None
    next_robot_state_poll_time = 0.0

    while True:
        if not cap.isOpened():
            print("[CAMERA ERROR] 카메라 연결이 닫혀 실험을 종료합니다.")
            metrics.mark_early_stop()
            break

        elapsed_time = time.time() - experiment_start_time
        manual_stop_requested = key in (27, ord("q"), ord("Q"))
        if elapsed_time >= MAX_EXPERIMENT_TIME_SEC or manual_stop_requested:
            if manual_stop_requested:
                print("[MANUAL STOP] Q/ESC input detected.")
                speak("실험 중단 키가 입력되어 실험을 종료합니다.")
                metrics.mark_early_stop()
            else:
                print("[TIMEOUT] Experiment time limit reached.")
                speak("제한 시간이 경과하여 실험을 종료합니다.")
            break

        current_voice = speech_recognizer.get_and_clear()

        ret, frame = cap.read()
        if not ret:
            break

        now = time.time()
        if now >= next_robot_state_poll_time:
            fetched_robot_state = GetRobotState()
            if fetched_robot_state:
                robot_state = fetched_robot_state
            next_robot_state_poll_time = now + ROBOT_STATE_POLL_SEC

        posture_sample = posture_estimator.process_frame(frame)
        entered_phase = robot_state != previous_robot_state
        posture_angle = posture_sample.shoulder_angle_deg if posture_sample.shoulder_angle_deg is not None else 0.0

        match robot_state:
            case "PICKING" | "IDLE" | "UNKNOWN":
                # 준비/대기 상태:
                # 이전 사이클에서 남은 리뷰 결과를 정리하고 review pending 상태를 꺼둔다.
                if entered_phase:
                    review_cycle_result = None
                    SetReviewPending(False)

            case "AT_TASK":
                # 작업 상태:
                # 의자 이동이 N 키로 확인된 뒤 자세 샘플을 누적하고,
                # SPACE 또는 음성 완료 의도만 처리한다.
                # 완료 의도가 들어오면 cycle 측정을 끝내 review_cycle_result에 저장한 뒤
                # SendHoldFinished()로 로봇이 RETURNING으로 넘어가게 알린다.
                if entered_phase:
                    speech_recognizer.get_and_clear()
                    current_voice = None
                    speak_and_wait("의자를 작업 위치로 옮겨주세요.")
                    print("[CHAIR CONFIRMATION] Press C when the chair is in position.")
                    waiting_for_chair_confirmation = True
                    review_cycle_result = None
                    last_task_sample_time = 0.0
                    key = -1

                if waiting_for_chair_confirmation and key in (ord("c"), ord("C")):
                    waiting_for_chair_confirmation = False
                    speech_recognizer.get_and_clear()
                    current_voice = None
                    speak("블록의 네 개 볼트에 있는 너트를 드릴로 빼주세요.")
                    metrics.start_cycle()
                    last_task_sample_time = 0.0
                    rest_s = 0.0
                    rest_start = None
                    key = -1

                if not waiting_for_chair_confirmation and review_cycle_result is None:
                    sample_time = time.time()
                    dt = max(0.0, sample_time - last_task_sample_time) if last_task_sample_time else 0.0
                    last_task_sample_time = sample_time
                    metrics.add_posture_sample(posture_sample, dt)

                    rest_now = time.monotonic()
                    if key in (ord("r"), ord("R")) and rest_start is None:
                        rest_start = rest_now
                        print("[REST START]")
                        key = -1
                    elif key in (ord("t"), ord("T")) and rest_start is not None:
                        rest_s += rest_now - rest_start
                        rest_start = None
                        print(f"[REST END] accumulated={rest_s:.2f}s")
                        key = -1

                    intent = manual_task_completion_from_key(key)
                    if intent is None and current_voice:
                        intent = manual_task_completion_from_text(current_voice)
                    if intent is None and current_voice and intent_interpreter is not None:
                        intent = intent_interpreter.interpret_task_completion(current_voice)
                        metrics.record_llm_call(intent_interpreter.last_latency_s)
                        save_llm_response_json(f"trial_{trial_count + 1:03d}_task_completion")
                        if getattr(intent_interpreter, "last_error", None):
                            print(f"[TASK COMPLETION LLM WARNING] {intent_interpreter.last_error}")
                            speak_and_wait("의도 해석 서버 호출에 실패했습니다. 잠시 후 다시 말씀해주세요.")
                            speech_recognizer.get_and_clear()
                            current_voice = None
                            intent = None

                    if intent is not None and intent.action == ACTION_COMPLETE:
                        print(f"[TASK COMPLETE] reason={intent.reason}")
                        if rest_start is not None:
                            rest_s += rest_now - rest_start
                            rest_start = None
                        speak_and_wait("작업 완료를 확인했습니다.")
                        review_cycle_result = metrics.finish_cycle(rest_time_s=rest_s)
                        rest_limit_s = min(review_cycle_result.task_time_s * 0.4, 20.0)
                        if review_cycle_result.rest_time_s > rest_limit_s:
                            print(
                                "[REST LIMIT EXCEEDED] "
                                f"task={review_cycle_result.task_time_s:.2f}s "
                                f"rest={review_cycle_result.rest_time_s:.2f}s "
                                f"limit={rest_limit_s:.2f}s"
                            )
                            metrics.mark_early_stop()
                            speak_and_wait("휴식 시간이 허용 한도를 초과하여 실험을 종료합니다.")
                            break
                        SendHoldFinished()
                    elif intent is not None:
                        print(f"[TASK COMPLETION UNCLEAR] action={intent.action} reason={intent.reason}")
                        speak_and_wait("의도 파악을 잘 못했습니다. 다시 한번 말씀해주세요.")
                        speech_recognizer.get_and_clear()
                        current_voice = None

            case "RETURNING":
                # 리뷰 상태:
                # 확정된 cycle_result를 조건별 target_info로 바꾸고,
                # 그 target_info를 pose_generator와 SendPassGoal/CSV 기록으로 연결한다.
                if review_cycle_result is not None:
                    SetReviewPending(True)
                    target_info = resolve_returning_pose_target(review_cycle_result)
                    action = target_info.get("action")
                    target_type = target_info.get("target_type")
                    previous_target_floor_height_m = current_target_floor_height_m
                    trial_number = trial_count + 1

                    if action == "adjust" and target_type == "shoulder_angle":
                        if target_info.get("target_shoulder_angle_deg") is None:
                            raise RuntimeError(f"Missing target_shoulder_angle_deg in target_info: {target_info!r}")
                        pose_result = pose_generator.generate_pose_from_shoulder_angle(
                            target_info["target_shoulder_angle_deg"],
                            human_profile,
                        )
                        next_target_floor_height_m, adjustment_m, robot_command_sent = send_pose_goal(
                            pose_result,
                            trial_number,
                            previous_target_floor_height_m,
                        )
                    elif action == "adjust" and target_type == "floor_height":
                        if target_info.get("target_floor_height_m") is None:
                            raise RuntimeError(f"Missing target_floor_height_m in target_info: {target_info!r}")
                        pose_result = pose_generator.generate_pose_from_floor_height(
                            target_info["target_floor_height_m"],
                            human_profile,
                        )
                        next_target_floor_height_m, adjustment_m, robot_command_sent = send_pose_goal(
                            pose_result,
                            trial_number,
                            previous_target_floor_height_m,
                        )
                    elif action == "keep" and target_type == "none":
                        pose_result = pose_generator.generate_pose_from_floor_height(
                            current_target_floor_height_m,
                            human_profile,
                        )
                        next_target_floor_height_m = current_target_floor_height_m
                        adjustment_m = 0.0
                        robot_command_sent = False
                    else:
                        raise RuntimeError(f"Unexpected target_info in RETURNING: {target_info!r}")

                    write_target_trial_record(
                        target_info=target_info,
                        cycle=review_cycle_result,
                        pose_result=pose_result,
                        trial_number=trial_number,
                        previous_target_floor_height_m=previous_target_floor_height_m,
                        next_target_floor_height_m=next_target_floor_height_m,
                        adjustment_m=adjustment_m,
                        robot_command_sent=robot_command_sent,
                    )
                    current_target_floor_height_m = next_target_floor_height_m
                    current_target_shoulder_angle_deg = pose_generator.shoulder_angle_from_floor_height(
                        profile=human_profile,
                        floor_height_m=current_target_floor_height_m,
                    )
                    trial_count = trial_number
                    review_cycle_result = None
                    SetReviewPending(False)

            case _:
                # 그 외 상태:
                # 아직 실험 판단에는 쓰지 않는 로봇 상태이므로 화면 표시만 유지한다.
                # Other robot states only keep the display alive.
                pass

        phase_label = robot_state

        cv2.putText(frame, f"HRI State: {phase_label}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(frame, f"Robot State: {robot_state}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 220, 0), 2)
        cv2.putText(frame, f"Trial: {trial_count}", (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
        cv2.putText(frame, f"Posture Angle: {posture_angle:.1f} deg", (20, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

        current_elapsed_sec = time.time() - experiment_start_time
        elapsed_mins = int(current_elapsed_sec // 60)
        elapsed_secs = int(current_elapsed_sec % 60)
        timer_text = f"Time: {elapsed_mins:02d}:{elapsed_secs:02d} / 08:00"
        cv2.putText(frame, timer_text, (20, 165), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
        if robot_state == "AT_TASK" and waiting_for_chair_confirmation:
            cv2.putText(
                frame,
                "Move chair to work position, then press C",
                (20, 415),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 255),
                2,
            )
        cv2.putText(
            frame,
            "[Manual Override] C: Start | R: Rest Start | T: Rest End | SPACE: Done | Q/ESC: Stop",
            (20, 450),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 200, 200),
            2,
        )

        cv2.imshow(DISPLAY_WINDOW_NAME, frame)
        key = cv2.waitKey(10) & 0xFF
        previous_robot_state = robot_state

    finalize_experiment(say_goodbye=True)


if __name__ == "__main__":
    main()

