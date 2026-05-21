# 002_voice_flow_v3

Apple Watch/iPhone 음성메모 → STT(WhisperX) → 멀티에이전트 분석 → Notion 자동 파이프라인 (v3).

---

## 전체 아키텍처

```
[iPhone/Watch 녹음]
    ↓ iCloud (voicememod 데몬이 CloudKit sync)
[Voice Memos 앱] ← 앱이 열려있어야 iCloud 다운로드 발생
    ↓ WatchPaths + 5분 주기 (com.swlee.voiceflow-sync)
[recordings_mirror/]  ← + orchestrator 시작 시 fallback 직접 복사
    ↓ 매시 :05분 (com.swlee.voiceflow-orchestrator)
[STT] → [NER] → [Dictionary] → [Grouping] → [Analysis+SelfHeal] → [Validation] → [Classification] → [Notion] → [Wiki]
    ↓ 배치 완료 후
[TodoAgent] → todos_YYYY-MM-DD.json
```

---

## ★★ Notion 타겟 DB — 개인기록_DB

VoiceFlow v3의 모든 회의록은 **개인기록_DB**에 저장한다 (기존에 쌓아온 DB 그대로 사용).

- **DB**: 개인기록_DB
- **DB ID**: `3227b1d7-e380-80ed-ae95-c763973229b4`
- **env 변수**: `NOTION_DATABASE_ID=3227b1d7-e380-80ed-ae95-c763973229b4`
- **[고객] 미팅노트 2026 DB는 사용하지 않는다** — 삭제하거나 무시

### 개인기록_DB 필드 매핑 (notion_writer.py 기준)

| 필드명 | Notion 타입 | 파이프라인 소스 |
|--------|------------|---------------|
| `회의 이름` | title | `meeting_title` |
| `날짜` | date | 파일명 `YYYYMMDD` 추출 |
| `유형` | select | ClassificationAgent `meeting_type` |
| `참석자 1` | text | AnalysisAgent `participants` join |
| `요약` | text | AnalysisAgent `summary` |
| `다음액션` | text | AnalysisAgent `actions` join |
| `상태` | select | ClassificationAgent `status` (유효값: "후속필요", "완료", "진행 중") |
| `우선순위` | select | ClassificationAgent `priority` (예: "긴급", "높음", "보통") |
| `고객명` | select | AnalysisAgent `customer` |

### 일일 브리핑 (개인기록_DB와 별도)

- 브리핑은 `NOTION_BRIEFING_DATABASE_ID`에 별도 저장 — 개인기록_DB와 독립
- `scripts/daily_briefing.py`가 매일 08:00 (평일) 자동 실행
- 전날 개인기록_DB에 저장된 회의록을 읽어 브리핑 페이지 생성

---

## 에이전트 목록 & 모델 할당

| 에이전트 | 파일 | 모델 | 역할 |
|---------|------|------|------|
| STTAgent | `src/agents/stt_agent.py` | mlx-whisper (로컬) | 음성→텍스트, SHA256 캐시 |
| NERAgent | `src/agents/ner_agent.py` | claude-sonnet-4-6 | 신규 고유명사 추출 |
| DictionaryAgent | `src/agents/dictionary_agent.py` | 규칙 기반 (LLM 없음) | custom_terms.txt 후처리 |
| GroupingAgent | `src/agents/grouping_agent.py` | claude-sonnet-4-6 | 유사 파일 에피소드 병합 |
| AnalysisAgent | `src/agents/analysis_agent.py` | claude-opus-4-7 | 토픽별 5W1H + 핵심사실/결정/액션 |
| ValidationAgent | `src/agents/validation_agent.py` | claude-sonnet-4-6 | Analysis 품질 검증 |
| SelfHealAgent | `src/agents/self_heal_agent.py` | claude-sonnet-4-6 | Validation 실패 시 재시도 파라미터 결정 |
| ClassificationAgent | `src/agents/classification_agent.py` | claude-haiku-4-5-20251001 | 미팅 유형/프로젝트 분류 |
| NotionAgent | `src/agents/notion_agent.py` | Notion API (LLM 없음) | Notion 페이지 생성 |
| WikiAgent | `src/agents/wiki_agent.py` | 규칙 기반 (LLM 없음) | wiki/index.md 자동 업데이트 |
| TodoAgent | `src/agents/todo_agent.py` | claude-haiku-4-5-20251001 | 배치 액션 → 다음날 To-Do 파일 생성 |

