"""TodoAgent — batch action collection → next-day to-do groupings."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_PROMPT = """다음 액션 아이템들을 프로젝트별로 묶어 다음날 ({date}) To-Do로 정리하세요.

액션 목록:
{actions}

JSON으로만 응답:
{{
  "todos": [
    {{"project": "프로젝트명", "items": ["할 일1", "할 일2"]}}
  ]
}}

규칙:
- 중복/유사 항목은 하나로 합친다
- project가 모호하면 "기타"로
"""


class TodoAgent(BaseAgent):
    name = "todo"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        actions = payload.get("batch_actions", [])
        target_date = payload["target_date"]

        if not actions:
            return {"todos": [], "target_date": target_date}

        actions_block = "\n".join(
            f'- [{a["source"]}] {a["action"]}' for a in actions
        )
        prompt = _PROMPT.format(actions=actions_block, date=target_date)
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Todo returned invalid JSON: {e}: {text[:200]}")
        return {"todos": parsed.get("todos", []), "target_date": target_date}
