"""Auto-ingest 신규 wiki anchor — Morning Briefing v2의 새 토픽을 wiki에 자동 반영.

기존: briefing이 new_anchors_suggested를 Notion 페이지에 "제안"으로 표시 → 담당이 수동으로 wiki 노트 생성
신규: briefing의 new_anchors_suggested + blocks[is_new_anchor=true]에 대해 자동으로 wiki 스텁 노트 생성 및 index/log 갱신

생성되는 노트는 entity 템플릿(frontmatter + 누적 인텔리전스 섹션)을 따른다.
`auto_generated: true` 필드로 표시되어 추후 /ingest 정제 대상임을 명시한다.

핵심 가드:
- 이미 존재하는 경로면 skip (덮어쓰지 않음)
- facts가 비어있으면 skip (의미 있는 시드가 없음)
- index.md에 동일 path 항목 있으면 append skip
- wiki/log.md에 처리 결과 1줄 기록

사용:
    from scripts.auto_ingest_anchors import auto_ingest_anchors
    result = auto_ingest_anchors(briefing, target_date="2026-05-14",
                                 wiki_root="/Users/swlee/Documents/Coding/000_second_brain/wiki")
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# 카테고리 → wiki 폴더 매핑 (briefing prompt와 동기화)
_CATEGORY_FOLDERS: dict[str, str] = {
    "business/customers": "business/customers",
    "business/competitors": "business/competitors",
    "business/internal": "business/internal",
    "business/industry": "business/industry",
    "business/deals": "business/deals",
    "leadership": "leadership/my-lessons",
    "creator": "creator/content",
}

# index.md의 카테고리 → 섹션 헤더 매핑 (### 라인)
_INDEX_SECTIONS: dict[str, tuple[str, str]] = {
    "business/customers": ("## Business", "### Customers"),
    "business/competitors": ("## Business", "### Competitors"),
    "business/internal": ("## Business", "### Internal"),
    "business/industry": ("## Business", "### Industry"),
    "business/deals": ("## Business", "### Deals"),
    "leadership": ("## Leadership", ""),
    "creator": ("## Creator", ""),
}

# frontmatter type 매핑
_CATEGORY_TYPES: dict[str, str] = {
    "business/customers": "customer",
    "business/competitors": "competitor",
    "business/internal": "internal",
    "business/industry": "industry",
    "business/deals": "deal",
    "leadership": "insight",
    "creator": "content",
}


def _normalize_category(category: str) -> str:
    """카테고리 문자열 정규화. 'Business/Customers' → 'business/customers'."""
    return (category or "").strip().lower().strip("/")


def _slugify_fallback(topic: str) -> str:
    """한글 topic에서 영문 slug를 추출할 수 없을 때 fallback. 영문/숫자만 남기고 kebab.

    Claude가 slug를 제대로 생성하면 사용되지 않음. 모든 영문/숫자가 사라지면 빈 문자열 반환.
    """
    # 영문 단어/숫자만 추출
    parts = re.findall(r"[A-Za-z][A-Za-z0-9]*|\d+", topic or "")
    slug = "-".join(p.lower() for p in parts)
    return slug


def _validate_slug(slug: str) -> bool:
    """slug가 안전한 파일명인지 검증. 영문 소문자/숫자/하이픈만 허용."""
    if not slug:
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", slug))


def _render_stub_note(
    *,
    topic: str,
    folder: str,
    facts: list[str],
    todos: list[str],
    meetings: list[str],
    meeting_urls: list[str],
    target_date: str,
    reason: str,
) -> str:
    """엔티티 스텁 노트 본문 생성 (schema.md 템플릿 따름)."""
    entity_type = _CATEGORY_TYPES.get(folder, "entity")
    source_titles = ", ".join(f'"{m}"' for m in meetings) if meetings else ""

    # 미팅 링크 bullet
    meeting_lines: list[str] = []
    for title, url in zip(meetings, meeting_urls + [""] * max(0, len(meetings) - len(meeting_urls))):
        if url:
            meeting_lines.append(f"- [{title}]({url})")
        else:
            meeting_lines.append(f"- {title}")
    meetings_block = "\n".join(meeting_lines) if meeting_lines else "- (없음)"

    facts_block = "\n".join(f"- {f}" for f in facts) if facts else "- (없음)"
    todos_block = "\n".join(f"- [ ] {t}" for t in todos) if todos else "- (없음)"

    return f"""---
