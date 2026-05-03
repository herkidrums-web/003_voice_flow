"""ValidationAgent — verifies analysis output completeness + title rule (이성우=담당)."""
import json
from unittest.mock import MagicMock

from src.agents.validation_agent import ValidationAgent


def _mock_client(payload):
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload, ensure_ascii=False))]
    c.messages.create.return_value = msg
    return c


def test_pass_when_all_fields_present_and_title_rule_ok():
    analyses = [{
        "topic": "네이버 KA",
        "five_w_one_h": {"who": "x"},
        "key_facts": ["fact1"],
        "decisions": ["d1"],
        "actions": [],
    }]
    agent = ValidationAgent(client=_mock_client({"pass": True, "issues": []}), model="m")
    result = agent.execute({"analyses": analyses, "transcript": "이성우 담당이 결정"})
    assert result.ok is True
    assert result.data["pass"] is True
    assert result.data["issues"] == []


def test_fail_when_analysis_missing_required_fields():
    analyses = [{"topic": "x"}]  # missing key_facts, decisions
    agent = ValidationAgent(client=_mock_client({"pass": True, "issues": []}), model="m")
    result = agent.execute({"analyses": analyses, "transcript": "x"})
    assert result.data["pass"] is False
    assert any("key_facts" in i or "decisions" in i for i in result.data["issues"])


def test_fail_when_title_uses_상무():
    """이성우 직함은 '담당'. '상무' 사용 금지 (Memory: feedback_title_rule)."""
    analyses = [{
        "topic": "T", "five_w_one_h": {}, "key_facts": ["x"], "decisions": ["x"], "actions": [],
    }]
    agent = ValidationAgent(client=_mock_client({"pass": True, "issues": []}), model="m")
    result = agent.execute({"analyses": analyses, "transcript": "이성우 상무가 보고"})
    assert result.data["pass"] is False
    assert any("상무" in i for i in result.data["issues"])
