"""DictionaryAgent — apply existing terms + append new ones."""
from pathlib import Path

import pytest

from src.agents.base import AgentResult
from src.agents.dictionary_agent import DictionaryAgent


@pytest.fixture
def terms_file(tmp_path) -> Path:
    f = tmp_path / "custom_terms.txt"
    f.write_text("원혁명=>권용현 부사장\n김태현=>김태원 대표(코람코)\n", encoding="utf-8")
    return f


def test_applies_existing_corrections(terms_file):
    agent = DictionaryAgent(terms_path=terms_file)
    result = agent.execute({
        "transcript": "오늘 원혁명 부사장과 김태현 대표를 만났습니다.",
        "new_terms": [],
    })
    assert isinstance(result, AgentResult)
    assert result.ok is True
    assert "권용현 부사장" in result.data["transcript"]
    assert "김태원 대표(코람코)" in result.data["transcript"]


def test_appends_new_terms_to_file(terms_file):
    agent = DictionaryAgent(terms_path=terms_file)
    result = agent.execute({
        "transcript": "PQCT 검토",
        "new_terms": [{"misheard": "피큐씨티", "correct": "PQCT"}],
    })
    assert result.ok is True
    content = terms_file.read_text(encoding="utf-8")
    assert "피큐씨티=>PQCT" in content


def test_does_not_duplicate_existing_term(terms_file):
    agent = DictionaryAgent(terms_path=terms_file)
    agent.execute({"transcript": "", "new_terms": [{"misheard": "원혁명", "correct": "권용현 부사장"}]})
    content = terms_file.read_text(encoding="utf-8")
    assert content.count("원혁명=>권용현 부사장") == 1
