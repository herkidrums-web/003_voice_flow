"""AnalysisAgent — Opus topic-based 5W1H + key_facts/decisions/actions extraction."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_PROMPT = """다음 미팅 STT를 분석하세요. JSON으로만 응답.

규칙:
- 이성우 직함은 '담당' (절대 '상무' 금지)
- 보고대상자 호칭/직함 정확히 인용
- 사실 기반, 자의적 판단 금지
- 대화에 없는 내용 추가 금지

전사문:
{transcript}

{extra}

JSON:
{{
  "summary": "전체 미팅 1-2문장 핵심 요약",
  "participants": ["이성우 담당", "참석자명 직함"],
  "customer": "외부 고객사명 (LG U+ 내부 회의면 빈 문자열)",
  "risks": ["리스크1", "리스크2"],
  "analyses": [
    {{
      "topic": "토픽명 (15자 이내 권장)",
      "five_w_one_h": {{
        "who": "핵심 관계자",
        "what": "무엇이 논의/결정됨",
        "when": "시점/기간",
        "where": "장소/채널",
        "why": "배경/이유",
        "how": "방법/절차"
      }},
      "key_facts": ["사실1 (원문 인용 포함 권장)", "사실2"],
      "decisions": ["결정1"],
      "actions": ["액션1 (@담당자) (~기한)"]
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
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Analysis returned invalid JSON: {e}: {text[:200]}")
        return {
            "analyses": parsed.get("analyses", []),
            "summary": parsed.get("summary", ""),
            "participants": parsed.get("participants", []),
            "customer": parsed.get("customer", ""),
            "risks": parsed.get("risks", []),
        }
