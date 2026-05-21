# Discord Calendar Bot 통합 설계

- **작성일**: 2026-05-21
- **작성자**: 이성우 담당 + Claude
- **참고**: https://aiccbiz2.github.io/20260520_discord-calendar-bot/
- **상태**: 설계 완료, 구현 대기

## 1. 배경 및 목표

이성우 담당은 B2B 영업 업무 중 카카오톡 캡처, Outlook 초대, 자연어 메시지 형태로 일정 정보를 자주 받는다. 이를 Google Calendar에 수기 입력하는 시간을 0에 가깝게 줄이고, iPhone Discord에서 캡처 한 장 던지면 자동 등록되도록 한다.

원본 프로젝트는 6개 모듈로 구성된 독립 봇이지만, 본 통합은 기존 `discord_listener.py`(`002_voice_flow_v3/scripts/`)에 핸들러 한 개를 추가하는 최소 변경으로 같은 결과를 얻는다.

### 운영 철학과의 정합성
- VoiceFlow가 2026-05-13 "자동 sync 폐기 → 수동 트리거"로 회귀한 학습과 일관(`voiceflow_manual_workflow`).
- CLI/MCP 우선 원칙: Calendar 등록은 `mcp__claude_ai_Google_Calendar__create_event`로만 수행. API 직접 호출 없음.
- Claude 호출은 CLI subprocess 방식 — Anthropic SDK 직접 사용 금지.

## 2. 결정 사항

| 항목 | 결정 |
|---|---|
| 통합 방식 | 기존 `discord_listener.py`에 핸들러 1개 추가 (신규 데몬 없음) |
| 등록 대상 | Google Calendar primary 캘린더만 |
| 주 입력 포맷 | 카카오톡 캡처 이미지 (보조: 자연어 텍스트) |
| 트리거 조건 | (a) 메시지 이미지 첨부 있음 OR (b) 텍스트가 `"일정"`/`"캘린더"`로 시작 |
| 모호한 날짜 처리 | "내일"/"다음주 화"는 오늘 기준 자동 계산. 날짜 자체가 없으면 되묻기 |
| 모호한 시각 처리 | "저녁"/"점심"/"오후" 등 시간대만 있으면 되묻지 않고 종일 이벤트 + "(시간 미정)" 표기 |
| 수정/삭제 인터페이스 | 자연어 답장 → 기존 `_handle_claude` 세션 컨텍스트 재사용 |
| 일정 외 메시지 | `not_event` 응답 → 🤷 리액션만, 답장 없음 |

## 3. 아키텍처

```
[iPhone Discord]
   ├─ 텍스트 + 카톡 캡처
   ├─ 이미지만 있는 메시지
   └─ "일정 ..." / "캘린더 ..." 명시 키워드
   ↓
[discord_listener.py] — 기존 데몬 (com.swlee.voiceflow-discord-listener)
   ├─ _match_command(): 이미지 OR 키워드 시작 → "calendar" 분기
   └─ _handle_calendar():
       ├─ 이미지를 /tmp/cal_<msg_id>.<ext>로 다운로드 (Discord CDN, 인증 불필요)
       ├─ Claude CLI subprocess 호출
       │   --permission-mode bypassPermissions
       │   --output-format json
       │   --model sonnet
       │   --resume <channel_session_id>
       └─ JSON 응답 파싱 → Discord 답장 → tempfile 정리
   ↓
[Claude CLI]
   ├─ 이미지 Read (Vision 내장)
   ├─ 일정 정보 추출 + 오늘 기준 모호 날짜 해석
   ├─ status 분기:
   │   ├─ created: mcp__claude_ai_Google_Calendar__create_event 호출
   │   ├─ need_confirmation: 사용자에게 질문
   │   └─ not_event: 일정 아님
   └─ JSON 한 덩어리 응답
   ↓
[Discord 답장]
   ├─ created: "✅ 5/22(금) 14:00 — 코람코 김태원 대표 미팅 (강남)\n🔗 <event_link>"
   ├─ need_confirmation: "❓ 다음주 화요일 = 5/26 맞으신가요?"
   └─ not_event: 🤷 리액션만
```

## 4. 변경 파일

