"""SelfHealAgent — adjusts analysis params based on validation issues."""
import json
from unittest.mock import MagicMock

from src.agents.self_heal_agent import SelfHealAgent


def _mock_client(payload):
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload, ensure_ascii=False))]
    c.messages.create.return_value = msg
    return c


def test_returns_adjusted_params():
    response = {"adjustments": {"temperature": 0.3, "extra_instruction": "key_facts 최소 3개"}}
    agent = SelfHealAgent(client=_mock_client(response), model="m", max_retries=5)
    result = agent.execute({
        "issues": ["analysis[0].key_facts missing"],
        "attempt": 1,
        "previous_params": {"temperature": 0.7},
    })
    assert result.ok is True
    assert result.data["next_params"]["temperature"] == 0.3
    assert "key_facts" in result.data["next_params"]["extra_instruction"]
    assert result.data["should_retry"] is True


def test_does_not_retry_when_max_attempts_reached():
    agent = SelfHealAgent(client=_mock_client({"adjustments": {}}), model="m", max_retries=5)
    result = agent.execute({"issues": ["x"], "attempt": 5, "previous_params": {}})
    assert result.data["should_retry"] is False


def test_does_not_retry_when_no_issues():
    agent = SelfHealAgent(client=_mock_client({"adjustments": {}}), model="m", max_retries=5)
    result = agent.execute({"issues": [], "attempt": 2, "previous_params": {}})
    assert result.data["should_retry"] is False
