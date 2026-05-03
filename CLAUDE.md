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

## 후처리 규칙 (STT → Notion 사이 필수)
- STT 직후, Notion 업로드 직전에 **업계 용어 사전을 반드시 참조하여 후처리**한다.
- 사전 소스: `custom_terms.txt` + Notion DB (`src/dictionary.py`로 로드)
- 적용 대상: 화자/조직/제품/사업/약어 등 STT 오인식이 잦은 고유명사
  - 예: "원혁명" → "권용현 부사장", "김태현" → "김태원 대표(코람코)"
  - 예: AIDC, DBO, KAM, MEDDPICC, PQCT 등 약어/업계 용어 표기 통일
- 처리 위치: `src/summarizer.py` Pass 1(STT 교정) 단계에서 사전 매칭을 우선 적용한 뒤, Claude가 문맥 기반 추가 교정
- 신규 오인식 패턴 발견 시 `custom_terms.txt`에 즉시 추가하여 다음 배치부터 자동 반영
- 검증: 보고대상자/임원 호칭(특히 "이성우 담당" — "상무" 금지)은 후처리 후 한 번 더 확인


## Notion 페이지 생성 후 Wiki 인덱스 업데이트 (필수)
- Notion 페이지 생성이 완료되면, 곧바로 `000_second_brain/wiki/index.md`에 해당 항목을 반영한다.
- 누락 시 세컨드 브레인이 새 미팅노트를 인지하지 못하므로, 페이지 생성 → 인덱스 업데이트는 한 묶음으로 처리한다.

## launchd 데몬
3개 데몬이 있음:
- `com.swlee.voiceflow-sync` — WatchPaths + 5분 주기, Voice Memos → recordings_mirror/ 복사
- `com.swlee.voiceflow-orchestrator` — 매시 :05분, recordings_mirror/ → Notion (v3)
- `com.swlee.voiceflow-briefing` — 평일 08:00, 일일 브리핑 생성

### 데몬 상태 확인
```bash
launchctl list | grep voiceflow           # PID, exit code 확인
cat /tmp/voiceflow-orchestrator.err.log  # 최근 에러
cat orchestrator.heartbeat               # 마지막 정상 실행 시각
```

### 데몬이 죽는 주요 원인 & 수정 방법

| 증상 | 원인 | 수정 |
|------|------|------|
| `ModuleNotFoundError: config` | PYTHONPATH 미설정 | run_orchestrator.py 상단 `sys.path.insert` 이미 수정됨 |
| `TypeError: authentication` | .env에 `ANTHROPIC_API_KEY` 없음 | `.env`에 `ANTHROPIC_API_KEY=sk-ant-...` 추가 |
| `exit code 2` | 배치 처리 일부 실패 | `/tmp/voiceflow-orchestrator.err.log` 확인 |
| `sync.log stale` | Voice Memos 앱 미실행 | `open -a "Voice Memos"` 실행 후 대기 |

### 데몬 재시작 구조 (KeepAlive)
- `KeepAlive: {SuccessfulExit: false}` + `ThrottleInterval: 60`
- 비정상 종료(exit≠0) 시 launchd가 60초 후 자동 재시작
- 정상 종료(exit=0: no files, sync stale)는 재시작 안 함 → 다음 :05분 대기

### 수동 재시작
```bash
launchctl unload ~/Library/LaunchAgents/com.swlee.voiceflow-orchestrator.plist
launchctl load   ~/Library/LaunchAgents/com.swlee.voiceflow-orchestrator.plist
```

### Voice Memos iCloud 동기화 구조
- iPhone/Watch 녹음 → iCloud → Mac Voice Memos 앱 → 로컬 폴더 (FDA 필요)
- 로컬 경로: `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/`
- **Voice Memos 앱이 열려있지 않으면 iCloud에서 내려오지 않음** (앱이 sync trigger)
- 앱을 열어놓지 않아도 되도록 sync 데몬이 5분마다 fallback 폴링하지만,
  앱이 닫혀있으면 최신 녹음이 로컬에 도착하지 않을 수 있음
- 자동 실행: `open -a "Voice Memos"` 또는 아래 방법

### ANTHROPIC_API_KEY 설정 (필수, 미설정 시 NER/Analysis 전부 실패)
```bash
# .env 파일에 추가 (Claude Code 키와 별도로 관리)
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> /Users/swlee/Documents/Coding/002_voice_flow_v3/.env
```

## 테스트
```bash
poc/.venv/bin/python -m pytest tests/ -v
```

## 환경
- Python 3.12, venv: `poc/.venv/bin/python` (루트 `.venv` 아님)
- Claude API timeout: 1800s (긴 녹취록 대응)
- `.env`: HF_TOKEN, ANTHROPIC_API_KEY, NOTION_API_KEY 등
