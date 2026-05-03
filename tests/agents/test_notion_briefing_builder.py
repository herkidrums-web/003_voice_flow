"""NotionBriefingPageBuilder — creates daily briefing page in Notion DB."""
from unittest.mock import MagicMock

from src.agents.notion_briefing_builder import NotionBriefingPageBuilder


def _mock_notion_client():
    client = MagicMock()
    client.pages.create.return_value = {"id": "page_xyz", "url": "https://www.notion.so/page_xyz"}
    return client


def test_builds_page_with_all_db_properties():
    client = _mock_notion_client()
    builder = NotionBriefingPageBuilder(client=client, database_id="db_abc")

    result = builder.build(
        date="2026-05-04",
        meetings=[
            {"title": "20260503_네이버_KA", "notion_url": "https://www.notion.so/m1",
             "topics": [{"topic": "T", "key_facts": ["f1"], "decisions": ["d1"], "actions": ["a1"]}]},
        ],
        todos=[{"project": "네이버클라우드", "items": ["임원일정 확정", "견적 수정"]}],
        failed=[],
    )

    assert result["url"] == "https://www.notion.so/page_xyz"
    assert result["page_id"] == "page_xyz"

    call = client.pages.create.call_args.kwargs
    assert call["parent"] == {"database_id": "db_abc"}
    props = call["properties"]
    assert props["제목"]["title"][0]["text"]["content"] == "2026-05-04 일일 브리핑"
    assert props["날짜"]["date"]["start"] == "2026-05-04"
    assert props["상태"]["select"]["name"] == "신규"
    assert props["어제미팅수"]["number"] == 1
    assert props["오늘To-Do수"]["number"] == 2
    assert props["실패건수"]["number"] == 0
    assert {ms["name"] for ms in props["프로젝트"]["multi_select"]} == {"네이버클라우드"}


def test_page_body_contains_meeting_link_and_todo_blocks():
    client = _mock_notion_client()
    builder = NotionBriefingPageBuilder(client=client, database_id="db")

    builder.build(
        date="2026-05-04",
        meetings=[
            {"title": "M1", "notion_url": "https://www.notion.so/m1",
             "topics": [{"topic": "T1", "key_facts": ["fact1"], "decisions": [], "actions": []}]},
        ],
        todos=[{"project": "P1", "items": ["할 일1", "할 일2"]}],
        failed=[],
    )

    children = client.pages.create.call_args.kwargs["children"]
    serialized = str(children)
    assert "어제 미팅 요약" in serialized
    assert "M1" in serialized
    assert "https://www.notion.so/m1" in serialized
    assert "fact1" in serialized
    assert "오늘 할 일" in serialized
    assert "할 일1" in serialized
    todo_blocks = [c for c in children if c.get("type") == "to_do"]
    assert len(todo_blocks) >= 2


def test_failed_section_only_when_present():
    client = _mock_notion_client()
    builder = NotionBriefingPageBuilder(client=client, database_id="db")

    builder.build(date="2026-05-04", meetings=[], todos=[], failed=[])
    children_no_fail = client.pages.create.call_args.kwargs["children"]
    assert "Self-Heal 실패" not in str(children_no_fail)

    builder.build(date="2026-05-04", meetings=[], todos=[],
                  failed=[{"file": "x.m4a", "stage": "validation", "error": "max retries"}])
    children_fail = client.pages.create.call_args.kwargs["children"]
    assert "Self-Heal 실패" in str(children_fail)
    assert "x.m4a" in str(children_fail)


def test_select_values_strip_commas():
    """Notion select API rejects commas (Memory: feedback_notion_select_comma)."""
    client = _mock_notion_client()
    builder = NotionBriefingPageBuilder(client=client, database_id="db")
    builder.build(
        date="2026-05-04", meetings=[],
        todos=[{"project": "네이버, 토스", "items": ["x"]}],
        failed=[],
    )
    props = client.pages.create.call_args.kwargs["properties"]
    for ms in props["프로젝트"]["multi_select"]:
        assert "," not in ms["name"]
