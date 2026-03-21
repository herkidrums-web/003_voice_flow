"""
PoC: 로컬 STT + 화자분리 파이프라인
Usage: python transcribe.py /path/to/audio.m4a
"""

import sys
import os
import json
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def transcribe_audio(audio_path: str):
    """mlx-whisper로 오디오 전사 (Apple Silicon GPU 가속)"""
    import mlx_whisper

    print("[1/3] STT 전사 중 (mlx-whisper, Apple Silicon GPU)...")
    start = time.time()

    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
        language="ko",
        word_timestamps=True,
        condition_on_previous_text=False,  # hallucination 반복 루프 방지
        no_speech_threshold=0.6,
    )

    transcript_segments = []
    for segment in result["segments"]:
        text = segment["text"].strip()
        # hallucination 필터: 빈 텍스트, 1글자 반복, 비한국어 잡음 제거
        if not text:
            continue
        if len(text) <= 1 and text in ("아", "어", "네", "음", ""):
            continue
        # 러시아어/영어 등 비한국어 hallucination 제거
        if any(ord(c) > 0x0400 and ord(c) < 0x0500 for c in text):  # Cyrillic
            continue
        transcript_segments.append({
            "start": segment["start"],
            "end": segment["end"],
            "text": text,
        })

    duration = result.get("duration", transcript_segments[-1]["end"] if transcript_segments else 0)
    print(f"  → 전사 완료: {len(transcript_segments)}개 세그먼트, "
          f"오디오 길이 {duration/60:.1f}분 ({time.time() - start:.1f}초)")

    return transcript_segments, duration


def diarize_audio(audio_path: str):
    """pyannote로 화자분리"""
    from pyannote.audio import Pipeline

    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("  ❌ HF_TOKEN이 .env에 설정되어 있지 않습니다!")
        print("  → https://huggingface.co/settings/tokens 에서 토큰을 생성하세요")
        print("  → https://huggingface.co/pyannote/speaker-diarization-3.1 에서 모델 사용 동의 필요")
        sys.exit(1)

    print("[2/3] 화자분리 중...")
    start = time.time()

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )

    result = pipeline(audio_path, min_speakers=2, max_speakers=5)

    # pyannote 4.x: DiarizeOutput → .speaker_diarization (Annotation)
    annotation = result.speaker_diarization

    # 화자별 세그먼트 추출
    speaker_segments = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        speaker_segments.append({
            "start": turn.start,
            "end": turn.end,
            "speaker": speaker,
        })

    speakers = set(seg["speaker"] for seg in speaker_segments)
    print(f"  → 화자분리 완료: {len(speakers)}명 감지, "
          f"{len(speaker_segments)}개 구간 ({time.time() - start:.1f}초)")

    return speaker_segments


def merge_transcript_and_speakers(transcript_segments, speaker_segments):
    """전사본과 화자분리 결과를 병합"""
    print("[3/3] 전사본 + 화자분리 병합 중...")

    merged = []
    for t_seg in transcript_segments:
        t_mid = (t_seg["start"] + t_seg["end"]) / 2

        # 해당 전사 세그먼트의 중간 시점에 가장 많이 겹치는 화자 찾기
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


def deduplicate_segments(segments):
    """연속 반복 텍스트 제거 (hallucination 패턴)"""
    if not segments:
        return segments

    deduped = [segments[0]]
    for seg in segments[1:]:
        prev_text = deduped[-1]["text"]
        curr_text = seg["text"]

        # 완전 동일 텍스트 연속 → 스킵
        if curr_text == prev_text:
            # 이전 세그먼트의 end만 확장
            deduped[-1]["end"] = seg["end"]
            continue

        # 이전 텍스트가 현재 텍스트의 시작부분에 포함 (부분 반복)
        # 예: "자유로운 영혼이..." → "자유로운 영혼이..." → "자유로운 영혼인데"
        if curr_text.startswith(prev_text[:min(len(prev_text), 8)]) and len(prev_text) < 15:
            deduped[-1] = seg  # 더 긴/완성된 버전으로 교체
            continue

        deduped.append(seg)

    return deduped


def consolidate_segments(segments, gap_threshold=1.5):
    """같은 화자의 연속 세그먼트를 하나로 병합 (gap_threshold초 이내)"""
    if not segments:
        return segments

    consolidated = [dict(segments[0])]  # 복사
    for seg in segments[1:]:
        prev = consolidated[-1]
        time_gap = seg["start"] - prev["end"]

        # 같은 화자 + 시간 간격이 threshold 이내 → 병합
        if seg["speaker"] == prev["speaker"] and time_gap <= gap_threshold:
            prev["end"] = seg["end"]
            prev["text"] += " " + seg["text"]
        else:
            consolidated.append(dict(seg))

    return consolidated


def format_timestamp(seconds: float) -> str:
    """초 → MM:SS 형식"""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def print_results(merged_segments):
    """결과 출력"""
    print("\n" + "=" * 60)
    print("📋 전사 결과 (화자분리)")
    print("=" * 60 + "\n")

    current_speaker = None
    for seg in merged_segments:
        if seg["speaker"] != current_speaker:
            current_speaker = seg["speaker"]
            print(f"\n[{current_speaker}] ({format_timestamp(seg['start'])})")

        print(f"  {seg['text']}")


def save_results(merged_segments, audio_path: str, duration: float):
    """결과를 JSON 파일로 저장"""
    output_path = Path(audio_path).with_suffix(".json")

    result = {
        "audio_file": str(audio_path),
        "duration_seconds": duration,
        "duration_formatted": format_timestamp(duration),
        "speaker_count": len(set(s["speaker"] for s in merged_segments)),
        "segments": merged_segments,
        "full_text": "\n".join(
            f"[{s['speaker']}] {s['text']}" for s in merged_segments
        ),
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n💾 결과 저장: {output_path}")
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python transcribe.py <audio_file>")
        print("  예: python transcribe.py ~/Downloads/meeting.m4a")
        sys.exit(1)

    audio_path = sys.argv[1]
    if not os.path.exists(audio_path):
        print(f"❌ 파일을 찾을 수 없습니다: {audio_path}")
        sys.exit(1)

    print(f"🎙️ 오디오 파일: {audio_path}")
    print(f"  파일 크기: {os.path.getsize(audio_path) / 1024 / 1024:.1f}MB")
    print()

    total_start = time.time()

    # 1. STT 전사 (mlx-whisper, Apple Silicon GPU)
    transcript_segments, duration = transcribe_audio(audio_path)

    # 2. 화자분리
    speaker_segments = diarize_audio(audio_path)

    # 3. 병합
    merged = merge_transcript_and_speakers(transcript_segments, speaker_segments)

    # 4. 후처리: 반복 제거 + 세그먼트 병합
    print("[후처리] 반복 제거 + 세그먼트 병합...")
    before_count = len(merged)
    merged = deduplicate_segments(merged)
    after_dedup = len(merged)
    merged = consolidate_segments(merged)
    print(f"  → {before_count}개 → 반복제거 {after_dedup}개 → 병합 {len(merged)}개")

    # 결과 출력 + 저장
    print_results(merged)
    save_results(merged, audio_path, duration)

    total_time = time.time() - total_start
    print(f"\n⏱️ 총 처리 시간: {format_timestamp(total_time)}")
    print(f"  (오디오 대비 {total_time/duration*100:.0f}% 시간 소요)")


if __name__ == "__main__":
    main()
