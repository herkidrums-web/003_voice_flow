"""Proper noun dictionary from Notion DBs + custom terms file."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from notion_client import Client
from notion_client.errors import APIResponseError

from config import get_settings

log = logging.getLogger(__name__)

# Cache Notion terms for 1 hour to avoid repeated API calls
_notion_cache: dict[str, list[str]] | None = None
_notion_cache_time: float = 0
_CACHE_TTL = 3600  # 1 hour

_PROJECT_ROOT = Path(__file__).parent.parent
CUSTOM_TERMS_FILE = _PROJECT_ROOT / "custom_terms.txt"

# 개인기록_DB properties to extract proper nouns from
_RECORD_DB_PROPERTIES = ["관련인물", "고객명", "프로젝트", "조직"]

# 고객연락처_DB data_source_id
_CONTACTS_DB_ID = "236db9e4-14c3-4b7a-a7ad-0f21d6c6581c"


def _get_client() -> Client:
    settings = get_settings()
    return Client(auth=settings.notion_api_key)


def _fetch_record_db_terms(client: Client) -> dict[str, list[str]]:
    """Fetch select/multi_select option names from 개인기록_DB."""
    settings = get_settings()
    if not settings.notion_data_source_id:
        return {}

    try:
        ds = client.data_sources.retrieve(
            data_source_id=settings.notion_data_source_id
        )
    except Exception as e:
        log.warning(f"개인기록_DB 스키마 조회 실패: {e}")
        return {}

    terms: dict[str, list[str]] = {}
    props = ds.get("properties", {})

    for prop_name in _RECORD_DB_PROPERTIES:
        prop = props.get(prop_name)
        if not prop:
            continue
        ptype = prop.get("type", "")
        if ptype not in ("select", "multi_select"):
            continue
        options = prop.get(ptype, {}).get("options", [])
        names = [o["name"] for o in options if o.get("name")]
        if names:
            terms[prop_name] = names

    return terms


def _fetch_contacts_db(client: Client) -> dict[str, list[str]]:
    """Fetch names and companies from 고객연락처_DB."""
    try:
        # Company names from select options
        ds = client.data_sources.retrieve(data_source_id=_CONTACTS_DB_ID)
        company_opts = ds.get("properties", {}).get("회사명", {}).get("select", {}).get("options", [])
        companies = [o["name"] for o in company_opts if o.get("name")]

        # Contact names from page titles
        names = []
        has_more = True
        start_cursor = None
        while has_more:
            kwargs = {"data_source_id": _CONTACTS_DB_ID, "page_size": 100}
            if start_cursor:
                kwargs["start_cursor"] = start_cursor
            results = client.data_sources.query(**kwargs)
            for page in results.get("results", []):
                title_prop = page.get("properties", {}).get("이름", {})
                title_items = title_prop.get("title", [])
                if title_items:
                    name = title_items[0].get("plain_text", "")
                    # Only Korean names (skip English names for STT correction)
                    if name and any("\uAC00" <= c <= "\uD7A3" for c in name):
                        names.append(name)
            has_more = results.get("has_more", False)
            start_cursor = results.get("next_cursor")

        terms = {}
        if names:
            terms["고객인물"] = names
        if companies:
            terms["고객회사"] = companies
        return terms

    except Exception as e:
        log.warning(f"고객연락처_DB 조회 실패: {e}")
        return {}


def _load_custom_terms() -> list[str]:
    """Load user-defined terms from custom_terms.txt (모든 라인)."""
    if not CUSTOM_TERMS_FILE.exists():
        return []

    terms = []
    for line in CUSTOM_TERMS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def _is_separator_comment(line: str) -> bool:
    """`# ====`, `# ----` 같은 구분선 주석 여부."""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    body = stripped.lstrip("#").strip()
    return not body or all(c in "=- " for c in body)


def _load_always_terms() -> list[str]:
    """Load terms from [ALWAYS] sections in custom_terms.txt.

    [ALWAYS] 섹션은 transcript-매칭 필터를 무시하고 STT 교정 프롬프트에 항상 주입된다.
    STT가 인명을 완전히 오인식한 경우(예: "권용현"→"원혁명")에도 교정 단서를 보장.

    섹션 마커 규칙:
    - 카테고리 헤더 = `# ====` 구분선 사이에 낀 주석 라인 (3줄 헤더 블록)
    - 헤더 본문에 `[ALWAYS]`가 있으면 그 카테고리는 always 모드 ON
    - 다음 헤더 블록을 만나면 OFF (서브 주석·인라인 주석은 모드 영향 없음)
    """
    if not CUSTOM_TERMS_FILE.exists():
        return []

    lines = CUSTOM_TERMS_FILE.read_text(encoding="utf-8").splitlines()
    always: list[str] = []
    in_always = False
    i = 0
    while i < len(lines):
        # 3-line header block: separator / header / separator
        if (
            i + 2 < len(lines)
            and _is_separator_comment(lines[i])
            and lines[i + 1].strip().startswith("#")
            and not _is_separator_comment(lines[i + 1])
            and _is_separator_comment(lines[i + 2])
        ):
            body = lines[i + 1].strip().lstrip("#").strip()
            in_always = "[ALWAYS]" in body
            i += 3
            continue

        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#") and in_always:
            always.append(stripped)
        i += 1
    return always


def _filter_relevant_terms(terms: list[str], transcript: str) -> list[str]:
    """Filter terms to only those potentially relevant to the transcript.

    Keeps a term if any 2-char substring of the term appears in the transcript.
    This catches STT misspellings while avoiding sending the entire dictionary.
    """
    if not transcript:
        return terms

    # Always include person names and org names (short, high value)
    relevant = []
    for term in terms:
        # Always keep short terms (≤3 chars) — low token cost
        if len(term) <= 3:
            relevant.append(term)
            continue
        # Check if any 2-char bigram of the term appears in transcript
        for i in range(len(term) - 1):
            bigram = term[i:i+2]
            if bigram in transcript:
                relevant.append(term)
                break
    return relevant


def load_dictionary(transcript: str = "") -> str:
    """Build proper noun hint string for STT correction prompt.

    Fetches from:
    1. 개인기록_DB: 관련인물, 고객명, 프로젝트, 조직
    2. 고객연락처_DB: 이름, 회사명
    3. custom_terms.txt: 사용자 정의 용어

    If transcript is provided, filters to only relevant terms.
    Returns formatted string to inject into Claude prompt, or empty string.
    """
    sections = []
    total = 0

    global _notion_cache, _notion_cache_time

    # Use cached Notion terms if fresh
    if _notion_cache is None or (time.time() - _notion_cache_time) > _CACHE_TTL:
        _notion_cache = {}
        try:
            client = _get_client()
            _notion_cache.update(_fetch_record_db_terms(client))
            _notion_cache.update(_fetch_contacts_db(client))
            _notion_cache_time = time.time()
        except Exception as e:
            log.warning(f"Notion 사전 로드 실패: {e}")

    _MAX_TERMS = 500  # 프롬프트 폭증 방지 (Claude CLI 타임아웃 대응)

    # 우선순위 카테고리 먼저 담기 (인물·고객 우선)
    priority_order = ["관련인물", "고객인물", "고객명", "고객회사", "조직", "프로젝트"]
    all_categories = list(_notion_cache.keys())
    ordered = [c for c in priority_order if c in all_categories] + [
        c for c in all_categories if c not in priority_order
    ]

    for category in ordered:
        names = _notion_cache.get(category, [])
        filtered = _filter_relevant_terms(names, transcript) if transcript else names
        if not filtered:
            continue
        remaining = _MAX_TERMS - total
        if remaining <= 0:
            break
        if len(filtered) > remaining:
            filtered = filtered[:remaining]
        sections.append(f"- {category}: {', '.join(filtered)}")
        total += len(filtered)

    # Custom terms (나머지)
    if total < _MAX_TERMS:
        custom = _load_custom_terms()
        always = set(_load_always_terms())
        # always 항목은 일반 custom에서 제거 (중복 방지)
        regular = [t for t in custom if t not in always]
        if transcript:
            regular = _filter_relevant_terms(regular, transcript)
        remaining = _MAX_TERMS - total
        if regular and remaining > 0:
            regular = regular[:remaining]
            sections.append(f"- 추가용어: {', '.join(regular)}")
            total += len(regular)

    # [ALWAYS] 항목은 transcript-매칭 무시하고 별도 섹션으로 항상 주입
    always_terms = _load_always_terms()
    if always_terms:
        sections.insert(
            0,
            "- 핵심인명(STT 오인식 빈발 — 발음 유사한 단어는 이 목록에서 우선 매칭): "
            + ", ".join(always_terms),
        )
        total += len(always_terms)

    if not sections:
        return ""

    log.info(f"고유명사 사전 로드: {total}개 용어 (최대 {_MAX_TERMS}, ALWAYS {len(always_terms)}개)")
    return "교정 시 참고할 고유명사 목록:\n" + "\n".join(sections)
