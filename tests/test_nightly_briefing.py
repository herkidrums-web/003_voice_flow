"""Tests for scripts/nightly_briefing.py — sync stale fallback drain 로직."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# scripts/ 디렉터리를 path에 추가
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import nightly_briefing  # noqa: E402


# ---------------------------------------------------------------------------
# 헬퍼: processing_state.jsonl 작성
# ---------------------------------------------------------------------------

def _write_state(state_path: Path, entries: list[dict]) -> None:
    with state_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _make_m4a(watch_dir: Path, name: str) -> Path:
    p = watch_dir / name
    p.write_bytes(b"fake m4a")
    return p


# ---------------------------------------------------------------------------
# _count_pending_in_mirror 유닛 테스트
# ---------------------------------------------------------------------------

class TestCountPendingInMirror:
    """미처리 파일 카운터 정확도."""

    def test_no_mirror_dir_returns_zero(self, tmp_path):
        missing = tmp_path / "nonexistent"
        state = tmp_path / "state.jsonl"
        assert nightly_briefing._count_pending_in_mirror(missing, state) == 0

    def test_all_done_returns_zero(self, tmp_path):
        watch = tmp_path / "mirror"
        watch.mkdir()
        _make_m4a(watch, "file1.m4a")
        state = tmp_path / "state.jsonl"
        _write_state(state, [
            {"file": "file1.m4a", "stage": "wiki", "status": "done", "ts": "2026-05-24T00:00:00"},
        ])
        assert nightly_briefing._count_pending_in_mirror(watch, state) == 0

    def test_undone_file_counted(self, tmp_path):
        watch = tmp_path / "mirror"
        watch.mkdir()
        _make_m4a(watch, "file1.m4a")
        state = tmp_path / "state.jsonl"
        _write_state(state, [
            {"file": "file1.m4a", "stage": "stt", "status": "done", "ts": "2026-05-24T00:00:00"},
        ])
        assert nightly_briefing._count_pending_in_mirror(watch, state) == 1

    def test_no_state_entry_counted_as_pending(self, tmp_path):
        watch = tmp_path / "mirror"
        watch.mkdir()
        _make_m4a(watch, "new_file.m4a")
        state = tmp_path / "state.jsonl"
        state.write_text("", encoding="utf-8")
        assert nightly_briefing._count_pending_in_mirror(watch, state) == 1

    def test_too_long_excluded(self, tmp_path):
        watch = tmp_path / "mirror"
        watch.mkdir()
        _make_m4a(watch, "long.m4a")
        state = tmp_path / "state.jsonl"
        _write_state(state, [
            {"file": "long.m4a", "stage": "none", "status": "too_long", "ts": "2026-05-24T00:00:00"},
        ])
        # too_long은 미처리로 카운트 안 함
        assert nightly_briefing._count_pending_in_mirror(watch, state) == 0

    def test_multiple_entries_latest_wins(self, tmp_path):
        """같은 파일의 여러 state entry 중 최신 ts만 유효."""
        watch = tmp_path / "mirror"
        watch.mkdir()
        _make_m4a(watch, "f.m4a")
        state = tmp_path / "state.jsonl"
        # 먼저 stt:done, 이후 wiki:done
        _write_state(state, [
            {"file": "f.m4a", "stage": "stt", "status": "done", "ts": "2026-05-24T01:00:00"},
            {"file": "f.m4a", "stage": "wiki", "status": "done", "ts": "2026-05-24T02:00:00"},
        ])
        assert nightly_briefing._count_pending_in_mirror(watch, state) == 0


# ---------------------------------------------------------------------------
# sync stale fallback drain 경로 테스트
# ---------------------------------------------------------------------------

class TestNightlyStaleFallback:
    """단계 3 핵심 로직 — sync stale + mirror 미처리 → drain 시도."""

    def _patch_nightly(self, tmp_path, sync_log_age_h: float, n_pending: int):
        """공통 픽스처 설정 헬퍼."""
        sync_log = tmp_path / "voiceflow-sync.log"
        sync_log.write_text("stale", encoding="utf-8")
        stale_mtime = time.time() - sync_log_age_h * 3600
        os.utime(sync_log, (stale_mtime, stale_mtime))

        watch_dir = tmp_path / "mirror"
        watch_dir.mkdir()
        state_path = tmp_path / "state.jsonl"

        for i in range(n_pending):
            _make_m4a(watch_dir, f"pending_{i}.m4a")
        # state 비워두기 (전부 미처리)
        state_path.write_text("", encoding="utf-8")

        return sync_log, watch_dir, state_path

    def test_stale_with_pending_files_attempts_drain(self, tmp_path):
        """sync stale + mirror 미처리 N건 → drain 경로 진입."""
        sync_log, watch_dir, state_path = self._patch_nightly(
            tmp_path, sync_log_age_h=20.0, n_pending=2
        )

        drain_called = []
        discord_msgs = []

        orig_sync_log = nightly_briefing.SYNC_LOG
        orig_threshold = nightly_briefing.STALE_THRESHOLD_HOURS
        orig_proj_root = nightly_briefing.PROJECT_ROOT

        try:
            nightly_briefing.SYNC_LOG = sync_log
            nightly_briefing.STALE_THRESHOLD_HOURS = 12.0
            # _run_drain을 mock
            orig_run_drain = nightly_briefing._run_drain
            nightly_briefing._run_drain = lambda: (drain_called.append(1), "ok")[1]

            orig_count = nightly_briefing._count_pending_in_mirror
            nightly_briefing._count_pending_in_mirror = lambda w, s: 2

            orig_notify = nightly_briefing._notify_discord
            nightly_briefing._notify_discord = lambda msg: discord_msgs.append(msg)

            # subprocess 호출 차단 (morning_briefing 실행 방지)
            orig_subprocess = nightly_briefing.subprocess
            import subprocess as _sp
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.stderr = ""
            nightly_briefing.subprocess = MagicMock()
            nightly_briefing.subprocess.run = MagicMock(return_value=mock_result)
            nightly_briefing.subprocess.TimeoutExpired = _sp.TimeoutExpired

            nightly_briefing.main()
        finally:
            nightly_briefing.SYNC_LOG = orig_sync_log
            nightly_briefing.STALE_THRESHOLD_HOURS = orig_threshold
            nightly_briefing._run_drain = orig_run_drain
            nightly_briefing._count_pending_in_mirror = orig_count
            nightly_briefing._notify_discord = orig_notify
            nightly_briefing.subprocess = _sp

        assert len(drain_called) >= 1, "drain이 호출돼야 한다"
        # Discord 경고 메시지에 '처리 시도' 포함 여부
        assert any("처리 시도" in m for m in discord_msgs), \
            f"'처리 시도' 메시지 없음. 발송: {discord_msgs}"

    def test_stale_no_pending_files_skips_drain(self, tmp_path):
        """sync stale + mirror 미처리 0건 → drain 스킵, 브리핑만 실행."""
        sync_log, watch_dir, state_path = self._patch_nightly(
            tmp_path, sync_log_age_h=20.0, n_pending=0
        )

        drain_called = []
        discord_msgs = []

        orig_sync_log = nightly_briefing.SYNC_LOG
        orig_threshold = nightly_briefing.STALE_THRESHOLD_HOURS

        try:
            nightly_briefing.SYNC_LOG = sync_log
            nightly_briefing.STALE_THRESHOLD_HOURS = 12.0

            orig_run_drain = nightly_briefing._run_drain
            nightly_briefing._run_drain = lambda: (drain_called.append(1), ("ok", ""))[1]

            orig_count = nightly_briefing._count_pending_in_mirror
            nightly_briefing._count_pending_in_mirror = lambda w, s: 0

            orig_notify = nightly_briefing._notify_discord
            nightly_briefing._notify_discord = lambda msg: discord_msgs.append(msg)

            import subprocess as _sp
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.stderr = ""
            nightly_briefing.subprocess = MagicMock()
            nightly_briefing.subprocess.run = MagicMock(return_value=mock_result)
            nightly_briefing.subprocess.TimeoutExpired = _sp.TimeoutExpired

            nightly_briefing.main()
        finally:
            nightly_briefing.SYNC_LOG = orig_sync_log
            nightly_briefing.STALE_THRESHOLD_HOURS = orig_threshold
            nightly_briefing._run_drain = orig_run_drain
            nightly_briefing._count_pending_in_mirror = orig_count
            nightly_briefing._notify_discord = orig_notify
            nightly_briefing.subprocess = _sp

        assert len(drain_called) == 0, "미처리 0건이면 drain 호출 금지"
        # Discord 경고는 있어야 함 (stale 알림)
        assert any("0건" in m or "미처리" in m for m in discord_msgs), \
            f"stale 0건 알림 없음. 발송: {discord_msgs}"

    def test_mtime_restored_after_drain(self, tmp_path):
        """force drain 완료 후 sync.log mtime이 원복돼야 한다."""
        sync_log, watch_dir, state_path = self._patch_nightly(
            tmp_path, sync_log_age_h=20.0, n_pending=1
        )

        original_mtime = sync_log.stat().st_mtime

        orig_sync_log = nightly_briefing.SYNC_LOG
        orig_threshold = nightly_briefing.STALE_THRESHOLD_HOURS

        import subprocess as _sp
        mock_result = MagicMock()
        mock_result.stdout = "no pending files"
        mock_result.stderr = ""

        try:
            nightly_briefing.SYNC_LOG = sync_log
            nightly_briefing.STALE_THRESHOLD_HOURS = 12.0

            orig_count = nightly_briefing._count_pending_in_mirror
            nightly_briefing._count_pending_in_mirror = lambda w, s: 1

            orig_notify = nightly_briefing._notify_discord
            nightly_briefing._notify_discord = lambda msg: None

            nightly_briefing.subprocess = MagicMock()
            nightly_briefing.subprocess.run = MagicMock(return_value=mock_result)
            nightly_briefing.subprocess.TimeoutExpired = _sp.TimeoutExpired

            nightly_briefing.main()
        finally:
            nightly_briefing.SYNC_LOG = orig_sync_log
            nightly_briefing.STALE_THRESHOLD_HOURS = orig_threshold
            nightly_briefing._count_pending_in_mirror = orig_count
            nightly_briefing._notify_discord = orig_notify
            nightly_briefing.subprocess = _sp

        restored_mtime = sync_log.stat().st_mtime
        # 원복 허용 오차: 1초
        assert abs(restored_mtime - original_mtime) < 1.0, (
            f"mtime 원복 실패: original={original_mtime:.3f}, restored={restored_mtime:.3f}"
        )
