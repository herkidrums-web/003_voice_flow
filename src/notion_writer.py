"""Notion 개인기록_DB page creator with rich block structure."""
from __future__ import annotations

import logging
from datetime import datetime

from notion_client import Client
from notion_client.errors import APIResponseError

from config import NonRetryableError, NotionWriteError, RetryableError, get_settings
from src.stt import TranscriptResult
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

import re

def _parse_rich_text(content: str) -> list[dict]:
    """Parse markdown-style formatting into Notion rich_text array.

    Supports: **bold**, *italic*, ~~strikethrough~~, `code`
    """
    if not content:
        return [{"type": "text", "text": {"content": ""}}]

    segments = []
    # Pattern: **bold**, *italic*, ~~strike~~, `code`
    pattern = re.compile(
        r'(\*\*(.+?)\*\*)'    # **bold**
        r'|(\*(.+?)\*)'        # *italic*
        r'|(~~(.+?)~~)'        # ~~strikethrough~~
        r'|(`(.+?)`)'          # `code`
    )

    last_end = 0
    for m in pattern.finditer(content):
        # Plain text before this match
        if m.start() > last_end:
            plain = content[last_end:m.start()]
            if plain:
                segments.append({"type": "text", "text": {"content": plain}})

        if m.group(2):  # **bold**
            segments.append({
                "type": "text",
                "text": {"content": m.group(2)},
                "annotations": {"bold": True},
            })
        elif m.group(4):  # *italic*
            segments.append({
                "type": "text",
                "text": {"content": m.group(4)},
                "annotations": {"italic": True},
            })
        elif m.group(6):  # ~~strikethrough~~
            segments.append({
                "type": "text",
                "text": {"content": m.group(6)},
                "annotations": {"strikethrough": True},
            })
        elif m.group(8):  # `code`
            segments.append({
                "type": "text",
                "text": {"content": m.group(8)},
                "annotations": {"code": True},
            })
        last_end = m.end()

    # Remaining plain text
    if last_end < len(content):
        remaining = content[last_end:]
        if remaining:
            segments.append({"type": "text", "text": {"content": remaining}})

    return segments if segments else [{"type": "text", "text": {"content": content}}]


def _rich_text(content: str) -> dict:
    """Single plain text segment (backward compatible)."""
    return {"type": "text", "text": {"content": content}}


def _heading2(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _parse_rich_text(text)},
    }


def _heading3(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": _parse_rich_text(text)},
    }


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _parse_rich_text(text)},
    }


def _bulleted_list(text: str, children: list[dict] | None = None) -> dict:
    block = {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _parse_rich_text(text)},
    }
    if children:
        block["bulleted_list_item"]["children"] = children
    return block


def _numbered_list(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": _parse_rich_text(text)},
    }


def _quote(text: str) -> dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": _parse_rich_text(text)},
    }


