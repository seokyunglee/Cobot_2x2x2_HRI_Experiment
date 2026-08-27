from __future__ import annotations

import time

import cv2

from hri_http_sender import GetRobotState, SendHoldFinished, SendPassGoal
from pose_generator import HumanArmProfile, HumanAwareTcpPoseGenerator
from posture_estimator import PostureEstimator
from voice_intent_interface import (
    ACTION_COMPLETE,
    ContinuousSpeechRecognizer,
    QueuedTtsSpeaker,
    manual_task_completion_from_key,
    manual_task_completion_from_text,
)


CAMERA_FRAME_WIDTH = 1920
CAMERA_FRAME_HEIGHT = 1080
DISPLAY_WINDOW_NAME = "HRI Zero-Condition Training"
DISPLAY_WINDOW_WIDTH = 1920
DISPLAY_WINDOW_HEIGHT = 1080
ROBOT_STATE_POLL_SEC = 0.2

TRAINING_TARGETS = {
    ord("1"): ("1", 130.0),
    ord("2"): ("2", 110.0),
    ord("3"): ("3", 90.0),
    ord("4"): ("4", 70.0),
}


def speak(speaker: QueuedTtsSpeaker, text: str) -> None:
    print(f"[TTS] {text}")
    speaker.speak(text)


def speak_and_wait(speaker: QueuedTtsSpeaker, text: str) -> None:
    speak(speaker, text)
    speaker.wait_until_done()


def prompt_float(prompt: str, default: float) -> float:
    try:
        raw = input(prompt)
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