entity: "{topic}"
type: {entity_type}
last_updated: "{target_date}"
auto_generated: true
sources: [{source_titles}]
tags: []
axis: [business]
---

# {topic}

> ⚠️ 자동 생성된 스텁 노트입니다 ({target_date} 일일 브리핑 기반). 추후 `/ingest`로 정제 예정.

## 생성 사유
{reason or "(브리핑에서 신규 토픽으로 감지)"}

## 관련 미팅
{meetings_block}

## 누적 인텔리전스

### {target_date}
{facts_block}

## 이성우 담당 To-Do
{todos_block}

## 관련 노트
- (TBD)
"""


def _already_in_index(index_text: str, rel_path: str) -> bool:
    """index.md에 동일 rel_path를 가진 항목이 이미 있는지 검사."""
    return f"]({rel_path})" in index_text


def _append_to_index(index_path: Path, folder: str, rel_path: str, topic: str, summary: str) -> bool:
    """index.md에 새 항목 1줄을 적절한 섹션 아래에 추가. 추가했으면 True."""
    if not index_path.exists():
        log.warning("index.md not found: %s", index_path)
        return False

    text = index_path.read_text(encoding="utf-8")
    if _already_in_index(text, rel_path):
        return False

    section = _INDEX_SECTIONS.get(folder)
    if section is None:
        log.warning("no index section mapping for folder=%s — skip index update", folder)
        return False

    parent, sub = section
    summary_clean = (summary or "").strip().replace("\n", " ")[:200]
    new_line = f"- [{topic}]({rel_path}) — {summary_clean}" if summary_clean else f"- [{topic}]({rel_path})"

    lines = text.splitlines()
    insert_idx = -1

    # 헤더는 "## Business (22 notes)" 처럼 카운트가 붙을 수 있어 startswith로 비교
    def _is_parent(s: str) -> bool:
        return s == parent or s.startswith(parent + " ")

    def _is_sub(s: str) -> bool:
        return s == sub or s.startswith(sub + " ")

    if sub:
        in_parent = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if _is_parent(stripped):
                in_parent = True
                continue
            if in_parent and _is_sub(stripped):
                j = i + 1
                last_bullet_idx = i
                while j < len(lines):
                    sj = lines[j].strip()
                    if sj.startswith("- ["):
                        last_bullet_idx = j
                    elif sj.startswith("## ") or sj.startswith("### "):
                        break
                    j += 1
                insert_idx = last_bullet_idx + 1
                break
    else:
        in_parent = False
        last_content_idx = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if _is_parent(stripped):
                in_parent = True
                last_content_idx = i
                continue
            if in_parent:
                if stripped.startswith("## ") and not _is_parent(stripped):
                    break
                if stripped:
                    last_content_idx = i
        if last_content_idx > -1:
            insert_idx = last_content_idx + 1

    if insert_idx < 0:
        log.warning("section %s/%s not found in index.md — skip", parent, sub)
        return False

    lines.insert(insert_idx, new_line)
    index_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return True


def _append_log(log_path: Path, target_date: str, summary: str) -> None:
    """wiki/log.md 테이블에 1줄 추가."""
    if not log_path.exists():
        return
    text = log_path.read_text(encoding="utf-8")
    new_row = f"| {target_date} | Morning Briefing 자동 anchor | 완료 | {summary} |\n"
    if not text.endswith("\n"):
        text += "\n"
    log_path.write_text(text + new_row, encoding="utf-8")


def _build_anchor_tasks(briefing: dict) -> list[dict]:
    """blocks(is_new_anchor=true) + new_anchors_suggested를 합쳐서 처리 단위로 변환.

    같은 topic은 1건으로 dedup. blocks의 facts/todos/meetings를 우선 채워주고,
    suggestion 쪽의 slug/category/reason은 메타로 사용.
    """
    suggestions: dict[str, dict] = {}
    for s in briefing.get("new_anchors_suggested", []) or []:
        title = (s.get("title") or "").strip()
        if not title:
            continue
        suggestions[title] = {
            "slug": (s.get("slug") or "").strip(),
            "category": _normalize_category(s.get("category", "")),
            "reason": (s.get("reason") or "").strip(),
        }

    tasks: list[dict] = []
    seen: set[str] = set()

    # 1) blocks 중 is_new_anchor=true
    for b in briefing.get("blocks", []) or []:
        if not b.get("is_new_anchor"):
            continue
        topic = (b.get("topic") or "").strip()
        if not topic or topic in seen:
            continue
        seen.add(topic)
        meta = suggestions.get(topic, {})
        tasks.append({
            "topic": topic,
            "slug": meta.get("slug", "") or _slugify_fallback(topic),
            "category": meta.get("category", "") or "business/internal",
            "reason": meta.get("reason", ""),
            "facts": b.get("facts", []) or [],
            "todos": b.get("todos", []) or [],
            "meetings": b.get("meetings", []) or [],
            "meeting_urls": b.get("meeting_urls", []) or [],
        })

    # 2) suggestions에만 있는 항목 (blocks 매칭 안 됨)
    for title, meta in suggestions.items():
        if title in seen:
            continue
        tasks.append({
            "topic": title,
            "slug": meta.get("slug", "") or _slugify_fallback(title),
            "category": meta.get("category", "") or "business/internal",
            "reason": meta.get("reason", ""),
            "facts": [],
            "todos": [],
            "meetings": [],
            "meeting_urls": [],
        })

    return tasks


def auto_ingest_anchors(
    briefing: dict[str, Any],
    target_date: str,
    wiki_root: str | Path,
) -> dict[str, Any]:
    """briefing 결과의 신규 anchor를 wiki에 자동 스텁 노트로 생성.

    Args:
        briefing: BriefingAgent 결과 dict (blocks, new_anchors_suggested 포함)
        target_date: YYYY-MM-DD
        wiki_root: wiki 루트 디렉토리 (e.g. .../000_second_brain/wiki)

    Returns:
        {
            "created": [{"topic", "path", "slug"}, ...],
            "skipped": [{"topic", "reason"}, ...],
        }
    """
    wiki_root = Path(wiki_root)
    index_path = wiki_root / "index.md"
    log_path = wiki_root / "log.md"

    created: list[dict] = []
    skipped: list[dict] = []

    tasks = _build_anchor_tasks(briefing)
    log.info("auto_ingest_anchors: %d task(s)", len(tasks))

    for task in tasks:
        topic = task["topic"]
        slug = task["slug"]
        category = task["category"]
        folder = _CATEGORY_FOLDERS.get(category)

        if folder is None:
            skipped.append({"topic": topic, "reason": f"unknown category: {category}"})
            continue

        if not _validate_slug(slug):
            fallback = _slugify_fallback(topic)
            if not _validate_slug(fallback):
                skipped.append({"topic": topic, "reason": f"invalid slug: {slug!r} (no English keywords)"})
                continue
            slug = fallback

        # facts/meetings가 모두 비어있으면 의미 있는 시드가 없음 → skip
        if not task["facts"] and not task["meetings"]:
            skipped.append({"topic": topic, "reason": "no facts or meetings to seed"})
            continue

        target_dir = wiki_root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{slug}.md"
        rel_path = f"{folder}/{slug}.md"

        if target_file.exists():
            skipped.append({"topic": topic, "reason": f"file exists: {rel_path}"})
            continue

        body = _render_stub_note(
            topic=topic,
            folder=folder,
            facts=task["facts"],
            todos=task["todos"],
            meetings=task["meetings"],
            meeting_urls=task["meeting_urls"],
            target_date=target_date,
            reason=task["reason"],
        )
        target_file.write_text(body, encoding="utf-8")
        log.info("created wiki stub: %s", rel_path)

        summary = task["reason"][:150] if task["reason"] else (task["facts"][0][:150] if task["facts"] else topic)
        _append_to_index(index_path, folder, rel_path, topic, summary)

        created.append({"topic": topic, "slug": slug, "path": rel_path})

    if created:
        summary = ", ".join(f"{c['slug']}.md" for c in created)
        _append_log(log_path, target_date, f"{len(created)}개 스텁 신규 생성 ({summary})")

    return {"created": created, "skipped": skipped, "target_date": target_date}


def main() -> int:
    """CLI: 가장 최근 briefing 결과를 JSON으로 받아 ingest 실행 (디버깅용)."""
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--briefing-json", required=True, help="BriefingAgent 결과 JSON 파일")
    parser.add_argument("--target-date", default=date.today().isoformat())
    parser.add_argument(
        "--wiki-root",
        default="/Users/swlee/Documents/Coding/000_second_brain/wiki",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    briefing = json.loads(Path(args.briefing_json).read_text(encoding="utf-8"))
    result = auto_ingest_anchors(briefing, args.target_date, args.wiki_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
