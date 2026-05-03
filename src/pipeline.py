"""Legacy sequential pipeline (v2). v3+ use src.agents.orchestrator.Orchestrator.

This module remains for backward-compat callers (process_date, watcher daemon).
New code paths must go through scripts/run_orchestrator.py.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from config import NonRetryableError, RetryableError, VoiceFlowError, get_settings
from src.dictionary import load_dictionary
from src.notion_writer import create_meeting_note
from src.stt import process_audio
from src.summarizer import summarize_transcript
from src.topic_merger import merge_analyses_by_topic

log = logging.getLogger(__name__)

# Module-level dictionary cache (loaded once per daemon lifecycle)
def _get_dictionary(transcript: str = "") -> str:
    """Load proper noun dictionary, filtered by transcript content."""
    return load_dictionary(transcript)


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
                         base_delay: float = 5.0, max_delay: float = 60.0):
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
            # Rate limit(429)은 더 긴 대기
            if getattr(e, "status_code", None) == 429:
                delay = min(60.0 * (2 ** attempt), 300.0)  # 60→120→300초
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
            log.warning(f"재시도 {attempt + 1}/{max_retries}, {delay}초 후 ({e})")
            time.sleep(delay)
        except VoiceFlowError:
            raise  # non-retryable pipeline errors
    raise last_error or RuntimeError("retry exhausted")


def _get_processed_log_path() -> Path:
    """Return processed.log path in the project directory (not watch_dir)."""
    return Path(__file__).parent.parent / get_settings().processed_log


def _get_state_path() -> Path:
    """Return processing_state.jsonl path."""
    return Path(__file__).parent.parent / "processing_state.jsonl"


def get_file_stage(filename: str) -> str:
    """Get the latest completed stage for a file.

    Returns: "none" | "stt" | "claude" | "notion" | "done"
    """
    state_path = _get_state_path()
    if not state_path.exists():
        # Fallback: check legacy processed.log
        log_path = _get_processed_log_path()
        if log_path.exists() and filename in log_path.read_text():
            return "done"
        return "none"

    latest_stage = "none"
    for line in state_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            if record.get("file") == filename and record.get("status") == "done":
                latest_stage = record["stage"]
        except (json.JSONDecodeError, KeyError):
            continue
    return latest_stage


def mark_stage(audio_path: str, stage: str, status: str = "done", error: str = "") -> None:
    """Record a pipeline stage completion/failure.

    Args:
        stage: "stt" | "claude" | "notion" | "done"
        status: "done" | "failed"
        error: error message (if failed)
    """
    state_path = _get_state_path()
    filename = Path(audio_path).name
    record = {
        "file": filename,
        "stage": stage,
        "status": status,
        "ts": datetime.now().isoformat(),
    }
    if error:
        record["error"] = error[:200]
    with open(state_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def is_processed(audio_path: str) -> bool:
    """Check if file was fully processed. Checks both new state and legacy log."""
    filename = Path(audio_path).name

    # New state system
    if get_file_stage(filename) == "done":
        return True

    # Legacy fallback
    log_path = _get_processed_log_path()
    if log_path.exists() and filename in log_path.read_text():
        return True

    return False


def mark_processed(audio_path: str) -> None:
    """Mark file as fully processed (both new state + legacy log)."""
    mark_stage(audio_path, "done")
    # Legacy compatibility
    log_path = _get_processed_log_path()
    filename = Path(audio_path).name
    with open(log_path, "a") as f:
        f.write(f"{filename}\n")


def _copy_to_temp(audio_path: str) -> str:
    """Copy audio file to temp dir for isolated processing.

    Source is now recordings_mirror/ (synced by bash script with FDA),
    so this is just for working-copy isolation during STT.
    """
    tmp_dir = tempfile.mkdtemp(prefix="voiceflow_")
    filename = Path(audio_path).name
    tmp_path = os.path.join(tmp_dir, filename)
    shutil.copy2(audio_path, tmp_path)
    return tmp_path


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

    tmp_paths = []
    try:
        # Recording datetime from first file (e.g., "20260401 093300-XXX.m4a")
        recording_date = ""
        first_name = group[0].name
        dt = _parse_file_datetime(first_name)
        if dt:
            recording_date = dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        elif len(first_name) >= 8 and first_name[:8].isdigit():
            recording_date = f"{first_name[:4]}-{first_name[4:6]}-{first_name[6:8]}"

        # Step 1: STT each file (copy to temp to avoid TCC permission issues)
        transcripts = []
        total_duration = 0.0
        for i, p in enumerate(group):
            log.info(f"[1/3] STT ({i + 1}/{len(group)}): {p.name}")
            tmp_path = _copy_to_temp(str(p))
            tmp_paths.append(tmp_path)
            t = _retry_with_backoff(process_audio, tmp_path)
            transcripts.append(t)
            total_duration += t.duration

        # Concatenate transcripts
        combined_text = "\n\n--- (녹음 재개) ---\n\n".join(
            t.full_text for t in transcripts
        )

        # Step 2: Claude 2-pass on combined text
        log.info(f"[2/3] Claude 분석 시작: {group_label}")
        hints = _get_dictionary(combined_text)
        analyses = _retry_with_backoff(
            summarize_transcript, combined_text, total_duration,
            recording_date, hints
        )

        # Step 3: Notion page(s) — one per session
        page_ids = []
        for i, analysis in enumerate(analyses):
            session_label = f"{group_label} (세션 {i+1}/{len(analyses)})" if len(analyses) > 1 else group_label
            log.info(f"[3/3] Notion 페이지 생성: {session_label}")
            page_id = _retry_with_backoff(
                create_meeting_note, analysis, transcripts[0], group_label
            )
            page_ids.append(page_id)

        # Mark ALL files as processed
        for p in group:
            mark_processed(str(p))

        page_ids_str = ", ".join(str(pid) for pid in page_ids)
        log.info(f"그룹 파이프라인 완료: {group_label} → {len(page_ids)}개 페이지 생성 (page_ids={page_ids_str})")
        return page_ids[0] if page_ids else None

    except Exception as e:
        log.error(f"그룹 파이프라인 실패: {group_label} ({e})")
        _notify_error("VoiceFlow Error", f"그룹 실패: {group_label}\n{str(e)[:100]}")
        return None
    finally:
        # Clean up temp files
        for tmp_path in tmp_paths:
            tmp_dir = os.path.dirname(tmp_path)
            shutil.rmtree(tmp_dir, ignore_errors=True)


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

    # Check stage for resume capability
    current_stage = get_file_stage(filename)
    if current_stage == "done":
        log.info(f"이미 처리 완료됨 (state), 스킵: {filename}")
        return None

    # Copy to temp dir to avoid macOS TCC permission issues with ffmpeg
    tmp_path = _copy_to_temp(audio_path)
    try:
        # Extract recording datetime from filename (e.g., "20260319 165322-8B01E3FB.m4a")
        recording_date = ""
        dt = _parse_file_datetime(filename)
        if dt:
            recording_date = dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        elif len(filename) >= 8 and filename[:8].isdigit():
            recording_date = f"{filename[:4]}-{filename[4:6]}-{filename[6:8]}"

        # Step 1: STT (캐시 있으면 자동 스킵)
        log.info(f"[1/3] STT 시작: {filename}")
        transcript = _retry_with_backoff(process_audio, tmp_path)
        mark_stage(audio_path, "stt")

        # Skip short recordings
        settings = get_settings()
        if transcript.duration < settings.min_duration:
            log.info(f"녹음 길이 {transcript.duration:.1f}초 < {settings.min_duration}초, 스킵: {filename}")
            mark_processed(audio_path)
            return None

        # Skip trivial content (too few meaningful words)
        meaningful_text = transcript.full_text.strip()
        # Count Korean characters as proxy for meaningful content
        korean_chars = sum(1 for c in meaningful_text if "\uAC00" <= c <= "\uD7A3")
        if korean_chars < 30:
            log.info(f"의미 있는 내용 부족 (한글 {korean_chars}자 < 30자), 스킵: {filename}")
            mark_processed(audio_path)
            return None

        # Step 2: Claude 2-pass (STT correction → comprehensive analysis)
        log.info(f"[2/3] Claude 분석 시작: {filename}")
        hints = _get_dictionary(transcript.full_text)
        analyses = _retry_with_backoff(
            summarize_transcript, transcript.full_text, transcript.duration,
            recording_date, hints
        )
        mark_stage(audio_path, "claude")

        # Step 3: Notion Page(s) Creation — one per session
        page_ids = []
        for i, analysis in enumerate(analyses):
            session_label = f"{filename} (세션 {i+1}/{len(analyses)})" if len(analyses) > 1 else filename
            log.info(f"[3/3] Notion 페이지 생성: {session_label}")
            page_id = _retry_with_backoff(
                create_meeting_note, analysis, transcript, filename
            )
            page_ids.append(page_id)
        mark_stage(audio_path, "notion")

        # Mark as fully processed
        mark_processed(audio_path)
        page_ids_str = ", ".join(str(pid) for pid in page_ids)
        log.info(f"파이프라인 완료: {filename} → {len(page_ids)}개 페이지 (page_ids={page_ids_str})")
        return page_ids[0] if page_ids else None

    except Exception as e:
        mark_stage(audio_path, "error", status="failed", error=str(e))
        log.error(f"파이프라인 실패: {filename} ({e})")
        _notify_error("VoiceFlow Error", f"파이프라인 실패: {filename}\n{str(e)[:100]}")
        return None
    finally:
        # Clean up temp file
        tmp_dir = os.path.dirname(tmp_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def process_date(target_date: str, skip_indices: list[int] | None = None) -> list[str]:
    """특정 날짜의 모든 미처리 음성메모를 처리하고 토픽 기반 병합 후 Notion 저장.

    Args:
        target_date: "20260422" 형식
        skip_indices: 스킵할 파일 인덱스 (0-based). None이면 전부 처리.

    Returns:
        생성된 Notion page_id 리스트
    """
    settings = get_settings()
    watch_dir = Path(settings.watch_dir)

    # 1. 대상 파일 스캔
    files = sorted(watch_dir.glob(f"{target_date}*.m4a"))
    all_files = files.copy()

    if not files:
        log.info(f"[{target_date}] 파일 없음")
        return []

    # 2. 스킵 적용
    if skip_indices:
        files = [f for i, f in enumerate(files) if i not in skip_indices]

    if not files:
        log.info(f"[{target_date}] 스킵 후 처리할 파일 없음")
        return []

    log.info(f"[{target_date}] {len(files)}개 파일 처리 시작 (전체 {len(all_files)}개 중)")

    # 3. 각 파일 개별 STT + Claude 분석 (Notion 저장 없이)
    all_analyses = []  # (analysis, transcript_text) 튜플
    for f in files:
        if is_processed(str(f)):
            log.info(f"이미 처리됨, 스킵: {f.name}")
            continue

        try:
            result = _process_single_for_merge(str(f))
            if result:
                all_analyses.extend(result)
        except Exception as e:
            log.error(f"[{f.name}] 처리 실패: {e}")

    if not all_analyses:
        log.info(f"[{target_date}] 분석 결과 없음")
        return []

    # 4. 토픽 기반 병합
    analyses_only = [a for a, _ in all_analyses]
    merged = merge_analyses_by_topic(analyses_only)
    log.info(f"[{target_date}] 병합 결과: {len(analyses_only)}개 → {len(merged)}개")

    # 5. Notion 저장
    page_ids = []
    for analysis in merged:
        try:
            page_id = _retry_with_backoff(
                create_meeting_note, analysis, None, f"{target_date}_merged"
            )
            page_ids.append(str(page_id))
            log.info(f"Notion 페이지 생성: {analysis.properties.title} → {page_id}")
        except Exception as e:
            log.error(f"Notion 생성 실패: {analysis.properties.title}: {e}")

    # 6. 처리 완료 기록
    for f in files:
        if not is_processed(str(f)):
            mark_processed(str(f))

    log.info(f"[{target_date}] 완료: {len(page_ids)}개 페이지 생성")
    return page_ids


def _process_single_for_merge(
    audio_path: str,
    participants: list[str] | None = None,
    meeting_title: str | None = None,
) -> list[tuple] | None:
    """단일 파일 STT + Claude 분석. Notion 저장 없이 (analysis, transcript_text) 리스트 반환.

    Args:
        participants: 담당 지정 참석자 (할루시네이션 방지)
        meeting_title: 담당 지정 회의 제목
    """
    filename = Path(audio_path).name

    if not filename.endswith(".m4a"):
        return None

    # Wait for file stability
    if not wait_for_file_stability(audio_path):
        log.warning(f"파일 안정화 타임아웃: {filename}")
        return None

    tmp_path = _copy_to_temp(audio_path)
    try:
        # Recording date from filename
        recording_date = ""
        dt = _parse_file_datetime(filename)
        if dt:
            recording_date = dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        elif len(filename) >= 8 and filename[:8].isdigit():
            recording_date = f"{filename[:4]}-{filename[4:6]}-{filename[6:8]}"

        # STT
        log.info(f"[STT] {filename}")
        transcript = _retry_with_backoff(process_audio, tmp_path)

        # Skip short/trivial
        settings = get_settings()
        if transcript.duration < settings.min_duration:
            log.info(f"녹음 길이 {transcript.duration:.1f}초 < {settings.min_duration}초, 스킵: {filename}")
            return None

        korean_chars = sum(1 for c in transcript.full_text.strip() if "\uAC00" <= c <= "\uD7A3")
        if korean_chars < 30:
            log.info(f"의미 있는 내용 부족 (한글 {korean_chars}자 < 30자), 스킵: {filename}")
            return None

        # Claude analysis (참석자/제목 힌트 주입)
        log.info(f"[Claude] {filename}")
        hints = _get_dictionary(transcript.full_text)
        analyses = _retry_with_backoff(
            summarize_transcript, transcript.full_text, transcript.duration,
            recording_date, hints,
            participants, meeting_title,
        )

        return [(a, transcript.full_text) for a in analyses]

    except Exception as e:
        log.error(f"처리 실패: {filename} ({e})")
        return None
    finally:
        tmp_dir = os.path.dirname(tmp_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def process_date_with_meta(
    target_date: str,
    meta_map: dict[str, dict],
) -> list[str]:
    """특정 날짜 음성메모를 **파일별 메타 정보 주입**하며 처리.

    meta_map 형식:
    {
        "20260423 092615-8CC8126F.m4a": {
            "title": "그룹주간회의 (안형균 그룹장 주재)",  # optional
            "participants": ["이성우 담당", "안형균 그룹장", ...],  # optional
            "skip": False,  # optional, True면 해당 파일 처리 안 함
        },
        ...
    }

    meta_map에 없는 파일은 메타 없이 처리.
    """
    settings = get_settings()
    watch_dir = Path(settings.watch_dir)

    files = sorted(watch_dir.glob(f"{target_date}*.m4a"))
    if not files:
        log.info(f"[{target_date}] 파일 없음")
        return []

    active_files = [
        f for f in files
        if not meta_map.get(f.name, {}).get("skip", False)
    ]

    if not active_files:
        log.info(f"[{target_date}] 스킵 후 처리할 파일 없음")
        return []

    log.info(
        f"[{target_date}] {len(active_files)}개 파일 처리 시작 "
        f"(전체 {len(files)}개, 스킵 {len(files) - len(active_files)}개, 파일별 즉시 저장 모드)"
    )

    page_ids: list[str] = []
    for idx, f in enumerate(active_files, 1):
        if is_processed(str(f)):
            log.info(f"[{idx}/{len(active_files)}] 이미 처리됨, 스킵: {f.name}")
            continue

        meta = meta_map.get(f.name, {})
        participants = meta.get("participants")
        title = meta.get("title")

        log.info(
            f"[{idx}/{len(active_files)}] [{f.name}] 메타 — 제목={title or '(없음)'}, "
            f"참석자={len(participants) if participants else 0}명"
        )

        # STT + Claude 분석
        try:
            result = _process_single_for_merge(
                str(f),
                participants=participants,
                meeting_title=title,
            )
        except Exception as e:
            log.error(f"[{f.name}] 분석 실패: {e}")
            continue

        if not result:
            continue

        # 파일별 즉시 Notion 저장 (병합 없이 개별 저장)
        file_page_ids: list[str] = []
        for analysis, _ in result:
            try:
                page_id = _retry_with_backoff(
                    create_meeting_note, analysis, None, f"{target_date}_{f.stem}"
                )
                file_page_ids.append(str(page_id))
                log.info(
                    f"[{idx}/{len(active_files)}] Notion 페이지 생성: "
                    f"{analysis.properties.title} → {page_id}"
                )
            except Exception as e:
                log.error(f"[{f.name}] Notion 생성 실패: {analysis.properties.title}: {e}")

        page_ids.extend(file_page_ids)

        # 이 파일 완료 시점에 processed 마킹 (다음 파일 문제 시에도 이 파일은 재처리 안 됨)
        if file_page_ids and not is_processed(str(f)):
            mark_processed(str(f))

    log.info(f"[{target_date}] 완료: {len(page_ids)}개 페이지 생성")
    return page_ids
