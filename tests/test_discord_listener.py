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


class TestMatchCommandCalendar:
    """이미지 첨부 OR '일정'/'캘린더' 시작 → calendar 분기."""

    def test_image_only_message_triggers_calendar(self):
        atts = [{"content_type": "image/png", "url": "https://cdn.discordapp.com/x.png"}]
        assert discord_listener._match_command("", atts) == "calendar"

    def test_image_with_caption_triggers_calendar(self):
        atts = [{"content_type": "image/jpeg", "url": "https://cdn.discordapp.com/y.jpg"}]
        assert discord_listener._match_command("내일 14시 토스 미팅", atts) == "calendar"

    def test_keyword_iljeong_triggers_calendar(self):
        assert discord_listener._match_command("일정 내일 14시 토스 미팅", None) == "calendar"

    def test_keyword_calendar_kr_triggers_calendar(self):
        assert discord_listener._match_command("캘린더 5/30 점심", None) == "calendar"

    def test_keyword_calendar_en_triggers_calendar(self):
        assert discord_listener._match_command("calendar 5/30 lunch", None) == "calendar"

    def test_keyword_slash_calendar_triggers_calendar(self):
        assert discord_listener._match_command("/calendar 5/30", None) == "calendar"

    def test_image_attachment_non_image_does_not_trigger(self):
        atts = [{"content_type": "application/pdf", "url": "https://x/y.pdf"}]
        assert discord_listener._match_command("문서 봐줘", atts) == "claude"

    def test_keyword_in_middle_does_not_trigger(self):
        # "오늘 일정 어때?" 같은 일반 질문은 calendar로 가지 않고 claude로
        assert discord_listener._match_command("오늘 일정 어때?", None) == "claude"

    def test_existing_commands_still_win_over_image(self):
        # 이미지 첨부 + "ping" → ping이 우선 (회귀 방지)
        atts = [{"content_type": "image/png", "url": "https://x/p.png"}]
        assert discord_listener._match_command("ping", atts) == "ping"
