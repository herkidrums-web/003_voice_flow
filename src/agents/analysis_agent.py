"""AnalysisAgent — Opus topic-based 5W1H + key_facts/decisions/actions extraction."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_PROMPT = """다음 미팅 STT를 토픽별로 분리하고, 각 토픽에 대해 5W1H + 핵심사실/결정/액션을 추출하세요.

규칙:
- 이성우 직함은 '담당' (절대 '상무' 금지)
- 보고대상자 호칭/직함을 정확히 인용
- 사실 기반, 자의적 판단 금지

전사문:
{transcript}

{extra}

JSON으로만 응답:
{{
  "analyses": [
    {{
      "topic": "토픽명",
      "five_w_one_h": {{"who": "", "what": "", "when": "", "where": "", "why": "", "how": ""}},
      "key_facts": ["사실1", "사실2"],
      "decisions": ["결정1"],
      "actions": ["액션1"]
    }}
  ]
}}
"""


class AnalysisAgent(BaseAgent):
    name = "analysis"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        extra = payload.get("extra_instruction", "")
        prompt = _PROMPT.format(
            transcript=payload["transcript"],
            extra=f"추가 지시: {extra}" if extra else "",
        )
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            temperature=payload.get("temperature", 0.7),
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Analysis returned invalid JSON: {e}: {text[:200]}")
        return {"analyses": parsed.get("analyses", [])}
