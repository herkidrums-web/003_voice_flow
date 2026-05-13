#!/bin/bash
# 수동 트리거 — sync + orchestrator 한 번에 실행.
# 매일 저녁/아침 1회 실행 권장. launchd 자동 sync는 폐기 (반수동 워크플로우).
set -e

PROJECT_ROOT="/Users/swlee/Documents/Coding/002_voice_flow_v3"
LOG="/tmp/voiceflow-manual-$(date +%Y%m%d-%H%M%S).log"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$( ts )] sync 시작 (shortcuts run \"VoiceFlow Sync\")" | tee -a "$LOG"
shortcuts run "VoiceFlow Sync" 2>&1 | tee -a "$LOG" || {
    echo "[$( ts )] sync 실패 — Shortcuts 권한 확인 필요" | tee -a "$LOG"
    exit 1
}

echo "[$( ts )] sync.log 마지막 줄:" | tee -a "$LOG"
tail -1 /tmp/voiceflow-sync.log | tee -a "$LOG"

echo "[$( ts )] orchestrator 시작" | tee -a "$LOG"
cd "$PROJECT_ROOT"
poc/.venv/bin/python scripts/run_orchestrator.py 2>&1 | tee -a "$LOG"

echo "[$( ts )] 완료. 로그: $LOG"
