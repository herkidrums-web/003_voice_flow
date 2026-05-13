"""GroupingAgent — content-similarity grouping with size-based fallback split."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_PROMPT = """다음 STT 전사문 목록을 **내용 유사성** 기준으로 그루핑하세요.
같은 회의/주제로 판단되면 같은 그룹, 별도 주제는 별도 그룹.

파일:
{files}

JSON으로만 응답:
{{"groups": [["a.m4a","b.m4a"], ["c.m4a"]]}}

규칙:
- 시간 정보는 무시. 내용 기준으로만 판단.
- 단일 파일이면 그대로 단일 그룹.
"""


def _split_oversized(group: list[str], transcripts: dict[str, str], max_chars: int) -> list[list[str]]:
    total = sum(len(transcripts.get(n, "")) for n in group)
    if total <= max_chars:
        return [group]
    return [[name] for name in group]


class GroupingAgent(BaseAgent):
    name = "grouping"

    def __init__(self, client, model: str, max_chars: int):
        self.client = client
        self.model = model
        self.max_chars = max_chars

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        files = payload["files"]
        transcripts = {f["name"]: f["transcript"] for f in files}

        if len(files) == 1:
            return {"groups": [[files[0]["name"]]]}

        files_block = "\n".join(
            f'- {f["name"]}: {f["transcript"][:500]}' for f in files
        )
        prompt = _PROMPT.format(files=files_block)
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Grouping returned invalid JSON: {e}: {text[:200]}")

        groups = parsed.get("groups", [[f["name"] for f in files]])

        out: list[list[str]] = []
        for g in groups:
            out.extend(_split_oversized(g, transcripts, self.max_chars))
        return {"groups": out}
