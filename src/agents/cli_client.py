"""ClaudeCLIClient — drop-in replacement for anthropic.Anthropic().

Uses `claude -p` subprocess so pipeline agents run under the user's
Claude subscription (no ANTHROPIC_API_KEY billing).

Interface mirrors the Anthropic SDK just enough for the agents:
    client.messages.create(model=..., max_tokens=..., messages=[...])
    → response.content[0].text
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)

# Default CLI path — overridden via ClaudeCLIClient(cli_path=...)
_DEFAULT_CLI = "/Users/swlee/.local/bin/claude"


def _strip_fence(text: str) -> str:
    """Remove markdown code fences that Claude CLI sometimes wraps around JSON."""
    if text.startswith("```"):
        lines = text.splitlines()
        # drop first line (```json or ```) and last line (```)
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        return "\n".join(inner).strip()
    return text


class _Content:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Content(text)]


class _Messages:
    def __init__(self, cli_path: str, timeout: float) -> None:
        self._cli = cli_path
        self._timeout = timeout

    def create(
        self,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        **kwargs,  # temperature, system, etc. silently ignored — CLI controls these
    ) -> _Response:
        # Extract last user-role message as the prompt
        prompt = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        cmd = [
            self._cli,
            "-p", prompt,
            "--model", model,
            "--output-format", "text",
            "--no-session-persistence",
            "--tools", "",          # disable all built-in tools — pure LLM reasoning
        ]
        log.debug("ClaudeCLI invoke: model=%s len=%d", model, len(prompt))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Claude CLI exited {result.returncode}: {result.stderr[:500]}"
            )
        return _Response(_strip_fence(result.stdout.strip()))


class ClaudeCLIClient:
    """Acts like `anthropic.Anthropic()` but routes through the CLI binary.

    Usage (same as Anthropic SDK):
        client = ClaudeCLIClient()
        r = client.messages.create(model="claude-sonnet-4-6", max_tokens=2048,
                                   messages=[{"role": "user", "content": "hi"}])
        print(r.content[0].text)
    """

    def __init__(
        self,
        cli_path: str = _DEFAULT_CLI,
        timeout: float = 1800.0,
        # api_key and other SDK kwargs accepted but ignored
        **_ignored,
    ) -> None:
        self.messages = _Messages(cli_path, timeout)
