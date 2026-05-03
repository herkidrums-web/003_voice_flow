"""Local STT + speaker diarization pipeline (mlx-whisper + pyannote)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import STTError, get_settings

log = logging.getLogger(__name__)

# Module-level model cache (persists across file processing in daemon)
_diarization_pipeline: Any = None


@dataclass
class TranscriptSegment:
    start: float
    end: float
    speaker: str
    text: str


@dataclass
class TranscriptResult:
    segments: list[TranscriptSegment]
    duration: float
    speaker_count: int
    full_text: str


def _get_diarization_pipeline():
    """Lazy-load pyannote pipeline (cached for daemon lifecycle)."""
    global _diarization_pipeline
    if _diarization_pipeline is None:
        from pyannote.audio import Pipeline

        settings = get_settings()
        log.info("loading pyannote pipeline...")
        _diarization_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=settings.hf_token,
        )
        log.info("pyannote pipeline loaded")
    return _diarization_pipeline


def filter_segments(segments: list[dict]) -> list[dict]:
    """Unified hallucination filter.

    Removes: empty text, single-char filler, Cyrillic noise, consecutive repeats.
    """
    if not segments:
        return segments

    filtered = []
    for seg in segments:
        text = seg["text"].strip() if seg.get("text") else ""

        # Empty text
        if not text:
            continue

        # Single-char filler sounds
        if len(text) <= 1 and text in ("아", "어", "네", "음", ""):
            continue

        # Cyrillic hallucination (러시아어 혼입)
        if any(0x0400 < ord(c) < 0x0500 for c in text):
            continue

        # Latin-only noise hallucination (e.g., "ifting-jedifting-je..." repeats)
        # Real Korean speech should contain at least some Korean characters
        has_korean = any("\uAC00" <= c <= "\uD7A3" for c in text)
        if not has_korean and len(text) > 5:
            continue

        # Repetitive substring hallucination (same 3+ char pattern repeated 3+ times)
        if len(text) > 15:
            for plen in range(3, 8):
                pattern = text[:plen]
                if text.count(pattern) >= 3 and len(set(text.replace(pattern, ""))) < 5:
                    text = ""
                    break
        if not text:
            continue

        filtered.append({**seg, "text": text})

    # Consecutive repeat removal
    if not filtered:
        return filtered

    deduped = [filtered[0]]
    for seg in filtered[1:]:
        prev_text = deduped[-1]["text"]
        curr_text = seg["text"]

        # Exact duplicate → extend end time
        if curr_text == prev_text:
            deduped[-1]["end"] = seg["end"]
            continue

        # Partial repeat (e.g., "자유로운 영혼이..." → "자유로운 영혼인데")
        if (
            len(prev_text) < 15
            and curr_text.startswith(prev_text[: min(len(prev_text), 8)])
        ):
            deduped[-1] = seg
            continue

        deduped.append(seg)

    return deduped


def _load_whisper_prompt() -> str:
    """Load high-priority proper nouns for Whisper initial_prompt.

    Whisper uses this to bias recognition toward known names.
    Keep short (~200 tokens) — only people, orgs, key terms.
    """
    from pathlib import Path

    terms_file = Path(__file__).parent.parent / "custom_terms.txt"
    if not terms_file.exists():
        return ""

    # Extract person names — lines with Korean name patterns (2~4 chars + title)
    priority_terms = []
    person_keywords = ("임원", "조직", "담당자", "기타 임원", "팀장", "상무", "부사장", "그룹장", "담당", "책임", "고객사")
    in_priority_section = False
    for line in terms_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            in_priority_section = any(kw in line for kw in person_keywords)
            continue
        if in_priority_section and len(line) <= 20:
            priority_terms.append(line)

    # Add essential org/product names
    essential = [
        "LG유플러스", "유플러스", "AIDC", "AICC", "기업AI사업그룹",
        "네이버클라우드", "토스", "코람코", "유진투자증권", "브룩필드",
        "파주AIDC", "익시", "비즈온", "오피스넷",
    ]
    all_terms = list(dict.fromkeys(priority_terms + essential))  # dedupe, preserve order

    prompt = ", ".join(all_terms[:80])  # limit to ~80 terms
    log.info(f"Whisper 고유명사 프롬프트: {len(all_terms)}개 용어")
    return prompt


def transcribe_audio(audio_path: str) -> tuple[list[dict], float]:
    """mlx-whisper STT (Apple Silicon GPU). Returns (segments, duration)."""
    import mlx_whisper

    whisper_prompt = _load_whisper_prompt()
    log.info("STT 전사 시작 (mlx-whisper)...")
    start = time.time()

    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
        language="ko",
        word_timestamps=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        initial_prompt=whisper_prompt if whisper_prompt else None,
    )

    raw_segments = [
        {"start": s["start"], "end": s["end"], "text": s["text"]}
        for s in result["segments"]
    ]

    filtered = filter_segments(raw_segments)
    duration = result.get("duration", filtered[-1]["end"] if filtered else 0)

    elapsed = time.time() - start
    log.info(f"STT 완료: {len(filtered)}개 세그먼트, {duration/60:.1f}분 ({elapsed:.1f}초)")
    return filtered, duration


def diarize_audio(audio_path: str) -> list[dict]:
    """pyannote speaker diarization. Returns speaker segments."""
    import torch
    # CPU 과부하 → 열 보호 재부팅 방지: 코어 절반만 사용
    import os
    max_threads = max(2, os.cpu_count() // 2) if os.cpu_count() else 4
    torch.set_num_threads(max_threads)
    log.info(f"화자분리 torch 스레드 제한: {max_threads}개")

    pipeline = _get_diarization_pipeline()

    log.info("화자분리 시작 (pyannote)...")
    start = time.time()

    result = pipeline(audio_path, min_speakers=2, max_speakers=5)
    annotation = result.speaker_diarization

    speaker_segments = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        speaker_segments.append({
            "start": turn.start,
            "end": turn.end,
            "speaker": speaker,
        })

    speakers = set(seg["speaker"] for seg in speaker_segments)
    elapsed = time.time() - start
    log.info(f"화자분리 완료: {len(speakers)}명, {len(speaker_segments)}개 구간 ({elapsed:.1f}초)")
    return speaker_segments


def merge_transcript_and_speakers(
    transcript_segments: list[dict], speaker_segments: list[dict],
    speaker_name_map: dict[str, str] | None = None,
) -> list[dict]:
    """Overlap-based merge of transcript + speaker segments.

    Args:
        speaker_name_map: {SPEAKER_XX: "이름"} 매핑 (voice profile 기반)
    """
    merged = []
    for t_seg in transcript_segments:
        best_speaker = "Unknown"
        best_overlap = 0

        for s_seg in speaker_segments:
            overlap_start = max(t_seg["start"], s_seg["start"])
            overlap_end = min(t_seg["end"], s_seg["end"])
            overlap = max(0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = s_seg["speaker"]

        # Voice profile 매핑 적용
        if speaker_name_map and best_speaker in speaker_name_map:
            best_speaker = speaker_name_map[best_speaker]

        merged.append({
            "start": t_seg["start"],
            "end": t_seg["end"],
            "speaker": best_speaker,
            "text": t_seg["text"],
        })

    return merged


def consolidate_segments(segments: list[dict], gap_threshold: float = 1.5) -> list[dict]:
    """Merge consecutive same-speaker segments within gap_threshold seconds."""
    if not segments:
        return segments

    consolidated = [dict(segments[0])]
    for seg in segments[1:]:
        prev = consolidated[-1]
        time_gap = seg["start"] - prev["end"]

        if seg["speaker"] == prev["speaker"] and time_gap <= gap_threshold:
            prev["end"] = seg["end"]
            prev["text"] += " " + seg["text"]
        else:
            consolidated.append(dict(seg))

    return consolidated


def format_timestamp(seconds: float) -> str:
    """Seconds -> MM:SS or HH:MM:SS."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _get_cache_path(audio_path: str) -> Path:
    """SHA256 해시 기반 캐시 파일 경로."""
    h = hashlib.sha256(Path(audio_path).read_bytes()).hexdigest()
    cache_dir = Path(get_settings().stt_cache_dir)
    cache_dir.mkdir(exist_ok=True)
    return cache_dir / f"{h}.json"


