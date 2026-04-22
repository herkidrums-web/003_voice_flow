"""summarizer v4 변경사항 테스트."""
import inspect
import pytest


def test_correction_prompt_no_speaker_tags():
    """CORRECTION_PROMPT에 화자분리 지시가 없는지 확인."""
    from src.summarizer import CORRECTION_PROMPT
    assert "화자분리" not in CORRECTION_PROMPT
    assert "[화자A]" not in CORRECTION_PROMPT
    # 화자 태그를 붙이라는 지시(긍정)가 없어야 함 — 금지 문구("추가하지")는 허용
    assert "화자 태그를 붙" not in CORRECTION_PROMPT
    assert "[이성우]" not in CORRECTION_PROMPT


def test_correct_transcript_no_participants_param():
    """correct_transcript에 participants_hint 파라미터가 없는지 확인."""
    from src.summarizer import correct_transcript
    sig = inspect.signature(correct_transcript)
    assert "participants_hint" not in sig.parameters


def test_summarize_transcript_no_participants_param():
    """summarize_transcript에 participants_hint 파라미터가 없는지 확인."""
    from src.summarizer import summarize_transcript
    sig = inspect.signature(summarize_transcript)
    assert "participants_hint" not in sig.parameters


def test_fact_extraction_prompt_has_topics_structure():
    """FACT_EXTRACTION_PROMPT가 토픽별 육하원칙 구조를 요구하는지 확인."""
    from src.summarizer import FACT_EXTRACTION_PROMPT
    assert "topics" in FACT_EXTRACTION_PROMPT
    assert "when" in FACT_EXTRACTION_PROMPT
    assert "where" in FACT_EXTRACTION_PROMPT
    assert "who" in FACT_EXTRACTION_PROMPT
    assert "what" in FACT_EXTRACTION_PROMPT
    assert "quotes" in FACT_EXTRACTION_PROMPT


def test_meeting_content_has_topics_field():
    """MeetingContent에 topics 필드가 있는지 확인."""
    from src.summarizer import MeetingContent
    mc = MeetingContent()
    assert hasattr(mc, "topics")
    assert isinstance(mc.topics, list)


def test_meeting_properties_has_topics_field():
    """MeetingProperties에 topics 필드가 있는지 확인."""
    from src.summarizer import MeetingProperties
    mp = MeetingProperties(title="test")
    assert hasattr(mp, "topics")
    assert isinstance(mp.topics, list)


def test_process_file_no_participants_param():
    """process_file에 participants_hint 파라미터가 없는지 확인."""
    import inspect
    from src.pipeline import process_file
    sig = inspect.signature(process_file)
    assert "participants_hint" not in sig.parameters


def test_process_file_group_no_participants_param():
    """process_file_group에 participants_hint 파라미터가 없는지 확인."""
    import inspect
    from src.pipeline import process_file_group
    sig = inspect.signature(process_file_group)
    assert "participants_hint" not in sig.parameters


def test_process_date_exists():
    """process_date 함수가 존재하는지 확인."""
    from src.pipeline import process_date
    import inspect
    sig = inspect.signature(process_date)
    assert "target_date" in sig.parameters
    assert "skip_indices" in sig.parameters
