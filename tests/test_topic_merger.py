"""토픽 병합 테스트."""
import pytest
from src.topic_merger import merge_analyses_by_topic


def test_no_merge_different_topics():
    """완전히 다른 토픽은 병합하지 않음."""
    analyses = [
        _make_analysis(topics=["카카오 AP"], title="카카오"),
        _make_analysis(topics=["부사장 1:1"], title="부사장"),
    ]
    result = merge_analyses_by_topic(analyses)
    assert len(result) == 2


def test_merge_same_topic():
    """동일 토픽은 병합."""
    analyses = [
        _make_analysis(topics=["카카오 AP"], title="카카오1"),
        _make_analysis(topics=["카카오 AP 후속"], title="카카오2"),
    ]
    result = merge_analyses_by_topic(analyses)
    assert len(result) == 1
    assert len(result[0].content.topics) >= 2


def test_partial_merge():
    """일부만 겹치면 겹치는 것만 병합."""
    analyses = [
        _make_analysis(topics=["카카오 AP", "넷마블 IDC"], title="혼합1"),
        _make_analysis(topics=["카카오 AP"], title="카카오"),
        _make_analysis(topics=["부사장 1:1"], title="부사장"),
    ]
    result = merge_analyses_by_topic(analyses)
    assert len(result) == 2


def test_empty_input():
    """빈 입력."""
    result = merge_analyses_by_topic([])
    assert result == []


def test_single_analysis():
    """단일 분석은 그대로."""
    analyses = [_make_analysis(topics=["토스"], title="토스")]
    result = merge_analyses_by_topic(analyses)
    assert len(result) == 1


def test_merge_by_client():
    """동일 고객명이면 병합."""
    analyses = [
        _make_analysis(topics=["토스 IDC"], title="토스1", client="토스"),
        _make_analysis(topics=["토스 마이그레이션"], title="토스2", client="토스"),
    ]
    result = merge_analyses_by_topic(analyses)
    assert len(result) == 1


def test_no_merge_different_clients():
    """다른 고객명이면 병합하지 않음 (토픽도 다를 때)."""
    analyses = [
        _make_analysis(topics=["IDC 계약"], title="카카오", client="카카오"),
        _make_analysis(topics=["DBO 검토"], title="코람코", client="코람코"),
    ]
    result = merge_analyses_by_topic(analyses)
    assert len(result) == 2


def _make_analysis(topics: list[str], title: str = "test", client: str = ""):
    """테스트용 MeetingAnalysis 헬퍼."""
    from src.summarizer import MeetingAnalysis, MeetingProperties, MeetingContent
    props = MeetingProperties(title=title, topics=topics, client=client)
    content = MeetingContent(
        topics=[{"name": t, "what": f"{t} 내용", "quotes": [f"{t} 인용"]} for t in topics]
    )
    return MeetingAnalysis(properties=props, content=content)
