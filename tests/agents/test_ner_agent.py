"""NERAgent — Sonnet-driven proper-noun extraction."""
import json
from unittest.mock import MagicMock

from src.agents.ner_agent import NERAgent


def _mock_client(payload):
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload, ensure_ascii=False))]
    c.messages.create.return_value = msg
    return c


def test_returns_new_terms_only():
    response = {"new_terms": [{"misheard": "원혁명", "correct": "권용현 부사장", "category": "person"}]}
    agent = NERAgent(client=_mock_client(response), model="claude-sonnet-4-6")
    result = agent.execute({
        "transcript": "오늘 원혁명 부사장과 미팅",
        "known_terms": ["김태원 대표"],
    })
    assert result.ok is True
    assert len(result.data["new_terms"]) == 1
    assert result.data["new_terms"][0]["correct"] == "권용현 부사장"


def test_known_terms_passed_to_prompt():
    response = {"new_terms": []}
    client = _mock_client(response)
    agent = NERAgent(client=client, model="m")
    agent.execute({"transcript": "x", "known_terms": ["A", "B"]})
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "A" in sent and "B" in sent


def test_invalid_json_returns_error():
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text="garbage")]
    c.messages.create.return_value = msg
    agent = NERAgent(client=c, model="m")
    result = agent.execute({"transcript": "x", "known_terms": []})
    assert result.ok is False


def test_long_transcript_is_truncated():
    # Regression: 2026-05-15 jetsam — 61k-char transcript made CLI subprocess
    # balloon. NER only needs each proper noun once, so head-only is sufficient.
    # Use an ASCII sentinel char absent from the prompt template to count exactly.
    sentinel = "Q"
    big = sentinel * 100_000
    response = {"new_terms": []}
    client = _mock_client(response)
    agent = NERAgent(client=client, model="m")
    agent.execute({"transcript": big, "known_terms": []})
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert sent.count(sentinel) == NERAgent._MAX_TRANSCRIPT_CHARS
