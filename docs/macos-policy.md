# VoiceFlow v3 — macOS 정책 & 운영 가이드

## 책임 분리

| 데몬 | 언어/권한 | 트리거 | 책임 |
|------|----------|--------|------|
| `com.swlee.voiceflow-sync` | bash (FDA 보유) | WatchPaths | Voice Memos → `recordings_mirror/` 복사 |
| `com.swlee.voiceflow-orchestrator` | python venv (FDA 불필요) | hourly (:05) | `recordings_mirror/` → STT → 분석 → Notion → Wiki |
| `com.swlee.voiceflow-briefing` | python venv | 평일 08:00 | 전날 미팅 + 오늘 To-Do → Notion 일일 브리핑 DB |

**핵심 원칙**: Python은 `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/` 를 절대 직접 읽지 않는다. brew upgrade로 Python Cellar 경로가 바뀌어도 FDA 권한이 깨지지 않는 이유.

## 헬스체크

`scripts/run_orchestrator.py:_check_sync_health()` 가 매 실행마다 `sync.log` mtime을 확인:

- 24시간 이내 업데이트 있음 → 진행
- 24시간 이상 정체 → macOS notification + `exit 0` (err log 비움)

알림 메시지: "Sync stale — Voice Memos 앱을 한번 띄워주세요."

## 트러블슈팅

### 신규 녹음이 Notion에 안 올라옴

1. **iCloud → Voice Memos 폴더 동기화 확인**
   ```bash
   ls -lt "/Users/swlee/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/"*.m4a | head -3
   ```
   가장 최근 파일이 실제 녹음 시각과 비슷한지. 안 보이면 워치/iPhone에서 Voice Memos 앱을 한 번 열어 동기화 트리거.

2. **mirror 동기화 확인**
   ```bash
   ls -lt /Users/swlee/Documents/Coding/002_voice_flow/recordings_mirror/*.m4a | head -3
   tail -10 /Users/swlee/Documents/Coding/002_voice_flow/sync.log
   ```
   원본에는 있는데 mirror에 없으면 sync 데몬 점검 (아래 #3).

3. **sync 데몬 상태**
   ```bash
   launchctl list | grep voiceflow-sync
   ```
   상태 0 또는 PID 없으면 reload:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.swlee.voiceflow-sync.plist
   launchctl load ~/Library/LaunchAgents/com.swlee.voiceflow-sync.plist
   ```

4. **orchestrator 데몬 상태**
   ```bash
   launchctl list | grep voiceflow-orchestrator
   tail -30 /tmp/voiceflow-orchestrator.log
   tail -30 /tmp/voiceflow-orchestrator.err.log
   ```

### `bash sync FAILED` 가 sync.log에 보임 (FDA 깨짐)

bash에 FDA를 다시 부여:

1. 시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근 권한
2. `/bin/bash` 가 목록에 있고 켜져 있는지 확인 (없으면 `+` 로 추가)
3. `launchctl unload`/`load` 로 sync 데몬 재시작

### Anthropic / Notion API 실패

orchestrator는 retry 후에도 실패하면 stage를 `failed`로 기록하고 다음 파일로 넘어감 (전체 배치를 막지 않음). 실패 알림은 macOS notification으로 한 번만.

특정 파일을 강제 재처리하려면:
```bash
# processing_state.jsonl 에서 해당 파일 라인을 제거하거나, .stt_cache/<sha>.txt 를 지우면 처음부터 재실행됨
```

## 절대 하지 말 것

- Python 코드에서 `Group Containers/group.com.apple.VoiceMemos.shared` 직접 접근 — bash sync를 거칠 것
- `voiceflow-sync` 데몬을 unload — 살아있어야 mirror가 갱신됨
- `recordings_mirror/`의 파일을 수동으로 삭제 — sync는 신규 파일만 복사하므로, 삭제하면 영구 손실
