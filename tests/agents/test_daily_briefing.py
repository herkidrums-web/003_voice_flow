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


def test_collect_yesterday_files_filters_by_date(tmp_path):
    from scripts.daily_briefing import _collect_yesterday_files

    state_path = tmp_path / "state.jsonl"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today = date.today().isoformat()
    # notion_url lives in the 'notion' stage done record (not wiki)
    _write_state(state_path, [
        {"file": "a.m4a", "stage": "notion", "status": "done", "ts": f"{yesterday}T09:00:00+00:00",
         "meta": {"notion_url": "u1"}},
        {"file": "a.m4a", "stage": "wiki", "status": "done", "ts": f"{yesterday}T10:00:00+00:00", "meta": {}},
        {"file": "b.m4a", "stage": "wiki", "status": "done", "ts": f"{today}T11:00:00+00:00", "meta": {}},
        {"file": "c.m4a", "stage": "validation", "status": "failed", "ts": f"{yesterday}T12:00:00+00:00",
         "meta": {"error": "max retries"}},
    ])
    files, failed = _collect_yesterday_files(state_path, yesterday)
    assert {f["file"] for f in files} == {"a.m4a"}
    assert files[0]["notion_url"] == "u1"
    assert {f["file"] for f in failed} == {"c.m4a"}


def test_main_creates_notion_page_and_notifies(tmp_path, monkeypatch):
    from scripts import daily_briefing as db

    state_path = tmp_path / "processing_state.jsonl"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _write_state(state_path, [
        {"file": "x.m4a", "stage": "wiki", "status": "done", "ts": f"{yesterday}T09:00:00+00:00",
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
