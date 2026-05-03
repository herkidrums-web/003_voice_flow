"""Orchestrator — coordinates all sub-agents end-to-end (mocked)."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.base import AgentResult
from src.agents.orchestrator import Orchestrator
from src.agents.state import OrchestratorState, Stage


def _ok(name, data):
    return AgentResult(agent=name, ok=True, data=data, elapsed_s=0.01)


def _fail(name, err):
    return AgentResult(agent=name, ok=False, error=err, elapsed_s=0.01)


@pytest.fixture
def state(tmp_path):
    return OrchestratorState(tmp_path / "state.jsonl")


def _make_orchestrator(state, **overrides):
    agents = {
        "stt": MagicMock(execute=MagicMock(return_value=_ok("stt", {"transcript": "T", "cache_hit": False, "sha256": "x"}))),
        "ner": MagicMock(execute=MagicMock(return_value=_ok("ner", {"new_terms": []}))),
        "dictionary": MagicMock(execute=MagicMock(return_value=_ok("dictionary", {"transcript": "T'"}))),
        "grouping": MagicMock(execute=MagicMock(return_value=_ok("grouping", {"groups": [["a.m4a"]]}))),
        "analysis": MagicMock(execute=MagicMock(return_value=_ok("analysis", {"analyses": [{"topic": "x", "five_w_one_h": {}, "key_facts": ["f"], "decisions": ["d"], "actions": ["a"]}]}))),
        "validation": MagicMock(execute=MagicMock(return_value=_ok("validation", {"pass": True, "issues": []}))),
        "self_heal": MagicMock(execute=MagicMock(return_value=_ok("self_heal", {"should_retry": False, "next_params": {}}))),
        "classification": MagicMock(execute=MagicMock(return_value=_ok("classification", {"project": ["P"], "meeting_type": "내부회의", "importance": "medium"}))),
        "notion": MagicMock(execute=MagicMock(return_value=_ok("notion", {"page_id": "p1", "notion_url": "https://n/p1"}))),
        "wiki": MagicMock(execute=MagicMock(return_value=_ok("wiki", {"appended": True}))),
    }
    agents.update(overrides)
    return Orchestrator(agents=agents, state=state)


def test_happy_path_records_done_at_each_stage(state, tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    orch = _make_orchestrator(state)
    result = orch.process_batch([audio])
    assert result["ok"] is True
    assert state.get_stage("a.m4a") == Stage.WIKI


def test_self_heal_loop_invoked_on_validation_fail(state, tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    val_mock = MagicMock()
    val_mock.execute.side_effect = [
        _ok("validation", {"pass": False, "issues": ["analysis[0].key_facts missing"]}),
        _ok("validation", {"pass": True, "issues": []}),
    ]
    heal_mock = MagicMock()
    heal_mock.execute.return_value = _ok("self_heal", {"should_retry": True, "next_params": {"temperature": 0.3, "extra_instruction": "더 풍부히"}, "attempt": 1})
    orch = _make_orchestrator(state, validation=val_mock, self_heal=heal_mock)
    result = orch.process_batch([audio])
    assert result["ok"] is True
    assert val_mock.execute.call_count == 2
    assert heal_mock.execute.call_count == 1


def test_max_retries_exceeded_marks_failed_and_skips(state, tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    val_mock = MagicMock()
    val_mock.execute.return_value = _ok("validation", {"pass": False, "issues": ["x"]})
    heal_responses = [
        _ok("self_heal", {"should_retry": True, "next_params": {"temperature": 0.3}, "attempt": i})
        for i in range(1, 6)
    ] + [_ok("self_heal", {"should_retry": False, "next_params": {}})]
    heal_mock = MagicMock()
    heal_mock.execute.side_effect = heal_responses
    orch = _make_orchestrator(state, validation=val_mock, self_heal=heal_mock)
    result = orch.process_batch([audio])
    assert result["ok"] is False
    assert state.get_stage("a.m4a") != Stage.WIKI


def test_resume_from_persisted_stage(state, tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    state.record("a.m4a", Stage.STT, status="done", meta={"transcript": "TC"})
    state.record("a.m4a", Stage.NER, status="done", meta={"new_terms": []})
    state.record("a.m4a", Stage.DICTIONARY, status="done", meta={"transcript": "TC"})

    orch = _make_orchestrator(state)
    orch.process_batch([audio])
    orch.agents["stt"].execute.assert_not_called()
    orch.agents["ner"].execute.assert_not_called()
    orch.agents["dictionary"].execute.assert_not_called()
    orch.agents["grouping"].execute.assert_called()