def _callout(text: str, emoji: str = "📋", color: str = "default", bold: bool = False) -> dict:
    rich = _parse_rich_text(text)
    if bold:
        # Make all segments bold
        for seg in rich:
            if "annotations" not in seg:
                seg["annotations"] = {}
            seg["annotations"]["bold"] = True
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich,
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
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
    transcript: TranscriptResult | None,
    audio_filename: str,
) -> list[dict]:
    """Build Notion block children — v4 topic-based 5W1H format."""
    blocks: list[dict] = []
    content = analysis.content
    props = analysis.properties

    # ═══ 📋 회의 개요 (테이블) ═══
    table_cells: list[tuple[str, str]] = []
    if props.date:
        table_cells.append(("날짜", props.date))
    if props.participants:
        table_cells.append(("참석자", props.participants))
    if props.meeting_type:
        table_cells.append(("유형", props.meeting_type))
    if transcript:
        from src.stt import format_timestamp
        table_cells.append(("녹음", f"{audio_filename} | {format_timestamp(transcript.duration)}"))
    elif audio_filename:
        table_cells.append(("녹음", audio_filename))

    if table_cells:
        table_children = []
        for label, value in table_cells:
            table_children.append({
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": label}, "annotations": {"bold": True}}],
                        [{"type": "text", "text": {"content": str(value)}}],
                    ]
                }
            })
        blocks.append({
            "object": "block",
            "type": "table",
            "table": {
                "table_width": 2,
                "has_column_header": False,
                "has_row_header": True,
                "children": table_children,
            }
        })
        blocks.append(_divider())

    # ═══ 📌 핵심 요약 ═══
    exec_summary = content.executive_summary
    if exec_summary:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _parse_rich_text("📌 핵심 요약"), "color": "blue_background"}
        })
        if isinstance(exec_summary, str):
            blocks.append(_callout(exec_summary, emoji="📌", color="blue_background"))
        elif isinstance(exec_summary, list):
            for section in exec_summary:
                if isinstance(section, dict):
                    topic = section.get("topic", "")
                    points = section.get("points", [])
                    if topic:
                        blocks.append(_callout(f"■ {topic}", emoji="📌", color="blue_background", bold=True))
                    for pt in points:
                        for chunk in _chunk_text(str(pt)):
                            blocks.append(_bulleted_list(chunk))
                else:
                    blocks.append(_paragraph(str(section)))
        blocks.append(_divider())

    # ═══ 토픽별 육하원칙 섹션 ═══
    topics = content.topics
    if topics:
        for i, topic in enumerate(topics, 1):
            if not isinstance(topic, dict):
                continue

            topic_name = topic.get("name", f"토픽 {i}")

            # heading_2: 토픽명
            blocks.append(_heading2(f"📊 토픽 {i}: {topic_name}"))

            # 육하원칙 테이블 (When/Where/Who/What/Why/How)
            w_cells = []
            for key, label in [("when", "When"), ("where", "Where"), ("who", "Who"),
                               ("what", "What"), ("why", "Why"), ("how", "How")]:
                val = topic.get(key, "")
                if val:
                    w_cells.append((label, str(val)))

            if w_cells:
                table_rows = []
                for label, value in w_cells:
                    table_rows.append({
                        "type": "table_row",
                        "table_row": {
                            "cells": [
                                [{"type": "text", "text": {"content": label}, "annotations": {"bold": True}}],
                                [{"type": "text", "text": {"content": value[:2000]}}],
                            ]
                        }
                    })
                blocks.append({
                    "object": "block",
                    "type": "table",
                    "table": {
                        "table_width": 2,
                        "has_column_header": False,
                        "has_row_header": True,
                        "children": table_rows,
                    }
                })

            # 핵심 팩트
            key_facts = topic.get("key_facts", [])
            if key_facts:
                blocks.append(_heading3("핵심 팩트"))
                for fact in key_facts:
                    if isinstance(fact, dict):
                        content_text = fact.get("content", "")
                        quote = fact.get("original_quote", "")
                        if content_text:
                            blocks.append(_bulleted_list(content_text))
                        if quote:
                            blocks.append(_quote(f"💬 {quote}"))
                    else:
                        blocks.append(_bulleted_list(str(fact)))

            # 관련 발언 (quotes)
            quotes = topic.get("quotes", [])
            if quotes:
                blocks.append(_heading3("관련 발언"))
                for q in quotes:
                    blocks.append(_quote(str(q)))

            # 액션 아이템
            topic_actions = topic.get("action_items", [])
            if topic_actions:
                blocks.append(_heading3("액션 아이템"))
                for ai in topic_actions:
                    if isinstance(ai, dict):
                        task = ai.get("task", "")
                        owner = ai.get("owner", "")
                        deadline = ai.get("deadline", "")
                        line = task
                        if owner:
                            line += f" (@{owner})"
                        if deadline:
                            line += f" (~{deadline})"
                        blocks.append({"object": "block", "type": "to_do", "to_do": {"rich_text": _parse_rich_text(line), "checked": False}})
                    else:
                        blocks.append(_bulleted_list(str(ai)))

            # 인사이트
            insights = topic.get("insights", [])
            if insights:
                blocks.append(_heading3("인사이트"))
                for ins in insights:
                    blocks.append(_bulleted_list(str(ins)))

            # 코칭 포인트 (enrichment data)
            coaching = topic.get("coaching_points", [])
            if coaching:
                blocks.append(_heading3("코칭 포인트"))
                for cp in coaching:
                    blocks.append(_bulleted_list(str(cp)))

            # So What (enrichment data)
            so_what = topic.get("so_what", "")
            if so_what:
                blocks.append(_callout(f"So What: {so_what}", emoji="💡", color="yellow_background"))

            blocks.append(_divider())

    # ═══ ✅ 결정사항 ═══
    if content.decisions:
        blocks.append(_heading2("✅ 결정사항"))
        for dec in content.decisions:
            for chunk in _chunk_text(str(dec)):
                blocks.append(_bulleted_list(chunk))
        blocks.append(_divider())

    # ═══ ⚠️ 리스크 ═══
    if content.risks:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _parse_rich_text("⚠️ 리스크"), "color": "yellow_background"}
        })
        for risk in content.risks:
            for chunk in _chunk_text(str(risk)):
                blocks.append(_bulleted_list(chunk))
        blocks.append(_divider())

    # ═══ 🔴 To-Do / Action Items (통합) ═══
    if content.action_items:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _parse_rich_text("🔴 To-Do / Action Items"), "color": "red_background"}
        })
        for ai in content.action_items:
            if isinstance(ai, dict):
                task = ai.get("task", "")
                owner = ai.get("owner", "")
                deadline = ai.get("deadline", "")
                line = task
                if owner:
                    line += f" (@{owner})"
                if deadline:
                    line += f" (~{deadline})"
                blocks.append({"object": "block", "type": "to_do", "to_do": {"rich_text": _parse_rich_text(line), "checked": False}})
            else:
                blocks.append(_bulleted_list(str(ai)))

    return blocks


