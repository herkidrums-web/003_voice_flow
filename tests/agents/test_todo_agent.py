"""TodoAgent — collects action items across batch + groups by project."""
import json
from unittest.mock import MagicMock

from src.agents.todo_agent import TodoAgent


def _mock_client(payload):
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload, ensure_ascii=False))]
    c.messages.create.return_value = msg
    return c


def test_groups_actions_by_project_via_llm():
    response = {"todos": [
        {"project": "네이버클라우드", "items": ["KA플랜 그룹장 보고", "임원 일정 확정"]},
        {"project": "DBO", "items": ["코람코 회신"]},
    ]}
    agent = TodoAgent(client=_mock_client(response), model="m")
    result = agent.execute({
        "batch_actions": [
            {"source": "20260503_네이버", "action": "KA플랜 그룹장 보고"},
            {"source": "20260503_네이버", "action": "임원 일정 확정"},
            {"source": "20260503_코람코", "action": "코람코 회신"},
        ],
        "target_date": "2026-05-04",
    })
    assert result.ok is True
    assert len(result.data["todos"]) == 2
    assert result.data["target_date"] == "2026-05-04"


def test_empty_batch_returns_empty_todos():
    agent = TodoAgent(client=_mock_client({"todos": []}), model="m")
    result = agent.execute({"batch_actions": [], "target_date": "2026-05-04"})
    assert result.ok is True
    assert result.data["todos"] == []
