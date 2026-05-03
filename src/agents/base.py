"""BaseAgent abstract class + AgentResult dataclass."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AgentResult:
    agent: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed_s: float = 0.0


class BaseAgent:
    name: str = ""

    def execute(self, payload: dict[str, Any]) -> AgentResult:
        if not self.name:
            raise NotImplementedError("Agent subclass must define `name`")
        start = time.monotonic()
        try:
            data = self._run(payload)
            elapsed = time.monotonic() - start
            log.info("agent=%s ok=true elapsed=%.2fs", self.name, elapsed)
            return AgentResult(agent=self.name, ok=True, data=data, elapsed_s=elapsed)
        except Exception as e:
            elapsed = time.monotonic() - start
            log.exception("agent=%s ok=false elapsed=%.2fs", self.name, elapsed)
            return AgentResult(agent=self.name, ok=False, error=f"{type(e).__name__}: {e}", elapsed_s=elapsed)

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
