#!/bin/bash
# Shortcut "VoiceFlow Sync" 내부에 붙여넣을 Shell Script.
# Shortcuts.app 컨텍스트에서 실행되므로 Voice Memos 폴더 접근 가능 (FDA 불필요).
LOG="/tmp/voiceflow-sync.log"
VOICE_MEMOS="/Users/swlee/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
MIRROR="/Users/swlee/Documents/Coding/002_voice_flow_v3/recordings_mirror"

ts=$(date '+%Y-%m-%d %H:%M:%S')

mkdir -p "$MIRROR"

if [ ! -d "$VOICE_MEMOS" ]; then
    echo "$ts [SYNC] ERROR: Voice Memos dir not found: $VOICE_MEMOS" >> "$LOG"
    exit 1
fi

shopt -s nullglob
files=("$VOICE_MEMOS"/*.m4a)
total=${#files[@]}

if [ "$total" -eq 0 ]; then
    echo "$ts [SYNC] heartbeat — no m4a files in Voice Memos" >> "$LOG"
    exit 0
fi

copied=0
failed=0
for f in "${files[@]}"; do
    name=$(basename "$f")
    dst="$MIRROR/$name"
    if [ -f "$dst" ]; then
        continue
    fi
    if cp "$f" "$dst" 2>>/tmp/voiceflow-sync-stderr.log; then
        size_mb=$(du -m "$dst" | cut -f1)
        echo "$ts [SYNC] copied: $name (${size_mb} MB)" >> "$LOG"
        copied=$((copied + 1))
    else
        echo "$ts [SYNC] FAILED: $name" >> "$LOG"
        failed=$((failed + 1))
    fi
done

skipped=$((total - copied - failed))
echo "$ts [SYNC] heartbeat — $total src | copied=$copied skipped=$skipped failed=$failed" >> "$LOG"

if [ "$failed" -gt 0 ]; then
    osascript -e "display notification \"파일 복사 실패 ${failed}건 — /tmp/voiceflow-sync.log 확인\" with title \"VoiceFlow Sync\""
    exit 1
fi
exit 0