### 모델 ID (config.py 기준)
```python
claude_model_haiku  = "claude-haiku-4-5-20251001"
claude_model_sonnet = "claude-sonnet-4-6"
claude_model_opus   = "claude-opus-4-7"
```

---

## ★★ Claude 호출 규칙 (API 직접 호출 절대 금지)

```python
# ✅ 올바른 방법 — CLI subprocess (구독 과금, API 키 불필요)
from src.agents.cli_client import ClaudeCLIClient
client = ClaudeCLIClient(cli_path="/Users/swlee/.local/bin/claude", timeout=1800)

# ❌ 금지 — Anthropic SDK 직접 (API 별도 과금)
from anthropic import Anthropic
client = Anthropic(api_key=...)
```

- CLI path: `/Users/swlee/.local/bin/claude`
- `cli_client.py`의 `_strip_fence()` 가 Claude CLI의 markdown fence(```json...```) 자동 제거
- 모든 에이전트는 `client.messages.create(model=..., max_tokens=..., messages=[...])` 동일 인터페이스 사용

---

## 파이프라인 처리 순서 (process_batch)

```
per-file (병렬 아님, 순차):
  1. STTAgent     — mlx-whisper 전사, SHA256 캐시 히트 시 0.03s
  2. NERAgent     — 신규 고유명사 추출 → custom_terms.txt 갱신 후보
  3. DictionaryAgent — 사전 기반 텍스트 후처리

batch:
  4. GroupingAgent — 유사 파일 에피소드 그룹핑

per-group:
  5. AnalysisAgent + SelfHealAgent loop (최대 self_heal_max_retries=5회)
  6. ValidationAgent — 품질 검증
  7. ClassificationAgent — 미팅 유형/프로젝트
  8. NotionAgent — Notion 페이지 생성
  9. WikiAgent — wiki/index.md 업데이트

배치 완료 후:
  10. TodoAgent — 전체 actions 수집 → todos_YYYY-MM-DD.json 저장
```

---

## 상태 머신 (processing_state.jsonl)

- 파일: `processing_state.jsonl` (append-only JSONL)
- Stage 순서: `none → stt → ner → dictionary → grouping → analysis → validation → classification → notion → wiki → done`
- `status=done + stage=wiki` 인 파일만 완료로 간주, 나머지는 재시도
- `status=too_long` 파일은 영구 스킵
- **v2에서 처리한 파일**: `stage=wiki, status=done, meta={note:"v2_already_processed"}` 로 마킹해 중복 방지

---

## 후처리 규칙 (STT → Notion 사이 필수)

- STT 직후, Notion 업로드 직전에 **업계 용어 사전을 반드시 참조하여 후처리**
- 사전 소스: `custom_terms.txt` + Notion DB (`src/dictionary.py`로 로드)
- 적용 대상: 화자/조직/제품/사업/약어 등 STT 오인식이 잦은 고유명사
  - 예: "원혁명" → "권용현 부사장", "김태현" → "김태원 대표(코람코)"
  - 예: AIDC, DBO, KAM, MEDDPICC, PQCT 등 약어/업계 용어 표기 통일
- 처리 위치: `src/agents/ner_agent.py` + `src/agents/dictionary_agent.py`
- 신규 오인식 패턴 발견 시 `custom_terms.txt`에 즉시 추가
- 검증: 이성우 **담당** ("상무" 절대 금지), 임원 호칭 후처리 후 재확인

---

## Notion 페이지 생성 후 Wiki 인덱스 업데이트 (필수)

- Notion 페이지 생성 완료 → 곧바로 `000_second_brain/wiki/index.md` 반영
- 누락 시 세컨드 브레인이 새 미팅노트를 인식 못함
- WikiAgent가 자동 처리 (`src/agents/wiki_agent.py`)

---

## ★★ 반수동 워크플로우 (2026-05-13 회귀 결정)

**자동 sync는 폐기.** launchd context FDA 권한이 macOS 정책상 안정적으로 유지되지 않아 (bash/Python/Shortcuts 모두 시도, 모두 주기적으로 깨짐) 매일 같은 오류 반복. **반수동으로 회귀**:

```
[1] 사용자 트리거 — 매일 저녁/아침 1회 수동 실행
    scripts/manual_run.sh   ← sync + orchestrator 한 번에
[2] sync — shortcuts run "VoiceFlow Sync" (수동 컨텍스트는 권한 정상)
[3] orchestrator — recordings_mirror/ → STT → Notion (수동 또는 매시 :05분 자동)
```

### launchd 데몬 구성 (현재)

