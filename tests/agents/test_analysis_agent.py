"""AnalysisAgent — Opus topic-based 5W1H extraction."""
import json
from unittest.mock import MagicMock

from src.agents.analysis_agent import AnalysisAgent


def _mock_client(payload):
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload, ensure_ascii=False))]
    c.messages.create.return_value = msg
    return c


def test_returns_topics_with_5w1h():
    response = {"analyses": [{
        "topic": "네이버 50MW 추가 수주",
        "five_w_one_h": {"who": "이성우 담당", "what": "PoC 합의", "when": "5월", "where": "파주", "why": "용량 부족", "how": "Phase2"},
        "key_facts": ["50MW 증설", "예산 7,000억"],
        "decisions": ["6월 PoC 시작"],
        "actions": ["담당자 지정"],
    }]}
    agent = AnalysisAgent(client=_mock_client(response), model="claude-opus-4-7")
    result = agent.execute({"transcript": "...", "extra_instruction": "", "temperature": 0.7})
    assert result.ok is True
    a = result.data["analyses"][0]
    assert a["topic"] == "네이버 50MW 추가 수주"
    assert a["five_w_one_h"]["who"] == "이성우 담당"


def test_extra_instruction_is_appended_to_prompt():
    client = _mock_client({"analyses": []})
    agent = AnalysisAgent(client=client, model="m")
    agent.execute({"transcript": "x", "extra_instruction": "key_facts 최소 3개", "temperature": 0.5})
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "key_facts 최소 3개" in sent
    assert client.messages.create.call_args.kwargs["temperature"] == 0.5


def test_invalid_json_raises():
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text="not json")]
    c.messages.create.return_value = msg
    agent = AnalysisAgent(client=c, model="m")
    result = agent.execute({"transcript": "x", "extra_instruction": "", "temperature": 0.7})
    assert result.ok is False
