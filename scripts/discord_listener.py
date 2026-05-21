#!/usr/bin/env python3
"""Discord listener daemon — polls a Discord text channel for whitelisted
commands from the user, runs the matched script, and replies with the result.

Polling (30s) over Discord REST API. Simple and reliable; switch to Gateway
WebSocket later if real-time push is needed.

Allowed senders are read from ~/.claude/channels/discord/access.json
(groups[channel_id].allowFrom). Bot token from ~/.claude/channels/discord/.env.

Commands (substring match, case-insensitive):
  "ping"                              → pong (liveness check)
  "음성메모", "voiceflow"             → manual_run.sh (sync + orchestrator)
  "브리핑", "일일", "morning"          → morning_briefing.py (v2, wiki anchor)
  "주간", "weekly", "지난주"           → weekly_briefing.py (지난 주 합성)
  "오늘 처리", "전체", "all"           → manual_run.sh + morning_briefing.py (combo)
  "상태", "status"                    → processing_state.jsonl 요약
  "/reset", "리셋"                    → Claude 채널 세션 초기화 (다음 메시지부터 새 대화)
  (그 외 모든 메시지)                  → Claude CLI 호출(`claude -p --resume <sid>`),
                                       채널당 연속 세션 유지, 답변 Discord 회신
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
import re
from pathlib import Path
from typing import Callable

import requests

PROJECT_ROOT = Path("/Users/swlee/Documents/Coding/002_voice_flow_v3")
ACCESS_FILE = Path.home() / ".claude/channels/discord/access.json"
ENV_FILE = Path.home() / ".claude/channels/discord/.env"
STATE_FILE = PROJECT_ROOT / "discord_listener_state.json"
LOG_FILE = Path("/tmp/voiceflow-discord-listener.log")
CHANNEL_ID = "1490245243285667981"  # text-channel
POLL_INTERVAL = 30.0
DISCORD_API = "https://discord.com/api/v10"
HTTP_TIMEOUT = 15.0

# Claude CLI 호출 설정 — 채널당 연속 세션 + bypassPermissions(모바일 작업용)
CLAUDE_CLI = "/Users/swlee/.local/bin/claude"
CLAUDE_CWD = "/Users/swlee/Documents/Coding"
CLAUDE_TIMEOUT = 900  # 15분 (긴 작업 대응)
CLAUDE_MODEL = "sonnet"  # 모바일 일상 대화용 (Opus는 결정적 순간만)
DISCORD_MSG_LIMIT = 1900  # 2000 - 안전 여유


def _load_token() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DISCORD_BOT_TOKEN not found in .env")


def _load_allowed_users() -> set[str]:
    access = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    group = access.get("groups", {}).get(CHANNEL_ID, {})
    return set(group.get("allowFrom", []))


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_message_id": None, "_first_run": True}


def _init_state_to_latest(token: str, state: dict) -> dict:
    """First run: skip channel history, start listening from latest message.
    Otherwise daemon replays every old user command on first deploy."""
    if not state.pop("_first_run", False):
        return state
    try:
        url = f"{DISCORD_API}/channels/{CHANNEL_ID}/messages"
        r = requests.get(
            url,
            headers={"Authorization": f"Bot {token}"},
            params={"limit": 1},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        msgs = r.json()
        if msgs:
            state["last_message_id"] = msgs[0]["id"]
    except Exception:
        pass
    return state


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _fetch_messages(token: str, after: str | None) -> list[dict]:
    url = f"{DISCORD_API}/channels/{CHANNEL_ID}/messages"
    params = {"limit": 20}
    if after:
        params["after"] = after
    r = requests.get(
        url,
        headers={"Authorization": f"Bot {token}"},
        params=params,
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return list(reversed(r.json()))


def _send(token: str, text: str, reply_to: str | None = None) -> dict:
    url = f"{DISCORD_API}/channels/{CHANNEL_ID}/messages"
    payload: dict = {"content": text[:2000]}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}
    r = requests.post(
        url,
        headers={"Authorization": f"Bot {token}"},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _react(token: str, message_id: str, emoji: str) -> None:
    from urllib.parse import quote
    url = f"{DISCORD_API}/channels/{CHANNEL_ID}/messages/{message_id}/reactions/{quote(emoji)}/@me"
    requests.put(
        url,
        headers={"Authorization": f"Bot {token}"},
        timeout=HTTP_TIMEOUT,
    )


def _match_command(content: str, attachments: list[dict] | None = None) -> str | None:
    lower = content.lower().strip()
    if not lower:
        return None  # 빈 메시지는 스킵 (이모지/첨부 only 등)
    # 세션 리셋 — 우선 매칭
    if lower in ("/reset", "리셋") or lower.startswith("/reset ") or lower.startswith("리셋 "):
        return "reset"
    if "ping" in lower:
        return "ping"
    # combo: process_voice + morning_briefing — 가장 우선 매칭
    if any(k in content for k in ("오늘 처리", "전체")) or "all" in lower.split():
        return "process_all"
    if any(k in content for k in ("음성메모",)) or "voiceflow" in lower:
        return "process_voice"
    # weekly가 daily보다 우선 (둘 다 "briefing" 류라 매칭 충돌)
    if any(k in content for k in ("주간", "지난주", "지난 주")) or "weekly" in lower:
        return "weekly_briefing"
    if any(k in content for k in ("브리핑", "일일")) or "morning" in lower:
        return "morning_briefing"
    if any(k in content for k in ("상태", "status")):
        return "status"
    # 그 외 모든 메시지 → Claude (채널당 연속 세션)
    return "claude"


def _handle_ping(token: str, msg: dict, log: logging.Logger) -> None:
    _send(token, "🏓 pong — daemon alive", reply_to=msg["id"])


def _handle_process_voice(token: str, msg: dict, log: logging.Logger) -> None:
    _react(token, msg["id"], "🎙️")
    _send(token, "🎙 음성메모 처리 시작 (sync + orchestrator)...", reply_to=msg["id"])
    try:
        result = subprocess.run(
            [str(PROJECT_ROOT / "scripts/manual_run.sh")],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        tail = (result.stdout + result.stderr)[-1500:]
        _send(token, f"✅ 처리 완료 (exit {result.returncode})\n```\n{tail}\n```")
    except subprocess.TimeoutExpired:
        _send(token, "⏱ timeout (1800s) — orchestrator 직접 확인 필요")
    except Exception as e:
        log.exception("process_voice failed")
        _send(token, f"❌ 실패: {type(e).__name__}: {e}")


def _run_morning_briefing(target_date: str, timeout_s: int = 600) -> tuple[int, str, str]:
    """Returns (exit_code, notion_url, tail_output)."""
    result = subprocess.run(
        [
            str(PROJECT_ROOT / "poc/.venv/bin/python"),
            str(PROJECT_ROOT / "scripts/morning_briefing.py"),
            "--date", target_date,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(PROJECT_ROOT),
    )
    url = ""
    for line in (result.stdout + result.stderr).splitlines():
        if "briefing created:" in line or "morning briefing created:" in line:
            url = line.split("created:")[-1].strip()
            break
    return result.returncode, url, (result.stdout + result.stderr)[-1200:]


def _parse_briefing_dates(content: str) -> list[str]:
    """메시지에서 날짜 의도 추출. 못 찾으면 [어제] 반환.

    지원 패턴 (현재 연도 가정):
    - "5/18", "5/18·19", "5/18,5/19"
    - "5월 18일", "5월18일"
    - "어제", "그저께/그제", "오늘"
    """
    today = date.today()
    found: list[str] = []

    def _add(m: int, d: int) -> None:
        try:
            iso = date(today.year, m, d).isoformat()
            if iso not in found:
                found.append(iso)
        except ValueError:
            pass

    # 슬래시 표기 — 5/DD + 같은 월 추가 일자 (·DD, ,DD)
    slash_iter = list(re.finditer(r"(\d{1,2})/(\d{1,2})", content))
    for m in slash_iter:
        _add(int(m.group(1)), int(m.group(2)))
    if slash_iter:
        last_month = int(slash_iter[-1].group(1))
        tail = content[slash_iter[-1].end():]
        for m in re.finditer(r"[·,]\s*(\d{1,2})", tail):
            _add(last_month, int(m.group(1)))

    # 한국어 표기 — "5월 18일" + 같은 월 추가 "19일", "20일"
    md_iter = list(re.finditer(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", content))
    for m in md_iter:
        _add(int(m.group(1)), int(m.group(2)))
    if md_iter:
        last_month = int(md_iter[-1].group(1))
        tail = content[md_iter[-1].end():]
        for m in re.finditer(r"(\d{1,2})\s*일", tail):
            _add(last_month, int(m.group(1)))
    # 자연어 키워드 (날짜 토큰이 없을 때만 보조 적용)
    if not found:
        if "그저께" in content or "그제" in content:
            found.append((today - timedelta(days=2)).isoformat())
        if "오늘" in content:
            found.append(today.isoformat())
        if "어제" in content or "어제자" in content:
            found.append((today - timedelta(days=1)).isoformat())
    # 기본값: 어제
    if not found:
        found.append((today - timedelta(days=1)).isoformat())
    # 중복 제거 + 정렬
    return sorted(set(found))


def _handle_morning_briefing(token: str, msg: dict, log: logging.Logger) -> None:
    content = msg.get("content") or ""
    targets = _parse_briefing_dates(content)
    _react(token, msg["id"], "📋")
    _send(
        token,
        f"📋 Morning briefing v2 생성 중 — 대상: {', '.join(targets)} (각 60-90초)",
        reply_to=msg["id"],
    )
    results: list[str] = []
    for target_date in targets:
        try:
            code, url, tail = _run_morning_briefing(target_date)
            if url:
                log.info("briefing OK %s → %s", target_date, url)
                results.append(f"✅ {target_date}\n🔗 {url}")
            else:
                log.warning("briefing %s exit=%s no URL. tail=%s",
                            target_date, code, tail[-300:])
                results.append(
                    f"⚠️ {target_date} URL 미발견 (exit={code}) — "
                    f"sync 지연 또는 처리된 미팅 0건 가능"
                )
        except subprocess.TimeoutExpired:
            log.warning("briefing %s timeout", target_date)
            results.append(f"⏱ {target_date} timeout")
        except Exception as e:
            log.exception("briefing %s failed", target_date)
            results.append(f"❌ {target_date} 실패: {type(e).__name__}: {e}")
    _send(token, "\n\n".join(results))


def _handle_weekly_briefing(token: str, msg: dict, log: logging.Logger) -> None:
    _react(token, msg["id"], "📅")
    _send(token, "📅 주간 브리핑 생성 중 (지난 주 월~일 합성)... (90-180초)", reply_to=msg["id"])
    try:
        result = subprocess.run(
            [
                str(PROJECT_ROOT / "poc/.venv/bin/python"),
                str(PROJECT_ROOT / "scripts/weekly_briefing.py"),
            ],
            capture_output=True, text=True, timeout=900, cwd=str(PROJECT_ROOT),
        )
        url = ""
        for line in (result.stdout + result.stderr).splitlines():
            if "weekly briefing created:" in line or "briefing created:" in line:
                url = line.split("created:")[-1].strip()
                break
        if url:
            _send(token, f"✅ 주간 브리핑 완료\n🔗 {url}")
        else:
            tail = (result.stdout + result.stderr)[-1000:]
            _send(token, f"⚠️ URL 미발견. exit={result.returncode}\n```\n{tail}\n```")
    except Exception as e:
        log.exception("weekly_briefing failed")
        _send(token, f"❌ 실패: {type(e).__name__}: {e}")


def _handle_process_all(token: str, msg: dict, log: logging.Logger) -> None:
    """Combo: manual_run.sh (sync + orchestrator) + morning_briefing.py."""
    _react(token, msg["id"], "🚀")
    _send(token, "🚀 전체 처리 시작 — 1) 음성메모 sync+처리 2) Morning briefing v2", reply_to=msg["id"])
    # Step 1
    try:
        result = subprocess.run(
            [str(PROJECT_ROOT / "scripts/manual_run.sh")],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        _send(token, f"✅ Step 1 완료 (exit {result.returncode}) — 음성메모 처리")
    except Exception as e:
        log.exception("process_all step1 failed")
        _send(token, f"❌ Step 1 실패: {type(e).__name__}: {e}")
        return
    # Step 2
    try:
        today_str = date.today().isoformat()
        code, url, tail = _run_morning_briefing(today_str)
        if url:
            _send(token, f"✅ Step 2 완료 — Morning briefing\n🔗 {url}")
        else:
            _send(token, f"⚠️ Step 2 URL 미발견. exit={code}\n```\n{tail}\n```")
    except Exception as e:
        log.exception("process_all step2 failed")
        _send(token, f"❌ Step 2 실패: {type(e).__name__}: {e}")


def _handle_status(token: str, msg: dict, log: logging.Logger) -> None:
    state_path = PROJECT_ROOT / "processing_state.jsonl"
    from collections import Counter
    latest: dict[str, dict] = {}
    for line in state_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        prev = latest.get(e["file"])
        if prev is None or e["ts"] >= prev["ts"]:
            latest[e["file"]] = e
    cnt = Counter(f"{e['stage']}:{e['status']}" for e in latest.values())
    today_str = date.today().isoformat()[:10]
    done_today = sum(
        1
        for e in latest.values()
        if e["stage"] in ("wiki", "done")
        and e["status"] == "done"
        and e["ts"].startswith(today_str)
    )
    body = "\n".join(f"  {k}: {n}" for k, n in cnt.most_common(8))
    _send(
        token,
        f"📊 VoiceFlow 상태\n오늘 처리: **{done_today}건**\n```\n{body}\n```",
        reply_to=msg["id"],
    )


def _chunks(text: str, size: int) -> list[str]:
    """Discord 2000자 제한 대응 — 줄 단위 우선, 안 되면 길이로 강제 분할."""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(line) > size:
            # 한 줄이 한계 초과면 강제로 잘게
            if buf:
                out.append(buf); buf = ""
            for i in range(0, len(line), size):
                out.append(line[i:i+size])
            continue
        if len(buf) + len(line) > size:
            out.append(buf); buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return out


def _handle_claude(token: str, msg: dict, log: logging.Logger, state: dict) -> None:
    """비명령 메시지 → Claude CLI 호출. 채널당 연속 세션(--resume) 유지."""
    content = (msg.get("content") or "").strip()
    if not content:
        return
    _react(token, msg["id"], "🤔")

    sid = state.get("claude_session_id")
    cmd = [
        CLAUDE_CLI, "-p",
        "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--permission-mode", "bypassPermissions",
    ]
    if sid:
        cmd += ["--resume", sid]
    cmd.append(content)

    log.info("claude call: resume=%s content=%r", sid, content[:120])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT, cwd=CLAUDE_CWD,
        )
    except subprocess.TimeoutExpired:
        _send(token, f"⏱ Claude timeout ({CLAUDE_TIMEOUT}s)", reply_to=msg["id"])
        return
    except Exception as e:
        log.exception("claude subprocess failed")
        _send(token, f"❌ Claude 호출 실패: {type(e).__name__}: {e}", reply_to=msg["id"])
        return

    if result.returncode != 0:
        tail = (result.stderr or result.stdout)[-1500:]
        _send(token, f"❌ Claude exit={result.returncode}\n```\n{tail}\n```",
              reply_to=msg["id"])
        return

    try:
        j = json.loads(result.stdout)
    except json.JSONDecodeError:
        _send(token, f"❌ JSON parse 실패\n```\n{result.stdout[-1500:]}\n```",
              reply_to=msg["id"])
        return

    text = (j.get("result") or "").strip() or "(빈 응답)"
    new_sid = j.get("session_id")
    if new_sid:
        state["claude_session_id"] = new_sid
        _save_state(state)
    cost = j.get("total_cost_usd")
    log.info("claude reply: session=%s cost=%s len=%d",
             new_sid, cost, len(text))

    parts = _chunks(text, DISCORD_MSG_LIMIT)
    for i, part in enumerate(parts):
        _send(token, part, reply_to=msg["id"] if i == 0 else None)


def _handle_reset(token: str, msg: dict, log: logging.Logger, state: dict) -> None:
    """Claude 채널 세션 초기화 — 다음 메시지부터 새 대화."""
    old = state.pop("claude_session_id", None)
    _save_state(state)
    log.info("claude session reset (was %s)", old)
    _send(token, f"🧹 Claude 세션 리셋 완료 (이전 세션: {old or '없음'})", reply_to=msg["id"])


HANDLERS: dict[str, Callable] = {
    "ping": _handle_ping,
    "process_voice": _handle_process_voice,
    "morning_briefing": _handle_morning_briefing,
    "weekly_briefing": _handle_weekly_briefing,
    "process_all": _handle_process_all,
    "status": _handle_status,
}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)],
    )
    log = logging.getLogger("discord_listener")

    token = _load_token()
    allowed = _load_allowed_users()
    state = _load_state()
    state = _init_state_to_latest(token, state)
    log.info("started — channel=%s allowed=%s last_id=%s",
             CHANNEL_ID, sorted(allowed), state.get("last_message_id"))

    while True:
        try:
            messages = _fetch_messages(token, state.get("last_message_id"))
            for m in messages:
                state["last_message_id"] = m["id"]
                if m["author"].get("bot"):
                    continue
                if m["author"]["id"] not in allowed:
                    log.warning("non-allowed sender %s (%s)",
                                m["author"]["id"], m["author"].get("username"))
                    continue
                cmd = _match_command(m.get("content", ""), m.get("attachments"))
                if cmd is None:
                    continue
                log.info("command=%s from=%s content=%r",
                         cmd, m["author"].get("username"),
                         (m.get("content") or "")[:200])
                try:
                    # claude/reset은 state(session_id)가 필요해 별도 디스패치
                    if cmd == "claude":
                        _handle_claude(token, m, log, state)
                    elif cmd == "reset":
                        _handle_reset(token, m, log, state)
                    else:
                        HANDLERS[cmd](token, m, log)
                except Exception:
                    log.exception("handler %s raised", cmd)
            _save_state(state)
        except requests.HTTPError as e:
            log.error("HTTP %s: %s", e.response.status_code if e.response else "?",
                      e.response.text[:200] if e.response else str(e))
        except Exception:
            log.exception("poll loop error")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main() or 0)
