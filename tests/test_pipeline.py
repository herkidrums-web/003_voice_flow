"""Tests for pipeline orchestration."""
from unittest.mock import MagicMock, patch

import pytest

from src.pipeline import is_processed, mark_processed, process_file, wait_for_file_stability

MOCK_SETTINGS = MagicMock(
    watch_dir="/tmp/test_watch",
    processed_log="processed.log",
    file_stability_interval=0.01,
    file_stability_checks=3,
    file_stability_timeout=1.0,
    retry_max_attempts=2,
    retry_base_delay=0.01,
    retry_max_delay=0.05,
)


@pytest.fixture(autouse=True)
def mock_settings():
    with patch("src.pipeline.get_settings", return_value=MOCK_SETTINGS):
        yield


class TestIsProcessed:
    def test_false_when_no_log(self, tmp_path):
        with patch("src.pipeline._get_processed_log_path", return_value=tmp_path / "processed.log"):
            assert is_processed(str(tmp_path / "test.m4a")) is False

    def test_true_when_in_log(self, tmp_path):
        log_file = tmp_path / "processed.log"
        log_file.write_text("test.m4a\n")
        with patch("src.pipeline._get_processed_log_path", return_value=log_file):
            assert is_processed(str(tmp_path / "test.m4a")) is True

    def test_false_for_different_file(self, tmp_path):
        log_file = tmp_path / "processed.log"
        log_file.write_text("other.m4a\n")
        with patch("src.pipeline._get_processed_log_path", return_value=log_file):
            assert is_processed(str(tmp_path / "test.m4a")) is False


class TestMarkProcessed:
    def test_appends_filename(self, tmp_path):
        log_file = tmp_path / "processed.log"
        with patch("src.pipeline._get_processed_log_path", return_value=log_file):
            mark_processed(str(tmp_path / "test.m4a"))
            content = log_file.read_text()
            assert "test.m4a" in content


class TestWaitForFileStability:
    def test_stable_file_returns_true(self, tmp_path):
        f = tmp_path / "test.m4a"
        f.write_bytes(b"x" * 1000)
        settings = MagicMock(
            file_stability_interval=0.01,
            file_stability_checks=3,
            file_stability_timeout=1.0,
        )
        with patch("src.pipeline.get_settings", return_value=settings):
            assert wait_for_file_stability(str(f)) is True

    def test_missing_file_returns_false(self):
        settings = MagicMock(
            file_stability_interval=0.01,
            file_stability_checks=3,
            file_stability_timeout=0.1,
        )
        with patch("src.pipeline.get_settings", return_value=settings):
            assert wait_for_file_stability("/nonexistent/file.m4a") is False


class TestProcessFile:
    def test_skips_non_m4a(self):
        assert process_file("/tmp/test.txt") is None

    def test_skips_already_processed(self, tmp_path):
        log_file = tmp_path / "processed.log"
        log_file.write_text("test.m4a\n")
        audio = tmp_path / "test.m4a"
        audio.write_bytes(b"fake audio")
        with patch("src.pipeline._get_processed_log_path", return_value=log_file):
            assert process_file(str(audio)) is None
