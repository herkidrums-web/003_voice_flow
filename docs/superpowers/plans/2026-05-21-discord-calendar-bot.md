# Discord Calendar Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discord 채널에 이미지/텍스트로 일정을 던지면 Google Calendar에 자동 등록되도록 기존 `discord_listener.py`에 핸들러 1개를 추가한다.

**Architecture:** 신규 데몬·신규 모듈 없음. listener의 `_match_command`에 분기 1개 추가 + `_handle_calendar` 신규 핸들러 + 디스패치 1줄 추가. Claude CLI(`--permission-mode bypassPermissions`)가 이미지 Vision Read + 일정 추출 + `mcp__claude_ai_Google_Calendar__create_event` 호출까지 한 턴에 수행. 채널당 `--resume <session_id>`로 세션 유지하여 수정/삭제는 자연어 답장으로 기존 `_handle_claude`가 처리.

**Tech Stack:** Python 3.12, `requests`, Discord REST API v10, Claude CLI (`/Users/swlee/.local/bin/claude`), Google Calendar MCP, pytest.

**Spec:** `docs/superpowers/specs/2026-05-21-discord-calendar-bot-design.md`

**Status (2026-05-21): 구현 완료 + 운영 중.** Task 1-5·7 커밋 `82e545a`~`cdab621`, 단위 테스트 30/30 통과. Task 6 smoke test에서 카톡 캡처 2건 실등록 확인. 이후 2건 보완: (a) 모호한 시각은 되묻지 말고 종일+"(시간 미정)" 등록(`07dccf2`), (b) calendar 세션을 일반 대화와 분리해 raw JSON 누출 버그 수정 + 수정/삭제 지원(`53ab78f`). 미실행 smoke 케이스(T4 일정 외 이미지·T5 기존 명령 회귀·T6 수정)는 단위 테스트로 회귀 방어 — 실사용 중 이슈 발견 시 점검.

---

## File Structure

**Modify:**
- `scripts/discord_listener.py` — `_match_command` 분기 확장, `_strip_json_fence` 헬퍼, `_handle_calendar` 신규 핸들러, 디스패치 분기 1줄

**Create:**
- `tests/test_discord_listener.py` — `_match_command` / `_strip_json_fence` 단위 테스트 (subprocess·HTTP 의존 부분은 수동 smoke test)

**Untouched (회귀 0):**
- `scripts/manual_run.sh`, `src/agents/**`, `tests/test_*` 외 모든 파일
- LaunchAgent plist 어느 것도 수정 없음 (listener 재로드만)

---

## Task 1: `_match_command` 시그니처 확장 + 회귀 테스트

기존 `_match_command(content: str)`에 `attachments` 인자를 추가해 이미지 첨부를 감지할 수 있게 한다. 분기 로직은 Task 2에서 추가하고, 이 Task에서는 인자 추가만 한 채로 **기존 동작 회귀 없음**을 테스트로 확정한다.

**Files:**
- Modify: `scripts/discord_listener.py:140` (`_match_command` 정의), `scripts/discord_listener.py:504` (호출부)
- Create: `tests/test_discord_listener.py`

- [ ] **Step 1: 새 테스트 파일 생성 — 기존 명령 매칭 회귀 테스트**

```python
# tests/test_discord_listener.py
"""Tests for scripts/discord_listener.py command matching & helpers."""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ 디렉터리를 path에 추가 (listener는 패키지가 아님)
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import discord_listener  # noqa: E402


class TestMatchCommandRegression:
    """새 시그니처(attachments 인자)로도 기존 명령 매칭이 동일해야 한다."""

    def test_ping(self):
        assert discord_listener._match_command("ping", None) == "ping"

    def test_voice_korean(self):
        assert discord_listener._match_command("음성메모", None) == "process_voice"

    def test_voiceflow_english(self):
        assert discord_listener._match_command("voiceflow", None) == "process_voice"

    def test_morning_briefing_korean(self):
        assert discord_listener._match_command("브리핑", None) == "morning_briefing"

    def test_weekly_briefing(self):
        assert discord_listener._match_command("주간", None) == "weekly_briefing"

    def test_process_all_combo(self):
        assert discord_listener._match_command("오늘 처리", None) == "process_all"

    def test_status(self):
        assert discord_listener._match_command("상태", None) == "status"

    def test_reset(self):
        assert discord_listener._match_command("/reset", None) == "reset"

    def test_empty_returns_none(self):
        assert discord_listener._match_command("", None) is None

    def test_arbitrary_text_falls_through_to_claude(self):
        assert discord_listener._match_command("안녕 오늘 일정 어때?", None) == "claude"
```

