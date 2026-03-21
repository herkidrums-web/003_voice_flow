"""2-pass Claude API pipeline: STT correction → comprehensive meeting analysis."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import anthropic

from config import NonRetryableError, RetryableError, SummaryError, get_settings

log = logging.getLogger(__name__)

# Module-level client cache
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        settings = get_settings()
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _call_claude(system: str, user: str) -> str:
    """Common Claude API call with error mapping. Returns raw text."""
    settings = get_settings()
    client = _get_client()

    try:
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError as e:
        raise RetryableError(f"Claude rate limit: {e}", status_code=429) from e
    except anthropic.InternalServerError as e:
        raise RetryableError(f"Claude server error: {e}", status_code=500) from e
    except anthropic.AuthenticationError as e:
        raise NonRetryableError(f"Claude auth error: {e}", status_code=401) from e
    except anthropic.APIError as e:
        raise SummaryError(f"Claude API error: {e}") from e

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    return raw


# ─── Pass 1: STT Correction ───

CORRECTION_PROMPT = """\
당신은 한국어 음성인식(STT) 후처리 전문가입니다.
아래 전사본에서 오류를 교정하세요.

교정 대상:
- 동음이의어 오류 (예: "기업" ↔ "기언", "영업" ↔ "연업")
- 고유명사 오류 (회사명, 인명, 지명, 프로젝트명 등)
- 문장 경계가 잘못 끊어진 부분
- 반복된 필러 (어, 음, 그, 아 등)
- 명백한 받아쓰기 오류

규칙:
- 원본 의미를 훼손하지 마세요
- 교정된 전사본만 출력하세요 (다른 텍스트 없이)
- 구어체 특성은 유지하세요 (완전한 문어체로 바꾸지 말 것)
- 줄바꿈 구조를 유지하세요
"""


def correct_transcript(raw_text: str) -> str:
    """Pass 1: Correct STT errors using Claude."""
    if not raw_text.strip():
        return raw_text

    log.info("Pass 1: STT 교정 시작...")
    corrected = _call_claude(
        system=CORRECTION_PROMPT,
        user=f"아래 전사본을 교정해주세요:\n\n{raw_text}",
    )
    log.info(f"Pass 1 완료 ({len(raw_text)}자 → {len(corrected)}자)")
    return corrected


# ─── Pass 2: Comprehensive Analysis ───

# Valid select options for 개인기록_DB
DB_OPTIONS = {
    "type": ["회의", "회식/네트워킹", "전략/의사결정", "아이디어/브레인스토밍", "고객미팅", "개인메모", "조직/인사", "출장"],
    "priority": ["긴급", "중요", "참고"],
    "status": ["정리완료", "후속필요", "대기중", "종결"],
}

ANALYSIS_PROMPT = """\
당신은 회의 전사본을 분석하여 상세한 미팅노트를 생성하는 전문가입니다.
제3자가 읽어도 회의에 참석한 것처럼 맥락을 이해할 수 있도록 작성하세요.

입력: 교정된 회의 전사본 (한국어)
출력: JSON 형식의 종합 미팅노트

JSON 스키마:
{
  "properties": {
    "title": "YYYYMMDD_조직_주제 형식의 제목 (예: 20260319_내부_업무보고체계논의)",
    "type": "회의 유형 (회의/회식·네트워킹/전략·의사결정/아이디어·브레인스토밍/고객미팅/개인메모/조직·인사/출장 중 택1)",
    "project": "관련 프로젝트명 (없으면 빈 문자열)",
    "client": "고객명 (없으면 빈 문자열)",
    "related_people": ["언급된 인물명"],
    "tags": ["핵심 키워드 태그 (3-5개)"],
    "priority": "긴급/중요/참고 중 택1",
    "status": "후속필요/정리완료 중 택1",
    "summary": "1-2문장 핵심 요약 (DB 프로퍼티용, 100자 이내)",
    "next_actions": "번호 매긴 후속 액션 목록 (예: 1. 보고서 작성\\n2. 미팅 일정 확인)",
    "participants": "참석자 이름 나열 (이름 모르면 참석자A, 참석자B)"
  },
  "content": {
    "core_summary": "5-10문장 핵심 요약. 회의 전체 흐름과 주요 결론 포함.",
    "meeting_overview": {
      "date": "추정 날짜 (YYYY-MM-DD) 또는 빈 문자열",
      "participants": ["참석자 목록"],
      "type": "미팅 성격 설명 (예: 1:1 식사 미팅, 팀 회의 등)",
      "location": "장소 (언급시, 아니면 빈 문자열)"
    },
    "discussions": [
      {
        "topic": "대주제",
        "sub_topics": [
          {
            "title": "세부 주제",
            "points": [
              "구체적인 내용 포인트. 구어체 인용 포함 (예: A가 '~'라고 했다)."
            ],
            "quotes": [
              "중요 발언 직접 인용 (예: 김대표: '우리가 선제적으로 움직여야 한다')"
            ]
          }
        ]
      }
    ],
    "key_persons": [
      {
        "name": "인물명",
        "observations": ["성향/태도/관심사 관찰 포인트"]
      }
    ],
    "risks": ["리스크 또는 불확실성 요소"],
    "strategic_insights": ["전략적 인사이트 또는 조언 (LLM 분석 관점)"],
    "action_items": [
      {"assignee": "담당자", "task": "실행 항목", "deadline": "기한 (언급시)"}
    ]
  }
}

