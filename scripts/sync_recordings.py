"""Voice Memos → recordings_mirror sync daemon.

Replaces the bash sync script. Runs every 5 minutes via launchd StartInterval.
Provides per-file COPY/SKIP/FAIL logging for reliable diagnosis.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

_VOICE_MEMOS_DIR = Path(
    "/Users/swlee/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
)
_MIRROR_DIR = _PROJ_ROOT / "recordings_mirror"
_LOG_FILE = Path("/tmp/voiceflow-sync.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SYNC] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger(__name__)


def _ensure_voice_memos_open() -> None:
    """Open Voice Memos app if not running — needed for iCloud sync."""
    result = subprocess.run(["pgrep", "-x", "VoiceMemos"], capture_output=True)
    if result.returncode != 0:
        log.info("VoiceMemos not running — launching")
        subprocess.Popen(
            ["open", "/System/Applications/VoiceMemos.app"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(10)  # wait for app to start and begin iCloud sync


def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass


def sync() -> int:
    _ensure_voice_memos_open()
    _MIRROR_DIR.mkdir(parents=True, exist_ok=True)

    if not _VOICE_MEMOS_DIR.exists():
        log.error("Voice Memos directory not found: %s", _VOICE_MEMOS_DIR)
        _notify("VoiceFlow Sync", "Voice Memos 폴더를 찾을 수 없습니다")
        return 1

    src_files = list(_VOICE_MEMOS_DIR.glob("*.m4a"))
    if not src_files:
        log.info("heartbeat — no m4a files in Voice Memos")
        return 0

    copied = skipped = failed = 0
    for src in src_files:
        dst = _MIRROR_DIR / src.name
        if dst.exists():
            skipped += 1
            continue
        try:
            shutil.copy2(src, dst)
            log.info("copied: %s (%.1f MB)", src.name, src.stat().st_size / 1_048_576)
            copied += 1
        except Exception as exc:
            log.error("FAILED: %s — %s", src.name, exc)
            failed += 1

    log.info(
        "heartbeat — %d src | copied=%d skipped=%d failed=%d",
        len(src_files), copied, skipped, failed,
    )
    if failed:
        _notify("VoiceFlow Sync", f"파일 복사 실패 {failed}건 — /tmp/voiceflow-sync.log 확인")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(sync())
