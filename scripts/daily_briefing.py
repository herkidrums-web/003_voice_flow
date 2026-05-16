"""Daily briefing generator — runs at 08:00 weekdays via launchd.

Collects yesterday's processed meetings + invokes TodoAgent for today's
action items, creates a Notion page in the briefing DB, fires macOS notification.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is on sys.path so launchd (no PYTHONPATH) can import config/src
_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from notion_client import Client as NotionClient

from config import get_settings
from src.agents.cli_client import ClaudeCLIClient
from src.agents.notion_briefing_builder import NotionBriefingPageBuilder
from src.agents.todo_agent import TodoAgent

log = logging.getLogger(__name__)


def _state_path() -> Path:
    return Path(__file__).resolve().parents[1] / "processing_state.jsonl"


def _meeting_date_from_filename(filename: str) -> str | None:
    """녹음 파일명 앞 8자리(YYYYMMDD)를 'YYYY-MM-DD'로 변환.

    예: '20260514 160215.m4a' → '2026-05-14'. 추출 실패 시 None.
    일일 브리핑은 '처리된 날짜'가 아니라 '미팅이 있었던 날짜'로 묶어야
    하므로(백로그 혼입 방지) 이 함수가 선별 기준이 된다.
    """
    import re

    m = re.match(r"\s*(\d{4})(\d{2})(\d{2})", filename or "")
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _collect_yesterday_files(state_path: Path, target_date: str) -> tuple[list[dict], list[dict]]:
    """Return (done_files, failed_files) for meetings whose **recording date**
    (filename YYYYMMDD) == target_date — NOT the processing timestamp.

    처리 ts 기준이면 mirror에 쌓인 과거 백로그(작년 12월 등)가 처리된 날
    함께 묶여 일일 브리핑이 '주간처럼' 보이는 문제가 생긴다. 미팅 날짜
    기준으로 필터하면 늦게 처리돼도 올바른 날짜 페이지로 귀속된다.

    notion_url lives in the 'notion' stage done record, not 'wiki'.
    Files that eventually reached wiki:done (on any date) are excluded from failed.
    """
    if not state_path.exists():
        return [], []
    notion_urls: dict[str, str] = {}   # file -> notion_url (from any notion done record)
    all_wiki_done: set[str] = set()    # files that ever reached wiki:done (any date)
    failed_recs: dict[str, dict] = {}  # file -> latest failed record (any date)
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
        if rec.get("status") == "done" and rec.get("stage") == "wiki":
            all_wiki_done.add(f)
        elif rec.get("status") == "failed":
            failed_recs[f] = {"file": f, "stage": rec.get("stage", ""),
                              "error": meta.get("error", "")}
    # done = 미팅 날짜가 target_date 인 파일 중 wiki:done 도달한 것
    seen_done = {
        f: {"file": f, "notion_url": notion_urls.get(f, ""), "title": f.rsplit(".", 1)[0]}
        for f in all_wiki_done
        if _meeting_date_from_filename(f) == target_date
    }
    # failed = 미팅 날짜가 target_date 인데 끝내 wiki:done 못 한 파일
    only_failed = {
        f: v for f, v in failed_recs.items()
        if f not in all_wiki_done and _meeting_date_from_filename(f) == target_date
    }
    return list(seen_done.values()), list(only_failed.values())


def _fetch_meeting_details(items: list[dict]) -> list[dict]:
    """Fetch topics from each Notion page. Returns minimal if fetch fails."""
    if not items:
        return []
    settings = get_settings()
    out: list[dict] = []
    try:
        notion = NotionClient(auth=settings.notion_api_key)
    except Exception as e:
        log.warning("Notion client init failed: %s", e)
        return [{"title": i["title"], "notion_url": i["notion_url"],
                 "summary": "", "decisions": [], "actions": [], "implications": [], "risks": []}
                for i in items]

    for item in items:
        url = item.get("notion_url", "")
        page_id = url.rsplit("/", 1)[-1].split("?")[0].split("-")[-1] if url else ""
        topics: list[dict] = []
        meeting_data: dict = {"summary": "", "decisions": [], "actions": [], "implications": [], "risks": []}
        try:
            if page_id:
                blocks = notion.blocks.children.list(block_id=page_id, page_size=100)
                meeting_data = _extract_meeting_summary(blocks.get("results", []))
        except Exception as e:
            log.warning("Notion fetch failed for %s: %s", item["file"], e)
        out.append({"title": item["title"], "notion_url": url, **meeting_data})
    return out


def _extract_meeting_summary(blocks: list[dict]) -> dict:
    """Extract structured summary from meeting page top-level blocks."""
    summary = ""
    decisions: list[str] = []
    actions: list[str] = []
    implications: list[str] = []
    risks: list[str] = []
    current_section = ""

    for b in blocks:
        btype = b.get("type", "")
        if btype.startswith("heading_"):
            current_section = "".join(
                seg.get("plain_text", "") for seg in b.get(btype, {}).get("rich_text", [])
            ).lower()
        elif btype == "callout":
            text = "".join(
                seg.get("plain_text", "") for seg in b.get("callout", {}).get("rich_text", [])
            ).strip()
            if text and not summary and ("요약" in current_section or "summary" in current_section):
                summary = text[:500]
        elif btype in ("bulleted_list_item", "numbered_list_item"):
            text = "".join(
                seg.get("plain_text", "") for seg in b.get(btype, {}).get("rich_text", [])
            ).strip()
            if not text:
                continue
            if "결정" in current_section:
                decisions.append(text)
            elif "시사점" in current_section:
                implications.append(text)
            elif "리스크" in current_section or "risk" in current_section:
                risks.append(text)
        elif btype == "to_do":
            text = "".join(
                seg.get("plain_text", "") for seg in b.get("to_do", {}).get("rich_text", [])
            ).strip()
            if text:
                actions.append(text)

    return {
        "summary": summary,
        "decisions": decisions,
        "actions": actions,
        "implications": implications,
        "risks": risks,
    }


def _call_todo_agent(batch_actions: list[dict], target_date: str) -> list[dict]:
    if not batch_actions:
        return []
    settings = get_settings()
    client = ClaudeCLIClient(cli_path=settings.claude_cli_path, timeout=settings.claude_api_timeout)
    agent = TodoAgent(client=client, model=settings.claude_model_sonnet)
    result = agent.execute({"batch_actions": batch_actions, "target_date": target_date})
    if not result.ok:
        log.warning("TodoAgent failed: %s", result.error)
        return []
    return result.data.get("todos", [])


def _build_briefing_builder(settings) -> NotionBriefingPageBuilder:
    notion = NotionClient(auth=settings.notion_api_key)
    return NotionBriefingPageBuilder(client=notion, database_id=settings.notion_briefing_database_id)


def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD", default=None,
        help="처리 파일 탐색 기준일 (기본: 어제). 수동 재생성 시 사용.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if not settings.notion_briefing_database_id:
        log.error("notion_briefing_database_id 미설정 — .env에 추가 후 재실행")
        _notify("VoiceFlow 브리핑", "Notion 일일 브리핑 DB ID가 .env에 없습니다 (NOTION_BRIEFING_DATABASE_ID)")
        return 2

    today = date.today()
    target_date = args.date if args.date else (today - timedelta(days=1)).isoformat()

    done, failed = _collect_yesterday_files(_state_path(), target_date)
    log.info("target=%s: %d done, %d failed", target_date, len(done), len(failed))

    meetings = _fetch_meeting_details(done)

    batch_actions: list[dict] = []
    for m in meetings:
        for action in m.get("actions", []):
            batch_actions.append({"source": m["title"], "action": action})
    todos = _call_todo_agent(batch_actions, today.isoformat())

    builder = _build_briefing_builder(settings)
    try:
        result = builder.build(date=today.isoformat(), meetings=meetings, todos=todos, failed=failed)
    except Exception as e:
        log.exception("briefing build failed: %s", e)
        _notify("VoiceFlow 브리핑", f"생성 실패: {e}")
        return 1

    log.info("briefing created: %s", result["url"])
    todo_count = sum(len(t.get('items', [])) for t in todos)
    _notify("VoiceFlow 브리핑", f"어제 {len(done)}건 · 오늘 To-Do {todo_count}건 → Notion 확인")
    return 0


if __name__ == "__main__":
    sys.exit(main())
