"""ClassificationAgent — tags meeting notes with project/type/importance via Haiku."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_PROMPT = """다음 미팅노트를 태깅하세요. JSON으로만 응답.

제목: {title}
요약: {summary}

JSON:
{{
  "project": ["프로젝트명"],
  "meeting_type": "임원보고|내부회의|고객미팅|업체미팅|기타",
  "importance": "high|medium|low",
  "tags": ["태그1", "태그2"],
  "knowledge_type": ["deal_progress|customer_intel|market_intel|internal_decision|other"]
}}

규칙:
- project / meeting_type / tags 값에 쉼표(,) 사용 금지 (Notion select API 제약)
- knowledge_type 복수 허용
- 명확하지 않으면 빈 배열 또는 "기타"
"""


def _strip_commas(value):
    if isinstance(value, list):
        return [v.replace(",", " ") for v in value]
    if isinstance(value, str):
        return value.replace(",", " ")
    return value


class ClassificationAgent(BaseAgent):
    name = "classification"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(title=payload["title"], summary=payload["summary"]),
            }],
        )
        text = msg.content[0].text.strip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Haiku returned invalid JSON: {e}: {text[:200]}")

        return {
            "project": _strip_commas(parsed.get("project", [])),
            "meeting_type": _strip_commas(parsed.get("meeting_type", "기타")),
            "importance": parsed.get("importance", "medium"),
            "tags": _strip_commas(parsed.get("tags", [])),
            "knowledge_type": _strip_commas(parsed.get("knowledge_type", [])),
        }
