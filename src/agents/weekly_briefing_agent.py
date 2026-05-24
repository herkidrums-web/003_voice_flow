"""WeeklyBriefingAgent — 지난 주(월~일) 미팅을 합성해 주간 브리핑 생성.

BriefingAgent의 확장 — 단순 그룹핑이 아니라 토픽별 진행 흐름(시작→진행→결과),
핵심 결정 누적, 다음 주 이성우 액션 제시를 추가.

설계 결정 (2026-05-13):
- 모델: Opus 4.7 — 시간순 흐름 + 인과 연결은 복잡 추론
- 입력 기간: 지난 주 월요일 00:00 ~ 일요일 23:59 (KST)
- 매주 월요일 07:05 launchd 자동 실행
- Notion 동일 briefing DB, 제목: '{년-W주차} 주간 브리핑 ({시작} ~ {끝})'
"""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent


_PROMPT = """당신은 이성우 담당(기업AI고객담당)의 **주간 브리핑**을 작성하는 비서입니다.

지난 주({week_start} ~ {week_end}) 처리된 미팅들을 시간 순서대로 합성하여,
토픽별 진행 흐름과 다음 주 이성우 본인의 액션을 제시합니다.

# Wiki 토픽 anchor 목록 (이걸 우선 사용)
{anchors_block}

# 지난 주 처리된 미팅들 (시간 순)
{meetings_block}

{exclude_block}

# 작업
1. **토픽 그룹핑** — 각 미팅을 Wiki anchor에 매칭. 같은 anchor에 묶인 미팅이 여러 건이면 시간 순서대로 진행 흐름 추적
2. **사실(facts) 추출** — 토픽별 핵심 사실. **시간순 변화** 포함 (예: "5/12 X 결정 → 5/15 Y 진행 → 5/17 Z 완료")
3. **이성우 다음 주 To-Do** — 다음 조건:
   - 주체가 명시적으로 "이성우/담당/제가/내가" 인 액션
   - 주체 명시 없지만 문맥상 영업담당 본인이 할 일
   - **미완료 + 다음 주 진행 필요** 액션 우선
   - 제외: 다른 직원/고객사/외부 액션
4. **week_overview** — 한 줄로 지난 주 핵심 요약 (Executive Summary용)
5. **top_themes** — 지난 주 가장 중요한 주제 5~7개
6. **new_anchors_suggested** — Wiki anchor 후보 (1~3개, 신규 주제 발견 시)

JSON만 응답. 다른 텍스트 금지.
{{
  "week_overview": "지난 주를 한 문장으로 요약",
  "blocks": [
    {{
      "topic": "토픽명",
      "anchor_path": "wiki/.../...md 또는 빈 문자열",
      "is_new_anchor": false,
      "meetings": ["미팅 제목 1", "미팅 제목 2"],
      "meeting_urls": ["url1", "url2"],
      "facts": ["5/12 사실", "5/15 진행 사실"],
      "todos": ["이성우 다음 주 액션 1"]
    }}
  ],
  "top_themes": ["테마 1", "테마 2"],
  "new_anchors_suggested": [
    {{"title": "신규 anchor", "category": "Business/Customers", "reason": "왜"}}
  ]
}}

규칙:
- 한국어 작성, 존댓말 사용
- 사실은 숫자/시점/금액 우선
- 모호한 수식어(많이/잘/적극) 금지
- 자화자찬(성공적으로/주효한) 금지
- 동일 토픽 여러 미팅 — 시간 순 묶음 + 변화 흐름 표기
- todos는 다음 주(월~일) 진행할 액션만 (지난 주 완료 액션 제외)
"""


class WeeklyBriefingAgent(BaseAgent):
    name = "weekly_briefing"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        meetings: list[dict] = payload["meetings"]
        anchors: list[dict] = payload.get("anchors", [])
        week_start: str = payload["week_start"]
        week_end: str = payload["week_end"]
        exclude_persons: list[str] = payload.get("exclude_persons", [])

        anchors_block = "\n".join(
            f"- [{a['title']}]({a['path']}) ({a['category']}): {a['summary']}"
            for a in anchors
        ) or "(없음)"

        meetings_block = "\n\n".join(
            f"## {i+1}. [{m.get('date', '')}] {m.get('title', 'untitled')}\n"
            f"- 시각: {m.get('time', '')}\n"
            f"- 참석자: {', '.join(m.get('participants', []))}\n"
            f"- 요약: {m.get('summary', '')}\n"
            f"- 결정: {' / '.join(m.get('decisions', [])) or '(없음)'}\n"
            f"- 액션: {' / '.join(m.get('actions', [])) or '(없음)'}\n"
            f"- 프로젝트: {', '.join(m.get('project', [])) if isinstance(m.get('project'), list) else m.get('project', '')}\n"
            f"- 고객: {m.get('customer', '')}\n"
            f"- Notion: {m.get('notion_url', '')}"
            for i, m in enumerate(meetings)
        ) or "(지난 주 처리된 미팅 없음)"

        exclude_block = ""
        if exclude_persons:
            names = ", ".join(exclude_persons)
            exclude_block = (
                f"# ⚠️ 제외 대상 인물: {names}\n"
                f"위 인물(들)이 주체이거나 책임진 사실/액션/토픽은 전부 제외해주세요.\n"
            )

        prompt = _PROMPT.format(
            week_start=week_start,
            week_end=week_end,
            anchors_block=anchors_block,
            meetings_block=meetings_block,
            exclude_block=exclude_block,
        )

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"WeeklyBriefing returned invalid JSON: {e}: {text[:200]}")

        return {
            "week_overview": parsed.get("week_overview", ""),
            "blocks": parsed.get("blocks", []),
            "top_themes": parsed.get("top_themes", []),
            "new_anchors_suggested": parsed.get("new_anchors_suggested", []),
            "week_start": week_start,
            "week_end": week_end,
        }
