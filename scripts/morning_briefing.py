"""Morning briefing v2 — Wiki anchor 기반 토픽 그룹핑 + 이성우 담당 To-Do 분리.

기존 daily_briefing 대체. BriefingAgent를 사용해 다음을 분리:
- 토픽별 사실(facts) + 미팅 링크 + 과거 wiki 노트 연결
- 이성우 담당이 직접 할 To-Do만 (다른 사람 액션 제외)
- 오늘의 top_themes (Executive Summary)
- 신규 anchor 제안 (Wiki 확장 신호)

사용법:
    poc/.venv/bin/python scripts/morning_briefing.py            # 어제 기준
    poc/.venv/bin/python scripts/morning_briefing.py --date 2026-05-13
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

import requests
from notion_client import Client as NotionClient

from config import get_settings
from src.agents.briefing_agent import BriefingAgent, parse_wiki_anchors
from src.agents.cli_client import ClaudeCLIClient
from src.calendar_fetcher import fetch_events, format_event_time
from scripts.auto_ingest_anchors import auto_ingest_anchors
from scripts.daily_briefing import (  # 재사용 — 이미 검증된 헬퍼
    _collect_yesterday_files,
    _fetch_meeting_details,
    _state_path,
    _notify,
)

log = logging.getLogger(__name__)

WIKI_INDEX_PATH = "/Users/swlee/Documents/Coding/000_second_brain/wiki/index.md"
DISCORD_CHANNEL_ID = "1490245243285667981"
DISCORD_ENV = Path.home() / ".claude/channels/discord/.env"


def _post_to_discord(text: str) -> None:
    """Best-effort Discord 알림 — 실패해도 브리핑 자체에 영향 없음."""
    try:
        token = next(
            l.split("=", 1)[1].strip()
            for l in DISCORD_ENV.read_text(encoding="utf-8").splitlines()
            if l.startswith("DISCORD_BOT_TOKEN=")
        )
        r = requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": text[:2000]},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("discord notify failed: %s", e)


def _rt(text: str, bold: bool = False, color: str = "default") -> list[dict]:
    """Notion rich_text helper."""
    ann = {}
    if bold:
        ann["bold"] = True
    if color != "default":
        ann["color"] = color
    seg = {"type": "text", "text": {"content": text[:2000]}}
    if ann:
        seg["annotations"] = ann
    return [seg]


def _rt_link(text: str, url: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:2000], "link": {"url": url}}}]


# 1) 고객사명 직접 매핑 (anchor 파일명 키워드 기반) — 카테고리 매핑보다 우선
_CUSTOMER_COLORS = {
    "naver": ("green_background", "🟢"),     # 네이버클라우드
    "toss": ("blue_background", "💙"),       # 토스
    "kakao": ("yellow_background", "🟡"),    # 카카오
    "koramko": ("orange_background", "🟠"),  # 코람코
    "keppel": ("red_background", "🔴"),      # 케펠
    "brookfield": ("gray_background", "🌐"),  # 브룩필드
    "netmarble": ("purple_background", "🎮"),  # 넷마블
}

# 2) 카테고리 fallback 매핑
_CATEGORY_COLORS = {
    "Customers": ("blue_background", "🏢"),
    "Internal": ("purple_background", "🏛"),
    "Deals": ("orange_background", "💼"),
    "Industry": ("brown_background", "📈"),
    "Competitors": ("red_background", "⚔️"),
    "Dashboard": ("gray_background", "📊"),
    "Leadership": ("pink_background", "👤"),
    "Creator": ("yellow_background", "🎨"),
}


def _category_style(anchor_path: str, is_new: bool, topic: str = "") -> tuple[str, str]:
    """anchor_path 또는 topic에서 (color, emoji) 매핑.

    우선순위: 고객사명 직접 매핑 → 카테고리 매핑 → 신규/기본.
    """
    if is_new or not anchor_path:
        return ("gray_background", "🆕")
    lower = (anchor_path + " " + topic).lower()
    # 1) 고객사명 우선
    for key, (color, emoji) in _CUSTOMER_COLORS.items():
        if key in lower:
            return (color, emoji)
    # 2) 카테고리 fallback
    for key, (color, emoji) in _CATEGORY_COLORS.items():
        if f"/{key.lower()}/" in lower or lower.startswith(f"{key.lower()}/"):
            return (color, emoji)
    return ("default", "📌")


# 순수 대인관계 정치 테마만 비공개 분리 (키맨/업무요청은 공개 — 키맨 관리 = B2B 본질)
_INTERNAL_THEME_KEYWORDS = (
    "조직정치", "조직 정치", "줄서기", "사내 정치", "라인업",
    "인사 이동", "인사이동", "관계 회복", "관계개선", "관계 개선",
)


def _is_internal_block(blk: dict) -> bool:
    """내부 정치/키맨/내부 보고 전술 토픽인지.

    판정: agent가 is_internal을 명시했으면 그 값을 신뢰(업무요청은 false로 살림).
    agent가 판단을 누락(None/키 없음)한 경우에만 anchor_path business/internal/로 backstop.
    """
    decided = blk.get("is_internal")
    if decided is not None:
        return decided is True
    return "business/internal/" in (blk.get("anchor_path", "") or "").lower()


def _filter_public_themes(themes: list[str], internal_blocks: list[dict]) -> list[str]:
    """top_themes에서 내부 정치 관련 테마 제거 (본문엔 공개 테마만)."""
    internal_topics = [b.get("topic", "").strip() for b in internal_blocks if b.get("topic")]
    out: list[str] = []
    for t in themes:
        tl = t.strip()
        if any(k in tl for k in _INTERNAL_THEME_KEYWORDS):
            continue
        if any(it and (it in tl or tl in it) for it in internal_topics):
            continue
        out.append(t)
    return out


def _build_private_blocks(internal_blocks: list[dict], target_date: str) -> list[dict]:
    """비공개 페이지용 blocks — 내부 토픽의 사실 + 본인 To-Do만 간결하게."""
    blocks: list[dict] = [{
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": _rt(
                f"🔒 {target_date} 비공개 메모 — 조직/키맨/내부 보고. "
                f"본문 브리핑에서 분리 보관됨.", bold=True),
            "icon": {"type": "emoji", "emoji": "🔒"},
            "color": "gray_background",
        },
    }, {"object": "block", "type": "divider", "divider": {}}]

    for blk in internal_blocks:
        topic = blk.get("topic", "")
        anchor_path = blk.get("anchor_path", "")
        header_rt = [{"type": "text", "text": {"content": topic},
                      "annotations": {"bold": True}}]
        if anchor_path:
            header_rt.append({"type": "text",
                              "text": {"content": f"  →  {anchor_path}"},
                              "annotations": {"italic": True, "color": "gray"}})
        blocks.append({
            "object": "block", "type": "callout",
            "callout": {"rich_text": header_rt,
                        "icon": {"type": "emoji", "emoji": "🏛"},
                        "color": "purple_background"},
        })
        for f in blk.get("facts", []):
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rt(f)},
            })
        todos = blk.get("todos", [])
        if todos:
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": _rt(f"✅ 이성우 담당 To-Do ({len(todos)})", bold=True)},
            })
            for t in todos:
                blocks.append({
                    "object": "block", "type": "to_do",
                    "to_do": {"rich_text": _rt(t), "checked": False},
                })
        blocks.append({"object": "block", "type": "divider", "divider": {}})
    return blocks


def _build_blocks(briefing: dict, target_date: str, meetings_count: int,
                  calendar_events: list[dict] | None = None,
                  calendar_date: str = "", sync_stale_msg: str = "") -> list[dict]:
    """BriefingAgent 결과 → Notion blocks (시각화 강화 + 캘린더 일정)."""
    blocks: list[dict] = []
    calendar_events = calendar_events or []

    # ── sync stale 경보 (전날 녹음이 안 들어왔을 수 있음 → 수동 실행 안내)
    if sync_stale_msg:
        blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": _rt(
                    f"⚠️ 동기화 지연 감지: {sync_stale_msg} "
                    f"— 전날 일부 녹음이 누락됐을 수 있습니다. "
                    f"Voice Memos 앱을 띄운 뒤 `./scripts/manual_run.sh` 실행 후 "
                    f"이 브리핑은 자동 갱신됩니다.", bold=True),
                "icon": {"type": "emoji", "emoji": "⚠️"},
                "color": "red_background",
            },
        })

    topic_blocks = briefing.get("blocks", [])
    themes = briefing.get("top_themes", [])
    new_anchors = briefing.get("new_anchors_suggested", [])
    total_todos = sum(len(b.get("todos", [])) for b in topic_blocks)

    # ── 상단 Stats 카드 (한눈에)
    stats_text = (
        f"📅 {target_date}    |    "
        f"🎙 미팅 {meetings_count}건    |    "
        f"📌 토픽 {len(topic_blocks)}개    |    "
        f"✅ 내 To-Do {total_todos}건    |    "
        f"📆 오늘 일정 {len(calendar_events)}건"
    )
    blocks.append({
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": _rt(stats_text, bold=True),
            "icon": {"type": "emoji", "emoji": "📊"},
            "color": "default",
        },
    })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # ── 오늘의 캘린더 일정 (Table block, 시간/제목/장소 3열)
    if calendar_events:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt(f"🌅 오늘의 일정 — {calendar_date}")},
        })
        table_rows = [
            {"type": "table_row", "table_row": {"cells": [
                _rt("시간", bold=True),
                _rt("일정", bold=True),
                _rt("계정 / 장소", bold=True),
            ]}}
        ]
        for ev in calendar_events:
            t = format_event_time(ev["start_dt"], ev.get("all_day", False))
            accounts = ev.get("accounts", [ev.get("account", "normal")])
            acc_emoji = "🏠💼" if len(accounts) > 1 else ("💼" if "work" in accounts else "🏠")
            location = ev.get("location", "")
            acc_loc = acc_emoji + (f"  @ {location[:40]}" if location else "")
            table_rows.append({
                "type": "table_row", "table_row": {"cells": [
                    _rt(t),
                    _rt(ev["summary"]),
                    _rt(acc_loc),
                ]}
            })
        blocks.append({
            "object": "block", "type": "table",
            "table": {
                "table_width": 3,
                "has_column_header": True,
                "has_row_header": False,
                "children": table_rows,
            },
        })
        blocks.append({"object": "block", "type": "divider", "divider": {}})

    # ── Executive Summary (top_themes)
    blocks.append({
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": _rt("📋 핵심 테마")},
    })
    for i, theme in enumerate(themes, 1):
        blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": _rt(f"{i}. {theme}"),
                "icon": {"type": "emoji", "emoji": "🎯"},
                "color": "yellow_background",
            },
        })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # ── 토픽별 블록
    blocks.append({
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": _rt("📌 토픽별 정리")},
    })

    for blk in topic_blocks:
        topic = blk.get("topic", "")
        anchor_path = blk.get("anchor_path", "")
        is_new = blk.get("is_new_anchor", False)
        color, emoji = _category_style(anchor_path, is_new, topic=topic)

        # 토픽 헤더 — colored callout (안에 토픽명 + anchor 링크)
        header_rt = [{"type": "text", "text": {"content": topic},
                      "annotations": {"bold": True, "color": "default"}}]
        if anchor_path and not is_new:
            header_rt.append({"type": "text",
                              "text": {"content": f"  →  {anchor_path}"},
                              "annotations": {"italic": True, "color": "gray"}})
        elif is_new:
            header_rt.append({"type": "text",
                              "text": {"content": "  (Wiki anchor 없음 — 신규 후보)"},
                              "annotations": {"italic": True, "color": "red"}})
        blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": header_rt,
                "icon": {"type": "emoji", "emoji": emoji},
                "color": color,
            },
        })

        # 미팅 링크 (📎)
        meetings = blk.get("meetings", [])
        urls = blk.get("meeting_urls", [])
        if meetings:
            for title, url in zip(meetings, urls + [""] * max(0, len(meetings) - len(urls))):
                if url:
                    rt = [{"type": "text", "text": {"content": "📎 "}},
                          {"type": "text", "text": {"content": title, "link": {"url": url}}}]
                else:
                    rt = [{"type": "text", "text": {"content": f"📎 {title}"}}]
                blocks.append({
                    "object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": rt},
                })

        # 사실(facts) ↔ To-Do — Column 2열 레이아웃
        facts = blk.get("facts", [])
        todos = blk.get("todos", [])
        if facts or todos:
            left_children = []
            right_children = []

            if facts:
                left_children.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rt(f"📝 사실 ({len(facts)})", bold=True)},
                })
                for f in facts:
                    left_children.append({
                        "object": "block", "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": _rt(f)},
                    })
            else:
                left_children.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rt("📝 사실 없음", color="gray")},
                })

            if todos:
                right_children.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rt(f"✅ 이성우 담당 To-Do ({len(todos)})", bold=True)},
                })
                for t in todos:
                    right_children.append({
                        "object": "block", "type": "to_do",
                        "to_do": {"rich_text": _rt(t), "checked": False},
                    })
            else:
                right_children.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rt("✅ 내 액션 없음 (참고만)", color="gray")},
                })

            blocks.append({
                "object": "block", "type": "column_list",
                "column_list": {
                    "children": [
                        {"object": "block", "type": "column",
                         "column": {"children": left_children}},
                        {"object": "block", "type": "column",
                         "column": {"children": right_children}},
                    ],
                },
            })

        # 토픽 간 구분선
        blocks.append({"object": "block", "type": "divider", "divider": {}})

    # ── 신규 anchor 제안
    if new_anchors:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt("🆕 Wiki 신규 anchor 제안")},
        })
        for a in new_anchors:
            title = a.get("title", "")
            category = a.get("category", "")
            reason = a.get("reason", "")
            rt = [
                {"type": "text", "text": {"content": title},
                 "annotations": {"bold": True}},
                {"type": "text", "text": {"content": f"  ({category})"},
                 "annotations": {"italic": True, "color": "gray"}},
                {"type": "text", "text": {"content": f"\n→ {reason}"}},
            ]
            blocks.append({
                "object": "block", "type": "callout",
                "callout": {
                    "rich_text": rt,
                    "icon": {"type": "emoji", "emoji": "🆕"},
                    "color": "gray_background",
                },
            })

    return blocks


def _page_icon_for_weekday(target_date: str) -> str:
    """요일별 이모지 icon."""
    try:
        from datetime import datetime as _dt
        wd = _dt.strptime(target_date, "%Y-%m-%d").weekday()
        return ["💼", "🚀", "☕", "📊", "🎉", "🌅", "📚"][wd]
    except Exception:
        return "☀️"


# Notion 기본 cover image URL (Unsplash, 안정적)
_COVER_URL = "https://images.unsplash.com/photo-1497436072909-60f360e1d4b1?w=1500&q=80"


def _page_title_text(page: dict) -> str:
    """Notion page properties에서 '제목' title 텍스트 추출 (없으면 '')."""
    try:
        rt = page.get("properties", {}).get("제목", {}).get("title", [])
        return "".join(seg.get("plain_text", "") for seg in rt)
    except Exception:
        return ""


def _archive_same_date_pages(notion, db_id: str, target_date: str, title: str) -> int:
    """같은 날짜·같은 제목의 기존 브리핑 페이지를 archive (멱등 재실행).

    매 run마다 새 페이지를 만들던 동작 → 같은 날짜는 1개로 수렴.
    늦게 처리된 녹음이 나중에 반영돼도 같은 페이지가 갱신되는 효과.
    """
    archived = 0
    try:
        # Notion API 2025-09-03: databases.query 폐기 → data_sources.query.
        # DB에서 data source id를 동적으로 해석 (하드코딩 회피).
        db = notion.databases.retrieve(database_id=db_id)
        ds_list = db.get("data_sources") or []
        if not ds_list:
            log.warning("data source 없음 — dedup skip")
            return 0
        ds_id = ds_list[0]["id"]
        res = notion.data_sources.query(
            data_source_id=ds_id,
            filter={"property": "날짜", "date": {"equals": target_date}},
        )
        for pg in res.get("results", []):
            if _page_title_text(pg).strip() == title.strip():
                notion.pages.update(page_id=pg["id"], archived=True)
                archived += 1
    except Exception as e:
        log.warning("기존 페이지 archive 실패 (계속 진행): %s", e)
    return archived


def _append_blocks(notion, page_id: str, blocks: list[dict]) -> None:
    rest = blocks[100:]
    while rest:
        notion.blocks.children.append(block_id=page_id, children=rest[:100])
        rest = rest[100:]


def _create_notion_page(settings, target_date: str, blocks: list[dict]) -> tuple[str, str]:
    """DB에 일일 브리핑 페이지 생성. 같은 날짜 기존 페이지는 먼저 archive.

    Returns: (url, page_id)
    """
    notion = NotionClient(auth=settings.notion_api_key)
    db_id = settings.notion_briefing_database_id
    if not db_id:
        raise RuntimeError("notion_briefing_database_id 미설정")

    title = f"{target_date} 일일 브리핑"
    n = _archive_same_date_pages(notion, db_id, target_date, title)
    if n:
        log.info("기존 '%s' 페이지 %d개 archive (중복 제거)", title, n)

    page = notion.pages.create(
        parent={"database_id": db_id},
        cover={"type": "external", "external": {"url": _COVER_URL}},
        icon={"type": "emoji", "emoji": _page_icon_for_weekday(target_date)},
        properties={
            "제목": {"title": [{"type": "text", "text": {"content": title}}]},
            "날짜": {"date": {"start": target_date}},
        },
        children=blocks[:100],
    )
    page_id = page["id"]
    _append_blocks(notion, page_id, blocks)
    return page.get("url", ""), page_id


def _create_private_subpage(settings, parent_page_id: str, target_date: str,
                            blocks: list[dict]) -> str:
    """내부 정치/키맨 토픽을 담는 비공개 자식 페이지 (메인 브리핑 하위).

    DB row가 아니라 메인 페이지의 child 라서 브리핑 목록엔 안 보이고,
    메인 페이지가 매 run archive→재생성되므로 옛 비공개 페이지도 함께 정리됨.
    """
    notion = NotionClient(auth=settings.notion_api_key)
    title = f"{target_date} 비공개 메모"  # 🔒는 page icon으로만 (중복 방지)
    page = notion.pages.create(
        parent={"page_id": parent_page_id},
        icon={"type": "emoji", "emoji": "🔒"},
        properties={"title": [{"type": "text", "text": {"content": title}}]},
        children=blocks[:100],
    )
    _append_blocks(notion, page["id"], blocks)
    return page.get("url", "")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD", default=None,
        help="기준일 (기본: 어제). manual_run.sh 직후 실행 시 today로.",
    )
    parser.add_argument(
        "--notify-discord", action="store_true",
        help="완료 후 Discord에 결과 알림 전송. cron 자동 실행 시 사용 (listener 명령은 별도 reply 함).",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="이름",
        help="브리핑에서 제외할 인물 이름. 반복 가능. 예: --exclude 정선형 --exclude 홍길동",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if not settings.notion_briefing_database_id:
        log.error("notion_briefing_database_id 미설정")
        _notify("Morning Briefing", "NOTION_BRIEFING_DATABASE_ID 미설정")
        return 2

    today = date.today()
    target_date = args.date if args.date else (today - timedelta(days=1)).isoformat()

    # 1. 처리된 미팅 수집
    done, _failed = _collect_yesterday_files(_state_path(), target_date)
    log.info("target=%s: %d done", target_date, len(done))

    if not done:
        # sync 상태를 먼저 확인 — stale이면 "단순 빈 브리핑"이 아니라 "동기화 사고"임을 알림
        sync_warn = ""
        try:
            from scripts.run_orchestrator import _check_sync_health
            ok, msg = _check_sync_health(Path("/tmp/voiceflow-sync.log"))
            if not ok:
                sync_warn = msg
        except Exception as e:
            log.warning("sync health check skip: %s", e)

        if sync_warn:
            log.warning("처리된 미팅 없음 + sync stale: %s", sync_warn)
            _notify(
                "Morning Briefing ⚠️",
                f"{target_date}: 미팅 0건. Voice Memos 동기화 지연 ({sync_warn}). 앱 켜고 manual_run.sh 실행 필요.",
            )
            if args.notify_discord:
                _post_to_discord(
                    f"⚠️ **{target_date} 브리핑 누락** — 처리된 미팅 0건\n"
                    f"원인 추정: Voice Memos 동기화 지연 ({sync_warn})\n"
                    f"조치: Voice Memos 앱을 띄운 뒤 `scripts/manual_run.sh` 실행 후 브리핑 재생성"
                )
        else:
            log.info("처리된 미팅 없음 (sync는 정상) — 빈 브리핑 페이지 skip")
            _notify("Morning Briefing", f"{target_date}: 처리된 미팅 없음")
            if args.notify_discord:
                _post_to_discord(f"🌅 오늘의 브리핑 ({target_date}): 처리된 미팅 없음. 건너뜁니다.")
        return 0

    # 2. Notion에서 미팅 상세 가져오기
    meetings = _fetch_meeting_details(done)

    # 3. Wiki anchor 파싱
    anchors = parse_wiki_anchors(WIKI_INDEX_PATH)
    log.info("wiki anchors: %d", len(anchors))

    # 4. BriefingAgent 호출
    client = ClaudeCLIClient(cli_path=settings.claude_cli_path, timeout=settings.claude_api_timeout)
    agent = BriefingAgent(client=client, model=settings.claude_model_opus)
    result = agent.execute({
        "meetings": meetings,
        "anchors": anchors,
        "target_date": target_date,
        "exclude_persons": args.exclude,
    })
    if not result.ok:
        log.error("BriefingAgent failed: %s", result.error)
        _notify("Morning Briefing", f"실패: {result.error[:100]}")
        return 1

    briefing = result.data
    log.info("blocks=%d themes=%d new_anchors=%d",
             len(briefing.get("blocks", [])),
             len(briefing.get("top_themes", [])),
             len(briefing.get("new_anchors_suggested", [])))

    # 5. 캘린더 일정 fetch (실패 시 silent skip — graceful)
    # target_date가 어제면 캘린더는 today, target이 today면 캘린더도 today
    calendar_date = today.isoformat() if target_date != today.isoformat() else target_date
    calendar_events: list[dict] = []
    try:
        calendar_events = fetch_events(calendar_date)
        log.info("calendar events for %s: %d", calendar_date, len(calendar_events))
    except Exception as e:
        log.warning("calendar fetch failed (skip section): %s", e)

    # 6. 내부 정치/키맨 토픽 분리 — 본문 제외, 별도 비공개 페이지로
    all_blocks = briefing.get("blocks", [])
    internal_blocks = [b for b in all_blocks if _is_internal_block(b)]
    public_blocks = [b for b in all_blocks if not _is_internal_block(b)]
    public_briefing = {
        **briefing,
        "blocks": public_blocks,
        "top_themes": _filter_public_themes(
            briefing.get("top_themes", []), internal_blocks),
        # 내부 카테고리 신규 anchor는 본문 제안에서 제외
        "new_anchors_suggested": [
            a for a in briefing.get("new_anchors_suggested", [])
            if "internal" not in str(a.get("category", "")).lower()
        ],
    }
    log.info("blocks split: public=%d internal=%d",
             len(public_blocks), len(internal_blocks))

    # sync 신선도 확인 — stale면 본문 상단에 경보 배너
    sync_stale_msg = ""
    try:
        from scripts.run_orchestrator import _check_sync_health
        ok, msg = _check_sync_health(Path("/tmp/voiceflow-sync.log"))
        if not ok:
            sync_stale_msg = msg
            log.warning("sync stale → 브리핑에 경보 배너 추가: %s", msg)
    except Exception as e:
        log.warning("sync health check skip: %s", e)

    # 7. Notion 페이지 생성 (본문 = 공개 토픽만)
    blocks = _build_blocks(
        public_briefing, target_date,
        meetings_count=len(meetings),
        calendar_events=calendar_events,
        calendar_date=calendar_date,
        sync_stale_msg=sync_stale_msg,
    )
    try:
        url, page_id = _create_notion_page(settings, target_date, blocks)
    except Exception as e:
        log.exception("notion page create failed")
        _notify("Morning Briefing", f"Notion 페이지 생성 실패: {e}")
        return 1

    # 7b. 비공개 메모 페이지 (내부 토픽 — 본인 To-Do 보존)
    private_url = ""
    if internal_blocks:
        try:
            private_url = _create_private_subpage(
                settings, page_id, target_date,
                _build_private_blocks(internal_blocks, target_date))
            NotionClient(auth=settings.notion_api_key).blocks.children.append(
                block_id=page_id,
                children=[{
                    "object": "block", "type": "callout",
                    "callout": {
                        "rich_text": _rt_link(
                            f"🔒 비공개 메모 {len(internal_blocks)}건 — 별도 페이지에서 보기",
                            private_url),
                        "icon": {"type": "emoji", "emoji": "🔒"},
                        "color": "gray_background",
                    },
                }],
            )
            log.info("private subpage created: %s", private_url)
        except Exception as e:
            log.warning("private subpage 생성 실패 (본문엔 영향 없음): %s", e)

    log.info("morning briefing created: %s", url)
    total_todos = sum(len(b.get("todos", [])) for b in public_blocks)
    topic_count = len(public_blocks)

    # 7. 신규 anchor 자동 wiki 반영 — 실패 시 silent (브리핑 자체에 영향 없음)
    ingest_summary = ""
    try:
        wiki_root = Path(WIKI_INDEX_PATH).parent
        ingest_result = auto_ingest_anchors(briefing, target_date, wiki_root)
        created = ingest_result.get("created", [])
        skipped = ingest_result.get("skipped", [])
        log.info("auto_ingest: created=%d skipped=%d", len(created), len(skipped))
        if created:
            slugs = ", ".join(c["slug"] for c in created)
            ingest_summary = f"\n📚 Wiki 자동 생성 {len(created)}건: {slugs}"
    except Exception as e:
        log.warning("auto_ingest_anchors failed (silent): %s", e)

    _notify("Morning Briefing",
            f"{target_date}: {len(done)}건 → 토픽 {topic_count}개, 이성우 To-Do {total_todos}건")
    if args.notify_discord:
        _post_to_discord(
            f"☀️ **Morning Briefing — {target_date}**\n"
            f"📊 미팅 {len(done)}건 · 토픽 {topic_count}개 · 내 To-Do {total_todos}건 "
            f"· 📆 오늘 일정 {len(calendar_events)}건"
            f"{ingest_summary}\n"
            f"🔗 {url}"
        )
    print(f"briefing created: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
