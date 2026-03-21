"""Unit tests for STT pure functions (no model loading required)."""
from src.stt import (
    consolidate_segments,
    filter_segments,
    format_timestamp,
    merge_transcript_and_speakers,
)


class TestFilterSegments:
    def test_removes_empty_text(self):
        segments = [{"start": 0, "end": 1, "text": ""}]
        assert filter_segments(segments) == []

    def test_removes_whitespace_only(self):
        segments = [{"start": 0, "end": 1, "text": "   "}]
        assert filter_segments(segments) == []

    def test_removes_cyrillic_hallucination(self):
        segments = [{"start": 0, "end": 1, "text": "Привет мир"}]
        assert filter_segments(segments) == []

    def test_removes_single_char_filler(self):
        segments = [{"start": 0, "end": 1, "text": "아"}]
        assert filter_segments(segments) == []

    def test_keeps_valid_korean(self):
        segments = [{"start": 0, "end": 5, "text": "안녕하세요 회의를 시작하겠습니다"}]
        result = filter_segments(segments)
        assert len(result) == 1
        assert result[0]["text"] == "안녕하세요 회의를 시작하겠습니다"

    def test_removes_exact_consecutive_duplicates(self):
        segments = [
            {"start": 0, "end": 2, "text": "네 알겠습니다"},
            {"start": 2, "end": 4, "text": "네 알겠습니다"},
        ]
        result = filter_segments(segments)
        assert len(result) == 1
        assert result[0]["end"] == 4  # end time extended

    def test_removes_partial_repeat_hallucination(self):
        # Short prefix match: "hello" → "hello world"
        segments = [
            {"start": 0, "end": 2, "text": "네 알겠"},
            {"start": 2, "end": 4, "text": "네 알겠습니다"},
        ]
        result = filter_segments(segments)
        assert len(result) == 1
        assert result[0]["text"] == "네 알겠습니다"  # keeps longer version

    def test_empty_input(self):
        assert filter_segments([]) == []


class TestMergeTranscriptAndSpeakers:
    def test_assigns_speaker_by_max_overlap(self):
        transcript = [{"start": 0, "end": 5, "text": "안녕하세요"}]
        speakers = [
            {"start": 0, "end": 3, "speaker": "SPEAKER_00"},
            {"start": 3, "end": 6, "speaker": "SPEAKER_01"},
        ]
        result = merge_transcript_and_speakers(transcript, speakers)
        assert result[0]["speaker"] == "SPEAKER_00"  # 3s overlap vs 2s

    def test_unknown_when_no_overlap(self):
        transcript = [{"start": 10, "end": 15, "text": "늦은 발언"}]
        speakers = [{"start": 0, "end": 5, "speaker": "SPEAKER_00"}]
        result = merge_transcript_and_speakers(transcript, speakers)
        assert result[0]["speaker"] == "Unknown"


class TestConsolidateSegments:
    def test_merges_same_speaker_within_gap(self):
        segments = [
            {"start": 0, "end": 2, "speaker": "A", "text": "첫 번째"},
            {"start": 2.5, "end": 4, "speaker": "A", "text": "두 번째"},
        ]
        result = consolidate_segments(segments, gap_threshold=1.5)
        assert len(result) == 1
        assert "첫 번째 두 번째" in result[0]["text"]

    def test_keeps_different_speakers_separate(self):
        segments = [
            {"start": 0, "end": 2, "speaker": "A", "text": "발언 A"},
            {"start": 2.5, "end": 4, "speaker": "B", "text": "발언 B"},
        ]
        result = consolidate_segments(segments, gap_threshold=1.5)
        assert len(result) == 2

    def test_keeps_same_speaker_with_large_gap(self):
        segments = [
            {"start": 0, "end": 2, "speaker": "A", "text": "처음"},
            {"start": 10, "end": 12, "speaker": "A", "text": "나중"},
        ]
        result = consolidate_segments(segments, gap_threshold=1.5)
        assert len(result) == 2

    def test_empty_input(self):
        assert consolidate_segments([]) == []


class TestFormatTimestamp:
    def test_minutes_seconds(self):
        assert format_timestamp(125) == "02:05"

    def test_hours(self):
        assert format_timestamp(3661) == "01:01:01"

    def test_zero(self):
        assert format_timestamp(0) == "00:00"
