"""Tests for pipeline orchestration."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.pipeline import (
    _parse_file_datetime,
    group_files,
    is_processed,
    mark_processed,
    process_file,
    wait_for_file_stability,
)

MOCK_SETTINGS = MagicMock(
    watch_dir="/tmp/test_watch",
    processed_log="processed.log",
    file_stability_interval=0.01,
    file_stability_checks=3,
    file_stability_timeout=1.0,
    retry_max_attempts=2,
    retry_base_delay=0.01,
    retry_max_delay=0.05,
    grouping_daytime_gap=300.0,
    grouping_evening_start_hour=17,
    grouping_debounce=60.0,
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


class TestParseFileDatetime:
    def test_standard_filename(self):
        from datetime import datetime
        dt = _parse_file_datetime("20260319 165322-8B01E3FB.m4a")
        assert dt == datetime(2026, 3, 19, 16, 53, 22)

    def test_filename_without_uuid(self):
        from datetime import datetime
        dt = _parse_file_datetime("20260320 103022.m4a")
        assert dt == datetime(2026, 3, 20, 10, 30, 22)

    def test_invalid_filename(self):
        assert _parse_file_datetime("notes.m4a") is None

    def test_short_filename(self):
        assert _parse_file_datetime("short.m4a") is None


class TestGroupFiles:
    def test_daytime_close_gap(self):
        """Files < 5 min apart during daytime → same group."""
        files = [
            Path("/rec/20260319 100000-AAAA.m4a"),
            Path("/rec/20260319 100300-BBBB.m4a"),  # 3 min later
        ]
        groups = group_files(files)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_daytime_far_gap(self):
        """Files >= 5 min apart during daytime → separate groups."""
        files = [
            Path("/rec/20260319 100000-AAAA.m4a"),
            Path("/rec/20260319 110000-BBBB.m4a"),  # 1 hour later
        ]
        groups = group_files(files)
        assert len(groups) == 2
        assert len(groups[0]) == 1
        assert len(groups[1]) == 1

    def test_evening_always_merge(self):
        """All evening files on same date → one group regardless of gap."""
        files = [
            Path("/rec/20260319 180000-AAAA.m4a"),
            Path("/rec/20260319 190000-BBBB.m4a"),  # 1 hour later
            Path("/rec/20260319 200000-CCCC.m4a"),  # 2 hours later
        ]
        groups = group_files(files)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_different_dates_separate(self):
        """Files on different dates → always separate."""
        files = [
            Path("/rec/20260319 190000-AAAA.m4a"),
            Path("/rec/20260320 190000-BBBB.m4a"),
        ]
        groups = group_files(files)
        assert len(groups) == 2

    def test_mixed_daytime_evening(self):
        """Daytime files separate, evening files merge."""
        files = [
            Path("/rec/20260319 100000-AAAA.m4a"),  # daytime
            Path("/rec/20260319 110000-BBBB.m4a"),  # daytime, 1hr gap → separate
            Path("/rec/20260319 180000-CCCC.m4a"),  # evening
            Path("/rec/20260319 193000-DDDD.m4a"),  # evening, merged
        ]
        groups = group_files(files)
        assert len(groups) == 3  # daytime1, daytime2, evening
        assert len(groups[2]) == 2  # evening group has 2 files

    def test_empty_input(self):
        assert group_files([]) == []

    def test_single_file(self):
        files = [Path("/rec/20260319 100000-AAAA.m4a")]
        groups = group_files(files)
        assert len(groups) == 1
        assert len(groups[0]) == 1
