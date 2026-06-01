#experiment_controller.py (실험 조건 제어 뇌)
import json
from datetime import datetime
from openai import OpenAI

LINK0_HEIGHT_MM = 634.0

class PickAndPlaceExperiment:
    def __init__(self, api_key, base_url=None):
        if base_url:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            self.client = OpenAI(api_key=api_key)

    def run_task(
        self,
        condition,
        sh_angle,
        avg_sh_angle,
        target_pass_floor_z_mm,
        adj_mm,
        current_pass_floor_z_mm,
        h_sh,
        l1,
        l2,
    ):
        timestamp_iso = datetime.now().astimezone().isoformat(timespec="milliseconds")

        interv = condition.get("intervention", "개입")  
        lead = condition.get("lead", "시스템")          
        control = condition.get("control", "llm")       

        if interv == "비개입":
            final_target_pass_floor_z_mm = current_pass_floor_z_mm
            msg = "[No-Intervention Mode] 위험 범위 이탈이 파싱되었으나 대조군 조건이므로 제어를 차단합니다."
        else:
            final_target_pass_floor_z_mm = target_pass_floor_z_mm
            if control == "llm":
                # 로봇에 전달할 신체 실측 데이터를 통째로 LLM 프롬프트에 넘겨줍니다.
                msg = self._generate_gpt_msg(
                    lead,
                    avg_sh_angle,
                    adj_mm,
                    h_sh,
                    l1,
                    l2,
                    current_pass_floor_z_mm,
                )
            else:
                msg = self._generate_rule_msg(lead, adj_mm)

        # [수정됨] JSON 패키지에 그 시점의 사람 '겨드랑이 각도'를 추가하여 로봇에 함께 전달
        result_json = {
            "frame_id": "link0",
            "timestamp": timestamp_iso,
            "armpit_angle_deg": round(avg_sh_angle, 1), 
            "position": {
                "x": 0.45,
                "y": 0.00,
                "z": round(
                    (final_target_pass_floor_z_mm - LINK0_HEIGHT_MM) / 1000,
                    3,
                )
            },
            "orientation": {
                "x": 0.0,
                "y": 0.9239,
                "z": 0.0,
                "w": 0.3827
            },
            "description": msg  
        }
        
        return json.dumps(result_json, indent=2, ensure_ascii=False)

    def _generate_gpt_msg(self, lead, avg_sh_angle, adj, h_sh, l1, l2, curr_z):
        # [수정됨] 미사여구 제거, 공학적 수치와 측정값 중심의 정량적 프롬프트로 완전 개편
        prompt = f"""
        당신은 협동로봇의 인체공학적 자세를 계산하는 AI입니다. 단순 감성 위로나 미사여구를 철저히 배제하세요.
        
        [입력된 작업자 기초 데이터]
        - 바닥~어깨 높이: {h_sh}cm
        - 상완 길이: {l1}cm / 하완 길이: {l2}cm
        - 현재 pass 높이(바닥 기준): {curr_z:.1f}mm
        
        [그리퍼 열림 시점 실측 데이터]
        - 측정된 겨드랑이 각도: {avg_sh_angle:.1f}도 (위험 임계치 60도 초과)
        - 시스템 산출 필요 이동량: {abs(adj):.1f}mm
        
        [지시사항]
        위 수치들을 종합하여, 왜 해당 수치({abs(adj):.1f}mm)만큼 위치를 조정해야 하는지 인체공학적 계산 근거(중립 자세 복귀 등)를 포함해 객관적인 보고서 형태의 1~2문장으로만 출력하세요. 자연어가 필요하다면 이 측정값과 공학적 근거에 기반해서만 간결하게 언급하세요.
        """
        
        try:
            response = self.client.chat.completions.create(
                model="llama-3.1-8b-instant", 
                messages=[
                    {"role": "system", "content": "너는 공학 데이터를 분석하여 수치 기반의 결과만 도출하는 로봇 관제 엔진이다."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=150
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"[Llama API 통신 실패] 안전 확보를 위해 작업대를 {abs(adj):.1f}mm 정량 보정합니다."

    def _generate_rule_msg(self, lead, adj):
        if lead == "시스템":
            return f"[Rule-Base] 60도 초과 감지. 규정된 고정 수치({abs(adj):.1f}mm)만큼 이동합니다."
        else: 
            return f"[Rule-Base] 작업자 동의 수신. 규정된 고정 수치({abs(adj):.1f}mm)만큼 이동합니다."
