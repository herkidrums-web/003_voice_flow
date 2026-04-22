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
