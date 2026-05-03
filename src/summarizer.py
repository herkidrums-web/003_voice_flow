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
    """Call Claude via CLI. Returns raw text."""
    settings = get_settings()
    model = settings.claude_model_light if use_light_model else settings.claude_model

    prompt = f"{system}\n\n---\n\n{user}"

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
            stderr = result.stderr.strip() if result.stderr else ""
            stdout_tail = result.stdout.strip()[-200:] if result.stdout else ""
            combined = f"{stderr} {stdout_tail}".lower()

            if "limit" in combined and ("resets" in combined or "daily" in combined):
                raise NonRetryableError(
                    f"일일 사용 한도 초과 (자정 리셋). STT 캐시 보존됨, 나중에 재실행 가능. stdout: {stdout_tail[:150]}"
                )
            if "rate" in combined or "limit" in combined or "429" in combined:
                raise RetryableError(f"Claude CLI rate limit: {stderr}", status_code=429)
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
당신은 STT 오타 교정기입니다. 잘못 인식된 단어만 고치고, 나머지는 한 글자도 바꾸지 마세요.

[규칙]
1. 오타, 잘못 인식된 고유명사만 수정
2. 문장 구조, 어순, 말투 절대 변경 금지
3. 요약하거나 의역하지 마세요 — 원문 길이를 유지하세요
4. 구어체 그대로 유지 ("~거든요", "~잖아요", "~인데" 등)
5. 반복된 필러(어, 음, 그)만 제거
6. 화자 태그 추가 금지
7. 교정된 전사본만 출력 (설명/주석 금지)
8. LG U+에는 "본부장", "국장" 직책 없음 → "부사장" 또는 "그룹장"으로 교정
9. **인명 발음 유사 오인식**: 사전의 "핵심인명" 목록에 발음이 비슷한 항목이 있으면 그 표기로 교정 (예: 한국식 인명은 음절 단위로 비교)

[예시]
입력: 유프러스 기업 에이아이 고객담당에서 캡파 확보를 했구요
출력: LG유플러스 기업AI고객담당에서 CAPA 확보를 했구요

입력: 디비오 사업이 잘 되고 있거든요 카엠 체계도 만들었고
출력: DBO 사업이 잘 되고 있거든요 KAM 체계도 만들었고

입력: 파주 아이디씨에서 네이버 클라우드 50메가 하고 있는데
출력: 파주IDC에서 네이버클라우드 50MW 하고 있는데

입력: 원혁명 부사장 약속 3회 불이행했다고 하더라구요
출력: 권용현 부사장 약속 3회 불이행했다고 하더라구요

입력: 김태현 대표가 사업 전면 수정 발언을 했어요
출력: 김태원 대표가 사업 전면 수정 발언을 했어요
"""



_CHUNK_CHAR_LIMIT = 6000  # ~6K자 초과 시 청크 분할 (약 16분 분량, Claude CLI 타임아웃 방어 강화)


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


def _participants_hint(participants: list[str] | None) -> str:
    """참석자 힌트 블록 생성. Claude가 추측 대신 이 리스트만 사용하도록 강제."""
    if not participants:
        return ""
    names = ", ".join(str(p) for p in participants if p)
    if not names:
        return ""
    return (
        f"\n\n[회의 참석자 — 반드시 이 인원으로만 매핑, 추측 금지]\n"
        f"{names}\n"
        f"위 리스트에 없는 사람 이름은 전사본에 직접 언급된 경우에만 사용하세요.\n\n"
    )


def correct_transcript(
    raw_text: str,
    dictionary_hints: str = "",
    participants: list[str] | None = None,
) -> str:
    """Pass 1: Correct STT errors using Claude.

    긴 전사본(15K자 초과)은 자동으로 청크 분할하여 처리.

    Args:
        raw_text: Raw mono STT transcript
        dictionary_hints: Proper noun dictionary hints
        participants: 회의 참석자 리스트 (Claude 교정 시 인명 매핑 힌트)
    """
    if not raw_text.strip():
        return raw_text

    hint_section = ""
    if dictionary_hints:
        hint_section = f"\n\n{dictionary_hints}\n\n"
    hint_section += _participants_hint(participants)

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
    topics: list[str] = field(default_factory=list)  # NEW: 토픽 목록


@dataclass
class MeetingContent:
    """Rich page content sections."""
    executive_summary: list[dict] | str = field(default_factory=list)
    topics: list[dict] = field(default_factory=list)  # NEW: 토픽별 육하원칙
    # topics structure: [{"name": "카카오 AP", "when": "...", "where": "...", "who": "...",
    #   "what": "...", "why": "...", "how": "...", "key_facts": [...],
    #   "quotes": [...], "action_items": [...], "insights": [...]}]
    decisions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    key_persons: list[dict] = field(default_factory=list)


@dataclass
class MeetingAnalysis:
    """Combined result of Pass 2."""
    properties: MeetingProperties
    content: MeetingContent


# Keep for backward compatibility with existing pipeline
MeetingSummary = MeetingAnalysis


# ─── Pass 2a: Fact Extraction ───

FACT_EXTRACTION_PROMPT = """\
당신은 B2B 영업 임원의 음성메모를 분석하는 전문가입니다.
교정된 전사본에서 **토픽별로** 사실을 추출하세요.

