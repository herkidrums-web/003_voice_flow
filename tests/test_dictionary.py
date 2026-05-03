"""Tests for proper noun dictionary loader."""
from unittest.mock import MagicMock, patch

import pytest

from src.dictionary import _load_always_terms, _load_custom_terms, load_dictionary

MOCK_SETTINGS = MagicMock(
    notion_api_key="test-key",
    notion_database_id="test-db-id",
)


class TestLoadCustomTerms:
    def test_loads_terms_from_file(self, tmp_path):
        terms_file = tmp_path / "custom_terms.txt"
        terms_file.write_text("유플러스\n캡스톤파트너스\n# 주석\n\n인천도화")
        with patch("src.dictionary.CUSTOM_TERMS_FILE", terms_file):
            terms = _load_custom_terms()
        assert terms == ["유플러스", "캡스톤파트너스", "인천도화"]

    def test_empty_when_no_file(self, tmp_path):
        with patch("src.dictionary.CUSTOM_TERMS_FILE", tmp_path / "nonexistent.txt"):
            terms = _load_custom_terms()
        assert terms == []


class TestLoadAlwaysTerms:
    def test_extracts_always_section(self, tmp_path):
        content = (
            "# ============================================\n"
            "# [ALWAYS] 핵심 인명\n"
            "# ============================================\n"
            "권용현 부사장\n"
            "김유일 부회장\n"
            "\n"
            "# ============================================\n"
            "# 일반 카테고리\n"
            "# ============================================\n"
            "그외용어1\n"
            "그외용어2\n"
        )
        terms_file = tmp_path / "custom_terms.txt"
        terms_file.write_text(content)
        with patch("src.dictionary.CUSTOM_TERMS_FILE", terms_file):
            always = _load_always_terms()
        assert always == ["권용현 부사장", "김유일 부회장"]

    def test_no_always_section_returns_empty(self, tmp_path):
        terms_file = tmp_path / "custom_terms.txt"
        terms_file.write_text("# 일반\n용어1\n용어2")
        with patch("src.dictionary.CUSTOM_TERMS_FILE", terms_file):
            assert _load_always_terms() == []

    def test_subsection_comments_dont_break_always_mode(self, tmp_path):
        """서브 주석(# 당사 임원)은 헤더 블록이 아니므로 모드를 끄지 않아야 한다."""
        content = (
            "# ============================================\n"
            "# [ALWAYS] 핵심 인명\n"
            "# ============================================\n"
            "# 당사 임원\n"
            "권용현 부사장\n"
            "안형균 그룹장\n"
            "# 코람코\n"
            "김유일 부회장\n"
            "김태원 대표\n"
            "\n"
            "# ============================================\n"
            "# 일반 카테고리\n"
            "# ============================================\n"
            "# 서브주석2\n"
            "그외용어1\n"
        )
        terms_file = tmp_path / "custom_terms.txt"
        terms_file.write_text(content)
        with patch("src.dictionary.CUSTOM_TERMS_FILE", terms_file):
            always = _load_always_terms()
        assert always == ["권용현 부사장", "안형균 그룹장", "김유일 부회장", "김태원 대표"]
        assert "그외용어1" not in always


class TestLoadDictionary:
    def test_combines_all_sources(self):
        mock_ds_record = {
            "properties": {
                "관련인물": {
                    "type": "multi_select",
                    "multi_select": {"options": [{"name": "김대표"}, {"name": "이부장"}]},
                },
                "고객명": {
                    "type": "select",
                    "select": {"options": [{"name": "토스"}, {"name": "KT"}]},
                },
            }
        }
        mock_ds_contacts = {
            "properties": {
                "회사명": {
                    "type": "select",
                    "select": {"options": [{"name": "넷마블"}]},
                },
            }
        }
        mock_query = {
            "results": [
                {"properties": {"이름": {"title": [{"plain_text": "홍길동"}]}}},
                {"properties": {"이름": {"title": [{"plain_text": "John"}]}}},  # English, skipped
            ],
            "has_more": False,
        }

        mock_client = MagicMock()
        mock_client.data_sources.retrieve.side_effect = [mock_ds_record, mock_ds_contacts]
        mock_client.data_sources.query.return_value = mock_query

        with (
            patch("src.dictionary._get_client", return_value=mock_client),
            patch("src.dictionary.get_settings", return_value=MOCK_SETTINGS),
            patch("src.dictionary._load_custom_terms", return_value=["유플러스"]),
        ):
            result = load_dictionary()

        assert "김대표" in result
        assert "토스" in result
        assert "홍길동" in result
        assert "John" not in result
        assert "넷마블" in result
        assert "유플러스" in result

    def test_returns_empty_on_failure(self):
        import src.dictionary as _mod
        _mod._notion_cache = None  # 이전 테스트 캐시 초기화
        with (
            patch("src.dictionary._get_client", side_effect=Exception("fail")),
            patch("src.dictionary.get_settings", return_value=MOCK_SETTINGS),
            patch("src.dictionary._load_custom_terms", return_value=[]),
            patch("src.dictionary._load_always_terms", return_value=[]),
        ):
            result = load_dictionary()
        assert result == ""

    def test_always_terms_injected_even_without_transcript_match(self):
        """[ALWAYS] 항목은 transcript에 없어도 사전에 주입돼야 한다 — STT 인명 오인식 방어 핵심."""
        import src.dictionary as _mod
        _mod._notion_cache = None
        with (
            patch("src.dictionary._get_client", side_effect=Exception("no notion")),
            patch("src.dictionary.get_settings", return_value=MOCK_SETTINGS),
            patch("src.dictionary._load_custom_terms", return_value=["가나다라마바", "잘안나오는용어"]),
            patch(
                "src.dictionary._load_always_terms",
                return_value=["권용현 부사장", "김유일 부회장", "김태원 대표"],
            ),
        ):
            # transcript에 always 항목과 1글자도 안 겹치는 텍스트 — 매칭으로는 절대 안 잡힘
            result = load_dictionary(transcript="원혁명 상무가 김태현 사장을 만났습니다")
        assert "권용현 부사장" in result
        assert "김유일 부회장" in result
        assert "김태원 대표" in result
        assert "핵심인명" in result  # 별도 섹션 헤더 확인

    def test_always_terms_not_duplicated_in_regular_section(self):
        """[ALWAYS] 항목은 일반 추가용어 섹션에 중복으로 들어가지 않는다."""
        import src.dictionary as _mod
        _mod._notion_cache = None
        with (
            patch("src.dictionary._get_client", side_effect=Exception("no notion")),
            patch("src.dictionary.get_settings", return_value=MOCK_SETTINGS),
            patch(
                "src.dictionary._load_custom_terms",
                return_value=["권용현 부사장", "기타용어"],
            ),
            patch(
                "src.dictionary._load_always_terms",
                return_value=["권용현 부사장"],
            ),
        ):
            result = load_dictionary(transcript="권용현 부사장 미팅")
        # 핵심인명 섹션과 추가용어 섹션 양쪽에서 나오면 안 됨
        assert result.count("권용현 부사장") == 1
