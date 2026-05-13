"""Notion 2026 기업AI고객담당_DB page creator with rich block structure."""
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
    """Build Notion database properties for 2026 기업AI고객담당_DB."""
    props = analysis.properties
    db_props: dict = {
        "Name": {"title": [{"text": {"content": props.title}}]},
        "입력방식": {"select": {"name": "자동전사"}},
        "Tags": {"multi_select": [{"name": "Meeting"}]},
    }

    # Select properties (only set if non-empty)
    # Notion select는 쉼표 금지 — Claude가 쉼표로 여러 값을 보내면 첫 번째만 사용
    def _safe_select(value: str) -> str:
        return value.split(",")[0].strip() if "," in value else value

    if props.meeting_type:
        db_props["유형"] = {"select": {"name": _safe_select(props.meeting_type)}}
    if props.priority:
        # 우선순위 → 중요도 매핑
        _priority_map = {"높음": "🔴긴급", "중간": "🟡중요", "낮음": "⚪일반",
                         "high": "🔴긴급", "medium": "🟡중요", "low": "⚪일반"}
        mapped = _priority_map.get(props.priority.strip(), _safe_select(props.priority))
        db_props["중요도"] = {"select": {"name": mapped}}
    if props.status:
        # 상태는 STATUS 타입 — select가 아닌 status 키 사용
        db_props["상태"] = {"status": {"name": _safe_select(props.status)}}
    if props.project:
        db_props["프로젝트"] = {"select": {"name": _safe_select(props.project)}}
    if props.client:
        # 고객명은 RICH_TEXT (RELATION 매핑 불가)
        db_props["고객명"] = {"rich_text": [{"text": {"content": _safe_select(props.client)[:200]}}]}

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
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tag_list = [{"name": str(t).replace(",", "·")[:100]} for t in tags if t]
        if tag_list:
            db_props["태그"] = {"multi_select": tag_list}

    # Rich text properties — LLM이 list를 반환할 수 있으므로 str 강제 변환
    def _to_str(v: str | list) -> str:
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v)

    if props.summary:
        db_props["요약"] = {"rich_text": [{"text": {"content": _to_str(props.summary)[:2000]}}]}
    if props.next_actions:
        db_props["다음액션"] = {"rich_text": [{"text": {"content": _to_str(props.next_actions)[:2000]}}]}
    if props.participants:
        p = props.participants
        if isinstance(p, list):
            p = ", ".join(str(x) for x in p)
        db_props["참석자1"] = {"rich_text": [{"text": {"content": str(p)[:2000]}}]}

    # Date property
    if props.date:
        db_props["날짜"] = {"date": {"start": props.date}}

    return db_props


def create_meeting_note(
    analysis: MeetingAnalysis | None = None,
    transcript: TranscriptResult | None = None,
    audio_filename: str = "",
    *,
    # v3 multi-agent interface (keyword-only overload)
    database_id: str | None = None,
    api_key: str | None = None,
    title: str | None = None,
    date: str | None = None,
    analyses: list[dict] | None = None,
    properties: dict | None = None,
) -> str | dict:
    """Create a Notion page with a meeting note.

    Supports two calling conventions:

    **Legacy (pipeline)**::

        create_meeting_note(analysis, transcript, audio_filename) -> page_id (str)

    **v3 multi-agent**::

        create_meeting_note(
            database_id=..., api_key=..., title=..., date=...,
            transcript=..., analyses=..., properties=...
        ) -> {"id": ..., "url": ...}

    Raises:
        NotionWriteError: on persistent failure (legacy path)
        RetryableError: on transient API errors (legacy path)
        NonRetryableError: on auth errors (legacy path)
        RuntimeError: on API failure (v3 path)
    """
    # v3 multi-agent path: any v3-specific kwarg present → delegate
    if database_id is not None or api_key is not None or title is not None:
        return create_meeting_note_v3(
            database_id=database_id or "",
            api_key=api_key or "",
            title=title or "",
            date=date or "",
            transcript=transcript if isinstance(transcript, str) else "",
            analyses=analyses or [],
            properties=properties,
        )

    # Legacy path — original positional args required
    if analysis is None:
        raise ValueError("analysis is required for the legacy create_meeting_note call")

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

        # Create page in 2026 기업AI고객담당_DB
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


