"""NotionAgent — adapter over notion_writer.create_meeting_note with v3 properties."""
from __future__ import annotations

from typing import Any

from src.agents.base import BaseAgent
from src.notion_writer import create_meeting_note_v3 as create_meeting_note


class NotionAgent(BaseAgent):
    name = "notion"

    def __init__(self, database_id: str, api_key: str):
        self.database_id = database_id
        self.api_key = api_key

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        classification = payload.get("classification", {})
        properties = {
            "project": classification.get("project", []),
            "meeting_type": classification.get("meeting_type", "기타"),
            "importance": classification.get("importance", "medium"),
        }
        page = create_meeting_note(
            database_id=self.database_id,
            api_key=self.api_key,
            title=payload["title"],
            date=payload["date"],
            transcript=payload["transcript"],
            analyses=payload["analyses"],
            properties=properties,
        )
        return {"page_id": page["id"], "notion_url": page["url"]}
