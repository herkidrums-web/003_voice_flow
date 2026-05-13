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
        analyses = payload.get("analyses", [])

        all_actions = [a for item in analyses for a in item.get("actions", []) if a]
        status = "후속필요" if all_actions else "완료"
        priority_map = {"high": "긴급", "medium": "보통", "low": "낮음"}

        properties = {
            "project": classification.get("project", []),
            "meeting_type": classification.get("meeting_type", "기타"),
            "importance": classification.get("importance", "medium"),
            "tags": classification.get("tags", []),
            "knowledge_type": classification.get("knowledge_type", []),
            "participants": ", ".join(str(p) for p in payload.get("participants", [])),
            "customer": payload.get("customer", ""),
            "summary": payload.get("summary", ""),
            "next_actions": "; ".join(all_actions)[:1000],
            "status": status,
            "priority": priority_map.get(classification.get("importance", "medium"), "보통"),
            "risks": payload.get("risks", []),
            "source_filename": payload.get("source_filename", ""),
        }
        page = create_meeting_note(
            database_id=self.database_id,
            api_key=self.api_key,
            title=payload["title"],
            date=payload["date"],
            transcript=payload.get("transcript", ""),
            analyses=analyses,
            properties=properties,
        )
        return {"page_id": page["id"], "notion_url": page["url"]}
