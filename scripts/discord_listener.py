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
    has_image = any(
        (a.get("content_type") or "").startswith("image/")
        for a in (attachments or [])
    )
    if not lower and not has_image:
        return None  # 빈 메시지 + 이미지 첨부 없음 → 스킵 (이모지/리액션 등)
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
    if any(k in content for k in ("인텔", "외부인텔")) or "intel" in lower:
        return "daily_intel"
    if any(k in content for k in ("상태", "status")):
        return "status"
    # calendar — 이미지 첨부 OR "일정"/"캘린더" 시작 OR (캘린더 명사 + 등록/수정 동사)
    starts_with_keyword = (
        lower.startswith("일정")
        or lower.startswith("캘린더")
        or lower.startswith("calendar")
        or lower.startswith("/calendar")
    )
    # 문장 중간/끝에 "캘린더 등록해줘"처럼 와도 잡도록 명사+동사 조합 매칭
    has_cal_noun = ("캘린더" in content) or ("일정" in content)
    has_cal_verb = any(
        v in content
        for v in ("등록", "추가", "잡아", "잡어", "넣어", "수정", "변경",
                  "삭제", "지워", "지우", "옮겨", "옮기", "바꿔", "예약")
    )
    if has_image or starts_with_keyword or (has_cal_noun and has_cal_verb):
        return "calendar"
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


