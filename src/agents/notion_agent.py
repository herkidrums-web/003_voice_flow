"""NotionAgent — adapter over notion_writer.create_meeting_note with v3 properties.

자동 archive: 같은 source_filename의 이전 notion 페이지(state 기록)를 새 페이지
생성 직전에 archive 처리. 재처리 시 중복 페이지 방지.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from notion_client import Client as NotionClient

from src.agents.base import BaseAgent
from src.notion_writer import create_meeting_note_v3 as create_meeting_note

log = logging.getLogger(__name__)


def _page_id_from_url(url: str) -> str:
    if not url:
        return ""
    tail = url.rsplit("/", 1)[-1].split("?")[0]
    raw = tail[-32:] if len(tail) >= 32 else tail.split("-")[-1]
    if len(raw) == 32:
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    return raw


def _archive_old_pages_for_file(
    state_path: Path,
    source_filename: str,
    notion_client: NotionClient,
) -> int:
    """state에서 같은 파일의 이전 notion:done URL을 찾아서 archive."""
    if not state_path.exists() or not source_filename:
        return 0
    urls: list[str] = []
    for line in state_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (e.get("file") == source_filename
                and e.get("stage") == "notion"
                and e.get("status") == "done"):
            url = (e.get("meta") or {}).get("notion_url", "")
            if url:
                urls.append(url)
    archived = 0
    for url in urls:
        page_id = _page_id_from_url(url)
        if not page_id:
            continue
        try:
            notion_client.pages.update(page_id=page_id, archived=True)
            archived += 1
            log.info("auto-archived old page: %s (file=%s)", url, source_filename)
        except Exception as e:
            log.warning("auto-archive failed (%s): %s", url, e)
    return archived


class NotionAgent(BaseAgent):
    name = "notion"

    def __init__(self, database_id: str, api_key: str, state_path: Path | None = None):
        self.database_id = database_id
        self.api_key = api_key
        self.state_path = state_path
        self._notion_client = NotionClient(auth=api_key) if state_path else None

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
        # 같은 파일의 이전 페이지를 archive — 재처리 시 중복 방지
        archived_count = 0
        source_filename = payload.get("source_filename", "")
        if self.state_path and self._notion_client and source_filename:
            archived_count = _archive_old_pages_for_file(
                self.state_path, source_filename, self._notion_client,
            )

        page = create_meeting_note(
            database_id=self.database_id,
            api_key=self.api_key,
            title=payload["title"],
            date=payload["date"],
            transcript=payload.get("transcript", ""),
            analyses=analyses,
            properties=properties,
        )
        return {"page_id": page["id"], "notion_url": page["url"], "archived_old": archived_count}
