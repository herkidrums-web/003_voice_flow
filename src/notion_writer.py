"""Notion 개인기록_DB page creator with rich block structure."""
from __future__ import annotations

import logging
from datetime import datetime

from notion_client import Client
from notion_client.errors import APIResponseError

from config import NonRetryableError, NotionWriteError, RetryableError, get_settings
from src.stt import TranscriptResult, format_timestamp
from src.summarizer import MeetingAnalysis

log = logging.getLogger(__name__)

# Module-level client cache
_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        settings = get_settings()
        _client = Client(auth=settings.notion_api_key)
    return _client


# --- Block helpers ---

def _rich_text(content: str) -> dict:
    return {"type": "text", "text": {"content": content}}


def _heading2(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_rich_text(text)]},
    }


def _heading3(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [_rich_text(text)]},
    }


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_rich_text(text)]},
    }


def _bulleted_list(text: str, children: list[dict] | None = None) -> dict:
    block = {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_rich_text(text)]},
    }
    if children:
        block["bulleted_list_item"]["children"] = children
    return block


def _numbered_list(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [_rich_text(text)]},
    }


def _quote(text: str) -> dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": [_rich_text(text)]},
    }


def _callout(text: str, emoji: str = "📋") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [_rich_text(text)],
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _chunk_text(text: str, limit: int = 1900) -> list[str]:
    """Split text into chunks under Notion's 2000 char limit."""
    chunks = []
    while len(text) > limit:
        cut = text[:limit].rfind(".")
        if cut < 100:
            cut = text[:limit].rfind(" ")
        if cut < 100:
            cut = limit
        else:
            cut += 1
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


# --- Page content builder ---

def _build_page_children(
    analysis: MeetingAnalysis,
    transcript: TranscriptResult,
    audio_filename: str,
) -> list[dict]:
    """Build Notion block children matching 개인기록_DB page structure."""
    blocks: list[dict] = []
    content = analysis.content
    props = analysis.properties

    # ── 핵심 요약 ──
    blocks.append(_heading2("핵심 요약"))
    if content.core_summary:
        for chunk in _chunk_text(content.core_summary):
            blocks.append(_paragraph(chunk))

    blocks.append(_divider())

    # ── 미팅 개요 ──
    blocks.append(_heading2("미팅 개요"))
    overview = content.meeting_overview
    if overview.get("date"):
        blocks.append(_bulleted_list(f"일시: {overview['date']}"))
    if overview.get("participants"):
        blocks.append(_bulleted_list(f"참석자: {', '.join(overview['participants'])}"))
    if overview.get("type"):
        blocks.append(_bulleted_list(f"성격: {overview['type']}"))
    if overview.get("location"):
        blocks.append(_bulleted_list(f"장소: {overview['location']}"))
    meta = (
        f"녹음 파일: {audio_filename} | "
        f"녹음 길이: {format_timestamp(transcript.duration)}"
    )
    blocks.append(_bulleted_list(meta))

    blocks.append(_divider())

    # ── 주요 논의 내용 ──
    for disc in content.discussions:
        blocks.append(_heading2(disc.get("topic", "논의사항")))

        for sub in disc.get("sub_topics", []):
            blocks.append(_heading3(sub.get("title", "")))

            # Detail points
            for point in sub.get("points", []):
                for chunk in _chunk_text(point):
                    blocks.append(_bulleted_list(chunk))

            # Direct quotes
            for q in sub.get("quotes", []):
                blocks.append(_quote(q))

        blocks.append(_divider())

    # ── 키맨 프로파일 ──
    if content.key_persons:
        blocks.append(_heading2("키맨 프로파일"))
        for person in content.key_persons:
            blocks.append(_heading3(person.get("name", "")))
            for obs in person.get("observations", []):
                blocks.append(_bulleted_list(obs))
        blocks.append(_divider())

    # ── 리스크 & 불확실성 ──
    if content.risks:
        blocks.append(_heading2("리스크 & 불확실성"))
        for risk in content.risks:
            blocks.append(_bulleted_list(risk))
        blocks.append(_divider())

    # ── 전략적 인사이트 ──
    if content.strategic_insights:
        blocks.append(_heading2("전략적 인사이트"))
        for i, insight in enumerate(content.strategic_insights, 1):
            blocks.append(_numbered_list(insight))
        blocks.append(_divider())

    # ── 다음 액션 ──
    blocks.append(_heading2("다음 액션"))
    if content.action_items:
        for ai in content.action_items:
            deadline = f" (기한: {ai['deadline']})" if ai.get("deadline") else ""
            blocks.append(_bulleted_list(f"[{ai['assignee']}] {ai['task']}{deadline}"))
    else:
        blocks.append(_paragraph("(액션아이템 없음)"))

    return blocks


