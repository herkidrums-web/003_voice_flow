"""Watchdog-based file system watcher for new voice memos."""
from __future__ import annotations

import logging

from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from src.pipeline import process_file

log = logging.getLogger(__name__)


class VoiceMemoHandler(FileSystemEventHandler):
    """Handle new .m4a files in the watch directory."""

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        if not event.src_path.endswith(".m4a"):
            return
        log.info(f"새 파일 감지: {event.src_path}")
        process_file(event.src_path)


def start_watcher(watch_dir: str) -> Observer:
    """Start watching directory for new voice memos. Returns the observer."""
    observer = Observer()
    observer.schedule(VoiceMemoHandler(), watch_dir, recursive=False)
    observer.start()
    log.info(f"감시 시작: {watch_dir}")
    return observer
