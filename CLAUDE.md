# 002_voice_flow

Apple Watch 음성메모 → STT(WhisperX) → Claude 분석 → Notion 자동 파이프라인.

## 스킬
- `/voiceflow` 또는 "음성메모 처리해줘" — 음성메모 자동 처리
- 수동: 파일 리스트 표시 → 스킵 지정 → `process_date()` 실행
- 핵심 함수: `from src.pipeline import process_date`

## 구조
- `src/stt.py` — WhisperX STT (화자분리 없음, v4)
- `src/summarizer.py` — Claude 2-pass: STT 교정 → 토픽별 육하원칙 추출 + 데이터자산 보강
- `src/topic_merger.py` — 토픽 유사성 기반 에피소드 병합 (Union-Find)
- `src/notion_writer.py` — Notion 페이지 생성 (토픽별 5W1H 구조)
- `src/dictionary.py` — 업계 용어 사전 (custom_terms.txt + Notion DB)
- `src/pipeline.py` — 파일 그루핑 + 처리 오케스트레이션 + `process_date()`
- `src/watcher.py` — watchdog + 60s 디바운스 큐
- `config.py` — pydantic-settings 설정 + 에러 계층
- `main.py` — CLI 엔트리포인트

## 파일 그루핑 규칙
- 낮(8-17시): 파일 간격 < 5분 → 같은 회의 (통합), >= 5분 → 별도
- 저녁(17시+): 같은 날짜 저녁 파일은 항상 통합 (전화 중단 대응)
- 디바운스: 60초 대기 후 일괄 그루핑

## launchd 데몬
- `~/Library/LaunchAgents/com.swlee.voiceflow.plist`
- 자동 시작, 파일 감시 → STT → 분석 → Notion

## 테스트
```bash
poc/.venv/bin/python -m pytest tests/ -v
```

## 환경
- Python 3.12, venv: `poc/.venv/bin/python` (루트 `.venv` 아님)
- Claude API timeout: 1800s (긴 녹취록 대응)
- `.env`: HF_TOKEN, ANTHROPIC_API_KEY, NOTION_API_KEY 등