def _load_cache(audio_path: str) -> TranscriptResult | None:
    """캐시에서 TranscriptResult 로드. 없거나 깨졌으면 None."""
    try:
        cache_path = _get_cache_path(audio_path)
        if not cache_path.exists():
            return None
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return TranscriptResult(
            segments=[TranscriptSegment(**s) for s in data["segments"]],
            duration=data["duration"],
            speaker_count=data["speaker_count"],
            full_text=data["full_text"],
        )
    except Exception as e:
        log.warning(f"STT 캐시 로드 실패 (무시): {e}")
        return None


def _save_cache(audio_path: str, result: TranscriptResult) -> None:
    """TranscriptResult를 캐시에 저장."""
    try:
        cache_path = _get_cache_path(audio_path)
        data = {
            "segments": [
                {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text}
                for s in result.segments
            ],
            "duration": result.duration,
            "speaker_count": result.speaker_count,
            "full_text": result.full_text,
        }
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"STT 캐시 저장: {cache_path.name}")
    except Exception as e:
        log.warning(f"STT 캐시 저장 실패 (무시): {e}")


def process_audio(audio_path: str) -> TranscriptResult:
    """Full STT pipeline: transcribe -> diarize -> merge -> postprocess.

    Raises:
        STTError: on any processing failure
    """
    # 캐시 체크
    cached = _load_cache(audio_path)
    if cached:
        log.info(f"STT 캐시 히트: {Path(audio_path).name} ({cached.duration/60:.1f}분, {len(cached.segments)}세그먼트)")
        return cached

    try:
        log.info(f"파이프라인 시작: {audio_path}")
        total_start = time.time()

        # 1. STT
        transcript_segments, duration = transcribe_audio(audio_path)

        # 2. Speaker diarization
        settings = get_settings()
        if settings.enable_diarization and duration >= 30:  # 30초 이상 + 설정 활성화
            try:
                speaker_segments = diarize_audio(audio_path)

                # Voice profile 기반 화자 식별
                speaker_name_map: dict[str, str] = {}
                try:
                    from src.voice_profile import identify_speakers
                    speaker_name_map = identify_speakers(speaker_segments, audio_path)
                    if speaker_name_map:
                        log.info(f"화자 식별 결과: {speaker_name_map}")
                except Exception as e:
                    log.warning(f"화자 식별 실패 (프로필 미등록?): {e}")

                merged = merge_transcript_and_speakers(
                    transcript_segments, speaker_segments, speaker_name_map
                )
                consolidated = consolidate_segments(merged)
                segments = [
                    TranscriptSegment(
                        start=s["start"], end=s["end"],
                        speaker=s["speaker"], text=s["text"]
                    )
                    for s in consolidated
                ]
            except Exception as e:
                log.warning(f"화자분리 실패, 단일 화자로 처리: {e}")
                segments = [
                    TranscriptSegment(
                        start=s["start"], end=s["end"],
                        speaker="Speaker", text=s["text"]
                    )
                    for s in transcript_segments
                ]
        else:
            segments = [
                TranscriptSegment(
                    start=s["start"], end=s["end"],
                    speaker="Speaker", text=s["text"]
                )
                for s in transcript_segments
            ]

        # Build full text with speaker labels
        full_text = "\n".join(
            f"[{s.speaker}] {s.text}" if s.speaker != "Speaker" else s.text
            for s in segments
        )

        elapsed = time.time() - total_start
        log.info(f"파이프라인 완료: {len(segments)}개 세그먼트, {elapsed:.1f}초")

        result = TranscriptResult(
            segments=segments,
            duration=duration,
            speaker_count=len(set(s.speaker for s in segments)),
            full_text=full_text,
        )

        # 캐시 저장
        _save_cache(audio_path, result)
        return result
    except Exception as e:
        raise STTError(f"STT pipeline failed: {e}") from e
