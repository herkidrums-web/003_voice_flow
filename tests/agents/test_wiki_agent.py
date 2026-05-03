"""WikiAgent — append meeting note link to wiki/index.md."""
from pathlib import Path

import pytest

from src.agents.wiki_agent import WikiAgent


@pytest.fixture
def index_file(tmp_path) -> Path:
    f = tmp_path / "wiki" / "index.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Wiki Index\n\n## Meetings\n", encoding="utf-8")
    return f


def test_appends_meeting_link_under_section(index_file):
    agent = WikiAgent(index_path=index_file)
    result = agent.execute({
        "title": "20260503_네이버_KA플랜",
        "notion_url": "https://www.notion.so/abc123",
        "section": "Meetings",
        "date": "2026-05-03",
    })
    assert result.ok is True
    content = index_file.read_text(encoding="utf-8")
    assert "[20260503_네이버_KA플랜](https://www.notion.so/abc123)" in content
    assert "2026-05-03" in content


def test_creates_section_if_missing(index_file):
    agent = WikiAgent(index_path=index_file)
    result = agent.execute({
        "title": "무제",
        "notion_url": "https://www.notion.so/xyz",
        "section": "Calls",
        "date": "2026-05-03",
    })
    assert result.ok is True
    content = index_file.read_text(encoding="utf-8")
    assert "## Calls" in content
    assert "[무제](https://www.notion.so/xyz)" in content


def test_skips_duplicate_link(index_file):
    agent = WikiAgent(index_path=index_file)
    payload = {"title": "T", "notion_url": "https://www.notion.so/dup", "section": "Meetings", "date": "2026-05-03"}
    agent.execute(payload)
    agent.execute(payload)
    content = index_file.read_text(encoding="utf-8")
    assert content.count("https://www.notion.so/dup") == 1
