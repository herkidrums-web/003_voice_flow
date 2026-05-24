"""Tests for scripts/discord_listener.py command matching & helpers."""
from __future__ import annotations

import sys
import time
import unittest.mock as mock
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

# scripts/ 디렉터리를 path에 추가 (listener는 패키지가 아님)
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import discord_listener  # noqa: E402  # type: ignore[import-not-found]


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

    def test_keyword_question_does_not_trigger(self):
        # "오늘 일정 어때?" — 명사만 있고 동사 없음 → claude
        assert discord_listener._match_command("오늘 일정 어때?", None) == "claude"

    def test_keyword_noun_verb_midsentence_triggers_calendar(self):
        # "...캘린더 등록해줘" — 명사+동사가 문장 중간/끝에 와도 calendar로
        msg = "이번주 토요일 오후6시부터 다음날 오전6시까지 빌더데이 있어 캘린더 등록해줘"
        assert discord_listener._match_command(msg, None) == "calendar"

    def test_iljeong_verb_midsentence_triggers_calendar(self):
        assert discord_listener._match_command("이거 일정에 추가해줘", None) == "calendar"

    def test_calendar_delete_midsentence_triggers_calendar(self):
        assert discord_listener._match_command("빌더데이 캘린더에서 삭제해줘", None) == "calendar"

    def test_existing_commands_still_win_over_image(self):
        # 이미지 첨부 + "ping" → ping이 우선 (회귀 방지)
        atts = [{"content_type": "image/png", "url": "https://x/p.png"}]
        assert discord_listener._match_command("ping", atts) == "ping"


class TestStripJsonFence:
    """Claude CLI 응답의 ```json ... ``` 펜스 제거."""

    def test_no_fence_returns_unchanged(self):
        s = '{"status":"created"}'
        assert discord_listener._strip_json_fence(s) == s

    def test_json_fence_stripped(self):
        s = '```json\n{"status":"created"}\n```'
        assert discord_listener._strip_json_fence(s) == '{"status":"created"}'

    def test_plain_fence_stripped(self):
        s = '```\n{"status":"not_event"}\n```'
        assert discord_listener._strip_json_fence(s) == '{"status":"not_event"}'

    def test_fence_with_surrounding_whitespace(self):
        s = '   ```json\n{"a":1}\n```   '
        assert discord_listener._strip_json_fence(s) == '{"a":1}'

    def test_empty_string_safe(self):
        assert discord_listener._strip_json_fence("") == ""


class TestBuildCalendarPrompt:
    def test_includes_today_iso(self):
        prompt = discord_listener._build_calendar_prompt("내일 14시 미팅", [])
        assert date.today().isoformat() in prompt

    def test_includes_weekday_korean(self):
        prompt = discord_listener._build_calendar_prompt("내일 14시 미팅", [])
        weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][date.today().weekday()]
        assert weekday_kr in prompt

    def test_includes_image_paths(self):
        paths = [Path("/tmp/cal_x_0.png"), Path("/tmp/cal_x_1.jpg")]
        prompt = discord_listener._build_calendar_prompt("", paths)
        assert "/tmp/cal_x_0.png" in prompt
        assert "/tmp/cal_x_1.jpg" in prompt
        assert "Read 도구" in prompt

    def test_empty_content_marker(self):
        prompt = discord_listener._build_calendar_prompt("", [])
        assert "(텍스트 없음)" in prompt

    def test_required_status_keywords_present(self):
        prompt = discord_listener._build_calendar_prompt("test", [])
        for kw in ("not_event", "need_confirmation", "created",
                   "mcp__claude_ai_Google_Calendar__create_event",
                   "Asia/Seoul"):
            assert kw in prompt, f"missing keyword in prompt: {kw}"

    def test_modify_delete_instructions_present(self):
        prompt = discord_listener._build_calendar_prompt("test", [])
        for kw in ("updated", "deleted",
                   "mcp__claude_ai_Google_Calendar__update_event",
                   "mcp__claude_ai_Google_Calendar__delete_event",
                   "mcp__claude_ai_Google_Calendar__list_events"):
            assert kw in prompt, f"missing keyword in prompt: {kw}"

    def test_conflict_check_instructions_present(self):
        prompt = discord_listener._build_calendar_prompt("test", [])
        assert "conflicts" in prompt
        assert "충돌" in prompt


class TestConflictSuffix:
    """payload conflicts → Discord 경고 줄."""

    def test_no_conflicts_key_returns_empty(self):
        assert discord_listener._conflict_suffix({"status": "created"}) == ""

    def test_empty_conflicts_list_returns_empty(self):
        assert discord_listener._conflict_suffix({"conflicts": []}) == ""

    def test_single_conflict_formatted(self):
        out = discord_listener._conflict_suffix(
            {"conflicts": ["5/23 16:00-17:00 마사지 예약"]}
        )
        assert out == "\n⚠️ 시간 충돌: 5/23 16:00-17:00 마사지 예약"

    def test_multiple_conflicts_joined(self):
        out = discord_listener._conflict_suffix(
            {"conflicts": ["A 일정", "B 일정"]}
        )
        assert "A 일정" in out and "B 일정" in out
        assert out.startswith("\n⚠️ 시간 충돌: ")


