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

from anthropic import Anthropic
from notion_client import Client as NotionClient

from config import get_settings
from src.agents.notion_briefing_builder import NotionBriefingPageBuilder
from src.agents.todo_agent import TodoAgent

log = logging.getLogger(__name__)


def _state_path() -> Path:
    return Path(__file__).resolve().parents[1] / "processing_state.jsonl"


def _collect_yesterday_files(state_path: Path, target_date: str) -> tuple[list[dict], list[dict]]:
    """Return (done_files, failed_files) where ts is on target_date."""
    if not state_path.exists():
        return [], []
    seen_done: dict[str, dict] = {}
    seen_failed: dict[str, dict] = {}
    for line in state_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts", "")
        if not ts.startswith(target_date):
            continue
        f = rec.get("file")
        if not f:
            continue
        meta = rec.get("meta", {}) or {}
        if rec.get("status") == "done" and rec.get("stage") == "wiki":
            seen_done[f] = {"file": f, "notion_url": meta.get("notion_url", ""), "title": meta.get("title", f)}
        elif rec.get("status") == "failed":
            seen_failed[f] = {"file": f, "stage": rec.get("stage", ""), "error": meta.get("error", "")}
    return list(seen_done.values()), list(seen_failed.values())


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
        return [{"title": i["title"], "notion_url": i["notion_url"], "topics": []} for i in items]

    for item in items:
        url = item.get("notion_url", "")
        page_id = url.rsplit("/", 1)[-1].split("?")[0].split("-")[-1] if url else ""
        topics: list[dict] = []
        try:
            if page_id:
                blocks = notion.blocks.children.list(block_id=page_id, page_size=100)
                topics = _extract_topics_from_blocks(blocks.get("results", []))
        except Exception as e:
            log.warning("Notion fetch failed for %s: %s", item["file"], e)
        out.append({"title": item["title"], "notion_url": url, "topics": topics})
    return out


def _extract_topics_from_blocks(blocks: list[dict]) -> list[dict]:
    topics: list[dict] = []
    current: dict | None = None
    for b in blocks:
        btype = b.get("type", "")
        if btype.startswith("heading_"):
            if current:
                topics.append(current)
            heading_text = "".join(rt.get("plain_text", "") for rt in b.get(btype, {}).get("rich_text", []))
            current = {"topic": heading_text, "key_facts": [], "decisions": [], "actions": []}
        elif current and btype in ("bulleted_list_item", "numbered_list_item"):
            text = "".join(rt.get("plain_text", "") for rt in b.get(btype, {}).get("rich_text", []))
            tl = current["topic"].lower()
            if "사실" in current["topic"] or "fact" in tl:
                current["key_facts"].append(text)
            elif "결정" in current["topic"] or "decision" in tl:
                current["decisions"].append(text)
            elif "액션" in current["topic"] or "action" in tl:
                current["actions"].append(text)
    if current:
        topics.append(current)
    return topics


def _call_todo_agent(batch_actions: list[dict], target_date: str) -> list[dict]:
    if not batch_actions:
        return []
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key, timeout=settings.claude_api_timeout)
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if not settings.notion_briefing_database_id:
        log.error("notion_briefing_database_id 미설정 — .env에 추가 후 재실행")
        _notify("VoiceFlow 브리핑", "Notion 일일 브리핑 DB ID가 .env에 없습니다 (NOTION_BRIEFING_DATABASE_ID)")
        return 2

    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()

    done, failed = _collect_yesterday_files(_state_path(), yesterday)
    log.info("yesterday: %d done, %d failed", len(done), len(failed))

    meetings = _fetch_meeting_details(done)

    batch_actions: list[dict] = []
    for m in meetings:
        for t in m.get("topics", []):
            for action in t.get("actions", []):
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
