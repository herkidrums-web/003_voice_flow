"""NotionBriefingPageBuilder — creates one Notion page per day in the briefing DB."""
from __future__ import annotations

from typing import Any


def _strip_commas(s: str) -> str:
    return s.replace(",", " ")


def _rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:2000]}}]


def _heading(level: int, text: str) -> dict:
    htype = f"heading_{level}"
    return {"object": "block", "type": htype, htype: {"rich_text": _rt(text)}}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(text)}}


def _todo(text: str) -> dict:
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": _rt(text), "checked": False}}


def _link_callout(label: str, url: str) -> dict:
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [
                {"type": "text", "text": {"content": label + " — "}},
                {"type": "text", "text": {"content": "Notion 열기", "link": {"url": url}}},
            ],
            "icon": {"type": "emoji", "emoji": "🔗"},
        }
    }


def _summary_callout(text: str) -> dict:
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
            "icon": {"type": "emoji", "emoji": "📌"},
            "color": "blue_background",
        }
    }


class NotionBriefingPageBuilder:
    def __init__(self, client: Any, database_id: str):
        self.client = client
        self.database_id = database_id

    def _build_meeting_blocks(self, meetings: list[dict]) -> list[dict]:
        blocks: list[dict] = [_heading(1, f"어제 미팅 요약 ({len(meetings)}건)")]
        if not meetings:
            blocks.append(_paragraph("어제 처리된 미팅 없음"))
            return blocks
        for m in meetings:
            blocks.append(_heading(2, m.get("title", "(제목 없음)")))
            url = m.get("notion_url", "")
            if url:
                blocks.append(_link_callout(m.get("title", ""), url))
            if m.get("summary"):
                blocks.append(_summary_callout(m["summary"]))
            if m.get("decisions"):
                blocks.append(_heading(3, "✅ 결정사항"))
                for d in m["decisions"]:
                    blocks.append(_bullet(d))
            if m.get("actions"):
                blocks.append(_heading(3, "🔴 액션 아이템"))
                for a in m["actions"]:
                    blocks.append(_todo(a))
            if m.get("implications"):
                blocks.append(_heading(3, "⚡ 시사점"))
                for imp in m["implications"]:
                    blocks.append(_bullet(imp))
            if m.get("risks"):
                blocks.append(_heading(3, "⚠️ 리스크"))
                for r in m["risks"]:
                    blocks.append(_bullet(r))
        return blocks

    def _build_todo_blocks(self, todos: list[dict]) -> list[dict]:
        total = sum(len(g.get("items", [])) for g in todos)
        blocks: list[dict] = [_heading(1, f"오늘 할 일 ({total}건)")]
        if not todos:
            blocks.append(_paragraph("생성된 To-Do 없음"))
            return blocks
        for group in todos:
            blocks.append(_heading(2, group.get("project", "기타")))
            for item in group.get("items", []):
                blocks.append(_todo(item))
        return blocks

    def _build_failed_blocks(self, failed: list[dict]) -> list[dict]:
        if not failed:
            return []
        blocks: list[dict] = [_heading(1, f"⚠️ Self-Heal 실패 ({len(failed)}건) — 수동 검토")]
        for f in failed:
            blocks.append(_bullet(
                f"{f.get('file', '')} · {f.get('stage', '')} · {f.get('error', '')[:200]}"
            ))
        return blocks

    def _build_properties(self, date: str, meetings: list[dict], todos: list[dict], failed: list[dict]) -> dict:
        projects = sorted({_strip_commas(g.get("project", "기타")) for g in todos})
        total_todos = sum(len(g.get("items", [])) for g in todos)
        return {
            "제목": {"title": [{"type": "text", "text": {"content": f"{date} 일일 브리핑"}}]},
            "날짜": {"date": {"start": date}},
            "상태": {"select": {"name": "신규"}},
            "어제미팅수": {"number": len(meetings)},
            "오늘To-Do수": {"number": total_todos},
            "실패건수": {"number": len(failed)},
            "프로젝트": {"multi_select": [{"name": p} for p in projects if p]},
        }

    def build(self, date: str, meetings: list[dict], todos: list[dict], failed: list[dict]) -> dict[str, Any]:
        children = (
            self._build_meeting_blocks(meetings)
            + self._build_todo_blocks(todos)
            + self._build_failed_blocks(failed)
        )
        # Notion API allows max 100 children per request; send first 100 on create, rest via append
        page = self.client.pages.create(
            parent={"database_id": self.database_id},
            properties=self._build_properties(date, meetings, todos, failed),
            children=children[:100],
        )
        page_id = page["id"]
        for i in range(100, len(children), 100):
            self.client.blocks.children.append(
                block_id=page_id,
                children=children[i:i + 100],
            )
        return {"page_id": page_id, "url": page["url"]}
