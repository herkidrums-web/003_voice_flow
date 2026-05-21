"""Entry point invoked by launchd hourly — processes new files in watch_dir.

Topology:
  bash sync (com.swlee.voiceflow-sync) — Voice Memos → recordings_mirror/  (FDA)
  python orch (this script, hourly)    — recordings_mirror/ → Notion       (no FDA)

This script is the second link. It refuses to run if sync.log is stale,
because that means recordings_mirror/ does not reflect the device state
and we'd silently skip the latest recordings.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path so launchd (no PYTHONPATH) can import config/src
_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from config import get_settings
from src.agents.analysis_agent import AnalysisAgent
from src.agents.classification_agent import ClassificationAgent
from src.agents.cli_client import ClaudeCLIClient
from src.agents.dictionary_agent import DictionaryAgent
from src.agents.grouping_agent import GroupingAgent
from src.agents.ner_agent import NERAgent
from src.agents.notion_agent import NotionAgent
from src.agents.orchestrator import Orchestrator
from src.agents.self_heal_agent import SelfHealAgent
from src.agents.state import OrchestratorState, Stage
from src.agents.stt_agent import STTAgent
from src.agents.todo_agent import TodoAgent
from src.agents.validation_agent import ValidationAgent
from src.agents.wiki_agent import WikiAgent

log = logging.getLogger(__name__)


_VOICE_MEMOS_DIR = Path(
    "/Users/swlee/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
)


def _mirror_fallback_sync(watch_dir: Path) -> int:
    """Fallback: copy any m4a from Voice Memos folder missing in watch_dir.

    Runs after the sync daemon check so we catch files the daemon missed.
    Returns the number of files copied.
    """
    if not _VOICE_MEMOS_DIR.exists():
        return 0
    copied = 0
    for src in _VOICE_MEMOS_DIR.glob("*.m4a"):
        dst = watch_dir / src.name
        if dst.exists():
            continue
        try:
            import shutil
            shutil.copy2(src, dst)
            log.info("fallback copy: %s", src.name)
            copied += 1
        except Exception as exc:
            log.warning("fallback copy failed %s: %s", src.name, exc)
    return copied


def _check_sync_health(sync_log: Path, max_age_hours: int = 24) -> tuple[bool, str]:
    """Inspect sync.log mtime to verify the bash sync daemon is alive.

    Returns (ok, message). When ok=False, run_orchestrator() exits cleanly
    (does NOT raise) — a stale sync is a Voice-Memos-app-not-running issue,
    not a code bug.
    """
    if not sync_log.exists():
        return False, f"sync.log not found at {sync_log}"
    age_hours = (time.time() - sync_log.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return False, f"sync.log stale ({age_hours:.1f}h since last update; threshold {max_age_hours}h)"
    return True, f"sync.log fresh ({age_hours:.1f}h)"


def _check_orchestrator_health(heartbeat: Path, max_age_hours: int = 25) -> tuple[bool, str]:
    """Check orchestrator.heartbeat freshness for external monitoring.

    Returns (ok, message). Called by briefing agent or monitoring scripts.
    max_age_hours=25 allows one missed hourly run before alerting.
    """
    if not heartbeat.exists():
        return False, f"orchestrator.heartbeat not found — has it ever run? ({heartbeat})"
    age_hours = (time.time() - heartbeat.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return False, f"orchestrator stale ({age_hours:.1f}h since last run; threshold {max_age_hours}h)"
    return True, f"orchestrator alive ({age_hours:.1f}h ago)"


def _free_memory_gb() -> float | None:
    """Return free + speculative + inactive pages in GB via vm_stat. None if unavailable.

    `free` alone underestimates available memory on macOS; inactive/speculative
    are reclaimable. Used by the memory guard to detect pressure before STT
    triggers jetsam (see 2026-05-15 incident: mlx-whisper unified memory accrual
    drove free → 161 MB while running an 11-file batch).
    """
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return None
        m_page = re.search(r"page size of (\d+) bytes", r.stdout)
        page = int(m_page.group(1)) if m_page else 16384
        def grab(label: str) -> int:
            m = re.search(rf"Pages {label}:\s+(\d+)", r.stdout)
            return int(m.group(1)) if m else 0
        pages = grab("free") + grab("speculative") + grab("inactive")
        return pages * page / (1024 ** 3)
    except Exception:
        return None


def _wait_for_memory(min_free_gb: float, max_wait_s: int = 300) -> bool:
    """Block until free memory ≥ min_free_gb. Returns True if pressure cleared,
    False if max_wait_s elapsed (caller can abort)."""
    start = time.monotonic()
    while True:
        free = _free_memory_gb()
        if free is None:
            log.warning("vm_stat unavailable; skipping memory guard")
            return True
        if free >= min_free_gb:
            return True
        elapsed = time.monotonic() - start
        if elapsed > max_wait_s:
            log.warning("memory pressure persisted %.0fs (free=%.1fGB < %.1fGB); aborting batch",
                        elapsed, free, min_free_gb)
            return False
        log.warning("memory pressure: free=%.1fGB < %.1fGB threshold; sleeping 30s",
                    free, min_free_gb)
        time.sleep(30)


def _notify(title: str, message: str) -> None:
    """Best-effort macOS notification. Never raises."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


