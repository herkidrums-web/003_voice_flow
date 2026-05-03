"""OrchestratorState jsonl persistence tests."""
import json
from pathlib import Path

import pytest

from src.agents.state import OrchestratorState, Stage


@pytest.fixture
def state_path(tmp_path) -> Path:
    return tmp_path / "state.jsonl"


def test_record_and_get_stage(state_path):
    s = OrchestratorState(state_path)
    s.record("file_a.m4a", Stage.STT, status="done", meta={"cache_hit": True})
    assert s.get_stage("file_a.m4a") == Stage.STT


def test_latest_stage_wins(state_path):
    s = OrchestratorState(state_path)
    s.record("f.m4a", Stage.STT, status="done")
    s.record("f.m4a", Stage.NER, status="done")
    s.record("f.m4a", Stage.DICTIONARY, status="done")
    assert s.get_stage("f.m4a") == Stage.DICTIONARY


def test_failed_status_does_not_advance(state_path):
    s = OrchestratorState(state_path)
    s.record("f.m4a", Stage.STT, status="done")
    s.record("f.m4a", Stage.NER, status="failed", meta={"err": "x"})
    assert s.get_stage("f.m4a") == Stage.STT


def test_list_files_at_stage(state_path):
    s = OrchestratorState(state_path)
    s.record("a.m4a", Stage.STT, status="done")
    s.record("b.m4a", Stage.STT, status="done")
    s.record("a.m4a", Stage.NER, status="done")
    assert set(s.list_files_at_stage(Stage.STT)) == {"b.m4a"}
    assert set(s.list_files_at_stage(Stage.NER)) == {"a.m4a"}


def test_jsonl_format_on_disk(state_path):
    s = OrchestratorState(state_path)
    s.record("f.m4a", Stage.STT, status="done", meta={"k": "v"})
    line = state_path.read_text().strip()
    rec = json.loads(line)
    assert rec["file"] == "f.m4a"
    assert rec["stage"] == "stt"
    assert rec["status"] == "done"
    assert rec["meta"] == {"k": "v"}
    assert "ts" in rec