**`scripts/discord_listener.py` 1개 파일만 수정.** 새 모듈 0개, 새 LaunchAgent 0개, 새 의존성 0개.

## 5. 코드 변경 상세

### 5.1 `_match_command()` 분기 추가

기존 키워드(브리핑·음성메모·상태·ping)는 그대로 유지. 새 분기는 **기존 명령보다 뒤에**, `_handle_claude` fallback보다 **앞에** 평가한다.

```python
def _match_command(content: str, attachments: list[dict] | None = None) -> str | None:
    # ... (기존 ping, process_all, process_voice, weekly_briefing, morning_briefing, status, reset 그대로)
    lower = content.lower().strip()

    # 새 분기 — calendar
    has_image = any(
        (a.get("content_type") or "").startswith("image/")
        for a in (attachments or [])
    )
    starts_with_keyword = (
        lower.startswith("일정") or lower.startswith("캘린더") or
        lower.startswith("/calendar") or lower.startswith("calendar")
    )
    if has_image or starts_with_keyword:
        return "calendar"

    # 그 외 → Claude
    return "claude"
```

호출부 변경: `_match_command(m.get("content", ""))` → `_match_command(m.get("content", ""), m.get("attachments"))`.

### 5.2 `_handle_calendar()` 신규 핸들러

```python
def _handle_calendar(token: str, msg: dict, log: logging.Logger, state: dict) -> None:
    """이미지/키워드 트리거 → Claude CLI로 일정 추출+Google Calendar 등록."""
    content = (msg.get("content") or "").strip()
    attachments = msg.get("attachments") or []
    image_paths: list[Path] = []

    # 1. 이미지 다운로드 (Discord CDN, 봇 토큰 불필요)
    for i, a in enumerate(attachments):
        ctype = (a.get("content_type") or "")
        if not ctype.startswith("image/"):
            continue
        ext = ctype.split("/", 1)[1].split(";")[0] or "png"
        path = Path(f"/tmp/cal_{msg['id']}_{i}.{ext}")
        try:
            r = requests.get(a["url"], timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            path.write_bytes(r.content)
            image_paths.append(path)
        except Exception:
            log.exception("attachment download failed: %s", a.get("url"))

    _react(token, msg["id"], "📅")

    # 2. Claude CLI 프롬프트 구성
    today = date.today()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][today.weekday()]
    image_hint = ""
    if image_paths:
        image_hint = "\n첨부 이미지:\n" + "\n".join(f"- {p}" for p in image_paths) + \
                     "\nRead 도구로 이미지를 열어 텍스트(카카오톡 메시지 등)를 추출하세요."

    prompt = f"""\
오늘은 {today.isoformat()} ({weekday_kr}). 사용자는 이성우 담당(LG U+ 기업AI고객담당).
시간대는 Asia/Seoul.

입력 메시지:
{content or "(텍스트 없음)"}
{image_hint}

작업:
1. 입력에서 일정 정보(제목, 일시, 장소, 참석자)를 추출
2. 일정과 무관한 잡담/광고/링크/사진이면 {{"status":"not_event"}} 반환
3. 날짜 자체가 불명확하면(날짜 없음, "조만간", "다음에") → {{"status":"need_confirmation","question":"..."}}
   ("내일"/"다음주 화" 등은 오늘 날짜 기준으로 계산하여 확정, 되묻지 않음)
4. 날짜가 확정되면 mcp__claude_ai_Google_Calendar__create_event 호출:
   - calendar_id: "primary"
   - time_zone: "Asia/Seoul"
   - 시각 처리 규칙 (시각이 모호해도 되묻지 말 것):
     * 정확한 시각 명시 + 종료 시각 미지정 → 1시간 이벤트
     * 시각 정보 없이 날짜만, 또는 "점심"/"저녁"/"오전"/"오후"처럼 시간대만 표기
       → 종일(all-day) 이벤트로 등록. summary 끝에 "(시간 미정)" 표기, 시간대 단어는 제목에 유지
         (예: "김태원 대표 저녁 식사 (시간 미정)")
   - summary는 핵심만 (예: "코람코 김태원 대표 미팅")
   - description에 원문 텍스트와 참석자 정보 기록
   - attendees는 이메일 모르면 비움
5. 등록 성공 시 {{"status":"created","event_link":"<htmlLink>","summary":"<KR 한줄>","event_id":"<id>"}}

★ 응답은 JSON 한 덩어리만. 다른 텍스트 금지.
"""

    # 3. Claude CLI 호출 (기존 _handle_claude와 동일 패턴, --resume으로 세션 연결)
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

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT, cwd=CLAUDE_CWD,
        )
    except subprocess.TimeoutExpired:
        _send(token, f"⏱ Claude timeout ({CLAUDE_TIMEOUT}s)", reply_to=msg["id"])
        return
    finally:
        for p in image_paths:
            try: p.unlink(missing_ok=True)
            except Exception: pass

    if result.returncode != 0:
        tail = (result.stderr or result.stdout)[-1500:]
        _send(token, f"❌ Claude exit={result.returncode}\n```\n{tail}\n```", reply_to=msg["id"])
        return

    # 4. JSON 응답 파싱 (Claude CLI의 envelope → result 안에 우리 JSON)
    try:
        envelope = json.loads(result.stdout)
        new_sid = envelope.get("session_id")
        if new_sid:
            state["claude_session_id"] = new_sid
            _save_state(state)
        payload_text = (envelope.get("result") or "").strip()
        # 펜스 제거 (Claude가 ```json...``` 으로 감싸는 경우 방어)
        if payload_text.startswith("```"):
            payload_text = payload_text.split("```", 2)[1]
            if payload_text.startswith("json"):
                payload_text = payload_text[4:]
            payload_text = payload_text.strip()
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, KeyError) as e:
        _send(token, f"❌ JSON parse 실패: {e}\n```\n{result.stdout[-1500:]}\n```", reply_to=msg["id"])
        return

    status = payload.get("status")

    if status == "created":
        link = payload.get("event_link", "")
        summary = payload.get("summary", "(요약 없음)")
        _send(token, f"✅ 등록 완료 — {summary}\n🔗 {link}", reply_to=msg["id"])
    elif status == "need_confirmation":
        q = payload.get("question", "확인이 필요합니다.")
        _send(token, f"❓ {q}", reply_to=msg["id"])
    elif status == "not_event":
        # 조용히 무시 (리액션만 변경)
        _react(token, msg["id"], "🤷")
    else:
        _send(token, f"⚠️ 알 수 없는 status: {status}\n```\n{payload_text[:1500]}\n```", reply_to=msg["id"])
