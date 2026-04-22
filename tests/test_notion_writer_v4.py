"""notion_writer v4 페이지 구조 테스트."""
from src.notion_writer import _build_page_children
from src.summarizer import MeetingAnalysis, MeetingProperties, MeetingContent


def test_page_has_topic_sections():
    """페이지에 토픽별 섹션이 있는지 확인."""
    analysis = _make_test_analysis()
    blocks = _build_page_children(analysis, None, "test.m4a")
    headings = [b for b in blocks if b.get("type") == "heading_2"]
    heading_texts = [_extract_text(b) for b in headings]
    assert any("카카오 AP" in t for t in heading_texts)


def test_page_has_quotes():
    """토픽 섹션에 인용문이 있는지 확인."""
    analysis = _make_test_analysis()
    blocks = _build_page_children(analysis, None, "test.m4a")
    quote_blocks = [b for b in blocks if b.get("type") == "quote"]
    assert len(quote_blocks) >= 1


def test_page_has_5w1h():
    """토픽 섹션에 육하원칙 테이블이 있는지 확인."""
    analysis = _make_test_analysis()
    blocks = _build_page_children(analysis, None, "test.m4a")
    table_blocks = [b for b in blocks if b.get("type") == "table"]
    assert len(table_blocks) >= 1


def test_page_handles_none_transcript():
    """transcript가 None이어도 동작하는지 확인."""
    analysis = _make_test_analysis()
    blocks = _build_page_children(analysis, None, "test.m4a")
    assert len(blocks) > 0


def test_page_has_action_items():
    """액션 아이템 섹션이 있는지 확인."""
    analysis = _make_test_analysis()
    blocks = _build_page_children(analysis, None, "test.m4a")
    todo_blocks = [b for b in blocks if b.get("type") == "to_do"]
    assert len(todo_blocks) >= 1


def _make_test_analysis():
    props = MeetingProperties(
        title="테스트", date="2026-04-22",
        summary="테스트 요약", participants="이성우",
        meeting_type="회의",
    )
    content = MeetingContent(
        executive_summary="핵심 요약입니다.",
        topics=[{
            "name": "카카오 AP",
            "when": "2026-04-22", "where": "사무실",
            "who": "이성우, 김대리", "what": "AP 전략 논의",
            "why": "Q 한계", "how": "P 전략 전환",
            "key_facts": [{"content": "단가 인상 필요", "original_quote": "단가를 올려야 합니다"}],
            "quotes": ["단가를 올려야 합니다", "평촌이 레버리지입니다"],
            "action_items": [{"task": "DART 수집", "owner": "이성우", "deadline": "4월 말"}],
            "insights": ["P 전략이 핵심"],
            "coaching_points": ["가격 협상 시 레버리지 활용"],
            "so_what": "카카오 P 전략 전환이 Q 한계 극복의 열쇠",
        }],
        decisions=["P 전략 전환"],
        risks=["가격 저항"],
        action_items=[{"task": "DART 수집", "owner": "이성우", "deadline": "4월 말"}],
    )
    return MeetingAnalysis(properties=props, content=content)


def _extract_text(block):
    """블록에서 텍스트 추출."""
    btype = block.get("type", "")
    inner = block.get(btype, {})
    rich_text = inner.get("rich_text", [])
    return "".join(rt.get("text", {}).get("content", "") for rt in rich_text)
