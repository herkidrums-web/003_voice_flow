# Voice Flow

Apple Watch 음성메모 → 로컬 STT → Claude 분석 → Notion 자동 생성 파이프라인

## 개요

음성메모를 녹음하면 자동으로 전사, 교정, 분석하여 Notion 데이터베이스에 상세한 미팅노트를 생성합니다.

### 파이프라인

```
Apple Watch 음성메모 (.m4a)
  ↓  watchdog 파일 감지
  ↓  mlx-whisper STT (Apple Silicon GPU)
  ↓  Claude Pass 1: STT 오류 교정
  ↓  Claude Pass 2: 종합 미팅 분석
  ↓  Notion 개인기록_DB 페이지 생성
```

### 주요 기능

- **로컬 STT**: mlx-whisper로 Apple Silicon GPU 가속 전사 (72분 녹음 → ~7분 처리)
- **2-pass Claude 분석**:
  - Pass 1: 동음이의어, 고유명사, 문장 경계 등 STT 오류 교정
  - Pass 2: 구어체 인용 포함 상세 미팅노트 + 전략적 인사이트 생성
- **Notion DB 연동**: 프로퍼티 자동 추출 (유형, 프로젝트, 고객명, 태그 등)
- **리치 블록 구조**: heading/quote/bulleted_list/numbered_list 등 구조화된 페이지
- **launchd 데몬**: macOS 시작 시 자동 실행, 새 음성메모 실시간 감지

## 프로젝트 구조

```
003_voice_flow/
├── src/
│   ├── watcher.py         # watchdog 파일 감시
│   ├── stt.py             # mlx-whisper STT + 후처리
│   ├── summarizer.py      # Claude 2-pass (교정 + 분석)
│   ├── notion_writer.py   # Notion DB 페이지 생성
│   └── pipeline.py        # 오케스트레이션 + 재시도
├── tests/                 # 46개 단위 테스트
├── config.py              # pydantic-settings 설정
├── main.py                # 데몬 엔트리포인트
└── poc/                   # PoC 코드 보존
```

## 설정

### 환경 변수 (.env)

```bash
HF_TOKEN=...                    # HuggingFace (pyannote 용, 현재 미사용)
ANTHROPIC_API_KEY=...           # Claude API
NOTION_API_KEY=...              # Notion Integration
NOTION_PARENT_PAGE_ID=...       # 상위 페이지 ID
NOTION_DATABASE_ID=...          # 개인기록_DB ID
WATCH_DIR=...                   # 음성메모 디렉토리
```

### 실행

```bash
# 직접 실행
python main.py

# launchd 데몬 등록
cp com.swlee.voiceflow.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.swlee.voiceflow.plist
```

### 테스트

```bash
pytest tests/ -v
```

## 기술 스택

- **STT**: mlx-whisper (whisper-large-v3-mlx)
- **LLM**: Claude Sonnet 4.5 (anthropic SDK, streaming)
- **Notion**: notion-client 2.7.0
- **파일 감시**: watchdog (FSEvents)
- **설정**: pydantic-settings
- **테스트**: pytest + pytest-mock
