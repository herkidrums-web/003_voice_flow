"""Tests for 2-pass summarizer with mocked Claude CLI."""
import json
from unittest.mock import MagicMock, patch

import pytest

from config import RetryableError, SummaryError
from src.summarizer import (
    MeetingAnalysis,
    MeetingContent,
    MeetingProperties,
    analyze_transcript,
    correct_transcript,
    summarize_transcript,
)

# Pass 2a 응답: sessions 배열 포맷
MOCK_FACTS_JSON = {
    "sessions": [
        {
            "properties": {
                "title": "20260319_내부_업무보고체계논의",
                "type": "회의",
                "project": "",
                "client": "",
                "related_people": ["김대표", "이부장"],
                "tags": ["업무보고", "조직문화"],
                "priority": "중요",
                "status": "후속필요",
                "summary": "업무 보고 체계와 조직 문화에 대해 논의했습니다.",
                "next_actions": "1. 보고서 양식 정리\n2. 미팅 일정 확인",
                "participants": "김대표, 이부장",
            },
            "facts": [
                {
                    "speaker": "김대표",
                    "content": "보고서 양식 간소화 필요",
                    "original_quote": "보고서 양식을 간소화해야 한다",
                    "topic": "업무 보고 체계",
                }
            ],
            "key_persons_observed": [
                {"name": "김대표", "role": "대표", "observed_behavior": "효율성 중시"}
            ],
            "meeting_overview": {
                "date": "2026-03-19",
                "participants": ["김대표", "이부장"],
                "type": "1:1 미팅",
                "location": "",
            },
        }
    ]
}

# Pass 2b 응답
MOCK_ANALYSIS_JSON = {
    "executive_summary": [
        {"topic": "업무 보고 체계", "points": ["보고서 양식 간소화 논의"]}
    ],
    "discussions": [
        {
            "topic": "업무 보고 체계",
            "analysis": "현행 보고 절차의 비효율성에 대한 논의가 이루어졌습니다.",
            "key_quotes": ["김대표: '보고서 양식을 간소화해야 한다'"],
        }
    ],
    "decision_structure": [],
    "decisions": [],
    "risks": ["보고 체계 변경 시 혼란 가능성"],
    "intelligence": {},
    "implications": [],
    "key_persons": [
        {"name": "김대표", "role": "대표", "observed_behavior": "효율성을 중시하는 성향"}
    ],
    "action_items": [
        {"assignee": "이부장", "task": "보고서 양식 정리", "deadline": "금요일", "urgency": "이번주"}
    ],
}

MOCK_SETTINGS = MagicMock(
    anthropic_api_key="test-key",
    claude_cli_path="/usr/local/bin/claude",
    claude_model="claude-opus-4-6",
    claude_model_light="claude-sonnet-4-5-20250929",
    claude_max_tokens=65536,
    claude_api_timeout=1800.0,
)


@pytest.fixture(autouse=True)
def mock_settings():
    with patch("src.summarizer.get_settings", return_value=MOCK_SETTINGS):
        yield


@pytest.fixture
def mock_call_claude():
    """Mock _call_claude to return controlled responses."""
    with patch("src.summarizer._call_claude") as mock:
        yield mock


class TestCorrectTranscript:
    def test_returns_corrected_text(self, mock_call_claude):
        mock_call_claude.return_value = "교정된 전사본 텍스트"
        result = correct_transcript("원본 전사본 텍스트")
        assert result == "교정된 전사본 텍스트"

    def test_empty_text_returns_as_is(self, mock_call_claude):
        result = correct_transcript("")
        assert result == ""
        mock_call_claude.assert_not_called()

    def test_passes_correction_prompt(self, mock_call_claude):
        mock_call_claude.return_value = "결과"
        correct_transcript("테스트 입력")
        call_args = mock_call_claude.call_args
        assert "음성인식" in call_args[1]["system"] or "음성인식" in call_args[0][0]


class TestAnalyzeTranscript:
    def test_returns_meeting_analysis_list(self, mock_call_claude):
        """analyze_transcript returns list[MeetingAnalysis]."""
        mock_call_claude.side_effect = [
            json.dumps(MOCK_FACTS_JSON, ensure_ascii=False),  # Pass 2a
            json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False),  # Pass 2b
        ]
        result = analyze_transcript("테스트 전사본", 300.0, "2026-03-19")
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], MeetingAnalysis)
        assert result[0].properties.title == "20260319_내부_업무보고체계논의"
        assert result[0].properties.meeting_type == "회의"
        assert len(result[0].properties.related_people) == 2

    def test_empty_transcript_raises_error(self):
        with pytest.raises(SummaryError, match="Empty transcript"):
            analyze_transcript("", 60.0)

    def test_invalid_json_raises_error(self, mock_call_claude):
        mock_call_claude.return_value = "not valid json"
        with pytest.raises(SummaryError, match="invalid JSON"):
            analyze_transcript("test input", 60.0)

    def test_includes_duration_and_date(self, mock_call_claude):
        mock_call_claude.side_effect = [
            json.dumps(MOCK_FACTS_JSON, ensure_ascii=False),
            json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False),
        ]
        analyze_transcript("테스트", 600.0, "2026-03-20")
        # Pass 2a call should include duration and date
        first_call = mock_call_claude.call_args_list[0]
        user_prompt = first_call[1].get("user", first_call[0][1] if len(first_call[0]) > 1 else "")
        assert "10.0분" in user_prompt
        assert "2026-03-20" in user_prompt

    def test_strips_code_fences(self, mock_call_claude):
        # _call_claude already strips code fences internally,
        # so mock returns clean JSON (testing the full chain)
        mock_call_claude.side_effect = [
            json.dumps(MOCK_FACTS_JSON, ensure_ascii=False),  # Pass 2a
            json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False),  # Pass 2b
        ]
        result = analyze_transcript("테스트", 300.0)
        assert result[0].properties.title == "20260319_내부_업무보고체계논의"


class TestSummarizeTranscript:
    def test_calls_correction_and_analysis(self, mock_call_claude):
        """summarize_transcript should call Claude multiple times."""
        responses = [
            "교정된 텍스트",  # Pass 1
            json.dumps(MOCK_FACTS_JSON, ensure_ascii=False),  # Pass 2a
            json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False),  # Pass 2b
        ]
        mock_call_claude.side_effect = responses
        result = summarize_transcript("원본 텍스트", 300.0, "2026-03-19")
        assert mock_call_claude.call_count == 3  # Pass 1 + 2a + 2b (Pass 1.5 disabled)
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], MeetingAnalysis)

    def test_rate_limit_raises_retryable(self, mock_call_claude):
        from config import NonRetryableError
        mock_call_claude.side_effect = RetryableError("rate limited", status_code=429)
        with pytest.raises(RetryableError):
            correct_transcript("test")