```

### 5.3 `HANDLERS` 및 디스패치

`HANDLERS`는 `(token, msg, log)` 3-인자 핸들러용. `_handle_calendar`는 `state`가 필요하므로 `_handle_claude`/`_handle_reset`과 같은 별도 분기로 디스패치:

```python
if cmd == "claude":
    _handle_claude(token, m, log, state)
elif cmd == "reset":
    _handle_reset(token, m, log, state)
elif cmd == "calendar":
    _handle_calendar(token, m, log, state)
else:
    HANDLERS[cmd](token, m, log)
```

## 6. 수정/삭제 시나리오

별도 핸들러 추가 없음. listener는 채널당 `--resume <session_id>`로 Claude 세션을 유지하므로, 사용자가 등록 직후 자연어로 답장하면 `_handle_claude`가 이전 컨텍스트(어떤 이벤트를 만들었는지)를 알고 처리한다.

| 사용자 답장 예시 | 처리 경로 |
|---|---|
| "방금 거 30분 늦춰줘" | `_handle_claude` → 세션 컨텍스트 → `mcp__update_event` |
| "방금 등록한 거 삭제해줘" | `_handle_claude` → 세션 컨텍스트 → `mcp__delete_event` |
| "토스 미팅 시간을 16시로 옮겨" | `_handle_claude` → 캘린더 검색 → `mcp__update_event` |

세션 만료(`/reset` 또는 새 채널) 시는 캘린더 직접 조작 또는 새 메시지로 다시 트리거.

## 7. 안전장치

| 위험 | 대응 |
|---|---|
| 키워드 오트리거 ("오늘 일정 어때?") | `startswith("일정"/"캘린더")`만 매칭. 문장 중간 키워드는 `_handle_claude`로 흘림 |
| 이미지 첨부지만 일정 아닌 케이스 (강아지 사진) | 프롬프트에서 `not_event` 분류 → 🤷 리액션, 답장 없음 |
| 잘못된 자동 등록 | created 답장에 event_link 표시 → 캘린더에서 직접 삭제 가능. "삭제해줘" 자연어 지원 |
| tempfile 누적 | `try/finally`로 `unlink(missing_ok=True)` |
| Claude timeout | 기존 `CLAUDE_TIMEOUT=900s` 적용. 초과 시 Discord에 보고 |
| MCP 호출 실패 | bypassPermissions 컨텍스트라 권한 확인 불필요. 실패 시 Claude가 status 외 메시지로 응답 → "알 수 없는 status" 분기로 사용자에게 노출 |
| 기존 명령 회귀 | 새 분기는 모든 기존 키워드 매칭 **이후** 평가. 회귀 위험 0 |

## 8. 테스트 시나리오

| # | 입력 | 기대 동작 |
|---|---|---|
| T1 | 카톡 캡처 1장(텍스트 없음) "6/5 14:00 토스 김유일 부장님 회의 강남" | `🔗 event_link` 답장, 캘린더에 등록 확인 |
| T2 | "일정 내일 14시 토스 김유일 미팅" | 즉시 등록 (내일 = 오늘+1 자동 계산) |
| T2b | 캡처에 "6/22 김태원 대표 저녁 식사" (시각 없음) | 되묻지 않고 종일 이벤트 + "(시간 미정)" 등록 |
| T3 | "캘린더 5/30 오후 3시 강남 코람코 점심" | 즉시 등록 답장 (5/30 15:00) |
| T4 | 강아지 사진 + "귀엽다" | 🤷 리액션, 답장 없음 |
| T5 | "브리핑" / "음성메모" / "ping" / "상태" | 기존 동작 회귀 없음 |
| T6 | T1 등록 직후 "방금 거 30분 늦춰줘" | `_handle_claude` 컨텍스트로 update_event 호출, 캘린더 시간 변경 |
| T7 | 메시지 첨부 이미지 다운로드 실패 (네트워크) | 텍스트만 갖고 추출 시도, 부족하면 `need_confirmation` |

## 9. 스코프 밖 (YAGNI)

원본 프로젝트의 다음 기능은 1차 구현에서 제외한다. 필요성이 확인되면 별도 spec.

- ❌ SQLite 매핑 테이블 (`state.py`) — Claude 세션 컨텍스트가 대체
- ❌ Discord Embed의 [수정]/[삭제] 버튼 UI — 자연어 답장으로 대체
- ❌ 캐치업 로직 — 기존 listener의 `_init_state_to_latest`가 first-run skip 처리 중
- ❌ Notion 미팅노트 동기 등록 — 등록 대상은 Calendar만 (의도적)
- ❌ 별도 LaunchAgent / 별도 봇 프로세스

## 10. 운영 절차

### 배포
1. 코드 수정 (단일 파일)
2. listener 재시작:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.swlee.voiceflow-discord-listener.plist
   launchctl load   ~/Library/LaunchAgents/com.swlee.voiceflow-discord-listener.plist
   ```
3. Discord에서 "ping" → "pong" 회신 확인 (회귀 점검)
4. T1 시나리오 실행 → 캘린더 등록 확인

### 롤백
- 변경된 단일 파일을 `git revert` 후 listener 재시작. 새 의존성·새 데몬·새 DB 없으므로 잔여물 없음.

### 모니터링
- 기존 `/tmp/voiceflow-discord-listener.log` 그대로 사용
- `_handle_calendar`는 `log.info`로 모든 분기(`status=...`) 기록

## 11. 변경 영향 범위

| 영역 | 영향 |
|---|---|
| `voiceflow_v3` 파이프라인 | 없음 (orchestrator/brief/sync 모두 분리) |
| Notion 동기화 | 없음 |
| 기존 Discord 명령 | 없음 (새 분기는 후순위 평가) |
| Google Calendar | 신규 이벤트 생성/수정/삭제 가능 |
| Claude 세션 사용량 | 일정 등록 1회당 1 Sonnet turn 추가 (구독제, 별도 과금 없음) |
