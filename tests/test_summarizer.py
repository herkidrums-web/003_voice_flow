"""Tests for 2-pass summarizer with mocked Claude API."""
import json
from unittest.mock import MagicMock, call, patch

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

MOCK_ANALYSIS_JSON = {
    "properties": {
        "title": "20260319_내부_업무보고체계논의",
        "type": "회의",
        "project": "",
        "client": "내부",
        "related_people": ["김대표", "이부장"],
        "tags": ["업무보고", "조직문화"],
        "priority": "중요",
        "status": "후속필요",
        "summary": "업무 보고 체계와 조직 문화에 대해 논의했습니다.",
        "next_actions": "1. 보고서 양식 정리\n2. 미팅 일정 확인",
        "participants": "김대표, 이부장",
    },
    "content": {
        "core_summary": "업무 보고 체계 개선과 조직 문화 변화에 대해 심도있는 논의가 이루어졌습니다.",
        "meeting_overview": {
            "date": "2026-03-19",
            "participants": ["김대표", "이부장"],
            "type": "1:1 미팅",
            "location": "",
        },
        "discussions": [
            {
                "topic": "업무 보고 체계",
                "sub_topics": [
                    {
                        "title": "현행 보고 절차",
                        "points": ["현재 주간 보고가 비효율적이라는 의견이 나왔다."],
                        "quotes": ["김대표: '보고서 양식을 간소화해야 한다'"],
                    }
                ],
            }
        ],
        "key_persons": [
            {"name": "김대표", "observations": ["효율성을 중시하는 성향"]}
        ],
        "risks": ["보고 체계 변경 시 혼란 가능성"],
        "strategic_insights": ["단계적 도입이 혼란을 최소화할 수 있다"],
        "action_items": [
            {"assignee": "이부장", "task": "보고서 양식 정리", "deadline": "금요일"}
        ],
    },
}

MOCK_SETTINGS = MagicMock(
    anthropic_api_key="test-key",
    claude_model="claude-sonnet-4-5-20250929",
    claude_max_tokens=16384,
)


@pytest.fixture(autouse=True)
def mock_settings():
    with patch("src.summarizer.get_settings", return_value=MOCK_SETTINGS):
        yield


@pytest.fixture
def mock_claude():
    """Mock Claude to return analysis JSON."""
    with patch("src.summarizer._get_client") as mock_get:
        mock_client = MagicMock()
        mock_get.return_value = mock_client
        yield mock_client


class TestCorrectTranscript:
    def test_returns_corrected_text(self, mock_claude):
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="교정된 전사본 텍스트")]
        )
        result = correct_transcript("원본 전사본 텍스트")
        assert result == "교정된 전사본 텍스트"

    def test_empty_text_returns_as_is(self, mock_claude):
        result = correct_transcript("")
        assert result == ""
        mock_claude.messages.create.assert_not_called()

    def test_passes_correction_prompt(self, mock_claude):
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="결과")]
        )
        correct_transcript("테스트 입력")
        call_args = mock_claude.messages.create.call_args
        system = call_args.kwargs["system"]
        assert "음성인식" in system
        assert "교정" in system


class TestAnalyzeTranscript:
    def test_returns_meeting_analysis(self, mock_claude):
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text=json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False))]
        )
        result = analyze_transcript("테스트 전사본", 300.0, "2026-03-19")
        assert isinstance(result, MeetingAnalysis)
        assert result.properties.title == "20260319_내부_업무보고체계논의"
        assert result.properties.meeting_type == "회의"
        assert len(result.properties.related_people) == 2
        assert len(result.content.discussions) == 1
        assert len(result.content.strategic_insights) == 1

    def test_empty_transcript_raises_error(self):
        with pytest.raises(SummaryError, match="Empty transcript"):
            analyze_transcript("", 60.0)

    def test_invalid_json_raises_error(self, mock_claude):
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="not valid json")]
        )
        with pytest.raises(SummaryError, match="invalid JSON"):
            analyze_transcript("test input", 60.0)

    def test_includes_duration_and_date(self, mock_claude):
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text=json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False))]
        )
        analyze_transcript("테스트", 600.0, "2026-03-20")
        user_msg = mock_claude.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "10.0분" in user_msg
        assert "2026-03-20" in user_msg

    def test_strips_code_fences(self, mock_claude):
        fenced = "```json\n" + json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False) + "\n```"
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text=fenced)]
        )
        result = analyze_transcript("테스트", 300.0)
        assert result.properties.title == "20260319_내부_업무보고체계논의"


class TestSummarizeTranscript:
    def test_calls_both_passes(self, mock_claude):
        """summarize_transcript should call Claude twice (correction + analysis)."""
        mock_claude.messages.create.side_effect = [
            MagicMock(content=[MagicMock(text="교정된 텍스트")]),
            MagicMock(content=[MagicMock(text=json.dumps(MOCK_ANALYSIS_JSON, ensure_ascii=False))]),
        ]
        result = summarize_transcript("원본 텍스트", 300.0, "2026-03-19")
        assert mock_claude.messages.create.call_count == 2
        assert isinstance(result, MeetingAnalysis)

    def test_rate_limit_raises_retryable(self, mock_claude):
        import anthropic as _anthropic
        mock_claude.messages.create.side_effect = _anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        with pytest.raises(RetryableError):
            summarize_transcript("test", 60.0)
