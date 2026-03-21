"""Voice Flow daemon entry point."""
from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

from config import get_settings
from src.pipeline import process_file
from src.watcher import start_watcher

log = logging.getLogger(__name__)

_running = True


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

    log.info(f"기존 파일 {len(m4a_files)}개 스캔")
    for f in m4a_files:
        process_file(str(f))


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

    # Process existing files first
    process_existing_files()

    # Start file watcher
    observer = start_watcher(settings.watch_dir)

    try:
        while _running:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
        log.info("Voice Flow 데몬 종료")


if __name__ == "__main__":
    main()
