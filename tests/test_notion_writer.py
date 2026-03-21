"""Tests for Notion writer with mocked Notion client."""
from unittest.mock import MagicMock, patch

import pytest

from src.notion_writer import _build_db_properties, _build_page_children, create_meeting_note
from src.stt import TranscriptResult, TranscriptSegment
from src.summarizer import MeetingAnalysis, MeetingContent, MeetingProperties

MOCK_SETTINGS = MagicMock(
    notion_api_key="test-key",
    notion_database_id="test-db-id",
)


@pytest.fixture
def sample_analysis():
    return MeetingAnalysis(
        properties=MeetingProperties(
            title="20260319_내부_업무보고논의",
            meeting_type="회의",
            project="",
            client="내부",
            related_people=["김대표", "이부장"],
            tags=["업무보고", "조직문화"],
            priority="중요",
            status="후속필요",
            summary="업무 보고 체계에 대해 논의했습니다.",
            next_actions="1. 보고서 양식 정리",
            participants="김대표, 이부장",
            date="2026-03-19",
        ),
        content=MeetingContent(
            core_summary="핵심 요약 내용입니다.",
            meeting_overview={
                "date": "2026-03-19",
                "participants": ["김대표", "이부장"],
                "type": "1:1 미팅",
                "location": "",
            },
            discussions=[
                {
                    "topic": "업무 보고 체계",
                    "sub_topics": [
                        {
                            "title": "현행 보고 절차",
                            "points": ["현재 주간 보고가 비효율적이라는 의견"],
                            "quotes": ["김대표: '양식을 간소화해야 한다'"],
                        }
                    ],
                }
            ],
            key_persons=[
                {"name": "김대표", "observations": ["효율성 중시"]}
            ],
            risks=["보고 체계 변경 시 혼란"],
            strategic_insights=["단계적 도입 추천"],
            action_items=[
                {"assignee": "이부장", "task": "보고서 양식 정리", "deadline": "금요일"}
            ],
        ),
    )


@pytest.fixture
def sample_transcript():
    return TranscriptResult(
        segments=[
            TranscriptSegment(start=0, end=5, speaker="Speaker", text="안녕하세요"),
        ],
        duration=300.0,
        speaker_count=1,
        full_text="안녕하세요",
    )


class TestBuildPageChildren:
    def test_contains_required_sections(self, sample_analysis, sample_transcript):
        blocks = _build_page_children(sample_analysis, sample_transcript, "test.m4a")
        heading_texts = [
            b["heading_2"]["rich_text"][0]["text"]["content"]
            for b in blocks
            if b["type"] == "heading_2"
        ]
        assert "핵심 요약" in heading_texts
        assert "미팅 개요" in heading_texts
        assert "업무 보고 체계" in heading_texts  # discussion topic
        assert "키맨 프로파일" in heading_texts
        assert "리스크 & 불확실성" in heading_texts
        assert "전략적 인사이트" in heading_texts
        assert "다음 액션" in heading_texts

    def test_contains_heading3_subtopics(self, sample_analysis, sample_transcript):
        blocks = _build_page_children(sample_analysis, sample_transcript, "test.m4a")
        h3_texts = [
            b["heading_3"]["rich_text"][0]["text"]["content"]
            for b in blocks
            if b["type"] == "heading_3"
        ]
        assert "현행 보고 절차" in h3_texts
        assert "김대표" in h3_texts  # key person

    def test_contains_quotes(self, sample_analysis, sample_transcript):
        blocks = _build_page_children(sample_analysis, sample_transcript, "test.m4a")
        quote_texts = [
            b["quote"]["rich_text"][0]["text"]["content"]
            for b in blocks
            if b["type"] == "quote"
        ]
        assert any("양식을 간소화" in q for q in quote_texts)

    def test_contains_numbered_insights(self, sample_analysis, sample_transcript):
        blocks = _build_page_children(sample_analysis, sample_transcript, "test.m4a")
        numbered = [
            b["numbered_list_item"]["rich_text"][0]["text"]["content"]
            for b in blocks
            if b["type"] == "numbered_list_item"
        ]
        assert any("단계적" in n for n in numbered)

    def test_empty_action_items(self, sample_transcript):
        analysis = MeetingAnalysis(
            properties=MeetingProperties(title="테스트"),
            content=MeetingContent(
                core_summary="요약",
                meeting_overview={},
                discussions=[],
                action_items=[],
            ),
        )
        blocks = _build_page_children(analysis, sample_transcript, "test.m4a")
        paragraph_texts = [
            b["paragraph"]["rich_text"][0]["text"]["content"]
            for b in blocks
            if b["type"] == "paragraph"
        ]
        assert "(액션아이템 없음)" in paragraph_texts


class TestBuildDbProperties:
    def test_sets_title(self, sample_analysis):
        props = _build_db_properties(sample_analysis)
        assert props["회의 이름"]["title"][0]["text"]["content"] == "20260319_내부_업무보고논의"

    def test_sets_select_properties(self, sample_analysis):
        props = _build_db_properties(sample_analysis)
        assert props["유형"]["select"]["name"] == "회의"
        assert props["우선순위"]["select"]["name"] == "중요"
        assert props["상태"]["select"]["name"] == "후속필요"
        assert props["고객명"]["select"]["name"] == "내부"

    def test_sets_multi_select(self, sample_analysis):
        props = _build_db_properties(sample_analysis)
        people = [p["name"] for p in props["관련인물"]["multi_select"]]
        assert "김대표" in people
        assert "이부장" in people
        tags = [t["name"] for t in props["태그"]["multi_select"]]
        assert "업무보고" in tags

    def test_sets_date(self, sample_analysis):
        props = _build_db_properties(sample_analysis)
        assert props["날짜"]["date"]["start"] == "2026-03-19"

    def test_sets_rich_text(self, sample_analysis):
        props = _build_db_properties(sample_analysis)
        assert "업무 보고 체계" in props["요약"]["rich_text"][0]["text"]["content"]
        assert "보고서 양식" in props["다음액션"]["rich_text"][0]["text"]["content"]


class TestCreateMeetingNote:
    def test_creates_page_in_database(self, sample_analysis, sample_transcript):
        with (
            patch("src.notion_writer._get_client") as mock_get,
            patch("src.notion_writer.get_settings", return_value=MOCK_SETTINGS),
        ):
            mock_client = MagicMock()
            mock_client.pages.create.return_value = {"id": "test-page-id"}
            mock_get.return_value = mock_client

            page_id = create_meeting_note(
                sample_analysis, sample_transcript, "test.m4a"
            )

            assert page_id == "test-page-id"
            call_args = mock_client.pages.create.call_args
            assert call_args.kwargs["parent"]["database_id"] == "test-db-id"
            assert "회의 이름" in call_args.kwargs["properties"]
