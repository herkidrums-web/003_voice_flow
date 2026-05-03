"""Entry point invoked by launchd hourly — processes new files in watch_dir.

Topology:
  bash sync (com.swlee.voiceflow-sync) — Voice Memos → recordings_mirror/  (FDA)
  python orch (this script, hourly)    — recordings_mirror/ → Notion       (no FDA)

This script is the second link. It refuses to run if sync.log is stale,
because that means recordings_mirror/ does not reflect the device state
and we'd silently skip the latest recordings.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path so launchd (no PYTHONPATH) can import config/src
_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from anthropic import Anthropic

from config import get_settings
from src.agents.analysis_agent import AnalysisAgent
from src.agents.classification_agent import ClassificationAgent
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


def _build_agents(settings) -> dict:
    client = Anthropic(api_key=settings.anthropic_api_key, timeout=settings.claude_api_timeout)
    return {
        "stt": STTAgent(cache_dir=Path(settings.stt_cache_dir)),
        "ner": NERAgent(client=client, model=settings.claude_model_sonnet),
        "dictionary": DictionaryAgent(terms_path=Path(__file__).resolve().parents[1] / "custom_terms.txt"),
        "grouping": GroupingAgent(client=client, model=settings.claude_model_sonnet, max_chars=settings.grouping_max_chars),
        "analysis": AnalysisAgent(client=client, model=settings.claude_model_opus),
        "validation": ValidationAgent(client=client, model=settings.claude_model_sonnet),
        "self_heal": SelfHealAgent(client=client, model=settings.claude_model_sonnet, max_retries=settings.self_heal_max_retries),
        "classification": ClassificationAgent(client=client, model=settings.claude_model_haiku),
        "notion": NotionAgent(database_id=settings.notion_database_id, api_key=settings.notion_api_key),
        "wiki": WikiAgent(index_path=Path(settings.wiki_index_path)),
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    project_root = Path(__file__).resolve().parents[1]

    sync_log = project_root / "sync.log"
    ok, msg = _check_sync_health(sync_log)
    log.info("sync health: %s", msg)
    if not ok:
        _notify("VoiceFlow", f"Sync stale — Voice Memos 앱을 한번 띄워주세요. ({msg})")
        return 0

    state_path = project_root / "processing_state.jsonl"
    state = OrchestratorState(state_path)
    agents = _build_agents(settings)
    orchestrator = Orchestrator(agents=agents, state=state, max_self_heal=settings.self_heal_max_retries)

    watch_dir = Path(settings.watch_dir)
    if not watch_dir.exists():
        log.error("watch_dir not found: %s", watch_dir)
        _notify("VoiceFlow", f"watch_dir 없음: {watch_dir}")
        return 1

    pending = _list_pending(
        watch_dir, state,
        max_duration_minutes=settings.max_duration_minutes,
        min_duration_seconds=settings.min_duration,
    )
    if not pending:
        log.info("no pending files")
        return 0

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


if __name__ == "__main__":
    sys.exit(main())