def _handle_daily_intel(token: str, msg: dict, log: logging.Logger) -> None:
    """인텔/intel/외부인텔 → daily_intel manual_run.sh 비동기 실행."""
    _react(token, msg["id"], "📡")
    _send(
        token,
        "📡 daily-intel 실행 시작 (5~10분 소요, 완료 시 알림)",
        reply_to=msg["id"],
    )
    try:
        subprocess.Popen(
            ["/Users/swlee/Documents/Coding/003_ai_sales_agent/daily_intel/scripts/manual_run.sh"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("daily_intel launched (non-blocking)")
    except Exception as e:
        log.exception("daily_intel launch failed")
        _send(token, f"❌ daily-intel 실행 실패: {type(e).__name__}: {e}")


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


def _strip_json_fence(text: str) -> str:
    """Strip surrounding ```json ... ``` or ``` ... ``` fences (if any)."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    # ```json\n...\n```  또는  ```\n...\n```
    inner = s[3:]  # 첫 ``` 제거
    if inner.startswith("json"):
        inner = inner[4:]
    inner = inner.lstrip("\n").rstrip()
    if inner.endswith("```"):
        inner = inner[:-3].rstrip()
    return inner


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


def _download_attachments(
    attachments: list[dict], msg_id: str, log: logging.Logger,
) -> tuple[list[Path], int]:
    """Discord CDN URL은 인증 불필요. 이미지만 /tmp/cal_<msg_id>_<i>.<ext>로 저장.

    Returns (saved_paths, failed_image_count). Non-image attachments are not counted.
    """
    paths: list[Path] = []
    failed = 0
    for i, a in enumerate(attachments or []):
        ctype = (a.get("content_type") or "")
        if not ctype.startswith("image/"):
            continue
        raw_ext = ctype.split("/", 1)[1].split(";")[0].strip()
        ext = re.sub(r"[^a-zA-Z0-9]", "", raw_ext)[:16] or "png"
        path = Path(f"/tmp/cal_{msg_id}_{i}.{ext}")
        try:
            r = requests.get(a["url"], timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            path.write_bytes(r.content)
            paths.append(path)
        except Exception:
            log.exception("attachment download failed: %s", a.get("url"))
            failed += 1
    return paths, failed


def _build_calendar_prompt(content: str, image_paths: list[Path]) -> str:
    """Calendar 핸들러 전용 prompt. JSON 한 덩어리 응답을 요구."""
    today = date.today()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][today.weekday()]
    image_hint = ""
    if image_paths:
        image_hint = (
            "\n첨부 이미지:\n"
            + "\n".join(f"- {p}" for p in image_paths)
            + "\nRead 도구로 이미지를 열어 텍스트(카카오톡 메시지 등)를 추출하세요."
        )
    body = content or "(텍스트 없음)"
    return (
        f"오늘은 {today.isoformat()} ({weekday_kr}). 사용자는 이성우 담당(LG U+ 기업AI고객담당).\n"
        f"시간대는 Asia/Seoul.\n\n"
        f"입력 메시지:\n{body}\n{image_hint}\n\n"
        "작업:\n"
        "0. 메시지가 기존 일정의 수정/삭제 요청이면 (예: '방금 거 30분 늦춰줘',\n"
        "   '6/22 김태원 저녁 삭제해줘'):\n"
        "   - mcp__claude_ai_Google_Calendar__list_events 로 대상 이벤트를 검색\n"
        "     (fullText 키워드 또는 날짜 범위 활용)\n"
        "   - 수정: mcp__claude_ai_Google_Calendar__update_event 호출 →\n"
        "     {\"status\":\"updated\",\"event_link\":\"<htmlLink>\",\"summary\":\"<KR 한줄>\"}\n"
        "   - 삭제: mcp__claude_ai_Google_Calendar__delete_event 호출 →\n"
        "     {\"status\":\"deleted\",\"summary\":\"<KR 한줄>\"}\n"
        "   - 대상 이벤트가 둘 이상이거나 특정 불가하면 need_confirmation\n"
        "1. 신규 등록이면, 입력에서 일정 정보(제목, 일시, 장소, 참석자)를 추출\n"
        "2. 일정과 무관한 잡담/광고/링크/사진이면 {\"status\":\"not_event\"} 반환\n"
        "3. 날짜 자체가 불명확하면(날짜 없음, '조만간', '다음에' 등)\n"
        "   → {\"status\":\"need_confirmation\",\"question\":\"...\"}\n"
        "   ('내일'/'다음주 화' 등은 오늘 날짜 기준으로 계산하여 확정, 되묻지 않음)\n"
        "4. 날짜가 확정되면 mcp__claude_ai_Google_Calendar__create_event 호출:\n"
        "   - calendar_id: \"primary\"\n"
        "   - time_zone: \"Asia/Seoul\"\n"
        "   - 시각 처리 규칙 (시각이 모호해도 되묻지 말 것):\n"
        "     * 정확한 시각 명시 + 종료 시각 미지정 → 1시간 이벤트\n"
        "     * 시작·종료 시각이 모두 명시되면 그대로 사용. 종료가 시작보다 이르면\n"
        "       다음날로 넘어가는 것으로 처리 (예: '23일 18시~24일 6시' → 12시간 이벤트)\n"
        "     * 시각 정보 없이 날짜만, 또는 '점심'/'저녁'/'오전'/'오후'처럼 시간대만 표기 →\n"
        "       종일(all-day) 이벤트로 등록. summary 끝에 '(시간 미정)' 표기하고\n"
        "       시간대 단어는 제목에 유지 (예: '김태원 대표 저녁 식사 (시간 미정)')\n"
        "   - summary는 핵심만 (예: '코람코 김태원 대표 미팅')\n"
        "   - description에 원문 텍스트와 참석자 정보 기록\n"
        "   - attendees는 이메일 모르면 비움\n"
        "5. 등록 성공 시 {\"status\":\"created\",\"event_link\":\"<htmlLink>\","
        "\"summary\":\"<KR 한줄>\",\"event_id\":\"<id>\"}\n\n"
        "★ MCP 도구를 실제로 호출한 뒤 그 결과로만 JSON을 작성할 것. event_id·링크를 지어내지 말 것.\n"
        "★ 응답은 JSON 한 덩어리만. 다른 텍스트 금지.\n"
    )


def _handle_calendar(token: str, msg: dict, log: logging.Logger, state: dict) -> None:
    """이미지/키워드 트리거 → Claude CLI로 일정 추출+Google Calendar 등록."""
    content = (msg.get("content") or "").strip()
    image_paths, download_failures = _download_attachments(
        msg.get("attachments") or [], msg["id"], log
    )
    _react(token, msg["id"], "📅")
    if download_failures:
        _send(
            token,
            f"⚠️ 이미지 {download_failures}건 다운로드 실패 — 텍스트만으로 처리 시도합니다.",
            reply_to=msg["id"],
        )

    prompt = _build_calendar_prompt(content, image_paths)

    # calendar 전용 세션 — 일반 대화(_handle_claude) 세션과 분리해
    # "JSON만 응답" 지시가 일반 대화로 새지 않도록 격리한다.
    sid = state.get("calendar_session_id")
    cmd = [
        CLAUDE_CLI, "-p",
        "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--permission-mode", "bypassPermissions",
    ]
    if sid:
        cmd += ["--resume", sid]
    cmd.append(prompt)

    log.info("calendar call: resume=%s images=%d content=%r",
             sid, len(image_paths), content[:120])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT, cwd=CLAUDE_CWD,
        )
    except subprocess.TimeoutExpired:
        _send(token, f"⏱ Claude timeout ({CLAUDE_TIMEOUT}s)", reply_to=msg["id"])
        return
    except Exception as e:
        log.exception("calendar subprocess failed")
        _send(token, f"❌ Claude 호출 실패: {type(e).__name__}: {e}", reply_to=msg["id"])
        return
    finally:
        for p in image_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                log.exception("tempfile cleanup failed: %s", p)

    if result.returncode != 0:
        tail = (result.stderr or result.stdout)[-1500:]
        _send(token, f"❌ Claude exit={result.returncode}\n```\n{tail}\n```",
              reply_to=msg["id"])
        return

    # Claude CLI envelope: {"result": "...", "session_id": "...", "total_cost_usd": ...}
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        _send(token, f"❌ envelope JSON parse 실패\n```\n{result.stdout[-1500:]}\n```",
              reply_to=msg["id"])
        return

    new_sid = envelope.get("session_id")
    if new_sid:
        state["calendar_session_id"] = new_sid
        _save_state(state)

    payload_text = _strip_json_fence((envelope.get("result") or "").strip())
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        _send(token, f"❌ payload JSON parse 실패\n```\n{payload_text[:1500]}\n```",
              reply_to=msg["id"])
        return

    status = payload.get("status")
    log.info("calendar reply: status=%s", status)

    if status == "created":
        link = payload.get("event_link", "")
        summary = payload.get("summary", "(요약 없음)")
        _send(token, f"✅ 등록 완료 — {summary}\n🔗 {link}", reply_to=msg["id"])
    elif status == "updated":
        link = payload.get("event_link", "")
        summary = payload.get("summary", "(요약 없음)")
        _send(token, f"✅ 수정 완료 — {summary}\n🔗 {link}", reply_to=msg["id"])
    elif status == "deleted":
        summary = payload.get("summary", "(요약 없음)")
        _send(token, f"🗑️ 삭제 완료 — {summary}", reply_to=msg["id"])
    elif status == "need_confirmation":
        q = payload.get("question", "확인이 필요합니다.")
        _send(token, f"❓ {q}", reply_to=msg["id"])
    elif status == "not_event":
        _react(token, msg["id"], "🤷")
    else:
        _send(token, f"⚠️ 알 수 없는 status: {status!r}\n```\n{payload_text[:1500]}\n```",
              reply_to=msg["id"])


def _handle_reset(token: str, msg: dict, log: logging.Logger, state: dict) -> None:
    """채널 세션 초기화 — 일반 대화·calendar 세션 모두 다음 메시지부터 새로."""
    old = state.pop("claude_session_id", None)
    old_cal = state.pop("calendar_session_id", None)
    _save_state(state)
    log.info("session reset (claude=%s calendar=%s)", old, old_cal)
    _send(
        token,
        f"🧹 세션 리셋 완료 (대화: {old or '없음'} / 캘린더: {old_cal or '없음'})",
        reply_to=msg["id"],
    )


HANDLERS: dict[str, Callable] = {
    "ping": _handle_ping,
    "process_voice": _handle_process_voice,
    "morning_briefing": _handle_morning_briefing,
    "weekly_briefing": _handle_weekly_briefing,
    "process_all": _handle_process_all,
    "daily_intel": _handle_daily_intel,
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
                    # claude/reset/calendar은 state(session_id)가 필요해 별도 디스패치
                    if cmd == "claude":
                        _handle_claude(token, m, log, state)
                    elif cmd == "reset":
                        _handle_reset(token, m, log, state)
                    elif cmd == "calendar":
                        _handle_calendar(token, m, log, state)
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
