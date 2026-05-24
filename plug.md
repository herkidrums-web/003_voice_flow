# VoiceFlow Plugin Manifest

> Apple Watch/iPhone 음성메모를 STT → 멀티에이전트 분석 → Notion 자동화로 연결하고, 매일 아침 Wiki anchor 기반 일일 브리핑을 자동 생성하며, iPhone Discord에서 양방향 컨트롤하는 통합 플러그인.

## 메타데이터

| 항목 | 값 |
|------|---|
| **name** | `voiceflow` |
| **version** | `1.0.0` |
| **author** | 이성우 (Lee Seongwoo) — herkidrums@gmail.com |
| **license** | MIT |
| **manifest** | `.claude-plugin/plugin.json` (Claude Code 표준) |
| **project root** | `/Users/swlee/Documents/Coding/002_voice_flow_v3/` |

---

## 구성 (Components)

### 1. 스킬 (Skills) — 2개

| Skill | 경로 | 트리거 |
|-------|------|--------|
| `voiceflow` | `~/.claude/skills/voiceflow/SKILL.md` | `/voiceflow`, `/음성메모`, "음성메모 처리해줘", "voiceflow 상태 확인" |
| `morning-briefing` | `~/.claude/skills/morning-briefing/SKILL.md` | `/morning-briefing`, "오늘 브리핑", "아침 브리핑", "일일 정리" |

### 2. 에이전트 (Agents) — 11개

| Agent | 모델 | 역할 |
|-------|------|------|
| STTAgent | mlx-whisper (로컬) | 음성 → 텍스트, SHA256 캐시 |
| NERAgent | claude-sonnet-4-6 | 신규 고유명사 추출 |
| DictionaryAgent | 규칙 기반 | custom_terms.txt 후처리 |
| GroupingAgent | claude-sonnet-4-6 | 유사 파일 에피소드 병합 |
| AnalysisAgent | claude-opus-4-7 | 토픽별 5W1H + 핵심사실/결정/액션 |
| ValidationAgent | claude-sonnet-4-6 | Analysis 품질 검증 |
| SelfHealAgent | claude-sonnet-4-6 | Validation 실패 시 재시도 파라미터 결정 |
| ClassificationAgent | claude-haiku-4-5-20251001 | 미팅 유형/프로젝트 분류 |
| NotionAgent | Notion API | Notion 페이지 생성 |
| WikiAgent | 규칙 기반 | wiki/index.md 자동 업데이트 |
| **BriefingAgent** ★ | claude-opus-4-7 | Wiki anchor 기반 토픽 그룹핑 + 이성우 담당 To-Do 분리 |

### 3. 자동화 데몬 (launchd LaunchAgents) — 4개

`~/Library/LaunchAgents/`

| Label | 주기 | 역할 |
|-------|------|------|
| `com.swlee.voiceflow-orchestrator` | **매시 :05분** | recordings_mirror/ → STT → Notion 파이프라인 |
| `com.swlee.voiceflow-morning-briefing` | **매일 07:00** | Wiki anchor 기반 일일 브리핑 → Notion + Discord 알림 |
| `com.swlee.voiceflow-discord-listener` | **24/7** | iPhone Discord 명령 → Mac 자동 실행+답장 (30초 polling) |
| `com.swlee.voiceflow-voicememos-activator` | 주기적 | Voice Memos 앱 백그라운드 활성화 (iCloud sync 트리거) |

### 4. Discord 명령 (양방향 컨트롤)

iPhone Discord text-channel(`1490245243285667981`)에 메시지 전송 → Mac이 자동 처리 + 결과 답장.

| 입력 키워드 | 동작 | 응답 |
|------------|------|------|
| `ping` | 🏓 생존 확인 | 30초 안 |
| `음성메모`, `voiceflow`, `처리해줘` | `manual_run.sh` (sync + orchestrator) | 5~30분 |
| `브리핑`, `일일`, `morning` | `morning_briefing.py` 단독 | 1~2분 |
| `오늘 처리해줘`, `전체`, `all` | **콤보**: manual_run.sh + morning_briefing.py | 5~30분 |
| `상태`, `status` | processing_state.jsonl 요약 | 30초 안 |

### 5. 외부 통합

- **Voice Memos** (Apple Watch/iPhone → iCloud → Mac)
- **mlx-whisper** (로컬 STT, M-series GPU)
- **Claude CLI** (구독 인증, `/Users/swlee/.local/bin/claude`)
- **Notion API** (개인기록_DB + 일일 브리핑 DB)
- **Google Calendar** (normal + work 계정, 일일 브리핑 일정 자동 표시)
- **Discord Bot API** (iPhone ↔ Mac 양방향 메시지)
- **Wiki 온톨로지** (`/Users/swlee/Documents/Coding/000_second_brain/wiki/index.md`, 28개 anchor)

---

## 스케줄 등록 내역 (Schedule Registration)

매일/매시 자동 동작하는 cron-style 스케줄.

### launchd LaunchAgent 등록 (활성)