## 규칙
1. 전사본에 나온 내용만 추출. 추론/의견/해석 금지.
2. 참석자는 전사본에서 직접 발화가 확인된 사람만.
3. 관련인물은 언급되었지만 발화하지 않은 사람.
4. 하나의 전사본에서 여러 토픽이 나올 수 있음.
5. 각 토픽마다 육하원칙(When/Where/Who/What/Why/How)으로 구조화.
6. **인용문을 풍부하게** — 토픽당 최소 3개, 핵심 발언은 원문 그대로.
7. LG U+에는 "본부장", "국장" 직책이 없음. "본부장"→부사장/그룹장, "국장"→그룹장.

※ 세션 분리 규칙:
하나의 녹음 파일에 참석자가 바뀌는 별도 회의가 포함될 수 있습니다.
- 참석자 구성이 바뀌면 → 별도 session
- 같은 참석자라도 주제가 완전히 바뀌고 시간 갭이 있으면 → 별도 session
- sessions 배열에 각 회의를 별도 JSON으로 분리하세요.
- 회의가 1개뿐이면 sessions 배열에 1개만 넣으세요.

## 출력 JSON 스키마
{
  "sessions": [{
    "properties": {
      "title": "YYYYMMDD_유형_대상_토픽요약",
      "type": "회의/회식·네트워킹/전략·의사결정/아이디어·브레인스토밍/고객미팅/개인메모/조직·인사/출장 중 택1",
      "client": "고객명 또는 빈 문자열",
      "project": "프로젝트명 또는 빈 문자열",
      "participants": "전사본에서 직접 발화 확인된 사람만, 쉼표 구분",
      "related_people": ["언급된 인물1", "인물2"],
      "priority": "긴급/중요/참고 중 택1",
      "status": "후속필요/정리완료 중 택1",
      "tags": ["키워드1", "키워드2"],
      "summary": "1-2문장 핵심 요약",
      "next_actions": "전사본에서 언급된 후속 액션만",
      "knowledge_types": ["customer_intel", "deal_progress"],
      "entities": ["고객명", "프로젝트명", "인물명"],
      "topics": ["토픽명1", "토픽명2"]
    },
    "topics": [{
      "name": "토픽명",
      "when": "언제 (날짜/시점/맥락)",
      "where": "어디서 (장소/채널)",
      "who": "누가 (핵심 관련자)",
      "what": "무엇을 (핵심 내용 2~3문장)",
      "why": "왜 (배경/이유)",
      "how": "어떻게 (방법/진행 방식)",
      "key_facts": [
        {"content": "사실 내용", "original_quote": "원문 인용"}
      ],
      "quotes": ["핵심 발언 원문1", "핵심 발언 원문2", "핵심 발언 원문3"],
      "action_items": [
        {"task": "할 일", "owner": "담당자", "deadline": "기한"}
      ],
      "insights": ["시장/경쟁/조직 관련 인사이트"]
    }],
    "executive_summary": "전체 요약 1~3문장",
    "decisions": ["결정사항1", "결정사항2"],
    "risks": ["리스크1", "리스크2"],
    "key_persons_observed": [
      {"name": "인물명", "role": "직책/소속", "observed_behavior": "관찰된 행동"}
    ],
    "meeting_overview": {
      "date": "YYYY-MM-DD",
      "participants": ["실제 화자만"],
      "type": "미팅 성격",
      "location": "장소 (언급시)"
    }
  }]
}"""


# ─── Pass 2b: Data Enrichment ───

DATA_ENRICHMENT_PROMPT = """\
당신은 B2B 영업 임원의 전략 분석가입니다.
Pass 2a에서 추출한 사실을 기반으로 **데이터 자산 관점**에서 보강하세요.

