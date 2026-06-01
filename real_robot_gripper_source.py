"""
real_robot_gripper_source.py

실제 IndyDCP3 그리퍼 상태를 폴링해서
닫힘 -> 열림 순간에만 공유 open_event를 set하는 최소 구현.
"""

from __future__ import annotations

import time
from threading import Event, Thread

from neuromeka import IndyDCP3
from neuromeka.enums import EndtoolState

# 요청대로 IP 고정
ROBOT_IP = "166.104.214.96"
PORT_NAME = "C"
OPEN_STATE = EndtoolState.HIGH_PNP
POLL_SEC = 0.05

# 열림 엣지(트리거) 횟수 저장
open_edge_count = 0


def record_open_edge_count() -> int:
    """열림 엣지 횟수를 +1 하고 현재 누적값을 반환한다."""
    global open_edge_count
    open_edge_count += 1
    return open_edge_count


def fetch_gripper_is_open(indy: IndyDCP3) -> bool:
    """
    현재 그리퍼 open 여부를 읽는다.
    gripper_node.py와 동일하게 get_endtool_do() 기반.
    """
    do_state = indy.get_endtool_do()

    # {"signals": [{"port": "C", "states": [-2]}]} 형태
    if isinstance(do_state, dict) and "signals" in do_state:
        signals = do_state.get("signals")
        if isinstance(signals, list):
            for signal in signals:
                if not isinstance(signal, dict):
                    continue
                port = signal.get("port")
                if str(port) != PORT_NAME:
                    continue
                value = signal.get("states")
                if isinstance(value, (list, tuple)) and value:
                    value = value[0]
                return value == OPEN_STATE
        raise KeyError(f"port '{PORT_NAME}' not found in DO state")

    # {"C": [value]} 또는 {"C": value} 형태
    if isinstance(do_state, dict):
        value = do_state.get(PORT_NAME)
        if value is None:
            raise KeyError(f"port '{PORT_NAME}' not found in DO state")
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        return value == OPEN_STATE

    # [("C", [value]), ...] 또는 [("C", value), ...] 형태
    if isinstance(do_state, (list, tuple)):
        for item in do_state:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            port, value = item[0], item[1]
            if str(port) != PORT_NAME:
                continue
            if isinstance(value, (list, tuple)) and value:
                value = value[0]
            return value == OPEN_STATE
        raise KeyError(f"port '{PORT_NAME}' not found in DO state")

    raise TypeError(f"unsupported DO state type: {type(do_state).__name__}")


def watch_gripper_open_edge_flag(open_event: Event) -> None:
    """
    무한 루프 폴링:
    닫힘 -> 열림 전이에서만 전달받은 open_event를 set.
    """
    print(
        "[REAL ROBOT] IndyDCP3 연결 시도: "
        f"ip={ROBOT_IP}, port={PORT_NAME}, open_state={OPEN_STATE}"
    )
    indy = IndyDCP3(ROBOT_IP)
    print("[REAL ROBOT] IndyDCP3 연결 완료")
    prev_open = False
    first_state_logged = False

    while True:
        try:
            curr_open = fetch_gripper_is_open(indy)
            if not first_state_logged:
                print(
                    "[REAL ROBOT] 최초 상태 읽기 성공: "
                    f"is_open={curr_open}"
                )
                first_state_logged = True
            if (not prev_open) and curr_open:
                count = record_open_edge_count()
                if count == 1:
                    print("[REAL ROBOT] 첫 GRIPPER_OPEN edge는 무시합니다")
                else:
                    open_event.set()
                    print(f"[REAL ROBOT] GRIPPER_OPEN edge #{count} -> event=set")
            prev_open = curr_open
        except Exception as exc:  # noqa: BLE001
            print(f"[REAL ROBOT] gripper poll error: {exc}")

        time.sleep(POLL_SEC)


def start_real_robot_gripper_listener(open_event: Event) -> Thread:
    """실기 그리퍼 open 엣지 리스너 스레드를 시작한다."""
    print("[REAL ROBOT] 리스너 시작 요청")
    thread = Thread(
        target=watch_gripper_open_edge_flag,
        args=(open_event,),
        daemon=True,
    )
    thread.start()
    print("[REAL ROBOT] 리스너 스레드 시작 완료")
    return thread