# --- DB properties builder ---

def _build_db_properties(analysis: MeetingAnalysis) -> dict:
    """Build Notion database properties for 개인기록_DB."""
    props = analysis.properties
    db_props: dict = {
        "회의 이름": {"title": [{"text": {"content": props.title}}]},
    }

    # Select properties (only set if non-empty)
    if props.meeting_type:
        db_props["유형"] = {"select": {"name": props.meeting_type}}
    if props.priority:
        db_props["우선순위"] = {"select": {"name": props.priority}}
    if props.status:
        db_props["상태"] = {"select": {"name": props.status}}
    if props.project:
        db_props["프로젝트"] = {"select": {"name": props.project}}
    if props.client:
        db_props["고객명"] = {"select": {"name": props.client}}

    # Multi-select properties
    if props.related_people:
        db_props["관련인물"] = {
            "multi_select": [{"name": p} for p in props.related_people]
        }
    if props.tags:
        db_props["태그"] = {
            "multi_select": [{"name": t} for t in props.tags]
        }

    # Rich text properties
    if props.summary:
        db_props["요약"] = {"rich_text": [{"text": {"content": props.summary[:2000]}}]}
    if props.next_actions:
        db_props["다음액션"] = {"rich_text": [{"text": {"content": props.next_actions[:2000]}}]}
    if props.participants:
        db_props["참석자 1"] = {"rich_text": [{"text": {"content": props.participants[:2000]}}]}

    # Date property
    if props.date:
        db_props["날짜"] = {"date": {"start": props.date}}

    return db_props


def create_meeting_note(
    analysis: MeetingAnalysis,
    transcript: TranscriptResult,
    audio_filename: str,
) -> str:
    """Create a Notion page in 개인기록_DB with the meeting note.

    Returns:
        page_id of the created page

    Raises:
        NotionWriteError: on persistent failure
        RetryableError: on transient API errors
        NonRetryableError: on auth errors
    """
    settings = get_settings()
    client = _get_client()

    children = _build_page_children(analysis, transcript, audio_filename)
    db_properties = _build_db_properties(analysis)

    log.info(f"Notion 페이지 생성: {analysis.properties.title} ({len(children)}블록)")

    try:
        # Notion API allows max 100 children per request
        first_batch = children[:100]
        remaining = children[100:]

        # Create page in 개인기록_DB
        page = client.pages.create(
            parent={"database_id": settings.notion_database_id},
            properties=db_properties,
            children=first_batch,
        )

        # Append remaining blocks in batches of 100
        page_id = page["id"]
        while remaining:
            batch = remaining[:100]
            remaining = remaining[100:]
            client.blocks.children.append(block_id=page_id, children=batch)
            log.info(f"추가 블록 전송: {len(batch)}개")

    except APIResponseError as e:
        if e.status == 401:
            raise NonRetryableError(f"Notion auth failed: {e}", status_code=401) from e
        if e.status in (429, 500, 502, 503):
            raise RetryableError(f"Notion API error: {e}", status_code=e.status) from e
        raise NotionWriteError(f"Notion page creation failed: {e}") from e

    page_id = page["id"]
    log.info(f"Notion 페이지 생성 완료: {page_id}")
    return page_id
