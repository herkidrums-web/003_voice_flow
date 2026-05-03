"""End-to-end smoke test — all sub-agents mocked, runs through scripts entry point."""
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_run_orchestrator_module_imports():
    """The entry script must at least import cleanly."""
    project_root = Path(__file__).resolve().parents[2]
    py = project_root / "poc" / ".venv" / "bin" / "python"
    result = subprocess.run(
        [str(py), "-c", "import scripts.run_orchestrator"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"import failed: {result.stderr}"


def test_sync_health_passes_when_log_recent(tmp_path):
    log = tmp_path / "sync.log"
    log.write_text("ok")
    from scripts.run_orchestrator import _check_sync_health
    ok, msg = _check_sync_health(log, max_age_hours=24)
    assert ok is True
    assert "fresh" in msg.lower()


def test_sync_health_fails_when_log_stale(tmp_path):
    log = tmp_path / "sync.log"
    log.write_text("ok")
    old = time.time() - 48 * 3600
    os.utime(log, (old, old))
    from scripts.run_orchestrator import _check_sync_health
    ok, msg = _check_sync_health(log, max_age_hours=24)
    assert ok is False
    assert "stale" in msg.lower()


def test_sync_health_fails_when_log_missing(tmp_path):
    from scripts.run_orchestrator import _check_sync_health
    ok, msg = _check_sync_health(tmp_path / "nope.log", max_age_hours=24)
    assert ok is False
    assert "not found" in msg.lower()


# ── max_duration guard tests ──────────────────────────────────────────────

def test_get_duration_minutes_returns_none_when_ffprobe_missing(tmp_path):
    """If ffprobe is not on PATH, _get_duration_minutes returns None gracefully."""
    f = tmp_path / "test.m4a"
    f.write_bytes(b"fake")
    from scripts.run_orchestrator import _get_duration_minutes
    with patch("subprocess.run", side_effect=FileNotFoundError("ffprobe not found")):
        result = _get_duration_minutes(f)
    assert result is None


def test_get_duration_minutes_parses_ffprobe_output(tmp_path):
    """Correctly parses ffprobe stdout (seconds) into minutes."""
    f = tmp_path / "test.m4a"
    f.write_bytes(b"fake")
    from scripts.run_orchestrator import _get_duration_minutes
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "7200.0\n"  # 2 hours in seconds
    with patch("subprocess.run", return_value=mock_result):
        result = _get_duration_minutes(f)
    assert result == 120.0


def test_list_pending_skips_too_long_file_and_records_state(tmp_path):
    """Files exceeding max_duration_minutes are skipped and flagged in state."""
    from scripts.run_orchestrator import _list_pending
    from src.agents.state import OrchestratorState, Stage

    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    long_file = watch_dir / "20260429 195927-LONG.m4a"
    long_file.write_bytes(b"fake")
    short_file = watch_dir / "20260429 090000-SHORT.m4a"
    short_file.write_bytes(b"fake")

    state = OrchestratorState(tmp_path / "state.jsonl")

    def fake_ffprobe(path):
        if "LONG" in str(path):
            return 180.0  # 3 hours — over 120 min limit
        return 30.0       # 30 minutes — under limit

    with patch("scripts.run_orchestrator._get_duration_minutes", side_effect=fake_ffprobe), \
         patch("scripts.run_orchestrator._notify"):
        result = _list_pending(watch_dir, state, max_duration_minutes=120.0)

    # Long file excluded, short file included
    assert long_file not in result
    assert short_file in result
    # Long file flagged in state
    assert state.is_too_long("20260429 195927-LONG.m4a")


def test_list_pending_suppresses_repeat_notification_for_too_long(tmp_path):
    """Second run: already-flagged too-long file does not trigger _notify again."""
    from scripts.run_orchestrator import _list_pending
    from src.agents.state import OrchestratorState, Stage

    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    long_file = watch_dir / "20260429 195927-LONG.m4a"
    long_file.write_bytes(b"fake")

    state = OrchestratorState(tmp_path / "state.jsonl")
    # Pre-flag as too_long (simulates first run already having flagged it)
    state.record("20260429 195927-LONG.m4a", Stage.NONE, status="too_long", meta={"duration_minutes": 180.0})

    notify_mock = MagicMock()
    with patch("scripts.run_orchestrator._get_duration_minutes", return_value=180.0), \
         patch("scripts.run_orchestrator._notify", notify_mock):
        result = _list_pending(watch_dir, state, max_duration_minutes=120.0)

    assert long_file not in result
    notify_mock.assert_not_called()


def test_state_is_too_long_returns_false_for_normal_file(tmp_path):
    """is_too_long() returns False for files with no too_long record."""
    from src.agents.state import OrchestratorState, Stage
    state = OrchestratorState(tmp_path / "state.jsonl")
    state.record("normal.m4a", Stage.STT, status="done")
    assert state.is_too_long("normal.m4a") is False


def test_list_pending_zero_max_duration_disables_guard(tmp_path):
    """Setting max_duration_minutes=0 disables the guard entirely."""
    from scripts.run_orchestrator import _list_pending
    from src.agents.state import OrchestratorState

    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    big_file = watch_dir / "20260429 195927-BIG.m4a"
    big_file.write_bytes(b"fake")

    state = OrchestratorState(tmp_path / "state.jsonl")

    with patch("scripts.run_orchestrator._get_duration_minutes", return_value=600.0):
        result = _list_pending(watch_dir, state, max_duration_minutes=0.0)

    # Guard disabled — big file is included
    assert big_file in result