def _append_raw_transcript(blocks: list[dict], transcript_text: str) -> None:
    """Append raw transcript as a toggleable section for cross-checking."""
    if not transcript_text or not transcript_text.strip():
        return
    blocks.append(_divider())
    # Toggle heading with raw transcript inside
    toggle_children = []
    # Split into chunks (Notion block text limit ~2000 chars)
    for i in range(0, len(transcript_text), 1800):
        chunk = transcript_text[i:i+1800]
        toggle_children.append(_paragraph(chunk))

    blocks.append({
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "📝 원문 전사본 (크로스체크용)"}}],
            "children": toggle_children[:98],  # Notion limit: 100 children per block
        },
    })


# --- DB properties builder ---

def _build_db_properties(analysis: MeetingAnalysis) -> dict:
    """Build Notion database properties for 개인기록_DB."""
    props = analysis.properties
    db_props: dict = {
        "회의 이름": {"title": [{"text": {"content": props.title}}]},
    }

    # Select properties (only set if non-empty)
    # Notion select는 쉼표 금지 — Claude가 쉼표로 여러 값을 보내면 첫 번째만 사용
    def _safe_select(value: str) -> str:
        return value.split(",")[0].strip() if "," in value else value

    if props.meeting_type:
        db_props["유형"] = {"select": {"name": _safe_select(props.meeting_type)}}
    if props.priority:
        db_props["우선순위"] = {"select": {"name": _safe_select(props.priority)}}
    if props.status:
        db_props["상태"] = {"select": {"name": _safe_select(props.status)}}
    if props.project:
        db_props["프로젝트"] = {"select": {"name": _safe_select(props.project)}}
    if props.client:
        db_props["고객명"] = {"select": {"name": _safe_select(props.client)}}

    # Multi-select properties
    if props.related_people:
        people = props.related_people
        if isinstance(people, str):
            people = [p.strip() for p in people.split(",") if p.strip()]
        people_list = [{"name": str(p).replace(",", "·")[:100]} for p in people if p]
        if people_list:
            db_props["관련인물"] = {"multi_select": people_list}
    if props.tags:
        tags = props.tags
        # 문자열로 온 경우 배열로 변환
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        # 각 태그가 문자열인지 확인
        tag_list = [{"name": str(t)[:100]} for t in tags if t]
        if tag_list:
            db_props["태그"] = {"multi_select": tag_list}

    # Second Brain metadata (Multi-select)
    if hasattr(props, 'knowledge_types') and props.knowledge_types:
        kt_list = props.knowledge_types
        if isinstance(kt_list, str):
            kt_list = [k.strip() for k in kt_list.split(",") if k.strip()]
        kt_options = [{"name": str(k).replace(",", "·")[:100]} for k in kt_list if k]
        if kt_options:
            db_props["지식유형"] = {"multi_select": kt_options}
    if hasattr(props, 'entities') and props.entities:
        ent_list = props.entities
        if isinstance(ent_list, str):
            ent_list = [e.strip() for e in ent_list.split(",") if e.strip()]
        ent_options = [{"name": str(e).replace(",", "·")[:100]} for e in ent_list if e]
        if ent_options:
            db_props["엔티티"] = {"multi_select": ent_options}

    # Rich text properties
    if props.summary:
        db_props["요약"] = {"rich_text": [{"text": {"content": props.summary[:2000]}}]}
    if props.next_actions:
        db_props["다음액션"] = {"rich_text": [{"text": {"content": props.next_actions[:2000]}}]}
    if props.participants:
        p = props.participants
        if isinstance(p, list):
            p = ", ".join(str(x) for x in p)
        db_props["참석자 1"] = {"rich_text": [{"text": {"content": str(p)[:2000]}}]}

    # Date property
    if props.date:
        db_props["날짜"] = {"date": {"start": props.date}}

    return db_props


def create_meeting_note(
    analysis: MeetingAnalysis,
    transcript: TranscriptResult | None,
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
    if transcript:
        _append_raw_transcript(children, transcript.full_text)
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
