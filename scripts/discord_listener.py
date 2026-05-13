#!/usr/bin/env python3
"""Discord listener daemon — polls a Discord text channel for whitelisted
commands from the user, runs the matched script, and replies with the result.

Polling (30s) over Discord REST API. Simple and reliable; switch to Gateway
WebSocket later if real-time push is needed.

Allowed senders are read from ~/.claude/channels/discord/access.json
(groups[channel_id].allowFrom). Bot token from ~/.claude/channels/discord/.env.

Commands (substring match, case-insensitive):
  "ping"                              → pong (liveness check)
  "음성메모", "voiceflow", "처리해줘"  → manual_run.sh (sync + orchestrator)
  "브리핑", "일일", "오늘"             → daily_briefing.py --date today
  "상태", "status"                    → processing_state.jsonl 요약
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
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


def _match_command(content: str) -> str | None:
    lower = content.lower()
    if "ping" in lower:
        return "ping"
    if any(k in content for k in ("음성메모", "처리해줘")) or "voiceflow" in lower:
        return "process_voice"
    if any(k in content for k in ("브리핑", "일일", "오늘 브")):
        return "daily_briefing"
    if any(k in content for k in ("상태", "status")):
        return "status"
    return None


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


def _handle_daily_briefing(token: str, msg: dict, log: logging.Logger) -> None:
    _react(token, msg["id"], "📋")
    _send(token, "📋 일일브리핑 생성 중...", reply_to=msg["id"])
    try:
        today_str = date.today().isoformat()
        result = subprocess.run(
            [
                str(PROJECT_ROOT / "poc/.venv/bin/python"),
                str(PROJECT_ROOT / "scripts/daily_briefing.py"),
                "--date", today_str,
            ],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(PROJECT_ROOT),
        )
        url = ""
        for line in (result.stdout + result.stderr).splitlines():
            if "briefing created:" in line:
                url = line.split("briefing created:")[1].strip()
                break
        if url:
            _send(token, f"✅ 브리핑 완료\n🔗 {url}")
        else:
            tail = (result.stdout + result.stderr)[-1000:]
            _send(token, f"⚠️ URL 미발견. exit={result.returncode}\n```\n{tail}\n```")
    except Exception as e:
        log.exception("daily_briefing failed")
        _send(token, f"❌ 실패: {type(e).__name__}: {e}")


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


HANDLERS: dict[str, Callable] = {
    "ping": _handle_ping,
    "process_voice": _handle_process_voice,
    "daily_briefing": _handle_daily_briefing,
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
                cmd = _match_command(m.get("content", ""))
                if cmd is None:
                    continue
                handler = HANDLERS[cmd]
                log.info("command=%s from=%s content=%r",
                         cmd, m["author"].get("username"), m.get("content")[:200])
                try:
                    handler(token, m, log)
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