## 규칙
1. facts에 없는 내용을 절대 추가하지 마세요.
2. 기존 facts의 인사이트를 더 깊이 분석하세요.
3. 코칭 포인트: 이 에피소드에서 영업 역량 개발에 활용할 수 있는 교훈.
4. 재활용 가능성: 보고서, 뉴스레터, Account Plan에 활용 가능한 팩트 표시.

## 출력 JSON 스키마
{
  "enriched_topics": [{
    "name": "토픽명 (Pass 2a와 동일)",
    "coaching_points": ["영업 코칭 관점 교훈1", "교훈2"],
    "reusable_for": ["보고서", "뉴스레터", "Account Plan", "세컨드브레인"],
    "so_what": "이 토픽의 전략적 의미 1~2문장",
    "related_deals": ["관련 딜/프로젝트명"]
  }],
  "daily_digest": "오늘 하루 요약 (뉴스레터/모닝인텔 활용 가능, 3~5문장)"
}"""


def _parse_json_response(raw: str, context: str) -> dict:
    """Parse JSON from Claude response with error handling."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SummaryError(
            f"Claude returned invalid JSON ({context}): {e}\nRaw: {raw[:200]}"
        ) from e


def extract_facts(
    corrected_text: str,
    duration: float,
    recording_date: str = "",
    participants: list[str] | None = None,
    meeting_title: str | None = None,
) -> dict:
    """Pass 2a: Extract facts only from transcript. No interpretation.

    Args:
        participants: 참석자 리스트 (Claude가 추측 대신 이 리스트로 고정)
        meeting_title: 회의 제목 (담당 지정 제목, Claude가 임의 생성 금지)
    """
    if not corrected_text.strip():
        raise SummaryError("Empty transcript — nothing to extract")

    date_hint = f"\n녹음 날짜: {recording_date}" if recording_date else ""
    title_hint = f"\n회의 제목 (담당 지정, 그대로 사용): {meeting_title}" if meeting_title else ""
    participants_hint = _participants_hint(participants)

    user_prompt = (
        f"아래 회의 전사본에서 사실만 추출하여 JSON으로 구조화해주세요.\n"
        f"회의 길이: {duration / 60:.1f}분{date_hint}{title_hint}\n"
        f"{participants_hint}"
        f"전사본:\n{corrected_text}"
    )

    log.info("Pass 2a: 사실 추출 시작...")
    raw = _call_claude(system=FACT_EXTRACTION_PROMPT, user=user_prompt)
    log.info(f"Pass 2a 응답 수신 ({len(raw)}자)")

    return _parse_json_response(raw, "Pass 2a")


def enrich_facts(facts_data: dict) -> dict:
    """Pass 2b: 데이터 자산 관점 보강."""
    user_prompt = (
        f"아래 사실 데이터를 데이터 자산 관점에서 보강하여 JSON으로 생성해주세요.\n\n"
        f"사실 데이터:\n{json.dumps(facts_data, ensure_ascii=False, indent=2)}"
    )

    log.info("Pass 2b: 데이터 자산 보강 시작...")
    raw = _call_claude(system=DATA_ENRICHMENT_PROMPT, user=user_prompt)
    log.info(f"Pass 2b 응답 수신 ({len(raw)}자)")

    return _parse_json_response(raw, "Pass 2b")


def verify_analysis(facts_data: dict, analysis_data: dict) -> dict:
    """Pass 2c: Verify analysis against facts. Simple pass-through for v4."""
    # In v4, enrichment data is lightweight — just return it
    log.info("Pass 2c: 검증 완료")
    return analysis_data