| 데몬 | Label | 상태 | 역할 |
|------|-------|------|------|
| ~~sync~~ | ~~`com.swlee.voiceflow-sync`~~ | **비활성** (`_disabled/`로 이동) | Voice Memos → mirror, 수동 트리거로 대체 |
| orchestrator | `com.swlee.voiceflow-orchestrator` | 활성 (매시 :05분) | recordings_mirror/ → Notion |
| briefing | `com.swlee.voiceflow-briefing` | 활성 (평일 08:00) | 일일 브리핑 생성 |
| voicememos-activator | `com.swlee.voiceflow-voicememos-activator` | 활성 | Voice Memos 앱 백그라운드 활성화 (iCloud sync 트리거) |
| discord-listener | `com.swlee.voiceflow-discord-listener` | 활성 (24/7) | iPhone Discord 명령 수신 → VoiceFlow 실행 + 캘린더 등록 |

### 수동 실행 동선

```bash
# 권장: sync + orchestrator 한꺼번에
/Users/swlee/Documents/Coding/002_voice_flow_v3/scripts/manual_run.sh

# sync만 (orchestrator는 다음 :05분에 자동 처리)
shortcuts run "VoiceFlow Sync" && tail -3 /tmp/voiceflow-sync.log
```

자연어 트리거(스킬): "음성메모 처리해줘", "/voiceflow"

### Discord 캘린더 등록 (2026-05-21 추가)

iPhone Discord에서 다음 중 하나로 트리거 → Google Calendar(primary)에 자동 등록:
- 이미지 첨부(카톡 캡처 등) — 텍스트 없어도 OK
- 메시지가 "일정 ..." / "캘린더 ..." / "calendar ..." / "/calendar ..." 로 시작

처리 흐름:
- 📅 리액션 → Claude CLI(Vision + Google Calendar MCP) → 답장
- ✅ 등록 완료 (요약 + event_link)
- ❓ 모호한 일시는 되묻기 → 사용자 답장 시 기존 `_handle_claude` 세션 컨텍스트로 처리
- 🤷 일정 외 메시지는 조용히 무시
- ⚠️ 이미지 다운로드 실패 시 사용자에 경고 후 텍스트로 처리 시도

수정/삭제는 자연어 답장 ("방금 거 15시로", "삭제해줘") — 채널 세션 컨텍스트로 처리됨.

소스: `scripts/discord_listener.py` 의 `_handle_calendar`.

### KeepAlive 설정 (orchestrator/briefing만)
- `KeepAlive: {SuccessfulExit: false}` + `ThrottleInterval: 60`
- 비정상 종료(exit≠0) → 60초 후 자동 재시작
- 정상 종료(exit=0) → 다음 :05분 또는 WatchPaths 트리거 대기

### Mac 절전/복귀 동작
- `StartCalendarInterval @:05` (orchestrator): 복귀 즉시 → 놓친 :05분 interval 자동 보정

### Voice Memos 앱 / iCloud 동기화
- iPhone/Watch 녹음이 Mac으로 내려오려면 Voice Memos 앱이 열려있어야 함
- `voicememos-activator` 데몬이 백그라운드에서 주기 활성화
- 앱 실행 명령: `open /System/Applications/VoiceMemos.app` (**`open -a "Voice Memos"` 금지** — 앱 이름 인식 실패)
- `voicememod` (CloudKit sync) 재시작: `launchctl kickstart gui/$(id -u)/com.apple.voicememod`

### 자동 sync 폐기 사유 (이력)
1. Python + FDA: `brew upgrade python`마다 Cellar 경로 변경으로 FDA 리셋 (3회 재발)
2. bash + FDA: `/bin/bash` FDA가 시스템 업데이트로 깨짐
3. Shortcuts (2026-05-07 시도): launchd → `shortcuts run` 컨텍스트는 권한 또 약화, "no m4a files" 무한 반복

**원칙**: macOS 보호 폴더(Voice Memos `Group Containers`)의 자동 접근은 launchd 환경에서 안정적이지 않다. 사용자 컨텍스트에서만 수동 트리거.

### 환경변수 (orchestrator plist)
```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
</dict>
```
ffmpeg, whisperx 등 Homebrew 바이너리 사용을 위해 PATH 주입 필수.

---

## 데몬 상태 확인 & 진단

```bash
launchctl list | grep voiceflow           # PID, exit code 확인
cat /tmp/voiceflow-orchestrator.err.log | tail -30  # 최근 에러
cat /tmp/voiceflow-sync.log | tail -10    # sync heartbeat 확인
cat /tmp/voiceflow-sync-stderr.log        # sync 에러 확인
cat orchestrator.heartbeat               # 마지막 정상 실행 시각
```

