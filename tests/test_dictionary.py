"""Tests for proper noun dictionary loader."""
from unittest.mock import MagicMock, patch

import pytest

from src.dictionary import _load_custom_terms, load_dictionary

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
        with (
            patch("src.dictionary._get_client", side_effect=Exception("fail")),
            patch("src.dictionary.get_settings", return_value=MOCK_SETTINGS),
            patch("src.dictionary._load_custom_terms", return_value=[]),
        ):
            result = load_dictionary()
        assert result == ""