class TestSyncStaleThrottleFix:
    """단계 1·2 수정 검증 — fresh 전환 시 마커 보존 & throttle 준수."""

    def test_stale_check_fresh_does_not_clear_marker(self, tmp_path):
        """fresh 분기 진입 시 STALE_MARKER를 삭제하지 않아야 한다 (throttle 버그 수정)."""
        # 준비: sync.log fresh + STALE_MARKER 존재
        sync_log = tmp_path / "voiceflow-sync.log"
        sync_log.write_text("ok", encoding="utf-8")
        # mtime = 현재 (fresh)
        now = time.time()
        import os
        os.utime(sync_log, (now, now))

        stale_marker = tmp_path / "voiceflow-stale-notified.txt"
        stale_marker.write_text("2026-05-24 01:00:00", encoding="utf-8")

        log_mock = MagicMock()

        # 원본 상수를 임시로 교체
        orig_sync_log = discord_listener.SYNC_LOG
        orig_marker = discord_listener.STALE_MARKER
        orig_threshold = discord_listener.STALE_THRESHOLD_HOURS
        orig_last = discord_listener._last_stale_check_at
        try:
            discord_listener.SYNC_LOG = sync_log
            discord_listener.STALE_MARKER = stale_marker
            discord_listener.STALE_THRESHOLD_HOURS = 12.0
            discord_listener._last_stale_check_at = 0.0  # force check

            discord_listener._check_sync_stale_and_ping("fake_token", log_mock)
        finally:
            discord_listener.SYNC_LOG = orig_sync_log
            discord_listener.STALE_MARKER = orig_marker
            discord_listener.STALE_THRESHOLD_HOURS = orig_threshold
            discord_listener._last_stale_check_at = orig_last

        # 마커가 그대로 존재해야 함 (삭제 안 됨)
        assert stale_marker.exists(), "fresh 분기에서 STALE_MARKER를 삭제하면 안 된다"

    def test_stale_check_throttle_respected(self, tmp_path):
        """마커 age < STALE_NOTIFY_INTERVAL_HOURS이면 Discord ping을 발사하지 않는다."""
        import os

        # sync.log stale (mtime = 24h 전)
        sync_log = tmp_path / "voiceflow-sync.log"
        sync_log.write_text("old", encoding="utf-8")
        stale_mtime = time.time() - 24 * 3600
        os.utime(sync_log, (stale_mtime, stale_mtime))

        # 마커 = 방금 생성 (age ≈ 0h < 6h)
        stale_marker = tmp_path / "voiceflow-stale-notified.txt"
        stale_marker.write_text("recent", encoding="utf-8")
        recent_mtime = time.time()
        os.utime(stale_marker, (recent_mtime, recent_mtime))

        log_mock = MagicMock()
        sent_calls = []

        orig_sync_log = discord_listener.SYNC_LOG
        orig_marker = discord_listener.STALE_MARKER
        orig_threshold = discord_listener.STALE_THRESHOLD_HOURS
        orig_notify = discord_listener.STALE_NOTIFY_INTERVAL_HOURS
        orig_last = discord_listener._last_stale_check_at
        orig_send = discord_listener._send
        try:
            discord_listener.SYNC_LOG = sync_log
            discord_listener.STALE_MARKER = stale_marker
            discord_listener.STALE_THRESHOLD_HOURS = 12.0
            discord_listener.STALE_NOTIFY_INTERVAL_HOURS = 6.0
            discord_listener._last_stale_check_at = 0.0
            discord_listener._send = lambda *a, **k: sent_calls.append(a)

            discord_listener._check_sync_stale_and_ping("fake_token", log_mock)
        finally:
            discord_listener.SYNC_LOG = orig_sync_log
            discord_listener.STALE_MARKER = orig_marker
            discord_listener.STALE_THRESHOLD_HOURS = orig_threshold
            discord_listener.STALE_NOTIFY_INTERVAL_HOURS = orig_notify
            discord_listener._last_stale_check_at = orig_last
            discord_listener._send = orig_send

        assert len(sent_calls) == 0, "throttle 미만이면 Discord ping 발사 금지"

    def test_stale_alert_message_contains_action_hint(self, tmp_path):
        """stale 알림 메시지에 '답장하면 처리됩니다' 액션 힌트가 포함돼야 한다."""
        import os

        sync_log = tmp_path / "voiceflow-sync.log"
        sync_log.write_text("old", encoding="utf-8")
        stale_mtime = time.time() - 24 * 3600
        os.utime(sync_log, (stale_mtime, stale_mtime))

        stale_marker = tmp_path / "voiceflow-stale-notified.txt"

        log_mock = MagicMock()
        sent_msgs = []

        orig_sync_log = discord_listener.SYNC_LOG
        orig_marker = discord_listener.STALE_MARKER
        orig_threshold = discord_listener.STALE_THRESHOLD_HOURS
        orig_notify_int = discord_listener.STALE_NOTIFY_INTERVAL_HOURS
        orig_last = discord_listener._last_stale_check_at
        orig_send = discord_listener._send
        try:
            discord_listener.SYNC_LOG = sync_log
            discord_listener.STALE_MARKER = stale_marker
            discord_listener.STALE_THRESHOLD_HOURS = 12.0
            discord_listener.STALE_NOTIFY_INTERVAL_HOURS = 6.0
            discord_listener._last_stale_check_at = 0.0
            discord_listener._send = lambda token, msg, **k: sent_msgs.append(msg)

            discord_listener._check_sync_stale_and_ping("fake_token", log_mock)
        finally:
            discord_listener.SYNC_LOG = orig_sync_log
            discord_listener.STALE_MARKER = orig_marker
            discord_listener.STALE_THRESHOLD_HOURS = orig_threshold
            discord_listener.STALE_NOTIFY_INTERVAL_HOURS = orig_notify_int
            discord_listener._last_stale_check_at = orig_last
            discord_listener._send = orig_send

        assert len(sent_msgs) == 1, "stale 상태에서 알림 1건 발사해야 함"
        assert "답장하면 처리됩니다" in sent_msgs[0]
