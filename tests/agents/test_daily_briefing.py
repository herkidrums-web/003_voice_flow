"""daily_briefing — collects yesterday's meetings + creates Notion briefing page."""
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock


def _write_state(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for rec in records:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_meeting_date_from_filename():
    from scripts.daily_briefing import _meeting_date_from_filename

    assert _meeting_date_from_filename("20260514 160215.m4a") == "2026-05-14"
    assert _meeting_date_from_filename("20251222 185927-57AF436F.m4a") == "2025-12-22"
    assert _meeting_date_from_filename("noprefix.m4a") is None
    assert _meeting_date_from_filename("") is None


def test_collect_filters_by_meeting_date_not_processing_ts(tmp_path):
    """일일 브리핑은 '미팅 날짜'(파일명)로 묶여야 한다 — 처리 ts 무관.

    핵심 회귀 방지: mirror에 쌓인 과거 백로그가 target_date에 처리돼도
    (ts=target_date) 일일 브리핑에 섞이면 안 된다.
    """
    from scripts.daily_briefing import _collect_yesterday_files

    state_path = tmp_path / "state.jsonl"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today = date.today().isoformat()
    ymd = yesterday.replace("-", "")          # 미팅 날짜 = 어제
    _write_state(state_path, [
        # a: 어제 미팅, notion_url + wiki done → 포함
        {"file": f"{ymd} 0900.m4a", "stage": "notion", "status": "done",
         "ts": f"{today}T09:00:00+00:00", "meta": {"notion_url": "u1"}},
        {"file": f"{ymd} 0900.m4a", "stage": "wiki", "status": "done",
         "ts": f"{today}T10:00:00+00:00", "meta": {}},
        # b: 어제 미팅, 처리는 '오늘'(ts) — 그래도 미팅날짜=어제라 포함
        {"file": f"{ymd} 1400.m4a", "stage": "wiki", "status": "done",
         "ts": f"{today}T11:00:00+00:00", "meta": {}},
        # backlog: 작년 12월 미팅인데 어제 처리됨(ts=어제) → 반드시 제외
        {"file": "20251222 1859.m4a", "stage": "wiki", "status": "done",
         "ts": f"{yesterday}T12:00:00+00:00", "meta": {}},
        # c: 어제 미팅, 끝내 실패 → failed
        {"file": f"{ymd} 1600.m4a", "stage": "validation", "status": "failed",
         "ts": f"{today}T12:00:00+00:00", "meta": {"error": "max retries"}},
    ])
    files, failed = _collect_yesterday_files(state_path, yesterday)
    assert {f["file"] for f in files} == {f"{ymd} 0900.m4a", f"{ymd} 1400.m4a"}
    assert next(f for f in files if f["file"] == f"{ymd} 0900.m4a")["notion_url"] == "u1"
    assert {f["file"] for f in failed} == {f"{ymd} 1600.m4a"}


def test_main_creates_notion_page_and_notifies(tmp_path, monkeypatch):
    from scripts import daily_briefing as db

    state_path = tmp_path / "processing_state.jsonl"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    ymd = yesterday.replace("-", "")
    _write_state(state_path, [
        {"file": f"{ymd} 0900.m4a", "stage": "wiki", "status": "done",
         "ts": f"{yesterday}T09:00:00+00:00",
         "meta": {"notion_url": "https://www.notion.so/x", "title": "20260503_test"}},
    ])

    settings_mock = MagicMock(notion_briefing_database_id="db_test_123")
    monkeypatch.setattr(db, "get_settings", lambda: settings_mock)
    monkeypatch.setattr(db, "_state_path", lambda: state_path)
    monkeypatch.setattr(db, "_fetch_meeting_details", lambda items: [
        {"title": i["title"], "notion_url": i["notion_url"],
         "summary": "f", "decisions": [], "actions": ["a1"], "implications": [], "risks": []}
        for i in items
    ])
    monkeypatch.setattr(db, "_call_todo_agent", lambda actions, target_date: [
        {"project": "P", "items": ["할 일1"]},
    ])
    fake_builder = MagicMock()
    fake_builder.build.return_value = {"page_id": "p1", "url": "https://www.notion.so/p1"}
    monkeypatch.setattr(db, "_build_briefing_builder",
                        lambda settings: fake_builder)
    notifies: list = []
    monkeypatch.setattr(db.subprocess, "run",
                        lambda *a, **kw: notifies.append(a[0]) or MagicMock(returncode=0))
    monkeypatch.setattr("sys.argv", ["daily_briefing.py"])

    rc = db.main()
    assert rc == 0
    fake_builder.build.assert_called_once()
    call_kwargs = fake_builder.build.call_args.kwargs
    assert call_kwargs["date"] == date.today().isoformat()
    assert len(call_kwargs["meetings"]) == 1
    assert len(call_kwargs["todos"]) == 1
    assert any("osascript" in str(c) for c in notifies)


def test_main_returns_2_when_briefing_db_id_missing(tmp_path, monkeypatch):
    from scripts import daily_briefing as db

    monkeypatch.setattr(db, "_state_path", lambda: tmp_path / "nope.jsonl")
    settings_mock = MagicMock(notion_briefing_database_id="")
    monkeypatch.setattr(db, "get_settings", lambda: settings_mock)
    notifies: list = []
    monkeypatch.setattr(db.subprocess, "run",
                        lambda *a, **kw: notifies.append(a[0]) or MagicMock(returncode=0))
    monkeypatch.setattr("sys.argv", ["daily_briefing.py"])

    rc = db.main()
    assert rc == 2
    assert any("osascript" in str(c) for c in notifies)
