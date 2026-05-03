# 일일 브리핑 Notion DB 셋업 (1회)

매일 출근 시점 자동 생성될 일일 브리핑 페이지가 들어갈 Notion DB를 사전 1회 만든다.

## 1. Notion에서 신규 DB 생성

원하는 부모 페이지 (예: "VoiceFlow" 또는 "일일 기록")로 이동 → `/` → `Database - Full page` 선택 → 이름: **일일 브리핑**

## 2. 속성 정의 (정확한 한글명 — 코드와 일치 필수)

| 속성명 | 타입 | 옵션 |
|--------|------|------|
| 제목 | Title (기본) | — |
| 날짜 | Date | — |
| 상태 | Select | 신규 / 검토중 / 완료 |
| 어제미팅수 | Number | — |
| 오늘To-Do수 | Number | — |
| 실패건수 | Number | — |
| 프로젝트 | Multi-select | (자동 채워짐) |

> **이름은 정확히 일치해야 함.** Notion API는 속성을 한글명으로 매칭한다 (`src/agents/notion_briefing_builder.py:_build_properties` 참조).

## 3. 권장 view

- **Calendar view**: "날짜" 기준 — 월간 출근 패턴 시각화
- **Table view (default)**: "날짜" 내림차순 — 최신 브리핑이 위
- **Filter**: "상태 != 완료" — 검토 안 한 브리핑만 표시

## 4. DB ID 추출 + .env 등록

DB 페이지 URL: `https://www.notion.so/<workspace>/<DB_ID>?v=...`

`<DB_ID>` (32자 hex)를 복사해서 `.env`에 추가:

```bash
NOTION_BRIEFING_DATABASE_ID=12345678abcd1234efgh567890ijklmn
```

## 5. 권한 부여

Notion integration이 이 DB에 쓰기 권한을 가져야 한다:
- DB 페이지 우상단 ⋯ → "Connect to" → 기존 VoiceFlow integration 선택

## 6. 검증

```bash
cd /Users/swlee/Documents/Coding/002_voice_flow_v3
poc/.venv/bin/python scripts/daily_briefing.py
```

Expected: 새 페이지가 DB에 생성되고 macOS notification 표시.

---

## 7. launchd 등록 (Notion DB 셋업 완료 후)

```bash
# 파일은 docs/launchd/com.swlee.voiceflow-briefing.plist 참조
cp docs/launchd/com.swlee.voiceflow-briefing.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.swlee.voiceflow-briefing.plist
```

> **주의**: `NOTION_BRIEFING_DATABASE_ID` 가 `.env`에 세팅된 것을 확인한 뒤 load 할 것.
