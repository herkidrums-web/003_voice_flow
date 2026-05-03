"""ValidationAgent — structural + business-rule validation of analysis output."""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent

_REQUIRED_FIELDS = ["topic", "five_w_one_h", "key_facts", "decisions", "actions"]


class ValidationAgent(BaseAgent):
    name = "validation"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def _structural_issues(self, analyses: list[dict]) -> list[str]:
        issues: list[str] = []
        for i, a in enumerate(analyses):
            for f in _REQUIRED_FIELDS:
                if f not in a:
                    issues.append(f"analysis[{i}].{f} missing")
        return issues

    def _business_rule_issues(self, transcript: str, analyses: list[dict]) -> list[str]:
        issues: list[str] = []
        if "이성우 상무" in transcript:
            issues.append("'이성우 상무' 표현 사용 금지 — '이성우 담당'으로 교정 필요")
        haystack = json.dumps(analyses, ensure_ascii=False)
        if "이성우 상무" in haystack:
            issues.append("analyses에 '이성우 상무' 표현 포함")
        return issues

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        analyses = payload.get("analyses", [])
        transcript = payload.get("transcript", "")
        issues = self._structural_issues(analyses) + self._business_rule_issues(transcript, analyses)
        return {"pass": not issues, "issues": issues}