```bash
$ launchctl list | grep voiceflow
15663   0  com.swlee.voiceflow-discord-listener      # 24/7 polling
-       0  com.swlee.voiceflow-orchestrator          # 매시 :05분
-       0  com.swlee.voiceflow-morning-briefing      # 매일 07:00
-       0  com.swlee.voiceflow-voicememos-activator  # 주기 활성화
```

### 스케줄 정의

| Agent | Schedule | Plist 위치 |
|-------|----------|------------|
| morning-briefing | `매일 07:00` (StartCalendarInterval: Hour=7, Minute=0) | `~/Library/LaunchAgents/com.swlee.voiceflow-morning-briefing.plist` |
| orchestrator | `매시 :05분` (StartCalendarInterval: Minute=5) | `~/Library/LaunchAgents/com.swlee.voiceflow-orchestrator.plist` |
| discord-listener | `24/7 KeepAlive` (RunAtLoad + KeepAlive: true) | `~/Library/LaunchAgents/com.swlee.voiceflow-discord-listener.plist` |
| voicememos-activator | `주기 활성화` (Voice Memos 앱 켜기) | `~/Library/LaunchAgents/com.swlee.voiceflow-voicememos-activator.plist` |

### 비활성화된(폐기) 자동화

자동 sync는 macOS FDA 정책 한계로 폐기되어 `~/Library/LaunchAgents/_disabled/`로 이동:

- `com.swlee.voiceflow-sync.plist` — Shortcuts 기반 sync (launchd context에서 권한 깨짐)
- `com.swlee.voiceflow-briefing.plist` — v1 daily_briefing (v2로 교체)

---

## 데이터 흐름

```
[iPhone/Apple Watch 녹음]
  ↓ iCloud (voicememod)
[Voice Memos 앱 — Mac]
  ↓ shortcuts run "VoiceFlow Sync" (수동 트리거, 매일 1회)
[recordings_mirror/]
  ↓ orchestrator (매시 :05분)
[STT → NER → Dictionary → Grouping → Analysis → Validation → Classification → Notion → Wiki]
  ↓
[Notion 개인기록_DB 미팅 페이지]
  ↓
[BriefingAgent — 매일 07:00 또는 Discord 명령]
  ↓
[Notion 일일 브리핑 DB 페이지 — 토픽 그룹핑 + 이성우 To-Do + Wiki 연결]
  ↓
[Discord 자동 알림 → iPhone 출근 길]
```

---

## 디렉토리 구조

```
002_voice_flow_v3/
├── .claude-plugin/
│   └── plugin.json            # Claude Code 표준 manifest
├── plug.md                    # ← 이 파일 (사람이 읽는 manifest)
├── CLAUDE.md                  # 운영 가이드
├── README.md
├── config.py
├── custom_terms.txt
├── main.py
├── pyproject.toml
├── processing_state.jsonl     # 파일별 처리 stage 추적
├── poc/.venv/                 # Python venv
├── recordings_mirror/         # 음성 파일 mirror
├── src/
│   ├── agents/                # 11개 에이전트
│   ├── calendar_fetcher.py    # Google Calendar 통합
│   └── ...
└── scripts/
    ├── manual_run.sh          # sync + orchestrator 수동 트리거
    ├── run_orchestrator.py    # 파이프라인 엔트리
    ├── morning_briefing.py    # Wiki anchor 기반 브리핑 v2
    ├── daily_briefing.py      # (legacy v1, 사용 안 함)
    ├── discord_listener.py    # 24/7 Discord polling
    ├── setup_google_calendar_oauth.py
    └── ...
```

---

## 설치 / 활성화 검증

```bash
# 1. 플러그인 manifest 확인
cat .claude-plugin/plugin.json

# 2. launchd 데몬 활성화 확인
launchctl list | grep voiceflow

# 3. 각 데몬 plist 확인
ls ~/Library/LaunchAgents/com.swlee.voiceflow-*.plist

# 4. 처리 상태 확인 (현재까지 처리된 파일 수)
wc -l processing_state.jsonl

# 5. 스킬 인식 여부 (Claude Code에서)
ls ~/.claude/skills/voiceflow ~/.claude/skills/morning-briefing
```

---

## 주요 혁신 (vs v1)

| 영역 | v1 (legacy) | v2 (current) |
|------|-------------|---------------|
| 자동 sync | bash + FDA (안정성 0) | 반수동 + Discord 트리거 |
| 일일 브리핑 | 미팅 단위 나열, 모든 액션 To-Do | Wiki anchor 토픽 그룹핑 + 이성우 담당 To-Do만 |
| 온톨로지 | 없음 | wiki/index.md 28개 anchor 자동 연결 |
| 명령 인터페이스 | 데스크탑 CLI 전용 | Discord 양방향 (iPhone 어디서나) |
| 자동 알림 | macOS notification만 | Discord push 알림 (출근 길) |
| 캘린더 | 없음 | Google Calendar 2계정 통합 |
| JSON 파싱 안정성 | json.loads (Extra data 에러 빈발) | json.JSONDecoder.raw_decode (관용) |
