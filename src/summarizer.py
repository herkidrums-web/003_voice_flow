"""2-pass Claude CLI pipeline: STT correction → comprehensive meeting analysis."""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from config import NonRetryableError, RetryableError, SummaryError, get_settings

log = logging.getLogger(__name__)


def _call_claude(system: str, user: str, *, use_light_model: bool = False) -> str:
    """Call Claude via CLI. Returns raw text.

    use_light_model=True uses claude_model_light (Sonnet) for cheaper/faster tasks like validation.
    """
    settings = get_settings()
    model = settings.claude_model_light if use_light_model else settings.claude_model

    # Combine system + user into a single prompt for CLI
    prompt = f"{system}\n\n---\n\n{user}"

    # Write prompt to temp file to avoid shell escaping issues with long text
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        result = subprocess.run(
            [settings.claude_cli_path, "-p", "--model", model, "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=settings.claude_api_timeout,
            env={**__import__("os").environ, "LANG": "en_US.UTF-8"},
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout_tail = result.stdout.strip()[-200:] if result.stdout else ""
            combined = f"{stderr} {stdout_tail}".lower()

            # 일일 사용 한도 (자정 리셋) — 재시도 무의미
            if "limit" in combined and ("resets" in combined or "daily" in combined):
                raise NonRetryableError(
                    f"일일 사용 한도 초과 (자정 리셋). STT 캐시 보존됨, 나중에 재실행 가능. stdout: {stdout_tail[:150]}"
                )
            # 분 단위 rate limit — 대기 후 재시도 가능
            if "rate" in combined or "limit" in combined or "429" in combined:
                raise RetryableError(f"Claude CLI rate limit: {stderr}", status_code=429)
            # Treat transient CLI failures as retryable (e.g. network, overloaded)
            raise RetryableError(
                f"Claude CLI failed (exit {result.returncode}): stderr={stderr[:300]} stdout_tail={stdout_tail}",
                status_code=500,
            )

        raw = result.stdout.strip()

    except subprocess.TimeoutExpired as e:
        raise RetryableError(f"Claude CLI timeout after {settings.claude_api_timeout}s") from e
    except (OSError, FileNotFoundError) as e:
        raise NonRetryableError(f"Claude CLI not found: {e}") from e
    finally:
        Path(prompt_file).unlink(missing_ok=True)

    if not raw:
        raise SummaryError("Claude CLI returned empty response")

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    # Strip text preamble before JSON (Opus sometimes adds explanation before JSON)
    if not raw.startswith("{") and "{" in raw:
        json_start = raw.index("{")
        raw = raw[json_start:]
    # Strip trailing text after JSON
    if raw.startswith("{"):
        # Find the last closing brace
        depth = 0
        json_end = 0
        for i, ch in enumerate(raw):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    json_end = i + 1
                    break
        if json_end > 0:
            raw = raw[:json_end]

    return raw


# ─── Pass 1: STT Correction ───

CORRECTION_PROMPT = """\
당신은 한국어 음성인식(STT) 후처리 전문가입니다.
아래 mono 전사본을 읽고, STT 교정 작업을 수행하세요:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【STT 교정】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 동음이의어 오류 (예: "기업" ↔ "기언", "영업" ↔ "연업")
- 고유명사 오류 (회사명, 인명, 지명, 프로젝트명 등)
- 문장 경계가 잘못 끊어진 부분
- 반복된 필러 (어, 음, 그, 아 등)
- 명백한 받아쓰기 오류
- 고유명사가 불확실하면 "(추측)" 붙이세요
- LG U+에는 "본부장", "국장" 직책이 없음. "본부장"→"부사장" 또는 "그룹장", "국장"→"그룹장"으로 교정

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【출력 형식】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 화자를 구분하지 마세요. 발화자 태그를 추가하지 마세요.
- 교정된 전사본만 출력하세요 (다른 텍스트 없이)
- 구어체 특성 유지 (문어체로 바꾸지 말 것)
"""



_CHUNK_CHAR_LIMIT = 15000  # ~15K자 초과 시 청크 분할 (약 40분 분량)


def _split_into_chunks(text: str, limit: int = _CHUNK_CHAR_LIMIT) -> list[str]:
    """긴 전사본을 줄 단위로 청크 분할. 문장 경계를 최대한 유지."""
    if len(text) <= limit:
        return [text]

    chunks = []
    lines = text.split("\n")
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_len + line_len > limit and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def correct_transcript(raw_text: str, dictionary_hints: str = "") -> str:
    """Pass 1: Correct STT errors using Claude.

    긴 전사본(15K자 초과)은 자동으로 청크 분할하여 처리.

    Args:
        raw_text: Raw mono STT transcript
        dictionary_hints: Proper noun dictionary hints
    """
    if not raw_text.strip():
        return raw_text

    hint_section = ""
    if dictionary_hints:
        hint_section = f"\n\n{dictionary_hints}\n\n"

    # 긴 전사본 청크 분할
    chunks = _split_into_chunks(raw_text)
    if len(chunks) > 1:
        log.info(f"Pass 1: 긴 전사본 감지 ({len(raw_text)}자) → {len(chunks)}개 청크로 분할 처리")

    corrected_parts = []
    for i, chunk in enumerate(chunks):
        chunk_label = f" (청크 {i+1}/{len(chunks)})" if len(chunks) > 1 else ""
        log.info(f"Pass 1{chunk_label}: STT 교정 시작...")
        corrected = _call_claude(
            system=CORRECTION_PROMPT,
            user=f"{hint_section}아래 mono 전사본을 교정해주세요:\n\n{chunk}",
        )
        corrected_parts.append(corrected)
        log.info(f"Pass 1{chunk_label} 완료 ({len(chunk)}자 → {len(corrected)}자)")

    result = "\n\n".join(corrected_parts)
    log.info(f"Pass 1 전체 완료 ({len(raw_text)}자 → {len(result)}자)")
    return result


def validate_correction(raw_text: str, corrected_text: str) -> str:
    """Pass 1.5: Disabled — always returns corrected text."""
    return corrected_text


# ─── Pass 2: Comprehensive Analysis ───

# Valid select options for 개인기록_DB
DB_OPTIONS = {
    "type": ["회의", "회식/네트워킹", "전략/의사결정", "아이디어/브레인스토밍", "고객미팅", "개인메모", "조직/인사", "출장"],
    "priority": ["긴급", "중요", "참고"],
    "status": ["정리완료", "후속필요", "대기중", "종결"],
}

ANALYSIS_PROMPT = """\
당신은 LG U+ 기업AI고객담당 이성우의 개인 비서입니다.
회의 전사본을 분석하여 4가지 관점(정무/코칭/영업/리더십)으로 인텔리전스 브리핑을 생성합니다.

녹음자 프로파일:
- 이름: 이성우 (기업AI고객담당)
- 경력: LG U+ Enterprise 영업 15년, 초단기 승진자
- 커리어 경로: 임방현 상무(첫 상사, 현 공공영업담당) → 박성율 전무가 팀장 추천(온라인쇼핑영업팀) → 안형균 그룹장이 2024년 담당 발탁(기업AI고객담당)
- 보고라인: 이성우 → 안형균 그룹장 → 권용현 부사장 → 홍범식 CEO
- 관할팀: 이상윤(기업AI고객1팀/네이버클라우드), 전우경(기업AI고객2팀/토스), 박홍래(기업AI·DBO고객팀/네이버클라우드)
- 주요고객: 토스, 네이버클라우드, 코람코자산운용, 유진투자증권, 브룩필드
- 솔루션: AIDC(파주AI데이터센터), AICC(에이전틱콜봇), 익시(ixi-Gen/ixi-O), 전용회선(비즈온), U+SASE
- 핵심 관계: 박성율 전무(은인/추천자), 안형균 그룹장(현 직속 상사/발탁자), 임방현 상무(첫 멘토)
- 정치적 맥락:
  · 안형균 그룹장 ↔ 박성율 전무: 사이 좋지 않음. 이성우는 박성율이 추천했지만 안형균이 발탁 — 양쪽 관계 관리 필요
  · 정숙경 상무: 이성우와 작년 AIDC 성과 공동 창출 → 우호적 관계
  · 정영훈 상무: 이성우를 본인 수족으로 삼고 싶어함 → 긴장 관계. 주의 필요
  · 박성율 전무 라인: 박헌국 담당(기술지원), 박홍래 팀장(박헌국 과거 부사수)
  · 박헌국 담당: 이성우의 대선배이나 담당 동기(같은 시기 발탁). 원래 좋은 사이였으나 이성우가 작년 성과가 좋아서 미묘한 경쟁 관계
  · 공지훈 팀장: 박성율 전무 직속 사업추진팀장
  · 이정선 팀장: 빅마우스, 모든 사람과 술자리하며 위치 잡는 중. 담당 승진 노림
  · 담당 승진 경쟁 라이벌(팀장급): 이정선, 남규현(김현민 상무 아래), 박홍래, 엄기철(이성우 밑이었으나 올해 김현민 상무로 이동), 양준모(정영훈 상무 아래, 최근 열심히)
  · 김현민 상무: 엄기철·남규현의 상사
  · 고진태 상무: 올해 승진, 컨슈머에서 Enterprise로 내려옴 — 컨슈머 출신 동향 주시
  · 권동기 담당(기획): 박성율 전무와 친함 — 박성율 라인 정보 경로
  · 이상철 팀장(인재육성): 전우경 팀장과 친함 — 내부 네트워크
  · 최종보 담당(SW프로덕트): 권용현 부사장과 잦은 미팅 — 벤치마킹 대상
  · 임장혁 그룹장(고객그룹): 임방현 상무를 신뢰 — 임방현 라인 강화
  · 조계현 팀장(공공1팀): 이성우 동기, 이정선 팀장 라이벌급
  · 허준 담당(금융고객): 정영훈 상무가 키웠으나 농협 실주로 사이 악화. 주엄개 상무도 금융 영업 출신
  · 김현민 상무(수도권고객): 사업에서 밀려나 영업으로 옴, 막무가내 스타일
  · 김수경 상무(서부고객): 기업AI고객담당 출신 — 이성우 현 직책의 전임자급

입력: 교정된 회의 전사본 (한국어)
출력: JSON 형식의 인텔리전스 브리핑

JSON 스키마:
{
  "properties": {
    "title": "YYYYMMDD_내부또는외부_유관부서또는고객명_구체적주제 형식. 예: 20260327_내부_SDR팀_신규고객리드발굴개선, 20260329_외부_넷마블·아크_골프후회식실버타운사업논의, 20260320_외부_토스_데이터센터이전논의",
    "type": "회의/회식·네트워킹/전략·의사결정/아이디어·브레인스토밍/고객미팅/개인메모/조직·인사/출장 중 택1",
    "project": "관련 프로젝트명 (없으면 빈 문자열)",
    "client": "고객명 (없으면 빈 문자열)",
    "related_people": ["언급된 인물명"],
    "tags": ["핵심 키워드 태그 (3-5개)"],
    "priority": "긴급/중요/참고 중 택1",
    "status": "후속필요/정리완료 중 택1",
    "summary": "1-2문장 핵심 요약 (100자 이내)",
    "next_actions": "번호 매긴 후속 액션 목록",
    "participants": "참석자 이름 나열"
  },
  "content": {
    "core_summary": "5-10문장 핵심 요약. 회의 전체 흐름과 주요 결론.",
    "meeting_overview": {
      "date": "YYYY-MM-DD 또는 빈 문자열",
      "participants": ["참석자 목록"],
      "type": "미팅 성격 (예: 그룹장 보고, 고객 미팅, 팀 회의)",
      "location": "장소 (언급시)"
    },
    "discussions": [
      {
        "topic": "대주제",
        "sub_topics": [
          {
            "title": "세부 주제",
            "points": ["【원문 근거】 구체적 내용. 반드시 전사본의 실제 발화를 인용하여 기술."],
            "quotes": ["화자: '직접 인용문' — 전사본에 실제로 있는 발언만"]
          }
        ]
      }
    ],
    "key_persons": [
      {
        "name": "인물명",
        "role": "직책/소속",
        "observations": ["【원문 근거】 대화에서 드러난 성향/태도/관심사"]
      }
    ],
    "risks": ["리스크 또는 불확실성 요소"],

    "lens_political": {
      "power_dynamics": ["조직 내 파워 이동, 라인 변화, 누가 부상/하락 중인지"],
      "hidden_signals": ["표면적 발언 뒤의 실제 의미. 예: '착하다' = 전투력 부족 평가"],
      "danger_alerts": ["나(이성우)에게 불리할 수 있는 흐름, 즉시 대응 필요 사항"],
      "relationship_moves": ["관계 구축/유지를 위해 취해야 할 행동. 예: '권용현 부사장 아젠다 미팅 필요'"]
    },

    "lens_coaching": {
      "my_patterns": ["내 발언/행동에서 관찰되는 패턴 (좋은 것과 개선할 것 모두)"],
      "growth_points": ["이 상황에서 배울 수 있는 교훈"],
      "emotional_notes": ["감정 상태, 스트레스 신호, 동기부여 수준 관찰"]
    },

    "lens_sales": {
      "customer_intel": ["고객 관련 새로 알게 된 정보, 키맨 업데이트"],
      "talking_points": ["다음 고객 미팅에서 활용할 수 있는 대화 소재"],
      "competitive_moves": ["경쟁사 동향, 시장 변화 시그널"],
      "opportunities": ["새로운 사업 기회, 크로스셀/업셀 가능성"]
    },

    "lens_leadership": {
      "team_signals": ["팀원 상태, 사기, 성과 관련 관찰"],
      "decision_cases": ["의사결정 사례와 그 결과/교훈"],
      "motivation_points": ["팀 동기부여에 활용할 포인트"],
      "delegation_notes": ["위임/지시가 필요한 사항"]
    },

    "action_items": [
      {"assignee": "담당자", "task": "실행 항목", "deadline": "기한", "urgency": "즉시/이번주/중장기"}
    ]
  }
}

규칙:
1. 【원문 앵커링 필수】 discussions의 모든 points와 key_persons의 observations는 반드시 전사본에 실제로 있는 발화를 근거로 작성하세요. 전사본에 없는 대화, 발언, 사실을 절대 추가하지 마세요.
2. quotes에는 전사본에 실제로 있는 핵심 발언만 직접 인용하세요.
3. 4대 렌즈(political, coaching, sales, leadership)는 대화 내용에 해당 요소가 있을 때만 작성하세요. 없으면 빈 배열로 두세요. 억지로 채우지 마세요.
4. 고유명사가 확실하지 않으면 "(추측)"을 붙이세요.
5. 한국어로 작성하세요.
6. 반드시 유효한 JSON만 출력하세요 (다른 텍스트 없이).
7. lens_political.hidden_signals는 정무적 감각이 핵심입니다 — 표면적 의미가 아닌 조직 정치적 함의를 해석하세요.
8. lens_sales.talking_points는 다음 고객 미팅에서 자연스럽게 꺼낼 수 있는 구체적 대화 소재로 작성하세요.
9. lens_coaching는 코칭 관점에서 추후 멘토링/자기 성찰에 활용할 수 있도록 작성하세요.
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
    knowledge_types: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)


@dataclass
class MeetingContent:
    """Rich page content sections."""
    executive_summary: list[dict] | str = field(default_factory=list)
    meeting_overview: dict = field(default_factory=dict)
    discussions: list[dict] = field(default_factory=list)
    decision_structure: list[dict] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    intelligence: dict = field(default_factory=dict)
    implications: list[dict] = field(default_factory=list)
    key_persons: list[dict] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)


@dataclass
class MeetingAnalysis:
    """Combined result of Pass 2."""
    properties: MeetingProperties
    content: MeetingContent


# Keep for backward compatibility with existing pipeline
MeetingSummary = MeetingAnalysis


# ─── Pass 2a: Fact Extraction ───

FACT_EXTRACTION_PROMPT = """\
당신은 회의 전사본에서 사실만 추출하는 전문가입니다.
추론, 해석, 의견을 추가하지 마세요. 전사본에 있는 내용만 구조화하세요.

※ 절대 규칙 — 화자(참석자) vs 제3자 구분:
- "참석자(participants)" = 이 녹음에서 직접 목소리가 들리는 사람만. [SPEAKER_XX] 또는 [화자X]로 표기된 사람.
- "관련인물(related_people)" = 대화에서 이름이 거론되었지만 직접 말하지 않은 사람.
- 예: 이성우와 윤상명이 "안형균 그룹장이 어제 이렇게 말했대"라고 대화하면 → 참석자: 이성우, 윤상명 / 관련인물: 안형균
- 이 구분을 틀리면 전체 분석이 무의미해집니다. 반드시 지키세요.

출력: JSON (다른 텍스트 없이)

※ 세션 분리 규칙:
하나의 녹음 파일에 참석자가 바뀌는 별도 회의가 포함될 수 있습니다.
- 참석자 구성이 바뀌면 (사람이 나가고 새 사람이 들어오면) → 별도 session
- 같은 참석자라도 주제가 완전히 바뀌고 시간 갭이 있으면 → 별도 session
- sessions 배열에 각 회의를 별도 JSON으로 분리하세요.
- 회의가 1개뿐이면 sessions 배열에 1개만 넣으세요.

JSON 스키마:
{
  "sessions": [
    {
      "properties": {
        "title": "YYYYMMDD_내부또는외부_유관부서또는고객명_구체적주제. 예: 20260327_내부_SDR팀_신규고객리드발굴개선, 20260323_내부_후배들_조직정치인사동향, 20260329_외부_넷마블·아크_골프후회식실버타운사업논의",
        "type": "회의/회식·네트워킹/전략·의사결정/아이디어·브레인스토밍/고객미팅/개인메모/조직·인사/출장 중 택1",
        "project": "관련 프로젝트명 (없으면 빈 문자열)",
        "client": "고객명 (없으면 빈 문자열)",
        "related_people": ["대화에서 거론된 제3자 인물 (실제 화자가 아닌 사람). 최대한 많이 추출"],
        "tags": ["핵심 키워드 태그 (3-5개)"],
        "priority": "긴급/중요/참고 중 택1",
        "status": "후속필요/정리완료 중 택1",
        "summary": "1-2문장 핵심 요약 (100자 이내)",
        "next_actions": "전사본에서 언급된 후속 액션만",
        "participants": "※중요: 실제로 이 자리에서 직접 말한 사람(화자)만 기재. 대화에서 거론만 된 제3자는 절대 포함하지 마세요."
      },
      "facts": [
        {
          "speaker": "화자명 (불명이면 '불명')",
          "content": "전사본에서 확인된 사실. 반드시 원문 기반.",
          "original_quote": "전사본 원문 직접 인용",
          "topic": "이 사실이 속하는 대주제",
          "entities": ["이 사실에서 언급된 주요 엔티티 (인물명, 조직명, 프로젝트명, 기술명 등). 최대한 많이 추출"],
          "knowledge_type": "customer_intel/deal_progress/competitor_move/internal_info/industry_trend/leadership_insight/coaching_point/deal_lesson 중 택1"
        }
      ],
      "key_persons_observed": [
        {
          "name": "인물명",
          "role": "직책/소속 (전사본에서 언급된 경우만)",
          "observed_behavior": "전사본에서 직접 관찰된 행동/태도만"
        }
      ],
      "meeting_overview": {
        "date": "YYYY-MM-DD",
        "participants": ["실제 화자만"],
        "type": "미팅 성격",
        "location": "장소 (언급시)"
      }
    }
  ]
}

규칙:
1. 전사본에 없는 내용을 절대 추가하지 마세요.
2. facts의 content는 반드시 전사본 원문에 근거해야 합니다.
3. original_quote는 전사본에서 직접 복사한 원문이어야 합니다.
4. facts는 같은 주제/맥락끼리 묶되, 각 맥락 내에서는 대화 순서를 유지하세요.
5. 고유명사가 불확실하면 "(추측)"을 붙이세요.
6. 한국어로 작성하세요.
7. 반드시 유효한 JSON만 출력하세요.
8. LG U+에는 "본부장", "국장" 직책이 없음. "본부장"→부사장/그룹장, "국장"→그룹장.
"""


# ─── Pass 2b: 4-Lens Analysis ───

LENS_ANALYSIS_PROMPT = """\
당신은 LG U+ 기업AI고객담당 이성우의 수석 비서이자 전략 분석가입니다.
C-level 임원이 읽어도 바로 의사결정에 활용할 수 있는 수준의 인텔리전스 브리핑을 작성합니다.

""" + ANALYSIS_PROMPT.split("녹음자 프로파일:")[1].split("JSON 스키마:")[0] + """\

입력: 사실 추출 JSON
출력: 인텔리전스 브리핑 JSON (다른 텍스트 없이)

작성 원칙:
- 단순 사실 나열이 아닌, 맥락→의미→시사점→행동지침이 연결되는 서사 구조로 작성
- 제3자가 읽어도 회의에 참석한 것처럼 맥락을 완전히 이해할 수 있어야 함
- 각 논의 주제는 배경→핵심 내용→의미/영향까지 깊이 있게 분석
- 시사점은 "그래서 어쩌라고?"에 답할 수 있는 구체적 행동지침 수준으로 작성

JSON 스키마:
{
  "executive_summary": [
    {
      "topic": "주제명 (짧고 명확하게)",
      "points": ["핵심 포인트 1 (1줄, 제3자가 읽어도 이해 가능한 수준)", "핵심 포인트 2", "핵심 포인트 3 (2~3개)"]
    }
  ],

  "discussions": [
    {
      "topic": "주제 N: 구체적 주제명",
      "analysis": "이 주제에 대한 심층 분석. 단순 발언 나열이 아니라 배경→핵심 내용→맥락→의미를 연결하는 서술형 문단. 최소 3문장. 왜 이 논의가 중요한지, 어떤 맥락에서 나온 것인지, 향후 어떤 영향을 미칠 수 있는지까지 포함.",
      "key_quotes": ["화자: '핵심 직접 인용'"]
    }
  ],

  "decision_structure": [
    {
      "category": "의사결정 영역 (예: DC 신규 개발 투자)",
      "stakeholder": "주체",
      "influence": "높음/중간/낮음",
      "criteria": "판단 기준",
      "comment": "맥락 설명"
    }
  ],

  "decisions": ["이 회의에서 합의/결정된 사항만. 아직 결정 안 된 것은 제외."],

  "risks": ["핵심 리스크. 단순 나열이 아닌 왜 리스크인지 맥락 포함. 1-2문장씩."],

  "intelligence": {
    "market": ["시장/산업 동향 인텔리전스"],
    "competitive": ["경쟁사/경쟁 구도 인텔리전스"],
    "internal": ["조직 내부 역학/인사 인텔리전스"],
    "relationship": ["인물 관계/키맨 인텔리전스"]
  },

  "implications": [
    {
      "title": "시사점 제목 (1줄)",
      "detail": "구체적 분석과 행동지침. 왜 이것이 중요한지, 어떻게 활용해야 하는지, 구체적으로 무엇을 해야 하는지까지 포함. 최소 3문장.",
      "actions": ["이 시사점에서 도출되는 구체적 행동 항목"],
      "axis": ["이 시사점이 해당하는 축. business/leadership/creator 중 1개 이상 선택"],
      "reusable_as": ["이 시사점의 재활용 유형. deal_lesson/customer_pattern/leadership_insight/coaching_point/content_material 중 해당하는 것 모두"]
    }
  ],

  "key_persons": [
    {
      "name": "인물명",
      "role": "직책/소속",
      "observed_behavior": "이 회의에서 관찰된 행동/태도/발언의 의미"
    }
  ],

  "action_items": [
    {"assignee": "담당자", "task": "실행 항목", "deadline": "기한", "urgency": "즉시/이번주/중장기"}
  ]
}

규칙:
1. executive_summary는 주제별로 분리된 배열. 각 주제마다 topic(소제목)과 points(불릿 2~3개)로 구성. 각 포인트는 제3자가 읽어도 무슨 내용인지 파악 가능한 수준으로 작성. 마지막 주제로 '승부 포인트' 또는 '총평'을 넣을 것.
2. discussions의 analysis는 깊이 있는 분석. "~라고 했다" 수준이 아니라 "~한 배경에서 ~가 ~를 제안했으며, 이는 ~한 의미를 갖는다" 수준.
3. decision_structure는 이 회의와 관련된 의사결정 구조를 테이블로 정리. 누가 어떤 기준으로 결정하는지.
4. decisions와 action_items는 분리. decisions는 이미 합의된 것, action_items는 앞으로 해야 할 것.
5. intelligence는 4대 렌즈를 통합한 인텔리전스 섹션. 나중에 독립적으로 참고할 수 있도록 정리.
6. implications는 가장 중요한 섹션. 각 시사점에 title(1줄) + detail(3문장+) + actions(구체적 행동)를 포함. "그래서 어쩌라고?"에 답할 수 있어야 함.
7. facts에 없는 내용을 절대 추가하지 마세요.
8. 반드시 유효한 JSON만 출력하세요.
"""


def _parse_json_response(raw: str, context: str) -> dict:
    """Parse JSON from Claude response with error handling."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SummaryError(
            f"Claude returned invalid JSON ({context}): {e}\nRaw: {raw[:200]}"
        ) from e


def extract_facts(corrected_text: str, duration: float, recording_date: str = "") -> dict:
    """Pass 2a: Extract facts only from transcript. No interpretation."""
    if not corrected_text.strip():
        raise SummaryError("Empty transcript — nothing to extract")

    date_hint = f"\n녹음 날짜: {recording_date}" if recording_date else ""
    user_prompt = (
        f"아래 회의 전사본에서 사실만 추출하여 JSON으로 구조화해주세요.\n"
        f"회의 길이: {duration / 60:.1f}분{date_hint}\n\n"
        f"전사본:\n{corrected_text}"
    )

    log.info("Pass 2a: 사실 추출 시작...")
    raw = _call_claude(system=FACT_EXTRACTION_PROMPT, user=user_prompt)
    log.info(f"Pass 2a 응답 수신 ({len(raw)}자)")

    return _parse_json_response(raw, "Pass 2a")


def analyze_facts(facts_data: dict) -> dict:
    """Pass 2b: Analyze extracted facts through 4 lenses. Input is facts JSON only."""
    user_prompt = (
        f"아래 사실 데이터를 4대 렌즈로 분석하여 JSON으로 생성해주세요.\n\n"
        f"사실 데이터:\n{json.dumps(facts_data, ensure_ascii=False, indent=2)}"
    )

    log.info("Pass 2b: 4대 렌즈 분석 시작...")
    raw = _call_claude(system=LENS_ANALYSIS_PROMPT, user=user_prompt)
    log.info(f"Pass 2b 응답 수신 ({len(raw)}자)")

    return _parse_json_response(raw, "Pass 2b")


def verify_analysis(facts_data: dict, analysis_data: dict) -> dict:
    """Pass 2c: Verify analysis against facts. Remove ungrounded claims."""
    # Collect all fact content for keyword matching
    fact_contents = [f.get("content", "") for f in facts_data.get("facts", [])]
    fact_quotes = [f.get("original_quote", "") for f in facts_data.get("facts", [])]
    all_fact_text = " ".join(fact_contents + fact_quotes)

    verified = dict(analysis_data)
    removed_count = 0

    # Check discussions points for grounding via keyword overlap
    for disc in verified.get("discussions", []):
        for sub in disc.get("sub_topics", []):
            grounded_points = []
            for point in sub.get("points", []):
                # Check if key nouns in the point appear in facts
                # Short points or points with Korean content overlap are likely grounded
                korean_words = [w for w in point.split() if any("\uAC00" <= c <= "\uD7A3" for c in w)]
                overlap = sum(1 for w in korean_words if w in all_fact_text)
                if len(korean_words) == 0 or overlap >= len(korean_words) * 0.3:
                    grounded_points.append(point)
                else:
                    grounded_points.append(f"[미검증] {point}")
                    removed_count += 1
            sub["points"] = grounded_points

    if removed_count > 0:
        log.warning(f"Pass 2c: {removed_count}개 항목에 [미검증] 태그 부착")
    else:
        log.info("Pass 2c: 검증 완료 — 모든 항목 근거 확인됨")

    return verified


def _build_analysis(session_facts: dict, session_verified: dict, recording_date: str) -> MeetingAnalysis:
    """Build a MeetingAnalysis from a single session's facts + verified analysis."""
    props = session_facts.get("properties", {})

    raw_title = props.get("title", "")
    if not raw_title or raw_title in ("미팅노트", "회의록"):
        date_prefix = recording_date.replace("-", "") if recording_date else ""
        raw_title = f"{date_prefix}_음성메모" if date_prefix else "음성메모"

    # Second Brain: facts에서 entities와 knowledge_types 수집
    all_entities: list[str] = []
    all_knowledge_types: list[str] = []
    for fact in session_facts.get("facts", []):
        if isinstance(fact, dict):
            all_entities.extend(fact.get("entities", []))
            kt = fact.get("knowledge_type", "")
            if kt:
                all_knowledge_types.append(kt)
    unique_entities = list(dict.fromkeys(all_entities))[:20]
    unique_knowledge_types = list(dict.fromkeys(all_knowledge_types))

    return MeetingAnalysis(
        properties=MeetingProperties(
            title=raw_title,
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
            date=session_facts.get("meeting_overview", {}).get("date", recording_date),
            knowledge_types=unique_knowledge_types,
            entities=unique_entities,
        ),
        content=MeetingContent(
            executive_summary=session_verified.get("executive_summary", []),
            meeting_overview=session_facts.get("meeting_overview", {}),
            discussions=session_verified.get("discussions", []),
            decision_structure=session_verified.get("decision_structure", []),
            decisions=session_verified.get("decisions", []),
            risks=session_verified.get("risks", []),
            intelligence=session_verified.get("intelligence", {}),
            implications=session_verified.get("implications", []),
            key_persons=session_verified.get("key_persons", session_facts.get("key_persons_observed", [])),
            action_items=session_verified.get("action_items", []),
        ),
    )


def analyze_transcript(corrected_text: str, duration: float, recording_date: str = "") -> list[MeetingAnalysis]:
    """Pass 2 (3-stage): fact extraction → lens analysis → verification.

    Returns a list of MeetingAnalysis (multiple if sessions detected in one recording).

    Raises:
        SummaryError: on persistent/parse failure
        RetryableError: on transient API errors
        NonRetryableError: on auth errors
    """
    # Pass 2a: Extract facts (may contain sessions array)
    facts_data = extract_facts(corrected_text, duration, recording_date)

    # Check if multi-session format
    sessions = facts_data.get("sessions", [])
    if sessions:
        log.info(f"다중 세션 감지: {len(sessions)}개 회의를 별도 페이지로 생성합니다.")
        results = []
        for i, session_facts in enumerate(sessions):
            log.info(f"  세션 {i+1}/{len(sessions)}: {session_facts.get('properties', {}).get('title', '?')}")
            # Pass 2b per session
            session_analysis = analyze_facts(session_facts)
            # Pass 2c per session
            session_verified = verify_analysis(session_facts, session_analysis)
            results.append(_build_analysis(session_facts, session_verified, recording_date))
        return results

    # Single session (legacy or single-meeting recording)
    # Pass 2b: 4-lens analysis based on facts only
    analysis_data = analyze_facts(facts_data)

    # Pass 2c: Verify analysis against facts
    verified = verify_analysis(facts_data, analysis_data)

    return [_build_analysis(facts_data, verified, recording_date)]


def summarize_transcript(full_text: str, duration: float, recording_date: str = "",
                         dictionary_hints: str = "") -> list[MeetingAnalysis]:
    """Full pipeline: correct → validate → extract facts → analyze → verify.

    This is the main entry point called by pipeline.py.
    Returns a list of MeetingAnalysis (1 or more if multi-session detected).
    """
    # Pass 1: STT correction with proper noun hints
    corrected = correct_transcript(full_text, dictionary_hints)

    # Pass 1.5: Disabled (always passes through)
    validated = validate_correction(full_text, corrected)

    # Pass 2 (3-stage): fact extraction → lens analysis → verification
    return analyze_transcript(validated, duration, recording_date)
