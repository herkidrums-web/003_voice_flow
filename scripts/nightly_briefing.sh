#!/bin/bash
# Nightly briefing — 매일 00:01 실행.
# D일 00:01 = 전날(D-1) 23:59까지의 녹음을 sync → 처리(드레인) → 일일 브리핑 생성.
#   morning_briefing.py 기본 target = today-1 이므로 00:01(today=D) → D-1 정확히 묶임.
#
# sync 실패해도 브리핑은 진행한다(본문 상단에 stale 경보 배너 + 멱등 upsert).
# 07:00 morning-briefing 잡이 같은 페이지를 재실행으로 자가복구한다.
# launchd FDA 불안정성(이력)을 self-healing 3중망으로 보완하는 설계.

PROJECT_ROOT="/Users/swlee/Documents/Coding/002_voice_flow_v3"
LOG="/tmp/voiceflow-nightly-$(date +%Y%m%d-%H%M%S).log"

# 메모리 가드 (2026-05-15 jetsam: 11파일 일괄 → free 161MB). 작게 잡고 루프로 드레인.
export VOICEFLOW_MAX_PER_RUN="${VOICEFLOW_MAX_PER_RUN:-3}"
export VOICEFLOW_MIN_FREE_GB="${VOICEFLOW_MIN_FREE_GB:-6}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
cd "$PROJECT_ROOT" || exit 1

echo "[$( ts )] nightly 시작" | tee -a "$LOG"

# 1) sync (best-effort — 실패해도 계속)
echo "[$( ts )] sync (shortcuts run \"VoiceFlow Sync\")" | tee -a "$LOG"
shortcuts run "VoiceFlow Sync" 2>&1 | tee -a "$LOG" \
    || echo "[$( ts )] sync 실패 (계속 — 브리핑에 stale 배너)" | tee -a "$LOG"
tail -1 /tmp/voiceflow-sync.log 2>/dev/null | tee -a "$LOG"

# 2) orchestrator 드레인 — pending 없을 때까지 (최대 12회, 메모리 가드는 내부 처리)
for i in $(seq 1 12); do
    echo "[$( ts )] orchestrator pass $i" | tee -a "$LOG"
    OUT=$(poc/.venv/bin/python scripts/run_orchestrator.py 2>&1)
    echo "$OUT" | tee -a "$LOG"
    echo "$OUT" | grep -q "no pending files" && { echo "[$( ts )] 드레인 완료" | tee -a "$LOG"; break; }
    sleep 2
done

# 3) 일일 브리핑 (target = 전날, 미팅날짜 기준 선별 + 멱등 upsert)
echo "[$( ts )] morning_briefing 실행" | tee -a "$LOG"
poc/.venv/bin/python scripts/morning_briefing.py --notify-discord 2>&1 | tee -a "$LOG"

echo "[$( ts )] nightly 완료. 로그: $LOG"
