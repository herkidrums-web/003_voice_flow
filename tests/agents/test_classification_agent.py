"""ClassificationAgent — Haiku-driven multi-select tagging."""
import json
from unittest.mock import MagicMock

import pytest

from src.agents.classification_agent import ClassificationAgent


def _mock_anthropic_client(payload_dict: dict) -> MagicMock:
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload_dict, ensure_ascii=False))]
    client.messages.create.return_value = msg
    return client


def test_returns_tags_from_haiku_response():
    expected = {"project": ["네이버클라우드", "DBO"], "meeting_type": "임원보고", "importance": "high"}
    client = _mock_anthropic_client(expected)
    agent = ClassificationAgent(client=client, model="claude-haiku-4-5-20251001")

    result = agent.execute({
        "title": "20260503_네이버_KA플랜",
        "summary": "네이버클라우드 50MW 추가 수주 검토",
    })
    assert result.ok is True
    assert result.data["project"] == ["네이버클라우드", "DBO"]
    assert result.data["meeting_type"] == "임원보고"
    assert result.data["importance"] == "high"
    client.messages.create.assert_called_once()
    args = client.messages.create.call_args.kwargs
    assert args["model"] == "claude-haiku-4-5-20251001"


def test_strips_commas_from_select_values():
    """Notion select API rejects commas — Memory 'feedback_notion_select_comma'."""
    expected = {"project": ["네이버, 토스"], "meeting_type": "내부, 회의", "importance": "high"}
    agent = ClassificationAgent(client=_mock_anthropic_client(expected), model="m")
    result = agent.execute({"title": "T", "summary": "S"})
    assert "," not in result.data["meeting_type"]
    assert all("," not in p for p in result.data["project"])


def test_invalid_json_raises():
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text="NOT JSON")]
    client.messages.create.return_value = msg
    agent = ClassificationAgent(client=client, model="m")
    result = agent.execute({"title": "T", "summary": "S"})
    assert result.ok is False
    assert "JSON" in result.error or "json" in result.error.lower()