- [ ] **Step 2: 테스트 실행 — 시그니처 불일치로 실패 확인**

Run:
```bash
cd /Users/swlee/Documents/Coding/002_voice_flow_v3
poc/.venv/bin/python -m pytest tests/test_discord_listener.py -v
```

Expected: 전부 FAIL 또는 ERROR — `_match_command()`가 1-arg이므로 `TypeError`.

- [ ] **Step 3: 시그니처 확장 — `attachments` 키워드 인자 추가 (분기는 Task 2)**

[scripts/discord_listener.py:140](scripts/discord_listener.py#L140) 를 다음과 같이 수정:

```python
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
```

호출부 [scripts/discord_listener.py:504](scripts/discord_listener.py#L504) 를 수정:

```python
                cmd = _match_command(m.get("content", ""), m.get("attachments"))
```

- [ ] **Step 4: 테스트 재실행 — 회귀 없음 확인**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py -v
```

Expected: 10개 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/swlee/Documents/Coding/002_voice_flow_v3
git add scripts/discord_listener.py tests/test_discord_listener.py
git commit -m "test(listener): extend _match_command signature with attachments arg

준비 단계 — 분기 로직은 후속 커밋. 기존 명령(ping/음성메모/브리핑/주간/오늘처리/상태/리셋) 회귀 없음을 단위 테스트로 고정."
```

---

## Task 2: 캘린더 트리거 분기 추가 (이미지 첨부 OR 키워드 시작)

`_match_command`에 calendar 분기를 추가한다. 평가 순서는 **기존 모든 명령 매칭 이후, `claude` fallback 이전.** 이 위치 덕분에 회귀 위험 0.

**Files:**
- Modify: `scripts/discord_listener.py:140` (`_match_command` 본문)
- Modify: `tests/test_discord_listener.py` (calendar 분기 테스트 추가)

- [ ] **Step 1: calendar 분기 테스트 추가**

`tests/test_discord_listener.py` 끝에 추가:

```python
class TestMatchCommandCalendar:
    """이미지 첨부 OR '일정'/'캘린더' 시작 → calendar 분기."""

    def test_image_only_message_triggers_calendar(self):
        atts = [{"content_type": "image/png", "url": "https://cdn.discordapp.com/x.png"}]
        assert discord_listener._match_command("", atts) == "calendar"

    def test_image_with_caption_triggers_calendar(self):
        atts = [{"content_type": "image/jpeg", "url": "https://cdn.discordapp.com/y.jpg"}]
        assert discord_listener._match_command("내일 14시 토스 미팅", atts) == "calendar"

    def test_keyword_ilchung_triggers_calendar(self):
        assert discord_listener._match_command("일정 내일 14시 토스 미팅", None) == "calendar"

    def test_keyword_calendar_kr_triggers_calendar(self):
        assert discord_listener._match_command("캘린더 5/30 점심", None) == "calendar"

    def test_keyword_calendar_en_triggers_calendar(self):
        assert discord_listener._match_command("calendar 5/30 lunch", None) == "calendar"

    def test_keyword_slash_calendar_triggers_calendar(self):
        assert discord_listener._match_command("/calendar 5/30", None) == "calendar"

    def test_image_attachment_non_image_does_not_trigger(self):
        atts = [{"content_type": "application/pdf", "url": "https://x/y.pdf"}]
        assert discord_listener._match_command("문서 봐줘", atts) == "claude"

    def test_keyword_in_middle_does_not_trigger(self):
        # "오늘 일정 어때?" 같은 일반 질문은 calendar로 가지 않고 claude로
        assert discord_listener._match_command("오늘 일정 어때?", None) == "claude"

    def test_existing_commands_still_win_over_image(self):
        # 이미지 첨부 + "ping" → ping이 우선 (회귀 방지)
        atts = [{"content_type": "image/png", "url": "https://x/p.png"}]
        assert discord_listener._match_command("ping", atts) == "ping"
```

- [ ] **Step 2: 테스트 실행 — calendar 분기 미구현으로 실패 확인**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py::TestMatchCommandCalendar -v
```

Expected: 9개 중 일부 FAIL (이미지 첨부 케이스, 키워드 시작 케이스가 `claude`로 떨어짐).

- [ ] **Step 3: calendar 분기 구현**

[scripts/discord_listener.py:159](scripts/discord_listener.py#L159) `if any(k in content for k in ("상태", "status")):` 블록 **다음 줄에**, `# 그 외 모든 메시지 → Claude` 주석 **앞에** 다음을 추가:

```python
    # calendar — 이미지 첨부 OR "일정"/"캘린더" startswith
    has_image = any(
        (a.get("content_type") or "").startswith("image/")
        for a in (attachments or [])
    )
    starts_with_keyword = (
        lower.startswith("일정")
        or lower.startswith("캘린더")
        or lower.startswith("calendar")
        or lower.startswith("/calendar")
    )
    if has_image or starts_with_keyword:
        return "calendar"
```

- [ ] **Step 4: 테스트 재실행 — 전체 PASS 확인**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py -v
```

Expected: 19개 PASS (Task 1의 10개 + Task 2의 9개).

- [ ] **Step 5: Commit**

```bash
git add scripts/discord_listener.py tests/test_discord_listener.py
git commit -m "feat(listener): add calendar trigger (image attachment or 일정/캘린더 prefix)

이미지 첨부가 있거나 메시지가 '일정'/'캘린더'/'calendar'로 시작하면
calendar 핸들러로 분기. 평가 순서는 기존 모든 명령 매칭 이후라
회귀 위험 없음. 핸들러 본체는 후속 커밋."
```

---

## Task 3: `_strip_json_fence` 헬퍼 + 단위 테스트

Claude CLI가 `result` 필드 안에 코드를 펜스(` ```json ... ``` `)로 감싸 반환하는 경우가 있다. 이를 안전하게 벗기는 작은 헬퍼를 추출하여 테스트한다. (cli_client.py에 `_strip_fence`가 이미 있지만, listener는 별도 모듈이라 같은 패턴을 inline으로 둔다.)

**Files:**
- Modify: `scripts/discord_listener.py` (헬퍼 함수 추가)
- Modify: `tests/test_discord_listener.py` (헬퍼 테스트 추가)

- [ ] **Step 1: 테스트 추가**

`tests/test_discord_listener.py` 끝에 추가:

```python
class TestStripJsonFence:
    """Claude CLI 응답의 ```json ... ``` 펜스 제거."""

    def test_no_fence_returns_unchanged(self):
        s = '{"status":"created"}'
        assert discord_listener._strip_json_fence(s) == s

    def test_json_fence_stripped(self):
        s = '```json\n{"status":"created"}\n```'
        assert discord_listener._strip_json_fence(s) == '{"status":"created"}'

    def test_plain_fence_stripped(self):
        s = '```\n{"status":"not_event"}\n```'
        assert discord_listener._strip_json_fence(s) == '{"status":"not_event"}'

    def test_fence_with_surrounding_whitespace(self):
        s = '   ```json\n{"a":1}\n```   '
        assert discord_listener._strip_json_fence(s) == '{"a":1}'

    def test_empty_string_safe(self):
        assert discord_listener._strip_json_fence("") == ""
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py::TestStripJsonFence -v
```

Expected: 전부 FAIL — `_strip_json_fence` 미정의.

- [ ] **Step 3: 헬퍼 구현**

[scripts/discord_listener.py](scripts/discord_listener.py) 의 `_chunks` 함수 정의 **바로 위에** 추가 (대략 line 377 부근, `_handle_status` 함수 다음):

```python
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
```

- [ ] **Step 4: 테스트 재실행 — PASS 확인**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py::TestStripJsonFence -v
```

Expected: 5개 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/discord_listener.py tests/test_discord_listener.py
git commit -m "feat(listener): add _strip_json_fence helper for Claude CLI replies

Claude CLI가 result 필드에 코드를 ```json ... ``` 펜스로 감싸는 케이스를 안전하게 제거.
단위 테스트 5개 추가."
```

---

## Task 4: `_handle_calendar` 신규 핸들러 구현

이미지 다운로드 → Claude CLI subprocess(전용 프롬프트, `--resume` 채널 세션) → JSON 분기(`created`/`need_confirmation`/`not_event`) → Discord 답장 + tempfile 정리.

subprocess와 HTTP 모킹 부담이 커서 본 핸들러는 단위 테스트 없이 **manual smoke test(Task 6)** 로 검증한다. 단, JSON 분기 분리 함수는 Task 5에서 테스트한다.

**Files:**
- Modify: `scripts/discord_listener.py` (신규 함수 + import 1개 + 디스패치 분기)

- [ ] **Step 1: 핸들러 본체 추가**

[scripts/discord_listener.py](scripts/discord_listener.py) 의 `_handle_reset` 함수 **바로 위에** 다음을 추가:

```python
def _download_attachments(
    attachments: list[dict], msg_id: str, log: logging.Logger,
) -> list[Path]:
    """Discord CDN URL은 인증 불필요. 이미지만 /tmp/cal_<msg_id>_<i>.<ext>로 저장."""
    paths: list[Path] = []
    for i, a in enumerate(attachments or []):
        ctype = (a.get("content_type") or "")
        if not ctype.startswith("image/"):
            continue
        ext = ctype.split("/", 1)[1].split(";")[0].strip() or "png"
        path = Path(f"/tmp/cal_{msg_id}_{i}.{ext}")
        try:
            r = requests.get(a["url"], timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            path.write_bytes(r.content)
            paths.append(path)
        except Exception:
            log.exception("attachment download failed: %s", a.get("url"))
    return paths


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
        "1. 입력에서 일정 정보(제목, 일시, 장소, 참석자)를 추출\n"
        "2. 일정과 무관한 잡담/광고/링크/사진이면 {\"status\":\"not_event\"} 반환\n"
        "3. 날짜/시간이 모호('내일', '다음주 화', '점심', '저녁', '오전', '오후')\n"
        "   → {\"status\":\"need_confirmation\",\"question\":\"...\"}\n"
        "4. 명확하면 mcp__claude_ai_Google_Calendar__create_event 호출:\n"
        "   - calendar_id: \"primary\"\n"
        "   - time_zone: \"Asia/Seoul\"\n"
        "   - 시각 정보 유무에 따른 길이:\n"
        "     * 시각 명시 + 종료 시각 미지정 → 1시간 이벤트\n"
        "     * 시각 정보 없이 날짜만 (예: '5/30 김유일 부장 점심') → 종일(all-day) 이벤트\n"
        "     * '점심'/'저녁'/'오전'/'오후'만 있고 시각 모호 → need_confirmation\n"
        "   - summary는 핵심만 (예: '코람코 김태원 대표 미팅')\n"
        "   - description에 원문 텍스트와 참석자 정보 기록\n"
        "   - attendees는 이메일 모르면 비움\n"
        "5. 등록 성공 시 {\"status\":\"created\",\"event_link\":\"<htmlLink>\","
        "\"summary\":\"<KR 한줄>\",\"event_id\":\"<id>\"}\n\n"
        "★ 응답은 JSON 한 덩어리만. 다른 텍스트 금지.\n"
    )


def _handle_calendar(token: str, msg: dict, log: logging.Logger, state: dict) -> None:
    """이미지/키워드 트리거 → Claude CLI로 일정 추출+Google Calendar 등록."""
    content = (msg.get("content") or "").strip()
    image_paths = _download_attachments(msg.get("attachments") or [], msg["id"], log)
    _react(token, msg["id"], "📅")

    prompt = _build_calendar_prompt(content, image_paths)

    sid = state.get("claude_session_id")
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
        state["claude_session_id"] = new_sid
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
    elif status == "need_confirmation":
        q = payload.get("question", "확인이 필요합니다.")
        _send(token, f"❓ {q}", reply_to=msg["id"])
    elif status == "not_event":
        _react(token, msg["id"], "🤷")
    else:
        _send(token, f"⚠️ 알 수 없는 status: {status!r}\n```\n{payload_text[:1500]}\n```",
              reply_to=msg["id"])
```

- [ ] **Step 2: 디스패치 분기 추가**

[scripts/discord_listener.py:512](scripts/discord_listener.py#L512) 의 `_handle_claude`/`_handle_reset` 분기 옆에 calendar 추가:

```python
                    # claude/reset/calendar은 state(session_id)가 필요해 별도 디스패치
                    if cmd == "claude":
                        _handle_claude(token, m, log, state)
                    elif cmd == "reset":
                        _handle_reset(token, m, log, state)
                    elif cmd == "calendar":
                        _handle_calendar(token, m, log, state)
                    else:
                        HANDLERS[cmd](token, m, log)
```

- [ ] **Step 3: 기존 회귀 테스트 재실행 — 임포트 안정성 확인**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py -v
```

Expected: 24개 PASS (기존 19 + Task 3 의 5).

- [ ] **Step 4: 문법 점검 — Python 임포트 확인**

Run:
```bash
poc/.venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import discord_listener; print('OK', discord_listener._handle_calendar.__name__)"
```

Expected: `OK _handle_calendar` 출력.

- [ ] **Step 5: Commit**

```bash
git add scripts/discord_listener.py
git commit -m "feat(listener): add _handle_calendar — image/keyword → Google Calendar

- _download_attachments: Discord CDN에서 이미지 다운로드
- _build_calendar_prompt: 오늘 날짜/요일/타임존 주입 + JSON 응답 강제
- _handle_calendar: subprocess(Claude CLI) → status 분기
  - created → ✅ summary + event_link 답장
  - need_confirmation → ❓ 질문 답장 (사용자 답장은 _handle_claude가 세션 컨텍스트로 처리)
  - not_event → 🤷 리액션만, 답장 없음
- Discord 디스패치에 calendar 분기 1줄 추가
- tempfile은 try/finally로 unlink"
```

---

## Task 5: 프롬프트 빌더 단위 테스트 (오늘 날짜·이미지 힌트)

`_build_calendar_prompt`는 순수 함수라 테스트 가능. 핵심: 오늘 날짜·요일이 포함되는지, 이미지 경로 힌트가 정확히 들어가는지.

**Files:**
- Modify: `tests/test_discord_listener.py`

- [ ] **Step 1: 테스트 추가**

`tests/test_discord_listener.py` 끝에 추가:

```python
from datetime import date
from pathlib import Path as _Path


class TestBuildCalendarPrompt:
    def test_includes_today_iso(self):
        prompt = discord_listener._build_calendar_prompt("내일 14시 미팅", [])
        assert date.today().isoformat() in prompt

    def test_includes_weekday_korean(self):
        prompt = discord_listener._build_calendar_prompt("내일 14시 미팅", [])
        weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][date.today().weekday()]
        assert weekday_kr in prompt

    def test_includes_image_paths(self):
        paths = [_Path("/tmp/cal_x_0.png"), _Path("/tmp/cal_x_1.jpg")]
        prompt = discord_listener._build_calendar_prompt("", paths)
        assert "/tmp/cal_x_0.png" in prompt
        assert "/tmp/cal_x_1.jpg" in prompt
        assert "Read 도구" in prompt

    def test_empty_content_marker(self):
        prompt = discord_listener._build_calendar_prompt("", [])
        assert "(텍스트 없음)" in prompt

    def test_required_status_keywords_present(self):
        prompt = discord_listener._build_calendar_prompt("test", [])
        for kw in ("not_event", "need_confirmation", "created",
                   "mcp__claude_ai_Google_Calendar__create_event",
                   "Asia/Seoul"):
            assert kw in prompt, f"missing keyword in prompt: {kw}"
```

- [ ] **Step 2: 테스트 실행 — PASS 확인**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py::TestBuildCalendarPrompt -v
```

Expected: 5개 PASS.

- [ ] **Step 3: 전체 회귀 테스트**

Run:
```bash
poc/.venv/bin/python -m pytest tests/test_discord_listener.py -v
```

Expected: 29개 PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_discord_listener.py
git commit -m "test(listener): unit tests for _build_calendar_prompt"
```

---

## Task 6: listener 재로드 + Manual Smoke Test (T1~T7)

자동화 테스트로 커버 못한 통합 동작은 실제 Discord 채널에서 manual smoke test로 검증한다.

**Files:** 변경 없음. listener 재로드 + Discord에서 메시지 송신.

- [ ] **Step 1: listener 재로드**

Run:
```bash
launchctl unload ~/Library/LaunchAgents/com.swlee.voiceflow-discord-listener.plist
launchctl load   ~/Library/LaunchAgents/com.swlee.voiceflow-discord-listener.plist
tail -5 /tmp/voiceflow-discord-listener.log
```

Expected: 로그에 `started — channel=... allowed=... last_id=...` 라인이 새로 찍힘.

- [ ] **Step 2: 회귀 점검 — "ping"**

Discord 채널에서 `ping` 전송.

Expected: 🏓 `pong — daemon alive` 답장. (기존 동작 회귀 없음)

- [ ] **Step 3: T3 — 명확 자연어**

Discord 채널에서 전송: `캘린더 5/30 오후 3시 강남 코람코 점심`

Expected: 30초 이내 `✅ 등록 완료 — ... \n🔗 https://www.google.com/calendar/event?eid=...` 답장. Google Calendar에서 5/30 15:00 이벤트 확인.

- [ ] **Step 4: T2 — 모호 자연어**

Discord 채널에서 전송: `일정 내일 14시 토스 김유일 부장 미팅`

Expected: `❓ 내일 = 2026-MM-DD 맞으신가요?` 형태 답장. (실행 일자 기준 "내일")

이어서 `응` 답장.

Expected: 기존 `_handle_claude`가 같은 세션 컨텍스트로 처리하여 등록 완료 답장.

- [ ] **Step 5: T1 — 카톡 캡처 이미지 1장**

실제 카톡 일정 메시지를 캡처해 Discord 채널에 텍스트 없이 첨부만 전송.

Expected: 📅 리액션 → 60초 이내 `✅ 등록 완료 — ...` 답장. 캘린더에서 일정 정확도 확인 (제목/시각/장소).

- [ ] **Step 6: T4 — 일정 외 이미지**

일정과 무관한 사진(예: 음식 사진) + "맛있다" 같은 메시지 전송.

Expected: 📅 → 🤷 리액션만, 답장 없음. (`not_event` 분기)

- [ ] **Step 7: T6 — 수정 시나리오**

Step 3 또는 Step 5에서 등록한 직후 답장으로: `방금 거 30분 늦춰줘`

Expected: 🤔 리액션 → `_handle_claude`가 세션 컨텍스트로 처리 → 캘린더에서 시각이 30분 이동된 것 확인.

- [ ] **Step 8: T5 — 기존 명령 회귀**

`상태`, `브리핑`, `음성메모`(또는 `voiceflow`)를 차례로 전송.

Expected: 각각 기존 핸들러가 동작 (status/morning_briefing/process_voice). 회귀 없음.

- [ ] **Step 9: 로그 점검**

Run:
```bash
grep "calendar" /tmp/voiceflow-discord-listener.log | tail -20
```

Expected: 각 calendar 호출이 `calendar call: ...` + `calendar reply: status=...` 쌍으로 기록됨.

- [ ] **Step 10: smoke test 결과 commit (체크리스트 갱신)**

별도 코드 변경 없음. 본 task의 체크박스를 체크한 plan 파일을 커밋:

```bash
git add docs/superpowers/plans/2026-05-21-discord-calendar-bot.md
git commit -m "docs(listener): mark calendar bot smoke tests passed"
```

---

## Task 7: 운영 문서 추가 (CLAUDE.md 업데이트)

`002_voice_flow_v3/CLAUDE.md`에 새 명령(calendar 트리거)을 운영 가이드에 반영. 데몬 표·명령 표에 1행 추가.

**Files:**
- Modify: `CLAUDE.md` (Discord listener 운영 섹션이 있다면 그쪽, 없으면 적절한 위치)

- [ ] **Step 1: CLAUDE.md 확인 후 적절한 위치 찾기**

Run:
```bash
grep -n "discord_listener\|listener\|일정\|캘린더" /Users/swlee/Documents/Coding/002_voice_flow_v3/CLAUDE.md | head -20
```

- [ ] **Step 2: 운영 가이드 한 단락 추가**

CLAUDE.md 어딘가(예: 수동 실행 동선 인근) 다음 블록을 추가:

```markdown
### Discord 캘린더 등록 (2026-05-21 추가)

iPhone Discord에서 다음 중 하나로 트리거 → Google Calendar(primary)에 자동 등록:
- 이미지 첨부(카톡 캡처 등) — 텍스트 없어도 OK
- 메시지가 "일정 ..." / "캘린더 ..." / "calendar ..." / "/calendar ..." 로 시작

처리 흐름:
- 📅 리액션 → Claude CLI(Vision + Google Calendar MCP) → 답장
- ✅ 등록 완료 (요약 + event_link)
- ❓ 모호한 일시는 되묻기 → 사용자 답장 시 기존 _handle_claude 세션 컨텍스트로 처리
- 🤷 일정 외 메시지는 조용히 무시

수정/삭제는 자연어 답장 ("방금 거 15시로", "삭제해줘") — 채널 세션 컨텍스트로 처리됨.

소스: `scripts/discord_listener.py` 의 `_handle_calendar`.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document Discord calendar trigger in CLAUDE.md"
```

---

## Self-Review

**Spec coverage:**
- § 2 결정사항 7개 → Task 1·2(분기), Task 4(핸들러), Task 6(smoke)에서 전부 커버
- § 5.1 `_match_command` 분기 → Task 1·2
- § 5.2 `_handle_calendar` → Task 4
- § 5.3 디스패치 분기 → Task 4 Step 2
- § 6 수정/삭제 (세션 재사용) → Task 6 Step 7
- § 7 안전장치 7건 → Task 1 회귀 테스트(키워드 충돌 방지), Task 4 코드 내 try/finally·timeout·status 분기
- § 8 테스트 시나리오 T1~T7 → Task 6 Steps 2-8 (자동화 가능한 _match_command·_strip_json_fence·_build_calendar_prompt는 단위 테스트, 통합 동작은 smoke test)
- § 10 운영 절차 → Task 6 Step 1 (재로드), Task 7 (문서)

**Placeholder scan:** "TBD"/"implement later"/"appropriate error handling" 등 없음. 모든 step에 실제 코드/명령 포함.

**Type consistency:**
- `_match_command(content: str, attachments: list[dict] | None = None)` — Task 1 정의, Task 2 사용, Task 4 호출부 일관
- `_handle_calendar(token, msg, log, state)` 시그니처 — Task 4 정의, Task 4 Step 2 디스패치 일관
- `_strip_json_fence(text: str) -> str` — Task 3 정의, Task 4 호출 일관
- `_build_calendar_prompt(content, image_paths)` — Task 4 정의, Task 5 테스트 일관
- `_download_attachments(attachments, msg_id, log)` — Task 4 정의 및 호출 일관

이상 없음.