_DISCORD_ENV_FILE = Path.home() / ".claude/channels/discord/.env"
_DISCORD_CHANNEL_ID = "1490245243285667981"
_DISCORD_API = "https://discord.com/api/v10"
_STALE_MARKER = Path("/tmp/voiceflow-stale-notified.txt")
_STALE_NOTIFY_INTERVAL_HOURS = 12


def _load_discord_token() -> str | None:
    """Read Discord bot token from same .env discord_listener uses. None if unreadable."""
    try:
        for line in _DISCORD_ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


def _notify_discord(text: str) -> None:
    """Best-effort Discord channel message. Never raises. Truncates to 2000 chars."""
    token = _load_discord_token()
    if not token:
        return
    try:
        import requests
        requests.post(
            f"{_DISCORD_API}/channels/{_DISCORD_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": text[:2000]},
            timeout=10,
        )
    except Exception:
        pass


def _should_notify_discord_stale() -> bool:
    """Throttle stale-sync Discord pings: only every _STALE_NOTIFY_INTERVAL_HOURS."""
    if not _STALE_MARKER.exists():
        return True
    try:
        age_hours = (time.time() - _STALE_MARKER.stat().st_mtime) / 3600
        return age_hours >= _STALE_NOTIFY_INTERVAL_HOURS
    except Exception:
        return True


def _mark_stale_notified() -> None:
    try:
        _STALE_MARKER.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception:
        pass


def _clear_stale_marker() -> None:
    try:
        _STALE_MARKER.unlink(missing_ok=True)
    except Exception:
        pass


_ORCH_LOCK = Path("/tmp/voiceflow-orchestrator.lock")