### 주요 오류 & 수정

| 증상 | 원인 | 수정 |
|------|------|------|
| `NER returned invalid JSON` | Claude CLI가 markdown fence로 감쌈 | `cli_client.py`의 `_strip_fence()` 적용됨 (자동 해결) |
| `ModuleNotFoundError: config` | PYTHONPATH 미설정 | `run_orchestrator.py` 상단 `sys.path.insert` 수정됨 |
| `STTError: Broken pipe` | mlx-whisper 일시적 오류 | 재시도 시 자동 해결 (ThrottleInterval 60s 후 재시작) |
| `sync.log stale` | sync 데몬 미실행 or Voice Memos 앱 꺼짐 | `open /System/Applications/VoiceMemos.app` 실행 후 대기 |
| `Unable to find application named 'Voice Memos'` | `open -a` 앱 이름 인식 실패 | plist에서 `open /System/Applications/VoiceMemos.app` 사용 |
| `exit code -15` | launchd가 SIGTERM으로 종료 | 정상 (수동 kill 후 자동 재시작) |
| STT ffmpeg not found | launchd PATH에 Homebrew 없음 | orchestrator plist에 EnvironmentVariables 추가됨 |
| `Name is not a property that exists` | notion_writer.py 필드명 불일치 | 개인기록_DB 필드명 (`회의 이름`, `참석자 1`, `고객명`, `상태`, `유형`, `요약`, `다음액션`, `우선순위`) 으로 매핑 |
| `Could not find database with ID` | Notion DB가 MCP 인테그레이션과 미공유, 또는 잘못된 DB ID | `.env`의 `NOTION_DATABASE_ID`를 개인기록_DB ID(`3227b1d7-e380-80ed-ae95-c763973229b4`)로 변경 |
| launchd sync → `no m4a files in Voice Memos` | FDA 미부여로 launchd Python이 Voice Memos 접근 불가 | sync plist를 `shortcuts run "VoiceFlow Sync"` 방식으로 교체 (2026-05-07) |
| 녹음이 recordings_mirror에 없음 | WATCH_DIR이 v2 경로를 가리킴 | `.env`의 `WATCH_DIR`을 `002_voice_flow_v3/recordings_mirror`로 수정 |

### 수동 재시작
```bash
# orchestrator만 (sync는 폐기됨)
launchctl unload ~/Library/LaunchAgents/com.swlee.voiceflow-orchestrator.plist
launchctl load   ~/Library/LaunchAgents/com.swlee.voiceflow-orchestrator.plist
```

---

## 파일 처리 제한

- `min_duration: 5.0초` — 5초 미만 파일 스킵
- `max_duration_minutes: 180분` — 180분 초과 파일 스킵 + macOS 알림
- STT 캐시: SHA256 기반 (`.stt_cache/`), 동일 파일 재처리 시 0.03s 캐시 히트

---

## 중복 처리 방지 (v2 → v3 마이그레이션)

v2 파이프라인(`002_voice_flow/`)에서 이미 Notion에 올린 파일은 v3 state에 수동 마킹:
```python
# state.jsonl에 추가 (중복 업로드 방지)
{"file": "파일명.m4a", "stage": "wiki", "status": "done",
 "meta": {"note": "v2_already_processed"}, "ts": "..."}
```

---

## v2 데몬 비활성화 (재부팅 시 자동 실행 방지)

```
~/Library/LaunchAgents/_disabled/
  com.swlee.voiceflow.plist           ← v2 메인 데몬 (RunAtLoad=true였음)
  com.swlee.voiceflow-stt-hourly.plist ← v2 STT 데몬 (RunAtLoad=true였음)
```
절대 다시 load하지 말 것 — v3와 충돌, 중복 Notion 페이지 생성됨.

---

## 환경

- Python 3.12, venv: `poc/.venv/bin/python` (루트 `.venv` 아님)
- Claude CLI: `/Users/swlee/.local/bin/claude` (구독 인증)
- Claude API timeout: 1800s (긴 녹취록 대응)
- `.env`: `HF_TOKEN`, `NOTION_API_KEY` 필수. `ANTHROPIC_API_KEY` 불필요 (CLI 방식)

## 테스트

```bash
poc/.venv/bin/python -m pytest tests/ -v
# 143개 테스트 전부 통과해야 정상
```