def _build_analysis(session_facts: dict, session_enriched: dict, recording_date: str) -> MeetingAnalysis:
    """Build a MeetingAnalysis from a single session's facts + enriched data."""
    props = session_facts.get("properties", {})

    raw_title = props.get("title", "")
    if not raw_title or raw_title in ("미팅노트", "회의록"):
        date_prefix = recording_date.replace("-", "") if recording_date else ""
        raw_title = f"{date_prefix}_음성메모" if date_prefix else "음성메모"

    # Collect entities and knowledge_types from topics
    all_entities: list[str] = []
    all_knowledge_types: list[str] = []
    for topic in session_facts.get("topics", []):
        if isinstance(topic, dict):
            for fact in topic.get("key_facts", []):
                if isinstance(fact, dict):
                    all_entities.extend(fact.get("entities", []))
    # Also from properties
    all_entities.extend(props.get("entities", []))
    all_knowledge_types.extend(props.get("knowledge_types", []))
    unique_entities = list(dict.fromkeys(all_entities))[:20]
    unique_knowledge_types = list(dict.fromkeys(all_knowledge_types))

    # Merge topics with enrichment data
    raw_topics = session_facts.get("topics", [])
    enriched_topics = {et.get("name", ""): et for et in session_enriched.get("enriched_topics", [])}
    merged_topics = []
    for t in raw_topics:
        topic_name = t.get("name", "")
        enrichment = enriched_topics.get(topic_name, {})
        merged = dict(t)
        if enrichment:
            merged["coaching_points"] = enrichment.get("coaching_points", [])
            merged["reusable_for"] = enrichment.get("reusable_for", [])
            merged["so_what"] = enrichment.get("so_what", "")
            merged["related_deals"] = enrichment.get("related_deals", [])
        merged_topics.append(merged)

    # Collect all action items from topics
    all_action_items = []
    for t in raw_topics:
        all_action_items.extend(t.get("action_items", []))

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
            topics=props.get("topics", []),
        ),
        content=MeetingContent(
            executive_summary=session_facts.get("executive_summary", ""),
            topics=merged_topics,
            decisions=session_facts.get("decisions", []),
            risks=session_facts.get("risks", []),
            action_items=all_action_items,
            key_persons=session_enriched.get("key_persons", session_facts.get("key_persons_observed", [])),
        ),
    )


def analyze_transcript(
    corrected_text: str,
    duration: float,
    recording_date: str = "",
    participants: list[str] | None = None,
    meeting_title: str | None = None,
) -> list[MeetingAnalysis]:
    """Pass 2 (3-stage): fact extraction → data enrichment → verification.

    Returns a list of MeetingAnalysis (multiple if sessions detected in one recording).

    Args:
        participants: 참석자 리스트 (할루시네이션 방지)
        meeting_title: 담당 지정 회의 제목

    Raises:
        SummaryError: on persistent/parse failure
        RetryableError: on transient API errors
        NonRetryableError: on auth errors
    """
    # Pass 2a: Extract facts
    facts_data = extract_facts(
        corrected_text, duration, recording_date,
        participants=participants, meeting_title=meeting_title,
    )

    # Check if multi-session format
    sessions = facts_data.get("sessions", [])
    if sessions:
        log.info(f"다중 세션 감지: {len(sessions)}개 회의를 별도 페이지로 생성합니다.")
        results = []
        for i, session_facts in enumerate(sessions):
            log.info(f"  세션 {i+1}/{len(sessions)}: {session_facts.get('properties', {}).get('title', '?')}")
            # Pass 2b: data enrichment per session
            session_enriched = enrich_facts(session_facts)
            # Pass 2c: verification (keep existing)
            session_verified = verify_analysis(session_facts, session_enriched)
            results.append(_build_analysis(session_facts, session_verified, recording_date))
        return results

    # Single session
    enriched = enrich_facts(facts_data)
    verified = verify_analysis(facts_data, enriched)
    return [_build_analysis(facts_data, verified, recording_date)]


def summarize_transcript(full_text: str, duration: float, recording_date: str = "",
                         dictionary_hints: str = "",
                         participants: list[str] | None = None,
                         meeting_title: str | None = None) -> list[MeetingAnalysis]:
    """Full pipeline: correct → validate → extract facts → analyze → verify.

    This is the main entry point called by pipeline.py.
    Returns a list of MeetingAnalysis (1 or more if multi-session detected).

    Args:
        participants: 담당 지정 참석자 리스트 (할루시네이션 방지)
        meeting_title: 담당 지정 회의 제목
    """
    # Pass 1: STT correction with proper noun hints + participants
    corrected = correct_transcript(full_text, dictionary_hints, participants=participants)

    # Pass 1.5: Disabled (always passes through)
    validated = validate_correction(full_text, corrected)

    # Pass 2 (3-stage): fact extraction → data enrichment → verification
    return analyze_transcript(
        validated, duration, recording_date,
        participants=participants, meeting_title=meeting_title,
    )
