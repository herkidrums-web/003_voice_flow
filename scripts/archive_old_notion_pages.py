"""처리 중복으로 생긴 옛 Notion 페이지를 일괄 archive.

processing_state.jsonl을 스캔해 같은 파일에 대해 여러 번 notion:done이
기록된 경우, **가장 최근 페이지만 keep**하고 나머지는 Notion 측에서
archive 처리(notion.pages.update(page_id, archived=True))합니다.

사용법:
    poc/.venv/bin/python scripts/archive_old_notion_pages.py             # dry-run
    poc/.venv/bin/python scripts/archive_old_notion_pages.py --apply     # 실제 archive
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from notion_client import Client as NotionClient
from config import get_settings

log = logging.getLogger(__name__)


def _page_id_from_url(url: str) -> str:
    """https://www.notion.so/Title-35f7b1d7e3808170... → 35f7b1d7-e380-8170-... 형식."""
    if not url:
        return ""
    tail = url.rsplit("/", 1)[-1].split("?")[0]
    # 마지막 32자가 id
    raw = tail[-32:] if len(tail) >= 32 else tail.split("-")[-1]
    if len(raw) == 32:
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    return raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="실제 archive 실행 (없으면 dry-run)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()

    state_path = _PROJ_ROOT / "processing_state.jsonl"

    # 파일별 모든 notion:done 기록 수집 (시간순)
    by_file: dict[str, list[tuple]] = defaultdict(list)
    for line in state_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("stage") == "notion" and e.get("status") == "done":
            url = (e.get("meta") or {}).get("notion_url", "")
            if url:
                by_file[e["file"]].append((e["ts"], url))

    # 정리 대상 — 페이지가 2개 이상인 파일만
    to_archive: list[tuple[str, str, str]] = []  # (file, ts, url)
    for fname, records in by_file.items():
        if len(records) < 2:
            continue
        records.sort(key=lambda x: x[0])
        # 가장 최신 하나만 keep, 나머지는 archive
        keep_ts, keep_url = records[-1]
        for ts, url in records[:-1]:
            to_archive.append((fname, ts, url))

    print(f"중복 페이지 {len(to_archive)}개 발견 (파일 {sum(1 for v in by_file.values() if len(v)>=2)}개에서 옛 버전).")
    for fname, ts, url in to_archive[:20]:
        print(f"  - {fname[:50]} @ {ts[:19]} → {url}")
    if len(to_archive) > 20:
        print(f"  ... +{len(to_archive)-20}개 더")

    if not args.apply:
        print("\n--apply 없이 실행됨. 실제 archive 안 함. 적용하려면 --apply 옵션.")
        return 0

    notion = NotionClient(auth=settings.notion_api_key)
    ok = 0
    failed = 0
    for fname, ts, url in to_archive:
        page_id = _page_id_from_url(url)
        if not page_id:
            log.warning("page_id 추출 실패: %s", url)
            failed += 1
            continue
        try:
            notion.pages.update(page_id=page_id, archived=True)
            ok += 1
            log.info("archived: %s @ %s", fname[:40], ts[:19])
        except Exception as e:
            log.warning("archive failed (%s): %s", url, e)
            failed += 1

    print(f"\n완료: {ok} archive 성공 / {failed} 실패")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
