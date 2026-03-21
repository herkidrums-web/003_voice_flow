"""Proper noun dictionary from Notion DBs + custom terms file."""
from __future__ import annotations

import logging
from pathlib import Path

from notion_client import Client
from notion_client.errors import APIResponseError

from config import get_settings

log = logging.getLogger(__name__)

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
    if not settings.notion_database_id:
        return {}

    try:
        ds = client.data_sources.retrieve(
            data_source_id=settings.notion_database_id
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
    """Load user-defined terms from custom_terms.txt."""
    if not CUSTOM_TERMS_FILE.exists():
        return []

    terms = []
    for line in CUSTOM_TERMS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def load_dictionary() -> str:
    """Build proper noun hint string for STT correction prompt.

    Fetches from:
    1. 개인기록_DB: 관련인물, 고객명, 프로젝트, 조직
    2. 고객연락처_DB: 이름, 회사명
    3. custom_terms.txt: 사용자 정의 용어

    Returns formatted string to inject into Claude prompt, or empty string.
    """
    sections = []
    total = 0

    try:
        client = _get_client()

        # 개인기록_DB terms
        record_terms = _fetch_record_db_terms(client)
        for category, names in record_terms.items():
            sections.append(f"- {category}: {', '.join(names)}")
            total += len(names)

        # 고객연락처_DB terms
        contact_terms = _fetch_contacts_db(client)
        for category, names in contact_terms.items():
            sections.append(f"- {category}: {', '.join(names)}")
            total += len(names)

    except Exception as e:
        log.warning(f"Notion 사전 로드 실패: {e}")

    # Custom terms
    custom = _load_custom_terms()
    if custom:
        sections.append(f"- 추가용어: {', '.join(custom)}")
        total += len(custom)

    if not sections:
        return ""

    log.info(f"고유명사 사전 로드: {total}개 용어")
    return "교정 시 참고할 고유명사 목록:\n" + "\n".join(sections)
