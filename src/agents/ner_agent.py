"""NERAgent — extracts new proper nouns (people / companies / industry terms)."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_PROMPT = """다음 STT 전사문에서 **신규** 고유명사(사람·회사·업계용어 약어)를 추출하세요.
이미 사전에 등록된 항목은 제외합니다.

이미 등록된 항목:
{known}

전사문:
{transcript}

JSON으로만 응답:
{{
  "new_terms": [
    {{"misheard": "STT가 잘못 받아쓴 표기 (있으면)", "correct": "올바른 표기", "category": "person|company|term"}}
  ]
}}

규칙:
- misheard가 명확하지 않으면 빈 문자열로 두고 correct만 채운다 (사전에는 추가하지 않음)
- 신규 항목이 없으면 `{{"new_terms": []}}` 반환
"""


class NERAgent(BaseAgent):
    name = "ner"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        known = "\n".join(f"- {t}" for t in payload.get("known_terms", []))
        prompt = _PROMPT.format(known=known or "(없음)", transcript=payload["transcript"])
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"NER returned invalid JSON: {e}: {text[:200]}")
        new_terms = [t for t in parsed.get("new_terms", []) if t.get("misheard") and t.get("correct")]
        return {"new_terms": new_terms}
