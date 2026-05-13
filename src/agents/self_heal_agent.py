"""SelfHealAgent — proposes parameter adjustments after Validation failure."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_PROMPT = """Analysis 에이전트 출력이 다음 검증 항목에서 실패했다.

실패 항목:
{issues}

직전 파라미터:
{previous_params}

재시도 시 사용할 조정안을 JSON으로 반환:
{{
  "adjustments": {{
    "temperature": 0.0~1.0,
    "extra_instruction": "Analysis 프롬프트에 추가할 한 줄 보강 지시"
  }}
}}
"""


class SelfHealAgent(BaseAgent):
    name = "self_heal"

    def __init__(self, client, model: str, max_retries: int):
        self.client = client
        self.model = model
        self.max_retries = max_retries

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        issues = payload.get("issues", [])
        attempt = payload.get("attempt", 0)

        if not issues or attempt >= self.max_retries:
            return {"should_retry": False, "next_params": payload.get("previous_params", {})}

        prompt = _PROMPT.format(
            issues="\n".join(f"- {i}" for i in issues),
            previous_params=json.dumps(payload.get("previous_params", {}), ensure_ascii=False),
        )
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"SelfHeal returned invalid JSON: {e}: {text[:200]}")

        next_params = dict(payload.get("previous_params", {}))
        next_params.update(parsed.get("adjustments", {}))
        return {"should_retry": True, "next_params": next_params, "attempt": attempt + 1}
