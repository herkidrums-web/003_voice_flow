"""Tests for scripts/discord_listener.py command matching & helpers."""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ 디렉터리를 path에 추가 (listener는 패키지가 아님)
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import discord_listener  # noqa: E402


class TestMatchCommandRegression:
    """새 시그니처(attachments 인자)로도 기존 명령 매칭이 동일해야 한다."""

    def test_ping(self):
        assert discord_listener._match_command("ping", None) == "ping"

    def test_voice_korean(self):
        assert discord_listener._match_command("음성메모", None) == "process_voice"

    def test_voiceflow_english(self):
        assert discord_listener._match_command("voiceflow", None) == "process_voice"

    def test_morning_briefing_korean(self):
        assert discord_listener._match_command("브리핑", None) == "morning_briefing"

    def test_weekly_briefing(self):
        assert discord_listener._match_command("주간", None) == "weekly_briefing"

    def test_process_all_combo(self):
        assert discord_listener._match_command("오늘 처리", None) == "process_all"

    def test_status(self):
        assert discord_listener._match_command("상태", None) == "status"

    def test_reset(self):
        assert discord_listener._match_command("/reset", None) == "reset"

    def test_empty_returns_none(self):
        assert discord_listener._match_command("", None) is None

    def test_arbitrary_text_falls_through_to_claude(self):
        assert discord_listener._match_command("안녕 오늘 일정 어때?", None) == "claude"