# ---------------------------------------------------------------------------
# v3 multi-agent interface
# ---------------------------------------------------------------------------

def _build_page_children_v3(
    date: str,
    participants: str,
    meeting_type: str,
    source_filename: str,
    summary: str,
    analyses: list[dict],
    risks: list[str],
) -> list[dict]:
    """Build rich Notion block children from v3 multi-agent analysis output."""
    blocks: list[dict] = []

    # ── 📋 회의 개요 테이블 ──
    header_rows = [("날짜", date), ("참석자", participants), ("유형", meeting_type)]
    if source_filename:
        header_rows.append(("녹음", source_filename))
    header_rows = [(k, v) for k, v in header_rows if v]
    if header_rows:
        blocks.append({
            "object": "block",
            "type": "table",
            "table": {
                "table_width": 2,
                "has_column_header": False,
                "has_row_header": True,
                "children": [
                    {
                        "type": "table_row",
                        "table_row": {
                            "cells": [
                                [{"type": "text", "text": {"content": k}, "annotations": {"bold": True}}],
                                [{"type": "text", "text": {"content": str(v)}}],
                            ]
                        },
                    }
                    for k, v in header_rows
                ],
            },
        })
        blocks.append(_divider())

    # ── 📌 핵심 요약 ──
    if summary:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _parse_rich_text("📌 핵심 요약"), "color": "blue_background"},
        })
        blocks.append(_callout(summary, emoji="📌", color="blue_background"))
        blocks.append(_divider())

    # ── 토픽별 섹션 ──
    all_actions: list[str] = []
    all_decisions: list[str] = []

    for i, analysis in enumerate(analyses, 1):
        topic = analysis.get("topic", f"토픽 {i}")
        five_w = analysis.get("five_w_one_h", {})
        key_facts = analysis.get("key_facts", [])
        decisions = analysis.get("decisions", [])
        actions = analysis.get("actions", [])

        all_decisions.extend(decisions)
        all_actions.extend(actions)

        blocks.append(_heading2(f"📊 토픽 {i}: {topic}"))

        # 5W1H 테이블
        w_rows = [
            ("When", five_w.get("when", "")), ("Where", five_w.get("where", "")),
            ("Who", five_w.get("who", "")), ("What", five_w.get("what", "")),
            ("Why", five_w.get("why", "")), ("How", five_w.get("how", "")),
        ]
        w_rows = [(k, v) for k, v in w_rows if v]
        if w_rows:
            blocks.append({
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": 2,
                    "has_column_header": False,
                    "has_row_header": True,
                    "children": [
                        {
                            "type": "table_row",
                            "table_row": {
                                "cells": [
                                    [{"type": "text", "text": {"content": k}, "annotations": {"bold": True}}],
                                    [{"type": "text", "text": {"content": v[:2000]}}],
                                ]
                            },
                        }
                        for k, v in w_rows
                    ],
                },
            })

        # 핵심 팩트
        if key_facts:
            blocks.append(_heading3("핵심 팩트"))
            for fact in key_facts:
                blocks.append(_quote(f"💬 {str(fact)[:500]}"))

        # 액션 아이템
        if actions:
            blocks.append(_heading3("액션 아이템"))
            for action in actions:
                blocks.append({
                    "object": "block", "type": "to_do",
                    "to_do": {"rich_text": _parse_rich_text(str(action)[:200]), "checked": False},
                })

        # 인사이트 (decisions → 인사이트로 표시)
        if decisions:
            blocks.append(_heading3("인사이트"))
            for d in decisions:
                blocks.append(_bulleted_list(str(d)[:300]))

        # So What
        so_what = actions[0] if actions else (decisions[0] if decisions else "")
        if so_what:
            blocks.append(_callout(f"So What: {so_what[:300]}", emoji="💡", color="yellow_background"))

        blocks.append(_divider())

    # ── ✅ 결정사항 ──
    if all_decisions:
        blocks.append(_heading2("✅ 결정사항"))
        for d in all_decisions:
            for chunk in _chunk_text(d):
                blocks.append(_bulleted_list(chunk))
        blocks.append(_divider())

    # ── ⚠️ 리스크 ──
    if risks:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _parse_rich_text("⚠️ 리스크"), "color": "yellow_background"},
        })
        for risk in risks:
            for chunk in _chunk_text(risk):
                blocks.append(_bulleted_list(chunk))
        blocks.append(_divider())

    # ── 🔴 To-Do / Action Items ──
    if all_actions:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _parse_rich_text("🔴 To-Do / Action Items"), "color": "red_background"},
        })
        for action in all_actions:
            blocks.append({
                "object": "block", "type": "to_do",
                "to_do": {"rich_text": _parse_rich_text(str(action)[:200]), "checked": False},
            })

    return blocks


