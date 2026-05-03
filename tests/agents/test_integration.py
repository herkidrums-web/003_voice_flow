"""End-to-end smoke test — all sub-agents mocked, runs through scripts entry point."""
import os
import subprocess
import time
from pathlib import Path


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
