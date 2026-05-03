"""BaseAgent contract tests."""
import pytest
from src.agents.base import BaseAgent, AgentResult


class _Echo(BaseAgent):
    name = "echo"

    def _run(self, payload: dict) -> dict:
        return {"echoed": payload["msg"]}


def test_agent_execute_returns_result():
    agent = _Echo()
    result = agent.execute({"msg": "hi"})
    assert isinstance(result, AgentResult)
    assert result.ok is True
    assert result.data == {"echoed": "hi"}
    assert result.agent == "echo"
    assert result.elapsed_s >= 0


def test_agent_execute_captures_exception():
    class _Bad(BaseAgent):
        name = "bad"

        def _run(self, payload: dict) -> dict:
            raise ValueError("boom")

    result = _Bad().execute({})
    assert result.ok is False
    assert "boom" in result.error
    assert result.agent == "bad"


def test_agent_must_define_name():
    class _NoName(BaseAgent):
        def _run(self, payload):
            return {}

    with pytest.raises(NotImplementedError):
        _NoName().execute({})