def create_meeting_note_v3(
    *,
    database_id: str,
    api_key: str,
    title: str,
    date: str,
    transcript: str,
    analyses: list[dict],
    properties: dict | None = None,
) -> dict[str, str]:
    """Create a Notion page via the v3 multi-agent pipeline with rich block content."""
    client = Client(auth=api_key)
    extra = properties or {}
    risks: list[str] = extra.get("risks", [])

    # ── DB Properties (타겟: 개인기록_DB) ──
    def _safe_select(v: str) -> str:
        return v.split(",")[0].strip() if "," in v else v

    props: dict = {
        "회의 이름": {"title": [{"text": {"content": title}}]},
    }
    if date:
        props["날짜"] = {"date": {"start": date}}
    if extra.get("meeting_type"):
        props["유형"] = {"select": {"name": _safe_select(extra["meeting_type"])}}
    if extra.get("participants"):
        props["참석자 1"] = {"rich_text": [{"text": {"content": str(extra["participants"])[:2000]}}]}
    if extra.get("customer"):
        props["고객명"] = {"select": {"name": _safe_select(str(extra["customer"]))[:100]}}
    if extra.get("summary"):
        props["요약"] = {"rich_text": [{"text": {"content": str(extra["summary"])[:2000]}}]}
    if extra.get("next_actions"):
        props["다음액션"] = {"rich_text": [{"text": {"content": str(extra["next_actions"])[:2000]}}]}
    if extra.get("status"):
        props["상태"] = {"select": {"name": _safe_select(extra["status"])}}
    if extra.get("priority"):
        props["우선순위"] = {"select": {"name": _safe_select(extra["priority"])}}

    # ── Create page ──
    try:
        page = client.pages.create(
            parent={"database_id": database_id},
            properties=props,
        )
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    page_id = page["id"]

    # ── Append rich content blocks ──
    blocks = _build_page_children_v3(
        date=date,
        participants=extra.get("participants", ""),
        meeting_type=extra.get("meeting_type", ""),
        source_filename=extra.get("source_filename", ""),
        summary=extra.get("summary", ""),
        analyses=analyses,
        risks=risks,
    )

    try:
        for i in range(0, len(blocks), 100):
            client.blocks.children.append(block_id=page_id, children=blocks[i:i + 100])
    except Exception as exc:
        log.warning("v3 블록 추가 실패 (페이지는 생성됨): %s", exc)

    page_url = page.get("url", f"https://www.notion.so/{page_id.replace('-', '')}")
    log.info("v3 Notion 페이지 생성 완료: %s", page_id)
    return {"id": page_id, "url": page_url}
