"""Voice Flow daemon entry point."""
from __future__ import annotations

import logging
import shutil
import signal
import time
from pathlib import Path

from config import get_settings
from src.pipeline import group_files, is_processed, process_file_group
from src.watcher import start_watcher

log = logging.getLogger(__name__)

_running = True

# Sync source: Apple Voice Memos iCloud directory
_VOICE_MEMOS_DIR = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
_SYNC_INTERVAL = 300  # 5 minutes
_RETRY_INTERVAL = 600  # 10 minutes between retry sweeps
_MAX_RETRY_ATTEMPTS = 3


def setup_logging() -> None:
    """Configure logging with console output."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress noisy libraries
    for name in ("watchdog", "httpx", "httpcore", "urllib3", "pyannote"):
        logging.getLogger(name).setLevel(logging.WARNING)


def sync_recordings() -> int:
    """Sync .m4a files from Voice Memos to recordings_mirror.

    Integrated into main daemon so it inherits FDA from launchd plist.
    Returns number of newly synced files.
    """
    settings = get_settings()
    dst = Path(settings.watch_dir)
    dst.mkdir(parents=True, exist_ok=True)

    if not _VOICE_MEMOS_DIR.exists():
        log.warning(f"Voice Memos 디렉토리 없음: {_VOICE_MEMOS_DIR}")
        return 0

    count = 0
    for src_file in _VOICE_MEMOS_DIR.glob("*.m4a"):
        dst_file = dst / src_file.name
        if not dst_file.exists():
            try:
                shutil.copy2(src_file, dst_file)
                log.info(f"[SYNC] 복사: {src_file.name}")
                count += 1
            except Exception as e:
                log.warning(f"[SYNC] 복사 실패: {src_file.name} — {e}")

    if count > 0:
        log.info(f"[SYNC] {count}개 새 파일 동기화 완료")
    return count


# Track failed files for retry: {filepath: attempt_count}
_failed_files: dict[str, int] = {}


def process_existing_files() -> None:
    """Scan watch dir for unprocessed .m4a files on startup."""
    settings = get_settings()
    watch_dir = Path(settings.watch_dir)
    if not watch_dir.exists():
        log.warning(f"감시 디렉토리 없음: {watch_dir}")
        return

    m4a_files = sorted(watch_dir.glob("*.m4a"))
    if not m4a_files:
        log.info("미처리 파일 없음")
        return

    # Filter already processed
    unprocessed = [f for f in m4a_files if not is_processed(str(f))]
    if not unprocessed:
        log.info("미처리 파일 없음")
        return

    log.info(f"미처리 파일 {len(unprocessed)}개 스캔, 그룹핑 시작")
    groups = group_files(unprocessed)
    log.info(f"{len(groups)}개 그룹으로 분류됨")

    for group in groups:
        result = process_file_group(group)
        if result is None:
            # Track failed files for retry
            for f in group:
                fpath = str(f)
                if not is_processed(fpath):
                    _failed_files[fpath] = _failed_files.get(fpath, 0) + 1
                    log.info(f"실패 파일 재시도 대기열 추가: {f.name} (시도 {_failed_files[fpath]}회)")


def retry_failed_files() -> None:
    """Retry previously failed files."""
    if not _failed_files:
        return

    # Only retry files that haven't exceeded max attempts
    retryable = {
        fpath: count for fpath, count in _failed_files.items()
        if count < _MAX_RETRY_ATTEMPTS and not is_processed(fpath)
    }

    if not retryable:
        # Clear out exhausted entries
        exhausted = [f for f, c in _failed_files.items() if c >= _MAX_RETRY_ATTEMPTS]
        for f in exhausted:
            log.warning(f"최대 재시도 횟수 초과, 포기: {Path(f).name} ({_failed_files[f]}회 시도)")
            del _failed_files[f]
        return

    paths = [Path(f) for f in retryable]
    log.info(f"실패 파일 재시도: {len(paths)}개")
    groups = group_files(paths)

    for group in groups:
        result = process_file_group(group)
        for f in group:
            fpath = str(f)
            if is_processed(fpath):
                # Success — remove from failed list
                _failed_files.pop(fpath, None)
            elif fpath in _failed_files:
                _failed_files[fpath] += 1


def _signal_handler(signum: int, _frame) -> None:
    global _running
    sig_name = signal.Signals(signum).name
    log.info(f"{sig_name} 수신, 종료 중...")
    _running = False


def main() -> None:
    setup_logging()
    settings = get_settings()

    log.info("Voice Flow 데몬 시작")
    log.info(f"감시 대상: {settings.watch_dir}")

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Initial sync + process
    sync_recordings()
    process_existing_files()

    # Start file watcher
    observer = start_watcher(settings.watch_dir)

    last_sync = time.time()
    last_retry = time.time()

    try:
        while _running:
            now = time.time()

            # Periodic sync from Voice Memos
            if now - last_sync >= _SYNC_INTERVAL:
                sync_recordings()
                last_sync = now

            # Periodic retry of failed files
            if now - last_retry >= _RETRY_INTERVAL and _failed_files:
                retry_failed_files()
                last_retry = now

            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
        log.info("Voice Flow 데몬 종료")


if __name__ == "__main__":
    main()
