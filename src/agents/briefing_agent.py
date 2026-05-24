"""BriefingAgent — Wiki anchor 기반 토픽 그룹핑 + 이성우 담당 To-Do 필터링.

입력: 오늘 처리된 미팅 노트 list + Wiki anchor 목록
출력:
- briefing_blocks: 주제별 [{topic, anchor_path, meetings, facts, todos}]
- top_themes: 오늘 핵심 3-5개
- new_anchors_suggested: Wiki에 새로 만들면 좋을 주제 후보

설계 결정 (2026-05-13):
- 모델: Opus 4.7 — 그룹핑 + 필터링 + 온톨로지 매칭은 복잡한 추론이 필요
- 주제 추출: Wiki anchor 우선 매칭 + 매칭 안 되면 새 anchor 제안
- To-Do 필터: 액션 주체가 "이성우/담당/제가/내가" 또는 명시 없는데 영업담당 본인 액션일 때만 포함
- 다른 사람 액션(이상윤/박홍래/전우경/안형균 등 직원 액션, 고객사 액션)은 facts에만 남김 (To-Do 제외)
"""
from __future__ import annotations

import json
from typing import Any

from src.agents.base import BaseAgent


_PROMPT = """당신은 이성우 담당(기업AI고객담당)의 일일 브리핑을 작성하는 비서입니다.

오늘 ({target_date}) 처리된 미팅 노트들을 토픽별로 정리하고, 이성우 담당이 직접 할 To-Do만 분리합니다.

# Wiki 토픽 anchor 목록 (이걸 우선 사용)
{anchors_block}

# 처리된 미팅들
{meetings_block}

{exclude_block}

# 작업
1. **토픽 그룹핑**: 각 미팅을 위 Wiki anchor 중 하나에 매칭. 새 주제면 `new_anchor` 표시.
2. **사실(facts) 추출**: 토픽별 핵심 사실 요약 (누가/무엇/언제/금액/시점 등). 1-3 bullet.
3. **이성우 담당 To-Do 분리**: 다음 조건에 맞는 액션만 todos에 넣음:
   - 주체가 명시적으로 "이성우/담당/제가/내가" 인 액션
   - 주체 명시 없지만 문맥상 영업 담당(이성우) 본인이 할 일
   - **포함**: 이성우가 유관부서/키맨/직원/고객에게 업무를 **요청·지시·조율**한 것 (요청 주체가 이성우이므로 본인 액션 — 액션플랜으로 살림. 예: "이상윤에게 X 자료 요청", "박홍래와 일정 조율")
   - **제외**: 이상윤/박홍래/전우경/안형균/이재선 등 타인이 이성우 요청과 무관하게 **스스로 주체가 되어** 하기로 한 액션, 고객사 단독 액션, 외부 의뢰
4. **내부 정치 토픽 표시 (아주 좁게 적용)**: `is_internal: true`는 **업무 액션이 빠지면 사람 사이의 관계·정치만 남는** 토픽에만 부여합니다.
   - is_internal=true (비공개로 분리할 것): 특정 인물 간 관계 개선·갈등 봉합 필요("A가 B와 관계개선 필요"), 줄서기/라인, 임원·인사 이동 추측, 내부 인물 심리/성향 프로파일링·평가 — 이런 순수 대인관계 정치 내용
   - is_internal=false (공개 — 기본값): 이성우가 유관부서·키맨·직원에게 업무를 요청·지시·조율한 것, 조직을 관리하기 위한 액션, 키맨 대상 영업 액션(제안·미팅·자료 요청 등). 키맨/임원/직원이 등장해도 목적이 **업무 진행**이면 절대 is_internal로 빼지 말고 todos(액션플랜)에 남길 것
   - 헷갈리면 `false`. 확실히 "관계개선/줄서기/인물 평가" 그 자체일 때만 `true`.
5. **top_themes**: 오늘 가장 중요한 주제 3-5개 (Executive Summary용). **`is_internal: true` 토픽은 top_themes에 절대 넣지 않음** — 비공개로 분리됨
6. **new_anchors_suggested**: Wiki anchor 후보 (자동 wiki 노트 생성에 사용)
   - blocks의 `is_new_anchor=true`인 토픽 전부에 대해 1:1로 포함 (블록 토픽명과 동일하게)
   - `category`는 **반드시 다음 중 하나**: `Business/Customers`, `Business/Competitors`, `Business/Internal`, `Business/Industry`, `Business/Deals`, `Leadership`, `Creator`
   - `slug`는 영문 kebab-case 파일명 (확장자 제외, 예: `toss-integrated-idc`, `keyman-kim-byungryun`, `dbo-incheon-dohwa`). 한국어/숫자/공백 금지

JSON만 응답. 다른 텍스트 금지.
{{
  "blocks": [
    {{
      "topic": "토픽명 (anchor 일치하면 anchor 제목과 동일)",
      "anchor_path": "wiki/.../...md 또는 빈 문자열(신규)",
      "is_new_anchor": false,
      "is_internal": false,
      "meetings": ["미팅 제목 1", "미팅 제목 2"],
      "meeting_urls": ["url1", "url2"],
      "facts": ["사실 1", "사실 2"],
      "todos": ["이성우 To-Do 1", "이성우 To-Do 2"]
    }}
  ],
  "top_themes": ["테마 1", "테마 2", "테마 3"],
  "new_anchors_suggested": [
    {{"title": "신규 anchor 제목 (block.topic과 동일)", "slug": "toss-integrated-idc", "category": "Business/Customers", "reason": "왜 필요한지"}}
  ]
}}

규칙:
- 한국어 작성, 존댓말 사용
- 사실은 숫자/시점/금액 우선
- 모호한 수식어(많이/잘/적극) 금지
- 자화자찬(성공적으로/주효한) 금지
- 빈 todos[]도 OK — 모든 토픽이 다 To-Do를 갖는 건 아님
"""


