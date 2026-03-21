"""Local STT + speaker diarization pipeline (mlx-whisper + pyannote)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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


def transcribe_audio(audio_path: str) -> tuple[list[dict], float]:
    """mlx-whisper STT (Apple Silicon GPU). Returns (segments, duration)."""
    import mlx_whisper

    log.info("STT 전사 시작 (mlx-whisper)...")
    start = time.time()

    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
        language="ko",
        word_timestamps=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
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
    transcript_segments: list[dict], speaker_segments: list[dict]
) -> list[dict]:
    """Overlap-based merge of transcript + speaker segments."""
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


def process_audio(audio_path: str) -> TranscriptResult:
    """Full STT pipeline: transcribe -> diarize -> merge -> postprocess.

    Raises:
        STTError: on any processing failure
    """
    try:
        log.info(f"파이프라인 시작: {audio_path}")
        total_start = time.time()

        # 1. STT
        transcript_segments, duration = transcribe_audio(audio_path)

        # 2. Build segments (no speaker diarization)
        segments = [
            TranscriptSegment(
                start=s["start"], end=s["end"], speaker="Speaker", text=s["text"]
            )
            for s in transcript_segments
        ]

        full_text = "\n".join(s.text for s in segments)

        elapsed = time.time() - total_start
        log.info(f"파이프라인 완료: {len(segments)}개 세그먼트, {elapsed:.1f}초")

        return TranscriptResult(
            segments=segments,
            duration=duration,
            speaker_count=len(set(s.speaker for s in segments)),
            full_text=full_text,
        )
    except Exception as e:
        raise STTError(f"STT pipeline failed: {e}") from e
