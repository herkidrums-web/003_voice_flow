"""Pipeline orchestrator: audio file -> STT -> summary -> Notion."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from config import NonRetryableError, RetryableError, VoiceFlowError, get_settings
from src.dictionary import load_dictionary
from src.notion_writer import create_meeting_note
from src.stt import process_audio
from src.summarizer import summarize_transcript

log = logging.getLogger(__name__)

# Module-level dictionary cache (loaded once per daemon lifecycle)
_dictionary_hints: str | None = None


def _get_dictionary() -> str:
    """Lazy-load and cache the proper noun dictionary."""
    global _dictionary_hints
    if _dictionary_hints is None:
        _dictionary_hints = load_dictionary()
    return _dictionary_hints


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


def _parse_file_datetime(filename: str) -> datetime | None:
    """Parse datetime from voice memo filename like '20260319 165322-8B01E3FB.m4a'."""
    name = Path(filename).stem  # remove .m4a
    # Expected: "YYYYMMDD HHMMSS-UUID" or "YYYYMMDD HHMMSS"
    if len(name) < 15 or not name[:8].isdigit():
        return None
    try:
        date_part = name[:8]
        time_part = name[9:15]  # skip space at index 8
        if not time_part.isdigit():
            return None
        return datetime(
            int(date_part[:4]), int(date_part[4:6]), int(date_part[6:8]),
            int(time_part[:2]), int(time_part[2:4]), int(time_part[4:6]),
        )
    except (ValueError, IndexError):
        return None


def group_files(paths: list[Path]) -> list[list[Path]]:
    """Group audio files into sessions based on time proximity.

    Rules:
    - Evening (>= 17:00): all files from same date evening → one group
    - Daytime (< 17:00): gap < 5 min → same group, >= 5 min → new group
    - Different dates → always new group
    """
    if not paths:
        return []

    settings = get_settings()
    gap_threshold = settings.grouping_daytime_gap
    evening_hour = settings.grouping_evening_start_hour

    # Parse and sort by timestamp
    timed: list[tuple[datetime, Path]] = []
    ungroupable: list[Path] = []
    for p in paths:
        dt = _parse_file_datetime(p.name)
        if dt:
            timed.append((dt, p))
        else:
            ungroupable.append(p)

    timed.sort(key=lambda x: x[0])

    groups: list[list[Path]] = []
    current_group: list[Path] = []
    prev_dt: datetime | None = None

    for dt, path in timed:
        if not current_group:
            current_group = [path]
            prev_dt = dt
            continue

        same_date = prev_dt.date() == dt.date()
        both_evening = prev_dt.hour >= evening_hour and dt.hour >= evening_hour
        gap_seconds = (dt - prev_dt).total_seconds()

        if same_date and both_evening:
            # Evening: always merge same-date evening files
            current_group.append(path)
        elif same_date and gap_seconds < gap_threshold:
            # Daytime: merge if gap < threshold
            current_group.append(path)
        else:
            # New group
            groups.append(current_group)
            current_group = [path]

        prev_dt = dt

    if current_group:
        groups.append(current_group)

    # Ungroupable files become individual groups
    for p in ungroupable:
        groups.append([p])

    return groups


def process_file_group(group: list[Path]) -> str | None:
    """Process a group of audio files as a single session.

    Single-file groups delegate to process_file().
    Multi-file groups: STT each → concatenate → summarize once → one Notion page.
    """
    if not group:
        return None

    if len(group) == 1:
        return process_file(str(group[0]))

    filenames = [p.name for p in group]
    group_label = f"{filenames[0]} 외 {len(group) - 1}개"
    log.info(f"그룹 처리 시작 ({len(group)}개 파일): {group_label}")

    # Skip if ALL files already processed
    unprocessed = [p for p in group if not is_processed(str(p))]
    if not unprocessed:
        log.info(f"그룹 전체 처리됨, 스킵: {group_label}")
        return None

    # Wait for stability on all files
    for p in group:
        if not wait_for_file_stability(str(p)):
            log.warning(f"파일 안정화 타임아웃: {p.name}")
            _notify_error("VoiceFlow", f"파일 안정화 타임아웃: {p.name}")
            return None

    try:
        # Recording date from first file
        recording_date = ""
        first_name = group[0].name
        if len(first_name) >= 8 and first_name[:8].isdigit():
            recording_date = f"{first_name[:4]}-{first_name[4:6]}-{first_name[6:8]}"

        # Step 1: STT each file
        transcripts = []
        total_duration = 0.0
        for i, p in enumerate(group):
            log.info(f"[1/3] STT ({i + 1}/{len(group)}): {p.name}")
            t = _retry_with_backoff(process_audio, str(p))
            transcripts.append(t)
            total_duration += t.duration

        # Concatenate transcripts
        combined_text = "\n\n--- (녹음 재개) ---\n\n".join(
            t.full_text for t in transcripts
        )

        # Step 2: Claude 2-pass on combined text
        log.info(f"[2/3] Claude 분석 시작: {group_label}")
        hints = _get_dictionary()
        analysis = _retry_with_backoff(
            summarize_transcript, combined_text, total_duration,
            recording_date, hints
        )

        # Step 3: Notion page (use first transcript for metadata)
        log.info(f"[3/3] Notion 페이지 생성: {group_label}")
        page_id = _retry_with_backoff(
            create_meeting_note, analysis, transcripts[0], group_label
        )

        # Mark ALL files as processed
        for p in group:
            mark_processed(str(p))

        log.info(f"그룹 파이프라인 완료: {group_label} → page_id={page_id}")
        return page_id

    except Exception as e:
        log.error(f"그룹 파이프라인 실패: {group_label} ({e})")
        _notify_error("VoiceFlow Error", f"그룹 실패: {group_label}\n{str(e)[:100]}")
        return None


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
        hints = _get_dictionary()
        analysis = _retry_with_backoff(
            summarize_transcript, transcript.full_text, transcript.duration,
            recording_date, hints
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