def parse_wiki_anchors(index_md_path: str) -> list[dict]:
    """wiki/index.md를 파싱해 anchor 목록을 추출.

    Returns: [{title, path, summary, category}]
    """
    from pathlib import Path
    import re

    p = Path(index_md_path)
    if not p.exists():
        return []

    anchors: list[dict] = []
    current_section = ""
    current_subsection = ""

    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if line.startswith("## "):
            current_section = line[3:].split(" (")[0].strip()
            current_subsection = ""
        elif line.startswith("### "):
            current_subsection = line[4:].strip()
        else:
            # - [Title](path.md) — summary
            m = re.match(r"\s*-\s*\[([^\]]+)\]\(([^)]+)\)\s*(?:—\s*(.*))?", line)
            if m:
                anchors.append({
                    "title": m.group(1).strip(),
                    "path": m.group(2).strip(),
                    "summary": (m.group(3) or "").strip(),
                    "category": f"{current_section}/{current_subsection}".strip("/"),
                })
    return anchors


class BriefingAgent(BaseAgent):
    name = "briefing"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        meetings: list[dict] = payload["meetings"]
        anchors: list[dict] = payload.get("anchors", [])
        target_date: str = payload["target_date"]
        exclude_persons: list[str] = payload.get("exclude_persons", [])

        anchors_block = "\n".join(
            f"- [{a['title']}]({a['path']}) ({a['category']}): {a['summary']}"
            for a in anchors
        ) or "(없음)"

        meetings_block = "\n\n".join(
            f"## {i+1}. {m.get('title', 'untitled')}\n"
            f"- 시각: {m.get('time', '')}\n"
            f"- 참석자: {', '.join(m.get('participants', []))}\n"
            f"- 요약: {m.get('summary', '')}\n"
            f"- 액션: {' / '.join(m.get('actions', [])) or '(없음)'}\n"
            f"- 프로젝트: {', '.join(m.get('project', [])) if isinstance(m.get('project'), list) else m.get('project', '')}\n"
            f"- 고객: {m.get('customer', '')}\n"
            f"- Notion: {m.get('notion_url', '')}"
            for i, m in enumerate(meetings)
        ) or "(오늘 처리된 미팅 없음)"

        exclude_block = ""
        if exclude_persons:
            names = ", ".join(exclude_persons)
            exclude_block = (
                f"# ⚠️ 제외 대상 인물: {names}\n"
                f"위 인물(들)이 주체이거나 책임진 사실/액션/토픽은 전부 **제외**해주세요. "
                f"이 인물이 언급된 미팅이라도 다른 사람의 내용은 남기되, "
                f"제외 인물 본인의 발언·결정·액션은 절대 facts/todos에 포함하지 마세요. "
                f"제외 인물에 대한 평가/판단도 포함하지 마세요.\n"
            )

        prompt = _PROMPT.format(
            target_date=target_date,
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
            raise ValueError(f"Briefing returned invalid JSON: {e}: {text[:200]}")

        return {
            "blocks": parsed.get("blocks", []),
            "top_themes": parsed.get("top_themes", []),
            "new_anchors_suggested": parsed.get("new_anchors_suggested", []),
            "target_date": target_date,
        }
