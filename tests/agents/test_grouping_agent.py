"""GroupingAgent — content-similarity based grouping + 30k char split."""
import json
from unittest.mock import MagicMock

from src.agents.grouping_agent import GroupingAgent


def _mock_client(payload):
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload, ensure_ascii=False))]
    c.messages.create.return_value = msg
    return c


def test_groups_files_by_llm_decision():
    response = {"groups": [["a.m4a", "b.m4a"], ["c.m4a"]]}
    agent = GroupingAgent(client=_mock_client(response), model="m", max_chars=30000)
    result = agent.execute({"files": [
        {"name": "a.m4a", "transcript": "네이버 미팅 시작"},
        {"name": "b.m4a", "transcript": "네이버 미팅 계속"},
        {"name": "c.m4a", "transcript": "다른 주제"},
    ]})
    assert result.ok is True
    assert result.data["groups"] == [["a.m4a", "b.m4a"], ["c.m4a"]]


def test_splits_oversized_group_by_time_chunks():
    """30k자 초과 그룹은 시간 기준으로 분할한다."""
    response = {"groups": [["a.m4a", "b.m4a"]]}
    big = "x" * 20000
    agent = GroupingAgent(client=_mock_client(response), model="m", max_chars=30000)
    result = agent.execute({"files": [
        {"name": "a.m4a", "transcript": big},
        {"name": "b.m4a", "transcript": big},
    ]})
    groups = result.data["groups"]
    assert len(groups) == 2
    assert groups[0] == ["a.m4a"]
    assert groups[1] == ["b.m4a"]


def test_single_file_input_returns_single_group():
    agent = GroupingAgent(client=_mock_client({"groups": [["only.m4a"]]}), model="m", max_chars=30000)
    result = agent.execute({"files": [{"name": "only.m4a", "transcript": "x"}]})
    assert result.data["groups"] == [["only.m4a"]]