def _acquire_lock():
    """Acquire an exclusive non-blocking flock; return the open handle, or None if held.

    Prevents concurrent orchestrator runs — e.g. a manual_run.sh invocation
    overlapping the hourly launchd run — which would process the same files
    twice and create duplicate Notion pages (see 2026-05-21 incident).
    The flock auto-releases when the handle is closed or the process exits.
    """
    fh = open(_ORCH_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _build_agents(settings) -> dict:
    client = ClaudeCLIClient(cli_path=settings.claude_cli_path, timeout=settings.claude_api_timeout)
    return {
        "stt": STTAgent(cache_dir=Path(settings.stt_cache_dir)),
        "ner": NERAgent(client=client, model=settings.claude_model_sonnet),
        "dictionary": DictionaryAgent(terms_path=Path(__file__).resolve().parents[1] / "custom_terms.txt"),
        "grouping": GroupingAgent(client=client, model=settings.claude_model_sonnet, max_chars=settings.grouping_max_chars),
        "analysis": AnalysisAgent(client=client, model=settings.claude_model_opus),
        "validation": ValidationAgent(client=client, model=settings.claude_model_sonnet),
        "self_heal": SelfHealAgent(client=client, model=settings.claude_model_sonnet, max_retries=settings.self_heal_max_retries),
        "classification": ClassificationAgent(client=client, model=settings.claude_model_haiku),
        "notion": NotionAgent(
            database_id=settings.notion_database_id,
            api_key=settings.notion_api_key,
            state_path=Path(__file__).resolve().parents[1] / "processing_state.jsonl",
        ),
        "wiki": WikiAgent(index_path=Path(settings.wiki_index_path)),
        "todo": TodoAgent(client=client, model=settings.claude_model_haiku),
    }


def _get_duration_minutes(path: Path) -> float | None:
    """Return file duration in minutes via ffprobe. Returns None if ffprobe unavailable or fails."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip()) / 60
    except Exception:
        pass
    return None


def _list_pending(
    watch_dir: Path,
    state: OrchestratorState,
    max_duration_minutes: float = 0.0,
    min_duration_seconds: float = 5.0,
) -> list[Path]:
    pending: list[Path] = []
    for path in sorted(watch_dir.glob("*.m4a")):
        if state.get_stage(path.name) in (Stage.WIKI, Stage.DONE):
            continue

        # Skip files already flagged as too long (suppress repeated notifications)
        if state.is_too_long(path.name):
            log.debug("skipping previously flagged too-long file: %s", path.name)
            continue

        # Duration guards (single ffprobe call covers both min and max)
        if max_duration_minutes > 0 or min_duration_seconds > 0:
            dur = _get_duration_minutes(path)
            if dur is not None:
                if min_duration_seconds > 0 and dur * 60 < min_duration_seconds:
                    log.info(
                        "⏭ 파일 스킵 (%.1f초 < 최소 %.0f초): %s",
                        dur * 60, min_duration_seconds, path.name,
                    )
                    continue
                if max_duration_minutes > 0 and dur > max_duration_minutes:
                    log.warning(
                        "⏭ 파일 스킵 (%.0f분 > 한도 %.0f분): %s",
                        dur, max_duration_minutes, path.name,
                    )
                    state.record(path.name, Stage.NONE, status="too_long", meta={"duration_minutes": round(dur, 1)})
                    _notify(
                        "VoiceFlow — 장시간 녹음 감지",
                        f"{path.name}: {dur:.0f}분 녹음 (한도 {max_duration_minutes:.0f}분). 필요 없으면 파일 삭제해 주세요.",
                    )
                    continue

        pending.append(path)
    return pending


def _run() -> int:
    settings = get_settings()
    project_root = Path(__file__).resolve().parents[1]

    sync_log = Path("/tmp/voiceflow-sync.log")
    ok, msg = _check_sync_health(sync_log)
    log.info("sync health: %s", msg)
    if not ok:
        _notify("VoiceFlow", f"Sync stale — Voice Memos 앱을 한번 띄워주세요. ({msg})")
        if _should_notify_discord_stale():
            _notify_discord(
                f"⚠️ VoiceFlow sync stale — {msg}\n"
                f"→ Discord에서 \"음성메모 처리해줘\" 답장하면 backlog 처리됩니다."
            )
            _mark_stale_notified()
        return 0
    _clear_stale_marker()

    watch_dir = Path(settings.watch_dir)
    if not watch_dir.exists():
        log.error("watch_dir not found: %s", watch_dir)
        _notify("VoiceFlow", f"watch_dir 없음: {watch_dir}")
        return 1

    copied = _mirror_fallback_sync(watch_dir)
    if copied:
        log.info("fallback sync: %d files copied from Voice Memos", copied)

    state_path = project_root / "processing_state.jsonl"
    state = OrchestratorState(state_path)
    agents = _build_agents(settings)
    orchestrator = Orchestrator(
        agents=agents,
        state=state,
        max_self_heal=settings.self_heal_max_retries,
        todo_output_dir=project_root,
    )

    pending = _list_pending(
        watch_dir, state,
        max_duration_minutes=settings.max_duration_minutes,
        min_duration_seconds=settings.min_duration,
    )
    if not pending:
        log.info("no pending files")
        return 0

    # Limit batch size per run to avoid mlx-whisper unified memory accrual
    # triggering jetsam. Default 3 (see 2026-05-15: 11-file batch drove free → 161MB).
    # Override with VOICEFLOW_MAX_PER_RUN env when running interactively with monitoring.
    max_per_run = int(os.environ.get("VOICEFLOW_MAX_PER_RUN", "3"))
    if len(pending) > max_per_run:
        log.info("capping batch: %d → %d files (VOICEFLOW_MAX_PER_RUN=%d)", len(pending), max_per_run, max_per_run)
        pending = pending[-max_per_run:]  # sorted by name = chronological; take latest

    # Memory guard: refuse to start if system is already under pressure.
    # Disable with VOICEFLOW_MEMORY_GUARD=0 (e.g. CI).
    if os.environ.get("VOICEFLOW_MEMORY_GUARD", "1") != "0":
        min_free_gb = float(os.environ.get("VOICEFLOW_MIN_FREE_GB", "6"))
        if not _wait_for_memory(min_free_gb=min_free_gb, max_wait_s=300):
            _notify("VoiceFlow", f"메모리 부족 — 배치 중단 (free < {min_free_gb}GB)")
            return 3

    log.info("processing %d files", len(pending))
    result = orchestrator.process_batch(pending)
    log.info("batch result: %s", result)

    # Heartbeat: write timestamp so monitoring tools can detect a dead orchestrator
    heartbeat = project_root / "orchestrator.heartbeat"
    heartbeat.write_text(
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ok={result.get('ok')} files={len(pending)}\n",
        encoding="utf-8",
    )

    if not result.get("ok"):
        _notify("VoiceFlow", f"배치 일부 실패 — {result.get('reason', 'see log')}")
    return 0 if result.get("ok") else 2


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lock_fh = _acquire_lock()
    if lock_fh is None:
        log.info("다른 orchestrator 실행이 lock 보유 중 — 중복 처리 방지 위해 종료")
        return 0
    try:
        return _run()
    finally:
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
