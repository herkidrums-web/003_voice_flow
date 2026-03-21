"""Pipeline orchestrator: audio file -> STT -> summary -> Notion."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from config import NonRetryableError, RetryableError, VoiceFlowError, get_settings
from src.notion_writer import create_meeting_note
from src.stt import process_audio
from src.summarizer import summarize_transcript

log = logging.getLogger(__name__)


def _notify_error(title: str, message: str) -> None:
    """Send macOS notification on persistent failure."""
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{message}" with title "{title}"',
            ],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass  # notification is best-effort


def _retry_with_backoff(func, *args, max_retries: int = 2,
                         base_delay: float = 5.0, max_delay: float = 30.0):
    """Sync retry with exponential backoff. Retries only RetryableError."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args)
        except NonRetryableError:
            raise
        except RetryableError as e:
            last_error = e
            if attempt >= max_retries:
                break
            delay = min(base_delay * (2 ** attempt), max_delay)
            log.warning(f"재시도 {attempt + 1}/{max_retries}, {delay}초 후 ({e})")
            time.sleep(delay)
        except VoiceFlowError:
            raise  # non-retryable pipeline errors
    raise last_error or RuntimeError("retry exhausted")


def _get_processed_log_path() -> Path:
    """Return processed.log path in the project directory (not watch_dir)."""
    return Path(__file__).parent.parent / get_settings().processed_log


def is_processed(audio_path: str) -> bool:
    """Check if file was already processed (via processed.log)."""
    log_path = _get_processed_log_path()
    if not log_path.exists():
        return False
    filename = Path(audio_path).name
    return filename in log_path.read_text()


def mark_processed(audio_path: str) -> None:
    """Append filename to processed.log."""
    log_path = _get_processed_log_path()
    filename = Path(audio_path).name
    with open(log_path, "a") as f:
        f.write(f"{filename}\n")


def wait_for_file_stability(audio_path: str) -> bool:
    """Poll file size until stable. Returns True if stable, False on timeout."""
    settings = get_settings()
    stable_count = 0
    last_size = -1
    start = time.time()

    while time.time() - start < settings.file_stability_timeout:
        try:
            current_size = os.path.getsize(audio_path)
        except OSError:
            return False

        if current_size == last_size:
            stable_count += 1
            if stable_count >= settings.file_stability_checks:
                return True
        else:
            stable_count = 0
            last_size = current_size

        time.sleep(settings.file_stability_interval)

    return False


def process_file(audio_path: str) -> str | None:
    """Full pipeline for a single audio file.

    Returns:
        Notion page_id on success, None on skip/failure.
    """
    filename = Path(audio_path).name

    # Skip non-m4a
    if not filename.endswith(".m4a"):
        return None

    # Skip already processed
    if is_processed(audio_path):
        log.info(f"이미 처리됨, 스킵: {filename}")
        return None

    # Wait for file stability
    log.info(f"파일 안정화 대기: {filename}")
    if not wait_for_file_stability(audio_path):
        log.warning(f"파일 안정화 타임아웃: {filename}")
        _notify_error("VoiceFlow", f"파일 안정화 타임아웃: {filename}")
        return None

    try:
        # Extract recording date from filename (e.g., "20260319 165322-8B01E3FB.m4a")
        recording_date = ""
        if len(filename) >= 8 and filename[:8].isdigit():
            recording_date = f"{filename[:4]}-{filename[4:6]}-{filename[6:8]}"

        # Step 1: STT
        log.info(f"[1/3] STT 시작: {filename}")
        transcript = _retry_with_backoff(process_audio, audio_path)

        # Step 2: Claude 2-pass (STT correction → comprehensive analysis)
        log.info(f"[2/3] Claude 분석 시작: {filename}")
        analysis = _retry_with_backoff(
            summarize_transcript, transcript.full_text, transcript.duration, recording_date
        )

        # Step 3: Notion Page Creation
        log.info(f"[3/3] Notion 페이지 생성: {filename}")
        page_id = _retry_with_backoff(
            create_meeting_note, analysis, transcript, filename
        )

        # Mark as processed
        mark_processed(audio_path)
        log.info(f"파이프라인 완료: {filename} → page_id={page_id}")
        return page_id

    except Exception as e:
        log.error(f"파이프라인 실패: {filename} ({e})")
        _notify_error("VoiceFlow Error", f"파이프라인 실패: {filename}\n{str(e)[:100]}")
        return None
