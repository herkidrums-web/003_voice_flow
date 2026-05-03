"""NotionAgent — wraps notion_writer and applies v3 page properties."""
from unittest.mock import MagicMock, patch

from src.agents.notion_agent import NotionAgent


def test_creates_page_with_classification_props():
    with patch("src.agents.notion_agent.create_meeting_note") as mock_create:
        mock_create.return_value = {"id": "page_xyz", "url": "https://www.notion.so/page_xyz"}
        agent = NotionAgent(database_id="db_123", api_key="x")
        result = agent.execute({
            "title": "20260503_네이버_KA",
            "date": "2026-05-03",
            "transcript": "...",
            "analyses": [{"topic": "T", "five_w_one_h": {}, "key_facts": [], "decisions": [], "actions": []}],
            "classification": {"project": ["네이버"], "meeting_type": "고객미팅", "importance": "high"},
        })
        assert result.ok is True
        assert result.data["notion_url"] == "https://www.notion.so/page_xyz"
        assert result.data["page_id"] == "page_xyz"
        kwargs = mock_create.call_args.kwargs
        assert kwargs["database_id"] == "db_123"
        assert kwargs["properties"]["project"] == ["네이버"]
        assert kwargs["properties"]["meeting_type"] == "고객미팅"
        assert kwargs["properties"]["importance"] == "high"


def test_propagates_writer_failure():
    with patch("src.agents.notion_agent.create_meeting_note", side_effect=RuntimeError("notion 500")):
        agent = NotionAgent(database_id="db", api_key="x")
        result = agent.execute({
            "title": "T", "date": "2026-05-03", "transcript": "", "analyses": [],
            "classification": {"project": [], "meeting_type": "기타", "importance": "low"},
        })
        assert result.ok is False
        assert "notion 500" in result.error
