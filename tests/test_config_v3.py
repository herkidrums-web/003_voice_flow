"""V3 multi-agent config additions."""
from config import get_settings


def test_v3_model_ids_defined():
    s = get_settings()
    assert s.claude_model_haiku == "claude-haiku-4-5-20251001"
    assert s.claude_model_sonnet == "claude-sonnet-4-6"
    assert s.claude_model_opus == "claude-opus-4-7"


def test_v3_self_heal_max_retries():
    s = get_settings()
    assert s.self_heal_max_retries == 5


def test_v3_grouping_max_chars():
    s = get_settings()
    assert s.grouping_max_chars == 30000


def test_v3_wiki_index_path():
    s = get_settings()
    assert s.wiki_index_path.endswith("000_second_brain/wiki/index.md")


def test_v3_notion_briefing_database_id_defaults_empty():
    """일일 브리핑 DB ID. 사용자가 .env에 NOTION_BRIEFING_DATABASE_ID로 주입."""
    s = get_settings()
    assert hasattr(s, "notion_briefing_database_id")
    assert isinstance(s.notion_briefing_database_id, str)
