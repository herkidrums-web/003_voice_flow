"""Watchdog-based file system watcher with debounce for file grouping."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config import get_settings
from src.pipeline import group_files, is_processed, process_file_group

log = logging.getLogger(__name__)


class VoiceMemoHandler(FileSystemEventHandler):
    """Handle new .m4a files with debounce to allow grouping."""

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[str] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._debounce_seconds = get_settings().grouping_debounce

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        if not event.src_path.endswith(".m4a"):
            return

        log.info(f"새 파일 감지: {event.src_path}")

        with self._lock:
            self._pending.append(event.src_path)
            # Reset debounce timer
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._flush)
            self._timer.daemon = True
            self._timer.start()
            log.info(f"디바운스 타이머 리셋 ({self._debounce_seconds}초 후 처리)")

    def _flush(self) -> None:
        """Process all pending files after debounce period."""
        with self._lock:
            paths = [Path(p) for p in self._pending]
            self._pending.clear()
            self._timer = None

        if not paths:
            return

        # Filter already processed
        unprocessed = [p for p in paths if not is_processed(str(p))]
        if not unprocessed:
            log.info("대기 파일 모두 처리됨, 스킵")
            return

        log.info(f"디바운스 완료, {len(unprocessed)}개 파일 그룹핑 시작")
        groups = group_files(unprocessed)
        log.info(f"{len(groups)}개 그룹으로 분류됨")

        for group in groups:
            process_file_group(group)


def start_watcher(watch_dir: str) -> Observer:
    """Start watching directory for new voice memos. Returns the observer."""
    observer = Observer()
    observer.schedule(VoiceMemoHandler(), watch_dir, recursive=False)
    observer.start()
    log.info(f"감시 시작: {watch_dir}")
    return observer