def main() -> None:
    print("\n" + "=" * 60)
    print("Zero-condition training participant setup")
    print("=" * 60)

    participant_id = input(" Participant ID: ").strip()
    while not participant_id:
        print("[INPUT ERROR] Participant ID is required.")
        participant_id = input(" Participant ID: ").strip()

    user_height_cm = prompt_float(" 1. Height in cm [155.0]: ", 155.0)
    shoulder_height_cm = prompt_float(
        f" 2. Shoulder height in cm [{user_height_cm - 30.0:.1f}]: ",
        user_height_cm - 30.0,
    )
    upper_arm_cm = prompt_float(" 3. Upper arm length in cm [30.0]: ", 30.0)
    forearm_cm = prompt_float(" 4. Forearm length in cm [25.0]: ", 25.0)

    human_profile = HumanArmProfile(
        user_height_m=user_height_cm / 100.0,
        shoulder_height_m=shoulder_height_cm / 100.0,
        upper_arm_m=upper_arm_cm / 100.0,
        forearm_m=forearm_cm / 100.0,
    )
    pose_generator = HumanAwareTcpPoseGenerator()
    speaker = QueuedTtsSpeaker()
    speech_recognizer = ContinuousSpeechRecognizer(
        on_text=lambda text: print(f"[STT] '{text}'")
    )
    posture_estimator = PostureEstimator()
    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        print("[CAMERA ERROR] Training will not start.")
        cap.release()
        posture_estimator.close()
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_FRAME_HEIGHT)
    cv2.namedWindow(DISPLAY_WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(DISPLAY_WINDOW_NAME, DISPLAY_WINDOW_WIDTH, DISPLAY_WINDOW_HEIGHT)
    speech_recognizer.start()

    # No start key and no initial fixed pose: select 1--4 immediately.
    speak(speaker, "트레이닝 세션을 시작합니다.")
    print("[TRAINING READY] Select 1--4. Q/ESC stops training.")

    selected_key = "-"
    selected_angle_deg: float | None = None
    robot_state = "UNKNOWN"
    previous_robot_state: str | None = None
    next_robot_state_poll_time = 0.0
    pass_goal_sent_for_cycle = False
    hold_finished_sent_for_cycle = False
    waiting_for_chair_confirmation = False
    key = -1

    def send_selected_target(target_key: int) -> bool:
        nonlocal selected_key, selected_angle_deg, pass_goal_sent_for_cycle

        selected_key, selected_angle_deg = TRAINING_TARGETS[target_key]
        pose_result = pose_generator.generate_pose_from_shoulder_angle(
            selected_angle_deg,
            human_profile,
        )
        if pose_result.was_height_clamped:
            print("[POSE WARNING] Target height was clamped to the robot range.")

        payload = pose_result.to_pass_goal_dict(
            msg=f"Training target {selected_key}: {selected_angle_deg:.0f} deg",
        )
        if not SendPassGoal(payload):
            print("[PASS_GOAL ERROR] Select the same key to retry.")
            return False

        print(
            f"[TRAINING TARGET] key={selected_key} "
            f"shoulder={selected_angle_deg:.1f}deg "
            f"floor_height={pose_result.target_floor_height_m:.3f}m"
        )
        pass_goal_sent_for_cycle = True
        speech_recognizer.get_and_clear()
        return True

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                print("[CAMERA ERROR] Training is ending.")
                break

            now = time.time()
            if now >= next_robot_state_poll_time:
                fetched_state = GetRobotState()
                if fetched_state:
                    robot_state = fetched_state
                next_robot_state_poll_time = now + ROBOT_STATE_POLL_SEC

            posture = posture_estimator.process_frame(frame)
            shoulder_label = (
                f"{posture.shoulder_angle_deg:.1f} deg"
                if posture.shoulder_angle_deg is not None
                else "N/A"
            )
            elbow_label = (
                f"{posture.elbow_angle_deg:.1f} deg"
                if posture.elbow_angle_deg is not None
                else "N/A"
            )
            current_voice = speech_recognizer.get_and_clear()
            entered_at_task = robot_state == "AT_TASK" and previous_robot_state != "AT_TASK"

            # The polling result itself is the phase, matching main_integrated.py.
            match robot_state:
                case "PICKING" | "IDLE":
                    if key in TRAINING_TARGETS and not pass_goal_sent_for_cycle:
                        send_selected_target(key)

                case "AT_TASK":
                    if entered_at_task:
                        hold_finished_sent_for_cycle = False
                        waiting_for_chair_confirmation = True
                        speech_recognizer.get_and_clear()
                        current_voice = None
                        speak(speaker, "의자를 옮겨주세요.")
                        print("[CHAIR CONFIRMATION] Press C when the chair is in position.")

                    if waiting_for_chair_confirmation and key in (ord("c"), ord("C")):
                        waiting_for_chair_confirmation = False
                        speech_recognizer.get_and_clear()
                        current_voice = None
                        speak(speaker, "작업을 시작해주세요.")

                    if not waiting_for_chair_confirmation and not hold_finished_sent_for_cycle:
                        completion_intent = manual_task_completion_from_key(key)
                        if completion_intent is None and current_voice:
                            completion_intent = manual_task_completion_from_text(current_voice)

                        if completion_intent is not None and completion_intent.action == ACTION_COMPLETE:
                            speak(speaker, "작업 완료 확인")
                            if SendHoldFinished():
                                hold_finished_sent_for_cycle = True
                            else:
                                print("[HOLD_FINISHED ERROR] Complete input remains available for retry.")

                case "RETURNING":
                    # This is where main_integrated.py sends the next Pass Goal.
                    # Here the participant selects the next fixed training target instead.
                    if previous_robot_state != "RETURNING":
                        pass_goal_sent_for_cycle = False
                        waiting_for_chair_confirmation = False
                        speak(
                            speaker,
                            "불편자세가 감지되었습니다. 올려드릴까요, 내려드릴까요, 유지할까요?",
                        )
                    if key in TRAINING_TARGETS and not pass_goal_sent_for_cycle:
                        send_selected_target(key)

                case "UNKNOWN":
                    pass

                case _:
                    pass

            target_label = (
                f"{selected_key}: {selected_angle_deg:.0f} deg"
                if selected_angle_deg is not None
                else "-"
            )
            cv2.putText(frame, f"Robot State: {robot_state}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 220), 2)
            cv2.putText(frame, f"Selected Target: {target_label}", (20, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
            cv2.putText(frame, f"Posture Angle: {shoulder_label}", (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(frame, f"Elbow Angle: {elbow_label}", (20, 145), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 0), 2)
            if robot_state == "AT_TASK" and waiting_for_chair_confirmation:
                cv2.putText(frame, "Move chair to work position, then press C", (20, 385), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            cv2.putText(frame, "1:110  2:90  3:70  4:55 deg", (20, 420), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.putText(frame, "C: Start | SPACE: Done | Say task completion | Q/ESC: Stop", (20, 455), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 200), 2)
            cv2.imshow(DISPLAY_WINDOW_NAME, frame)

            key = cv2.waitKey(10) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
            previous_robot_state = robot_state
    finally:
        speech_recognizer.stop()
        posture_estimator.close()
        cap.release()
        cv2.destroyAllWindows()
        speak_and_wait(speaker, "수고하셨습니다. 실험이 종료되었습니다.")
        speaker.stop()


if __name__ == "__main__":
    main()
