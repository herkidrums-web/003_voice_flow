"""Weekly briefing — 지난 주(월~일) 미팅 합성. 매주 월요일 07:05 launchd 자동 실행.

morning_briefing.py 패턴을 따라, BriefingAgent 대신 WeeklyBriefingAgent 사용.
Notion briefing DB에 별도 페이지 생성. 제목 '{년-W주차} 주간 브리핑 ({시작} ~ {끝})'.

사용법:
    poc/.venv/bin/python scripts/weekly_briefing.py                        # 지난 주 (default)
    poc/.venv/bin/python scripts/weekly_briefing.py --week-end 2026-05-18   # 명시
    poc/.venv/bin/python scripts/weekly_briefing.py --exclude 정선형         # 인물 제외
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

import requests
from notion_client import Client as NotionClient

from config import get_settings
from src.agents.briefing_agent import parse_wiki_anchors
from src.agents.weekly_briefing_agent import WeeklyBriefingAgent
from src.agents.cli_client import ClaudeCLIClient
from scripts.daily_briefing import _fetch_meeting_details, _state_path, _notify
from scripts.morning_briefing import (
    _rt, _category_style, _page_icon_for_weekday, _COVER_URL, _post_to_discord,
)

log = logging.getLogger(__name__)
WIKI_INDEX_PATH = "/Users/swlee/Documents/Coding/000_second_brain/wiki/index.md"


def _collect_files_in_range(state_path: Path, start_date: str, end_date: str) -> list[dict]:
    """state에서 start_date <= ts <= end_date 범위의 wiki:done 파일 수집.
    notion_url 보존."""
    if not state_path.exists():
        return []
    notion_urls: dict[str, str] = {}
    seen: dict[str, dict] = {}
    for line in state_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        f = rec.get("file")
        if not f:
            continue
        meta = rec.get("meta", {}) or {}
        if rec.get("status") == "done" and rec.get("stage") == "notion":
            if meta.get("notion_url"):
                notion_urls[f] = meta["notion_url"]
        ts = rec.get("ts", "")
        # 파일명에서 날짜 (YYYYMMDD) 추출 — ts는 처리 시각이고 실제 미팅 날짜는 파일명
        try:
            file_date_str = f[:8]  # "20260512" 형식
            file_date = date(int(file_date_str[:4]), int(file_date_str[4:6]), int(file_date_str[6:8]))
            sd = date.fromisoformat(start_date)
            ed = date.fromisoformat(end_date)
            if not (sd <= file_date <= ed):
                continue
        except Exception:
            continue
        if rec.get("status") == "done" and rec.get("stage") == "wiki":
            seen[f] = {
                "file": f, "notion_url": notion_urls.get(f, ""),
                "title": f.rsplit(".", 1)[0],
                "date": file_date.isoformat(),
            }
    for v in seen.values():
        if not v["notion_url"]:
            v["notion_url"] = notion_urls.get(v["file"], "")
    return list(seen.values())


def _build_blocks(weekly: dict, week_start: str, week_end: str, meetings_count: int) -> list[dict]:
    blocks: list[dict] = []
    topic_blocks = weekly.get("blocks", [])
    themes = weekly.get("top_themes", [])
    new_anchors = weekly.get("new_anchors_suggested", [])
    overview = weekly.get("week_overview", "")
    total_todos = sum(len(b.get("todos", [])) for b in topic_blocks)

    # Stats
    blocks.append({
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": _rt(
                f"📅 {week_start} ~ {week_end}    |    "
                f"🎙 미팅 {meetings_count}건    |    "
                f"📌 토픽 {len(topic_blocks)}개    |    "
                f"✅ 다음 주 To-Do {total_todos}건",
                bold=True,
            ),
            "icon": {"type": "emoji", "emoji": "📊"},
            "color": "default",
        },
    })

    # Week overview
    if overview:
        blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": _rt(overview, bold=True),
                "icon": {"type": "emoji", "emoji": "🌐"},
                "color": "blue_background",
            },
        })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # Top themes
    blocks.append({
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": _rt(f"📋 주간 핵심 테마")},
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

    # 토픽별 (Column 2열: facts ↔ todos)
    blocks.append({
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": _rt("📌 토픽별 진행 흐름")},
    })

    for blk in topic_blocks:
        topic = blk.get("topic", "")
        anchor_path = blk.get("anchor_path", "")
        is_new = blk.get("is_new_anchor", False)
        color, emoji = _category_style(anchor_path, is_new, topic=topic)

        header_rt = [{"type": "text", "text": {"content": topic},
                      "annotations": {"bold": True, "color": "default"}}]
        if anchor_path and not is_new:
            header_rt.append({"type": "text",
                              "text": {"content": f"  →  {anchor_path}"},
                              "annotations": {"italic": True, "color": "gray"}})
        elif is_new:
            header_rt.append({"type": "text",
                              "text": {"content": "  (Wiki anchor 없음 — 신규)"},
                              "annotations": {"italic": True, "color": "red"}})
        blocks.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": header_rt,
                "icon": {"type": "emoji", "emoji": emoji},
                "color": color,
            },
        })

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

        facts = blk.get("facts", [])
        todos = blk.get("todos", [])
        if facts or todos:
            left_children = [{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": _rt(f"📝 시간순 사실 ({len(facts)})", bold=True)},
            }] if facts else [{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": _rt("📝 사실 없음", color="gray")},
            }]
            for f in facts:
                left_children.append({
                    "object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _rt(f)},
                })

            right_children = [{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": _rt(f"✅ 이성우 다음 주 To-Do ({len(todos)})", bold=True)},
            }] if todos else [{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": _rt("✅ 다음 주 액션 없음 (참고만)", color="gray")},
            }]
            for t in todos:
                right_children.append({
                    "object": "block", "type": "to_do",
                    "to_do": {"rich_text": _rt(t), "checked": False},
                })

            blocks.append({
                "object": "block", "type": "column_list",
                "column_list": {"children": [
                    {"object": "block", "type": "column", "column": {"children": left_children}},
                    {"object": "block", "type": "column", "column": {"children": right_children}},
                ]},
            })

        blocks.append({"object": "block", "type": "divider", "divider": {}})

    # 신규 anchor 제안
    if new_anchors:
        blocks.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt("🆕 Wiki 신규 anchor 제안")},
        })
        for a in new_anchors:
            rt = [
                {"type": "text", "text": {"content": a.get("title", "")},
                 "annotations": {"bold": True}},
                {"type": "text", "text": {"content": f"  ({a.get('category', '')})"},
                 "annotations": {"italic": True, "color": "gray"}},
                {"type": "text", "text": {"content": f"\n→ {a.get('reason', '')}"}},
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


def _create_notion_page(settings, week_start: str, week_end: str, year: int, week_num: int, blocks: list[dict]) -> str:
    notion = NotionClient(auth=settings.notion_api_key)
    db_id = settings.notion_briefing_database_id
    if not db_id:
        raise RuntimeError("notion_briefing_database_id 미설정")

    title = f"{year}-W{week_num:02d} 주간 브리핑 ({week_start} ~ {week_end})"
    page = notion.pages.create(
        parent={"database_id": db_id},
        cover={"type": "external", "external": {"url": _COVER_URL}},
        icon={"type": "emoji", "emoji": "📅"},
        properties={
            "제목": {"title": [{"type": "text", "text": {"content": title}}]},
            "날짜": {"date": {"start": week_start, "end": week_end}},
        },
        children=blocks[:100],
    )
    page_id = page["id"]
    rest = blocks[100:]
    while rest:
        notion.blocks.children.append(block_id=page_id, children=rest[:100])
        rest = rest[100:]
    return page.get("url", "")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--week-end", metavar="YYYY-MM-DD", default=None,
                        help="주간 종료일(일요일). 기본: 어제(가장 최근 일요일).")
    parser.add_argument("--notify-discord", action="store_true")
    parser.add_argument("--exclude", action="append", default=[], metavar="이름")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if not settings.notion_briefing_database_id:
        log.error("notion_briefing_database_id 미설정")
        return 2

    # 주간 범위 계산 — 월요일 시작, 일요일 종료 (지난 주)
    today = date.today()
    if args.week_end:
        week_end = date.fromisoformat(args.week_end)
    else:
        # 이번 주 월요일에서 1일 전 = 지난 일요일
        # today.weekday(): 월=0, 일=6
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        week_end = this_monday - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    year, week_num, _ = week_end.isocalendar()

    log.info("week range: %s ~ %s (W%02d)", week_start, week_end, week_num)

    # 1. 미팅 수집
    files = _collect_files_in_range(_state_path(), week_start.isoformat(), week_end.isoformat())
    log.info("collected %d files in range", len(files))

    if not files:
        log.info("지난 주 처리된 미팅 없음 — 빈 페이지 skip")
        _notify("Weekly Briefing", f"{week_start}~{week_end}: 처리된 미팅 없음")
        if args.notify_discord:
            _post_to_discord(f"📅 주간 브리핑 ({week_start} ~ {week_end}): 미팅 없음. 건너뜁니다.")
        return 0

    # 2. Notion 상세
    meetings = _fetch_meeting_details(files)
    # date 필드 보강
    for m in meetings:
        for f in files:
            if f["title"] == m.get("title"):
                m["date"] = f["date"]
                break

    # 3. Wiki anchor
    anchors = parse_wiki_anchors(WIKI_INDEX_PATH)

    # 4. WeeklyBriefingAgent
    client = ClaudeCLIClient(cli_path=settings.claude_cli_path, timeout=settings.claude_api_timeout)
    agent = WeeklyBriefingAgent(client=client, model=settings.claude_model_opus)
    result = agent.execute({
        "meetings": meetings,
        "anchors": anchors,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "exclude_persons": args.exclude,
    })
    if not result.ok:
        log.error("WeeklyBriefingAgent failed: %s", result.error)
        _notify("Weekly Briefing", f"실패: {result.error[:100]}")
        if args.notify_discord:
            _post_to_discord(f"❌ 주간 브리핑 실패: {result.error[:200]}")
        return 1

    weekly = result.data
    log.info("blocks=%d themes=%d new_anchors=%d",
             len(weekly.get("blocks", [])),
             len(weekly.get("top_themes", [])),
             len(weekly.get("new_anchors_suggested", [])))

    # 5. Notion 페이지
    blocks = _build_blocks(weekly, week_start.isoformat(), week_end.isoformat(), len(meetings))
    try:
        url = _create_notion_page(settings, week_start.isoformat(), week_end.isoformat(),
                                   year, week_num, blocks)
    except Exception as e:
        log.exception("notion page create failed")
        _notify("Weekly Briefing", f"Notion 페이지 생성 실패: {e}")
        return 1

    log.info("weekly briefing created: %s", url)
    total_todos = sum(len(b.get("todos", [])) for b in weekly.get("blocks", []))
    _notify("Weekly Briefing",
            f"W{week_num}: {len(meetings)}건 → 토픽 {len(weekly.get('blocks', []))}개, 다음 주 To-Do {total_todos}건")
    if args.notify_discord:
        _post_to_discord(
            f"📅 **주간 브리핑 — {year}-W{week_num:02d}** ({week_start} ~ {week_end})\n"
            f"📊 미팅 {len(meetings)}건 · 토픽 {len(weekly.get('blocks', []))}개 "
            f"· 다음 주 To-Do {total_todos}건\n"
            f"🔗 {url}"
        )
    print(f"weekly briefing created: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