규칙:
1. discussions를 최대한 많이, 상세하게 작성하세요. 대화 흐름을 빠짐없이 담으세요.
2. 실제 발화를 구어체 인용 형태로 포함하세요: "A가 '우리 이거 빨리 해야 돼'라고 하자, B가 '일정이 빠듯한데'라고 답했다."
3. quotes에는 핵심적인 직접 인용만 별도로 담으세요.
4. key_persons에는 대화에서 드러난 인물의 성향, 관심사, 의사결정 스타일 등을 기록하세요.
5. strategic_insights에는 대화 내용을 기반으로 한 전략적 분석과 조언을 제공하세요:
   - 놓친 관점이나 추가 고려사항
   - 의사결정에 도움이 될 분석
   - 잠재적 기회나 위험 요소
6. 전사본에 없는 사실을 추가하지 마세요 (인사이트/분석은 예외)
7. action_items가 없으면 빈 배열
8. 한국어로 작성하세요
9. 반드시 유효한 JSON만 출력하세요 (다른 텍스트 없이)
"""


@dataclass
class MeetingProperties:
    """DB properties for 개인기록_DB."""
    title: str
    meeting_type: str = "회의"
    project: str = ""
    client: str = ""
    related_people: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    priority: str = "참고"
    status: str = "정리완료"
    summary: str = ""
    next_actions: str = ""
    participants: str = ""
    date: str = ""


@dataclass
class MeetingContent:
    """Rich page content sections."""
    core_summary: str
    meeting_overview: dict
    discussions: list[dict]
    key_persons: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    strategic_insights: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)


@dataclass
class MeetingAnalysis:
    """Combined result of Pass 2."""
    properties: MeetingProperties
    content: MeetingContent


# Keep for backward compatibility with existing pipeline
MeetingSummary = MeetingAnalysis


def analyze_transcript(corrected_text: str, duration: float, recording_date: str = "") -> MeetingAnalysis:
    """Pass 2: Comprehensive meeting analysis using Claude API.

    Raises:
        SummaryError: on persistent/parse failure
        RetryableError: on transient API errors
        NonRetryableError: on auth errors
    """
    if not corrected_text.strip():
        raise SummaryError("Empty transcript — nothing to analyze")

    date_hint = f"\n녹음 날짜: {recording_date}" if recording_date else ""
    user_prompt = (
        f"아래 회의 전사본을 분석하여 종합 미팅노트를 JSON으로 생성해주세요.\n"
        f"회의 길이: {duration / 60:.1f}분{date_hint}\n\n"
        f"전사본:\n{corrected_text}"
    )

    log.info("Pass 2: 종합 분석 시작...")
    raw = _call_claude(system=ANALYSIS_PROMPT, user=user_prompt)
    log.info(f"Pass 2 응답 수신 ({len(raw)}자)")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SummaryError(
            f"Claude returned invalid JSON: {e}\nRaw: {raw[:200]}"
        ) from e

    props = data.get("properties", {})
    content = data.get("content", {})

    return MeetingAnalysis(
        properties=MeetingProperties(
            title=props.get("title", "미팅노트"),
            meeting_type=props.get("type", "회의"),
            project=props.get("project", ""),
            client=props.get("client", ""),
            related_people=props.get("related_people", []),
            tags=props.get("tags", []),
            priority=props.get("priority", "참고"),
            status=props.get("status", "정리완료"),
            summary=props.get("summary", ""),
            next_actions=props.get("next_actions", ""),
            participants=props.get("participants", ""),
            date=content.get("meeting_overview", {}).get("date", recording_date),
        ),
        content=MeetingContent(
            core_summary=content.get("core_summary", ""),
            meeting_overview=content.get("meeting_overview", {}),
            discussions=content.get("discussions", []),
            key_persons=content.get("key_persons", []),
            risks=content.get("risks", []),
            strategic_insights=content.get("strategic_insights", []),
            action_items=content.get("action_items", []),
        ),
    )


def summarize_transcript(full_text: str, duration: float, recording_date: str = "") -> MeetingAnalysis:
    """Full 2-pass pipeline: correct → analyze.

    This is the main entry point called by pipeline.py.
    """
    # Pass 1: STT correction
    corrected = correct_transcript(full_text)

    # Pass 2: Comprehensive analysis
    return analyze_transcript(corrected, duration, recording_date)
